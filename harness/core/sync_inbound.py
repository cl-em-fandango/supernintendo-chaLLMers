"""Inbound sync phase: GitHub issues -> harness queue (spec FR-1).

Two phases of the inbound contract live here:

* ingest (Slice 3) — open issues carrying the bare `snes` label become
  `pending/` task files with a sidecar linkage, idempotently (FR-1.2,
  FR-1.6, FR-1.7);
* halt (Slice 4) — `snes-parked` parks the matching task from any queue
  location (FR-1.3) and `snes-deleted` deletes it (FR-1.4), stopping
  in-flight work through the existing interrupt/stand-down mechanism
  (flag at the session boundary, never a kill).

Exactly one action per issue, ordered delete > park > demo > ingest
(demo spec FR-1.5). The `DEMO` action is an ingest that also records
`demo: true` in the sidecar (demo spec FR-1.1–FR-1.3); when the demo
feature is disabled (`InboundParams.demo_enabled`) the `snes-demo` label
is ignored entirely (demo spec FR-9).

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
from . import task_record
from .sync_labels import TRIGGER_PRECEDENCE, TriggerLabel
from .sync_linkage import SyncLinkage

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
    """One task as found on a queue scan: its name, its task-file path (or
    task directory) and the queue location it currently sits in.

    `name` is the task id the metadata record is keyed by, so linkage is
    resolved by task id (`task_record.read_record`) rather than by a path
    derived from this entry — a transition can never orphan it."""
    name: str          # file stem, or task-dir name
    path: Path         # the task .md file or the task directory
    location: str      # queue location ("pending", "active", ...)


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
    # The `demo.enabled` switch (demo spec FR-9): when False, `snes-demo`
    # labels are ignored — no listing, no action, no label changes.
    demo_enabled: bool = False


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
                    name=task_file.stem, path=task_file, location=location))
    for location in DIR_LOCATIONS:
        directory = queue_dir / location
        if directory.is_dir():
            for task_dir in sorted(p for p in directory.iterdir()
                                   if p.is_dir()):
                entries.append(TaskLocation(
                    name=task_dir.name, path=task_dir, location=location))
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


def find_matching_task(issue, entries: list[TaskLocation],
                       queue_dir: Path) -> TaskLocation | None:
    """The task representing `issue`, or None (FR-1.2, FR-1.6).

    A recorded linkage takes precedence: an entry whose record names this
    issue is the match, and an entry whose record names a *different*
    issue is off-limits to title matching (it belongs elsewhere). Only
    entries with no readable linkage fall back to the normalized-title
    comparison.

    One task can be listed twice — the terminal task dir and the review
    summary file left behind under the same name — and both now read the
    same id-keyed record, so the last match wins, which is the task dir
    (`scan_queue` lists dirs after files, the `find_task` precedence).
    """
    unlinked: list[TaskLocation] = []
    matches: list[TaskLocation] = []
    for entry in entries:
        linkage = task_record.read_linkage(queue_dir, entry.name)
        if linkage is None:
            unlinked.append(entry)
        elif linkage.issue == issue.number:
            matches.append(entry)
    if matches:
        return matches[-1]
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


def _flag_existing_task(issue, match: TaskLocation,
                        params: InboundParams) -> None:
    """Demo spec edge 1: `snes-demo` on an already-synced issue flags the
    existing task's sidecar without duplicating the task.

    A task with no readable linkage (no record, or one that does not parse)
    is left untouched; a fresh linkage is written only where the task was
    unlinked, matching the outbound fresh-title-match rule (FR-1.6).
    """
    linkage = task_record.read_linkage(params.queue_dir, match.name)
    if linkage is None:
        task_record.write_linkage(
            params.queue_dir, match.name,
            SyncLinkage(issue=issue.number, repo=params.repo, demo=True))
        params.log(f"  gh #{issue.number}: linked {match.name} in "
                   f"{match.location}/ with demo flag")
        return
    if linkage.demo:
        params.log(f"  gh #{issue.number}: skip (debug) — {match.name} in "
                   f"{match.location}/ already flagged demo")
        return
    task_record.write_linkage(
        params.queue_dir, match.name,
        SyncLinkage(issue=linkage.issue, repo=linkage.repo,
                    comment_ids=linkage.comment_ids, demo=True))
    params.log(f"  gh #{issue.number}: demo flag added to {match.name} "
               f"in {match.location}/")


def _ingest_issue(issue, entries: list[TaskLocation],
                  params: InboundParams, demo: bool = False) -> bool:
    """One issue -> one pending task file. True when a file was created.

    `demo=True` (the `snes-demo` trigger) records the flag in the sidecar
    and, when the issue already matches a task, flags that task instead
    of importing a duplicate (demo spec FR-1.3, edge 1).
    """
    match = find_matching_task(issue, entries, params.queue_dir)
    if match is not None:
        if demo:
            _flag_existing_task(issue, match, params)
        else:
            params.log(f"  gh #{issue.number}: skip (debug) — matches "
                       f"{match.name} in {match.location}/; not importing")
        return False
    filename = derive_task_filename(issue, entries)
    target = params.queue_dir / "pending" / filename
    if target.exists():
        params.log(f"  gh #{issue.number}: pending/{filename} already "
                   f"exists; skipping (no overwrite)")
        return False
    params.queue_dir.joinpath("pending").mkdir(parents=True, exist_ok=True)
    write_atomic(target, issue_task_body(issue))
    task_record.write_linkage(
        params.queue_dir, target.stem,
        SyncLinkage(issue=issue.number, repo=params.repo, demo=demo))
    entries.append(TaskLocation(name=target.stem, path=target,
                                location="pending"))
    params.log(f"  gh #{issue.number}: imported -> pending/{filename}")
    return True


def _trigger_for(issue, demo_enabled: bool = False) -> TriggerLabel | None:
    """The one action an issue's labels instruct, in FR-1.5 precedence
    (delete > park > demo > ingest). With the demo feature disabled,
    `snes-demo` is not a trigger at all (demo spec FR-9).
    """
    names = {label.name for label in issue.labels}
    for trigger in TRIGGER_PRECEDENCE:
        if trigger is TriggerLabel.DEMO and not demo_enabled:
            continue
        if trigger.value in names:
            return trigger
    return None


def _collect_trigger_issues(api,
                            demo_enabled: bool = False) -> dict[int, object]:
    """Every issue carrying a trigger label, keyed by number.

    Ingest reads open issues only (FR-1.2), as does the demo trigger
    (demo spec FR-1.2); the halt triggers apply to open *and* closed
    issues (FR-1.3). The open listing is taken first and wins a merge, so
    an issue's recorded state is `open` whenever it is open now (the
    delete anti-loop depends on it). The `snes-demo` listing happens only
    while the feature is enabled (demo spec FR-9).
    """
    queries = [(TriggerLabel.DELETE, (IssueState.OPEN, IssueState.CLOSED)),
               (TriggerLabel.PARK, (IssueState.OPEN, IssueState.CLOSED))]
    if demo_enabled:
        queries.append((TriggerLabel.DEMO, (IssueState.OPEN,)))
    queries.append((TriggerLabel.INGEST, (IssueState.OPEN,)))
    found: dict[int, object] = {}
    for label, states in queries:
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


def _park_issue_task(issue, entries: list[TaskLocation],
                     params: InboundParams) -> bool:
    """FR-1.3: park the task matching a `snes-parked` issue. True when moved."""
    match = find_matching_task(issue, entries, params.queue_dir)
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
    # The record is keyed by task id, so parking needs no metadata move
    # (FR-C.3): the linkage is still found by `match.name` in `parked/`.
    match.path = params.queue_dir / "parked" / match.name
    match.location = "parked"
    params.log(f"  gh #{issue.number}: parked {match.name} (was {was}/)")
    return True


def _close_and_unlabel(api, issue, params: InboundParams) -> None:
    """FR-1.4 anti-loop: close the issue and drop `snes`/`snes-deleted`.

    Only labels the issue actually carries are removed (no 404 churn);
    human labels are never touched. `snes-demo` is removed exactly where
    `snes` is removed (demo spec FR-1.6) — and only while the feature is
    enabled, since a disabled pass never acts on the label at all. A
    failure here is logged by the per-issue guard in `run_inbound`, not
    silently retried.
    """
    if issue.state is IssueState.OPEN:
        api.close_issue(issue.number)
    carried = {label.name for label in issue.labels}
    removable = [TriggerLabel.INGEST.value, TriggerLabel.DELETE.value]
    if params.demo_enabled:
        removable.append(TriggerLabel.DEMO.value)
    for name in removable:
        if name in carried:
            api.remove_label(issue.number, name)


def _delete_issue_task(api, issue, entries: list[TaskLocation],
                       params: InboundParams) -> bool:
    """FR-1.4: delete the task matching a `snes-deleted` issue. True if gone."""
    match = find_matching_task(issue, entries, params.queue_dir)
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
    # The task is gone, so its linkage goes with it — otherwise the record
    # would keep claiming a deleted task belongs to this (now closed) issue.
    if not task_record.clear_linkage(params.queue_dir, match.name):
        params.log(f"  gh #{issue.number}: anomaly — could not clear the "
                   f"linkage record of {match.name}")
    _close_and_unlabel(api, issue, params)
    entries.remove(match)
    params.log(f"  gh #{issue.number}: deleted {match.name} from "
               f"{match.location}/; issue closed and trigger labels removed")
    return True


def run_inbound(api, params: InboundParams) -> InboundResult:
    """FR-1 phase 1: apply every issue's trigger action to the queue.

    One action per issue in FR-1.5 precedence (delete > park > demo >
    ingest). A failure on one issue is logged and skipped — it never
    aborts the pass (NFR-1). Idempotent (FR-1.7): a second pass sees
    imports as sidecar matches, demo flags as already-flagged, parks as
    already-parked, and deletes as gone — no queue changes.
    """
    result = InboundResult()
    entries = scan_queue(params.queue_dir)
    actions = {
        TriggerLabel.INGEST: lambda issue: _ingest_issue(issue, entries, params),
        TriggerLabel.DEMO: lambda issue: _ingest_issue(issue, entries, params, demo=True),
        TriggerLabel.PARK: lambda issue: _park_issue_task(issue, entries, params),
        TriggerLabel.DELETE: lambda issue: _delete_issue_task(api, issue, entries, params),
    }
    tallies = {
        TriggerLabel.INGEST: "imported",
        TriggerLabel.DEMO: "imported",
        TriggerLabel.PARK: "parked",
        TriggerLabel.DELETE: "deleted",
    }
    issues = _collect_trigger_issues(api, demo_enabled=params.demo_enabled)
    for number in sorted(issues):
        trigger = _trigger_for(issues[number],
                               demo_enabled=params.demo_enabled)
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
