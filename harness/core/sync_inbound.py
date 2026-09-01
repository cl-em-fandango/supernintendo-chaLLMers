"""Inbound sync phase: GitHub issues -> harness queue (spec FR-1).

Two phases of the inbound contract live here:

* ingest (Slice 3) — open issues carrying the bare `snes` label become
  `pending/` task files with a sidecar linkage, idempotently (FR-1.2,
  FR-1.6, FR-1.7);
* halt (Slice 4) — `snes-parked` parks the matching task from any queue
  location (FR-1.3) and `snes-deleted` deletes it (FR-1.4), stopping
  in-flight work through the existing interrupt/stand-down mechanism
  (flag at the session boundary, never a kill). Exactly one action per
  issue, ordered delete > park > ingest (FR-1.5).

State and behavior (CODING_STANDARDS §2): `TaskLocation` and
`InboundParams` describe shape; `run_inbound()` acts. The GitHub side is
reached only through the injected client (`external/github_api.py` is the
HTTP boundary); the queue side is plain temp-dir-safe file work, with all
writes atomic (spec §9).

Task/issue matching (FR-1.2 + FR-1.6): a sidecar naming this issue wins;
an entry that carries a sidecar for a *different* issue is claimed by that
issue and never title-matched here; only unlinked entries fall back to the
normalized-title rule (lowercase, `_` <-> space).

Queue moves are never hand-rolled (spec §9): parking goes through
`TaskLifecycle.park(..., from_=...)`, the one path that stamps status and
writes the executive summary. Deleting has no lifecycle path — the task is
removed outright — so only that touches the filesystem directly.
"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from external.github_api import PASS_ABORT_ERRORS, IssueState

from ..workflow.task_lifecycle import (
    CLAIMED_LOCATION,
    TaskLifecycle,
    write_atomic,
)
from .interrupt import (
    InterruptMode,
    InterruptState,
    InterruptStatus,
    read_interrupt,
    write_interrupt,
)
from .sync_labels import TRIGGER_PRECEDENCE, TriggerLabel
from .sync_sidecar import (
    SyncLinkage,
    file_sidecar_path,
    move_sidecar_into_task_dir,
    read_linkage,
    task_dir_sidecar_path,
    write_linkage,
)

# FR-1.1: pathological titles are truncated from the end before any
# collision suffix is appended, so the path stays inside sane limits.
MAX_STEM_CHARS = 200

# Queue locations holding bare task `.md` files (review/ holds the summary
# file named after the task — a fine match target, never an import target).
FILE_LOCATIONS = ("pending", CLAIMED_LOCATION, "review")
# Queue locations holding a task *directory* per task.
DIR_LOCATIONS = ("active", "parked", "failed", "done")


@dataclass
class TaskLocation:
    """One task as found on a queue scan: its name, where it lives, and
    where its GitHub linkage sidecar would be."""
    name: str          # file stem, or task-dir name
    path: Path         # the task .md file or the task directory
    location: str      # queue location ("pending", "active", ...)
    sidecar: Path


@dataclass
class InboundParams:
    """Explicit parameters for one inbound pass (CODING_STANDARDS §5)."""
    queue_dir: Path
    repo: str          # "owner/name" the issues come from
    log: Callable[[str], None] = print
    # Work dir holding `state/interrupt.json`; None means there is no
    # stand-down state to consult, so in-flight moves proceed directly.
    work_dir: Path | None = None
    # The lifecycle authority for parking; None only in wiring that cannot
    # move tasks, and a halt then logs and skips.
    lifecycle: TaskLifecycle | None = None


@dataclass
class InboundResult:
    """What one inbound pass did, per action (feeds `SyncReport`)."""
    imported: int = 0
    parked: int = 0
    deleted: int = 0


def normalize_title(text: str) -> str:
    """FR-2.1 equality form: lowercase, `_` read as space."""
    return text.lower().replace("_", " ")


def scan_queue(queue_dir: Path) -> list[TaskLocation]:
    """Every task currently on the queue, in all synced locations."""
    entries: list[TaskLocation] = []
    for location in FILE_LOCATIONS:
        directory = queue_dir / location
        if directory.is_dir():
            for task_file in sorted(directory.glob("*.md")):
                entries.append(TaskLocation(
                    name=task_file.stem, path=task_file, location=location,
                    sidecar=file_sidecar_path(task_file)))
    for location in DIR_LOCATIONS:
        directory = queue_dir / location
        if directory.is_dir():
            for task_dir in sorted(p for p in directory.iterdir()
                                   if p.is_dir()):
                entries.append(TaskLocation(
                    name=task_dir.name, path=task_dir, location=location,
                    sidecar=task_dir_sidecar_path(task_dir)))
    return entries


def find_task(queue_dir: Path, task_id: str) -> TaskLocation | None:
    """Where `task_id` currently lives on the queue, or None.

    The targeted per-task sync (spec FR-3 in-flight rule) starts here. A
    terminal task dir wins over the review summary file the same task
    leaves behind: `scan_queue` lists dirs after files, so the dir — the
    task's real state — is the last match.
    """
    matches = [entry for entry in scan_queue(queue_dir)
               if entry.name == task_id]
    return matches[-1] if matches else None


def find_matching_task(issue, entries: list[TaskLocation]) -> TaskLocation | None:
    """The task representing `issue`, or None (FR-1.2, FR-1.6).

    Sidecar lookups take precedence: an entry whose sidecar names this
    issue is the match, and an entry whose sidecar names a *different*
    issue is off-limits to title matching (it belongs elsewhere). Only
    entries with no readable sidecar fall back to the normalized-title
    comparison.
    """
    unlinked: list[TaskLocation] = []
    for entry in entries:
        linkage = read_linkage(entry.sidecar)
        if linkage is None:
            unlinked.append(entry)
        elif linkage.issue == issue.number:
            return entry
    wanted = normalize_title(issue.title)
    return next((entry for entry in unlinked
                 if normalize_title(entry.name) == wanted), None)


def derive_task_stem(issue) -> str:
    """FR-1.1 stem: spaces to `_`, invalid chars stripped, whitespace and
    dots trimmed, capped at MAX_STEM_CHARS, issue number on an empty stem."""
    stem = issue.title.strip().replace(" ", "_")
    stem = stem.replace("/", "").replace("\x00", "").strip(".")
    return stem[:MAX_STEM_CHARS] or str(issue.number)


def derive_task_filename(issue, entries: list[TaskLocation]) -> str:
    """FR-1.1 filename: stem + `.md`, with the `-<number>` collision suffix
    when a same-named file already lives in some queue location.

    Called only when no task matches the issue, so any same-named file is
    by definition another task's file.
    """
    stem = derive_task_stem(issue)
    filename = f"{stem}.md"
    if any(entry.name == stem for entry in entries):
        filename = f"{stem}-{issue.number}.md"
    return filename


def issue_task_body(issue) -> str:
    """FR-1.2 content: the body verbatim, or a title+URL stub when empty."""
    if issue.body.strip():
        return issue.body
    return f"{issue.title}\n\n{issue.html_url}\n"


def _ingest_issue(issue, entries: list[TaskLocation],
                  params: InboundParams) -> bool:
    """One issue -> one pending task file. True when a file was created."""
    match = find_matching_task(issue, entries)
    if match is not None:
        params.log(f"  gh #{issue.number}: skip (debug) — matches {match.name} "
                   f"in {match.location}/; not importing")
        return False
    filename = derive_task_filename(issue, entries)
    target = params.queue_dir / "pending" / filename
    if target.exists():
        params.log(f"  gh #{issue.number}: pending/{filename} already "
                   f"exists; skipping (no overwrite)")
        return False
    params.queue_dir.joinpath("pending").mkdir(parents=True, exist_ok=True)
    write_atomic(target, issue_task_body(issue))
    sidecar = file_sidecar_path(target)
    write_linkage(sidecar, SyncLinkage(issue=issue.number, repo=params.repo))
    entries.append(TaskLocation(name=target.stem, path=target,
                                location="pending", sidecar=sidecar))
    params.log(f"  gh #{issue.number}: imported -> pending/{filename}")
    return True


def _trigger_for(issue) -> TriggerLabel | None:
    """The one action an issue's labels instruct, in FR-1.5 precedence."""
    names = {label.name for label in issue.labels}
    for trigger in TRIGGER_PRECEDENCE:
        if trigger.value in names:
            return trigger
    return None


def _collect_trigger_issues(api) -> dict[int, object]:
    """Every issue carrying a trigger label, keyed by number.

    Ingest reads open issues only (FR-1.2); the halt triggers apply to
    open *and* closed issues (FR-1.3). The open listing is taken first and
    wins a merge, so an issue's recorded state is `open` whenever it is open
    now (the delete anti-loop depends on it).
    """
    found: dict[int, object] = {}
    for label, states in ((TriggerLabel.INGEST, (IssueState.OPEN,)),
                          (TriggerLabel.PARK, (IssueState.OPEN,
                                               IssueState.CLOSED)),
                          (TriggerLabel.DELETE, (IssueState.OPEN,
                                                 IssueState.CLOSED))):
        for state in states:
            for issue in api.list_issues(labels=[label.value], state=state):
                found.setdefault(issue.number, issue)
    return found


def _stand_down_status(params: InboundParams) -> InterruptStatus | None:
    """The interrupt record gating a move of an in-flight task (FR-1.3).

    First sight writes the stand-down request (the running session stops at
    its next boundary — a flag, never a kill) and returns None; a request
    the session has acknowledged (`paused`) returns the record, meaning the
    session exited and the move may proceed; a request still pending
    returns None.
    """
    status = read_interrupt(params.work_dir, log=params.log)
    if status is None:
        write_interrupt(params.work_dir, InterruptMode.STAND_DOWN,
                        InterruptState.REQUESTED, requester_pid=os.getpid())
        params.log("  stand-down requested for the in-flight session; "
                   "the move waits for the session boundary")
        return None
    if status.state is InterruptState.PAUSED:
        return status
    params.log("  skip (debug) — stand-down still awaiting the session "
               "boundary; move deferred")
    return None


def _relocate_file_sidecar(match: TaskLocation, params: InboundParams) -> None:
    """Move a task-file linkage sidecar into the parked task dir (FR-1.6).

    `pending/X.md.gh.json` beside the moved file would otherwise strand the
    linkage. Claim-ownership sidecars are not ours and stay untouched.
    """
    parked_dir = params.queue_dir / "parked" / match.name
    move_sidecar_into_task_dir(match.sidecar, parked_dir)
    match.sidecar = task_dir_sidecar_path(parked_dir)


def _park_issue_task(issue, entries: list[TaskLocation],
                     params: InboundParams) -> bool:
    """FR-1.3: park the task matching a `snes-parked` issue. True when moved."""
    match = find_matching_task(issue, entries)
    if match is None or match.location == "parked":
        params.log(f"  gh #{issue.number}: skip (debug) — nothing to park "
                   f"({'no matching task' if match is None else 'already parked'})")
        return False
    if params.lifecycle is None:
        params.log(f"  gh #{issue.number}: no lifecycle wired; not parking")
        return False
    if match.location == "active" and _stand_down_status(params) is None:
        return False
    was = match.location
    params.lifecycle.park(match.name, f"parked via GitHub issue #{issue.number}",
                          from_=was)
    if was in FILE_LOCATIONS:
        _relocate_file_sidecar(match, params)
    match.path = params.queue_dir / "parked" / match.name
    match.location = "parked"
    params.log(f"  gh #{issue.number}: parked {match.name} (was {was}/)")
    return True


def _close_and_unlabel(api, issue, params: InboundParams) -> None:
    """FR-1.4 anti-loop: close the issue and drop `snes`/`snes-deleted`.

    Only labels the issue actually carries are removed (no 404 churn);
    human labels are never touched. A failure here is logged by the
    per-issue guard in `run_inbound`, not silently retried.
    """
    if issue.state is IssueState.OPEN:
        api.close_issue(issue.number)
    carried = {label.name for label in issue.labels}
    for name in (TriggerLabel.INGEST.value, TriggerLabel.DELETE.value):
        if name in carried:
            api.remove_label(issue.number, name)


def _delete_issue_task(api, issue, entries: list[TaskLocation],
                       params: InboundParams) -> bool:
    """FR-1.4: delete the task matching a `snes-deleted` issue. True if gone."""
    match = find_matching_task(issue, entries)
    if match is None:
        params.log(f"  gh #{issue.number}: skip (debug) — no matching task "
                   f"to delete")
        return False
    if match.location == "active" and _stand_down_status(params) is None:
        return False
    if match.path.is_dir():
        shutil.rmtree(match.path, ignore_errors=True)
    else:
        match.path.unlink(missing_ok=True)
        match.sidecar.unlink(missing_ok=True)
    _close_and_unlabel(api, issue, params)
    entries.remove(match)
    params.log(f"  gh #{issue.number}: deleted {match.name} from "
               f"{match.location}/; issue closed and trigger labels removed")
    return True


def run_inbound(api, params: InboundParams) -> InboundResult:
    """FR-1 phase 1: apply every issue's trigger action to the queue.

    One action per issue in FR-1.5 precedence (delete > park > ingest). A
    failure on one issue is logged and skipped — it never aborts the pass
    (NFR-1). Idempotent (FR-1.7): a second pass sees imports as sidecar
    matches, parks as already-parked, and deletes as gone — no queue changes.
    """
    result = InboundResult()
    entries = scan_queue(params.queue_dir)
    actions = {
        TriggerLabel.INGEST: lambda issue: _ingest_issue(issue, entries, params),
        TriggerLabel.PARK: lambda issue: _park_issue_task(issue, entries, params),
        TriggerLabel.DELETE: lambda issue: _delete_issue_task(api, issue, entries, params),
    }
    tallies = {
        TriggerLabel.INGEST: "imported",
        TriggerLabel.PARK: "parked",
        TriggerLabel.DELETE: "deleted",
    }
    issues = _collect_trigger_issues(api)
    for number in sorted(issues):
        trigger = _trigger_for(issues[number])
        if trigger is None:
            continue
        try:
            acted = actions[trigger](issues[number])
        except PASS_ABORT_ERRORS:
            raise  # edge 9 / FR-5: the engine aborts the pass, not the loop
        except Exception as exc:  # one bad issue must not sink the pass (NFR-1)
            params.log(f"  gh #{number}: {trigger.value} action failed: {exc}")
            continue
        if acted:
            setattr(result, tallies[trigger],
                    getattr(result, tallies[trigger]) + 1)
    return result
