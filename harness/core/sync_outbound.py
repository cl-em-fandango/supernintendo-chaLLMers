"""Outbound sync phase: harness queue -> GitHub issues (spec FR-2).

Every task in the seven synced locations gets exactly one open issue
carrying exactly one `snes-<state>` label matching its queue location:

* match (FR-2.1) — a sidecar naming this repo wins (FR-1.6); otherwise the
  lowest-numbered *open* issue whose title normalizes equal to the task
  name (lowercase, `_` read as space), with a warning when several match;
* closed match parks (FR-2.2) — no open match but a closed one moves the
  task to `parked/` through the lifecycle park path (reason
  `"GitHub issue closed"`) instead of recreating an issue; the park is a
  plain move, not a halt: closes never stand a session down (spec edge 2);
* create (FR-2.3) — neither open nor closed: a new issue titled with the
  task name (`_` back to spaces) with the task's markdown as its body,
  and the returned number recorded in the sidecar;
* state label (FR-2.4) — a diff, never replace-all: fetch the current
  labels, add the target state label when missing, remove only stale
  `snes-*` *state* labels. Non-`snes` (human) labels and the bare `snes`
  subscription marker are never touched.

Outbound iterates over tasks that exist and nothing else (FR-2.6, no
orphan chasing): a locally deleted task reopens, recreates or re-labels
nothing.

One task can appear on the scan in two locations at once — a terminal
task dir in `done/` leaves its review summary in `review/` — so entries
are collapsed per task name with the latest lifecycle location winning;
otherwise every pass would flip the issue's state label review -> done.

State and behavior (CODING_STANDARDS §2): `OutboundParams` /
`OutboundResult` describe shape; `run_outbound()` acts. The GitHub side
is reached only through the injected client; a failure on one task is
logged and skipped, never aborting the pass (NFR-1).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from external.github_api import PASS_ABORT_ERRORS, Issue, IssueState

from ..workflow.task_lifecycle import (
    CLAIMED_LOCATION,
    QUEUE_LOCATIONS_ALL,
    TaskLifecycle,
)
from .sync_inbound import (
    FILE_LOCATIONS,
    TaskLocation,
    find_task,
    normalize_title,
    scan_queue,
)
from .sync_labels import StateLabel, is_state_label
from .sync_sidecar import (
    SyncLinkage,
    move_sidecar_into_task_dir,
    read_linkage,
    task_dir_sidecar_path,
    write_linkage,
)

# The one state label an issue must carry per queue location (FR-2.4).
STATE_LABEL_FOR_LOCATION: dict[str, StateLabel] = {
    "pending": StateLabel.PENDING,
    CLAIMED_LOCATION: StateLabel.CLAIMED,
    "active": StateLabel.ACTIVE,
    "review": StateLabel.REVIEW,
    "parked": StateLabel.PARKED,
    "failed": StateLabel.FAILED,
    "done": StateLabel.DONE,
}


@dataclass
class OutboundParams:
    """Explicit parameters for one outbound pass (CODING_STANDARDS §5)."""
    queue_dir: Path
    repo: str          # "owner/name" the issues live in
    log: Callable[[str], None] = print
    # The lifecycle authority for closed-match parks; None only in wiring
    # that cannot move tasks, and such a park then logs and skips.
    lifecycle: TaskLifecycle | None = None


@dataclass
class OutboundResult:
    """What one outbound pass did (feeds `SyncReport`)."""
    created_issues: int = 0
    label_updates: int = 0
    parked: int = 0


def _dedupe_entries(entries: list[TaskLocation],
                    log: Callable[[str], None]) -> list[TaskLocation]:
    """One entry per task name; the latest lifecycle location wins.

    A done/parked/failed task keeps a review summary file beside its
    terminal dir; syncing both would fight over the issue's single state
    label every pass, so the later location (the task's real state) is
    the one synced.
    """
    best: dict[str, TaskLocation] = {}
    for entry in entries:
        key = normalize_title(entry.name)
        current = best.get(key)
        if current is None:
            best[key] = entry
        elif current.name != entry.name:
            # Spec edge 4 (FR-2.1): two *different* task names normalize to
            # one title, so they cannot share one issue. The first scanned
            # owns it; the other is skipped loudly rather than fighting it
            # for the state label every pass.
            log(f"  {entry.name}: skip — the title '{key}' is already "
                f"claimed by {current.name} (first wins, FR-2.1)")
        elif QUEUE_LOCATIONS_ALL.index(entry.location) > \
                QUEUE_LOCATIONS_ALL.index(current.location):
            best[key] = entry
            log(f"  {entry.name}: skip (debug) — the {current.location}/ "
                f"copy is superseded by {entry.location}/")
    return [entry for entry in entries if best.get(normalize_title(entry.name)) is entry]


def _task_body(entry: TaskLocation) -> str:
    """The task's markdown: the file itself, or `original.md` in a dir."""
    if entry.path.is_dir():
        original = entry.path / "original.md"
        return original.read_text() if original.is_file() else ""
    return entry.path.read_text()


def _title_matches(issues: list[Issue], name: str) -> list[Issue]:
    """Issues whose title matches `name` per FR-2.1, lowest number first."""
    wanted = normalize_title(name)
    return sorted((issue for issue in issues
                   if normalize_title(issue.title) == wanted),
                  key=lambda issue: issue.number)


def _find_number(issues: list[Issue], number: int) -> Issue | None:
    return next((issue for issue in issues if issue.number == number), None)


def _match_issue(entry: TaskLocation, linkage: SyncLinkage | None,
                 open_issues: list[Issue], closed_issues: list[Issue],
                 api, log: Callable[[str], None]) -> Issue | None:
    """The issue representing this task, or None (FR-1.6, FR-2.1, FR-2.2).

    A sidecar wins — including an issue that fell out of both listings,
    fetched by number. Otherwise the lowest-numbered open title match
    (warning on several), then a closed match, which the caller parks
    on rather than creating a replacement.
    """
    if linkage is not None:
        return (_find_number(open_issues, linkage.issue)
                or _find_number(closed_issues, linkage.issue)
                or api.get_issue(linkage.issue))
    for issues, state_name in ((open_issues, "open"), (closed_issues, "closed")):
        matches = _title_matches(issues, entry.name)
        if not matches:
            continue
        if len(matches) > 1:
            log(f"  {entry.name}: {len(matches)} {state_name} issues match "
                f"the title; using #{matches[0].number} (lowest wins, FR-2.1)")
        return matches[0]
    return None


def _create_issue(api, entry: TaskLocation,
                  params: OutboundParams) -> Issue:
    """FR-2.3: new issue from the task, linkage recorded in the sidecar."""
    issue = api.create_issue(title=entry.name.replace("_", " "),
                             body=_task_body(entry))
    write_linkage(entry.sidecar,
                  SyncLinkage(issue=issue.number, repo=params.repo))
    params.log(f"  {entry.name}: created gh #{issue.number} "
               f"('{issue.title}')")
    return issue


def _park_closed_issue(issue: Issue, entry: TaskLocation,
                       params: OutboundParams) -> bool:
    """FR-2.2: a closed matching issue parks the task. True when moved."""
    if entry.location == "parked":
        params.log(f"  {entry.name}: skip (debug) — issue #{issue.number} "
                   f"is closed and the task is already parked")
        return False
    if params.lifecycle is None:
        params.log(f"  {entry.name}: no lifecycle wired; not parking")
        return False
    was = entry.location
    params.lifecycle.park(entry.name, "GitHub issue closed", from_=was)
    parked_dir = params.queue_dir / "parked" / entry.name
    if was in FILE_LOCATIONS:
        move_sidecar_into_task_dir(entry.sidecar, parked_dir)
    entry.path = parked_dir
    entry.location = "parked"
    entry.sidecar = task_dir_sidecar_path(parked_dir)
    if read_linkage(entry.sidecar) is None:
        write_linkage(entry.sidecar,
                      SyncLinkage(issue=issue.number, repo=params.repo))
    params.log(f"  {entry.name}: parked — matching issue #{issue.number} "
               f"is closed (was {was}/)")
    return True


def _apply_state_label(api, issue: Issue, entry: TaskLocation,
                       params: OutboundParams) -> bool:
    """FR-2.4: set the issue's state label by diff. True when it changed.

    Add the target label only when missing; remove only stale `snes-*`
    state labels. The bare `snes` marker and every human label are
    outside `is_state_label` and therefore never in the removal set.
    """
    target = STATE_LABEL_FOR_LOCATION[entry.location]
    current = [label.name for label in api.list_labels(issue.number)]
    to_add = [] if target.value in current else [target.value]
    to_remove = [name for name in current
                 if name != target.value and is_state_label(name)]
    if not to_add and not to_remove:
        return False
    if to_add:
        api.add_labels(issue.number, to_add)
    for name in to_remove:
        api.remove_label(issue.number, name)
    changes = [f"+{name}" for name in to_add] \
        + [f"-{name}" for name in to_remove]
    params.log(f"  {entry.name}: gh #{issue.number} labels "
               f"{', '.join(changes)}")
    return True


def _sync_entry(api, entry: TaskLocation, params: OutboundParams,
                open_issues: list[Issue], closed_issues: list[Issue],
                result: OutboundResult) -> None:
    """Bring one task's issue into agreement with the queue (FR-2)."""
    linkage = read_linkage(entry.sidecar)
    if linkage is not None and linkage.repo and linkage.repo != params.repo:
        params.log(f"  {entry.name}: skip (debug) — linked to "
                   f"{linkage.repo}, not the synced repo {params.repo}")
        return
    issue = _match_issue(entry, linkage, open_issues, closed_issues,
                         api, params.log)
    if issue is None:
        issue = _create_issue(api, entry, params)
        result.created_issues += 1
    elif issue.state is IssueState.CLOSED:
        if _park_closed_issue(issue, entry, params):
            result.parked += 1
        return  # FR-2.2: a closed match skips further outbound work
    elif linkage is None:
        # A fresh title match: record it so the sidecar wins from now on
        # (FR-1.6) and later renames cannot orphan the link.
        write_linkage(entry.sidecar,
                      SyncLinkage(issue=issue.number, repo=params.repo))
    if _apply_state_label(api, issue, entry, params):
        result.label_updates += 1


def sync_one_task(api, task_id: str, params: OutboundParams) -> OutboundResult:
    """Targeted outbound (spec FR-3 in-flight rule): one task, no scan.

    Brings the single task's issue up to date — its state label for the
    queue location it is in right now — without listing or touching any
    other task. A task with no sidecar linkage has no issue to update:
    creating it is the full pass's job, so this is a logged no-op, and so
    is a closed match (parking an in-flight task mid-session is the full
    pass's call, not a hook's). Abort-class errors propagate so the
    engine can roll the work to the next pass (edge 9).
    """
    result = OutboundResult()
    entry = find_task(params.queue_dir, task_id)
    if entry is None:
        params.log(f"  {task_id}: skip (debug) — targeted sync, task not on "
                   f"the queue")
        return result
    linkage = read_linkage(entry.sidecar)
    if linkage is None:
        params.log(f"  {task_id}: skip (debug) — targeted sync, no issue "
                   f"linkage")
        return result
    if linkage.repo and linkage.repo != params.repo:
        params.log(f"  {task_id}: skip (debug) — targeted sync, linked to "
                   f"{linkage.repo}, not the synced repo {params.repo}")
        return result
    issue = api.get_issue(linkage.issue)
    if issue.state is IssueState.CLOSED:
        params.log(f"  {task_id}: skip (debug) — targeted sync, issue "
                   f"#{linkage.issue} is closed")
        return result
    if _apply_state_label(api, issue, entry, params):
        result.label_updates += 1
    return result


def run_outbound(api, params: OutboundParams) -> OutboundResult:
    """FR-2 phase 2: sync every task on the queue to its GitHub issue.

    A pass over an empty queue makes no API call at all. A failure on one
    task is logged and skipped — the pass and the pipeline stay healthy
    (NFR-1).
    """
    result = OutboundResult()
    entries = _dedupe_entries(scan_queue(params.queue_dir), params.log)
    if not entries:
        return result
    open_issues = api.list_issues(state=IssueState.OPEN)
    closed_issues = api.list_issues(state=IssueState.CLOSED)
    for entry in entries:
        try:
            _sync_entry(api, entry, params, open_issues, closed_issues,
                        result)
        except PASS_ABORT_ERRORS:
            raise  # edge 9 / FR-5: the engine aborts the pass, not the loop
        except Exception as exc:  # one bad task must not sink the pass
            params.log(f"  {entry.name}: outbound sync failed: {exc}")
    return result
