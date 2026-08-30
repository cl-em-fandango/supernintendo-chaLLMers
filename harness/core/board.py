"""Pure rendering of the `board` command: executive summary + kanban sections.

State collection lives in `cli/handlers.py`; this module turns collected data
into the rendered string (precedent: `stats.render_report`). It is a leaf: it
imports no workflow/cli code, mutates nothing, and its output is a
deterministic function of its input. Slice 2 added the task rows (id, origin
tag, ordering, `done/` cap); slice 3 adds the per-task state fields (stage,
checkpoints, last updated, owner, collapsed stats, terminal reason). Color
and the wide layout arrive in a later slice.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum

from .claim_metadata import OWNER_UNKNOWN
from .enums import TaskStatus, Verdict

# Outcomes that count as a decided session (spec FR-2): only a session whose
# outcome is one of these contributes to the pass / reject percentages. The
# enum members' values are the wire strings in `sessions.jsonl`.
DECIDED_OUTCOMES = frozenset({Verdict.PASS, Verdict.FAIL,
                              Verdict.KICKBACK, Verdict.KICKOUT})

# The decided outcomes that read as a rejection (spec FR-2: reject/kickout %).
REJECTED_OUTCOMES = frozenset({Verdict.FAIL, Verdict.KICKBACK,
                               Verdict.KICKOUT})

# What an empty location column shows so the board shape stays stable (FR-7).
EMPTY_COLUMN_MARKER = "-"

# Width of a section rule; fixed so output is deterministic.
_SECTION_RULE_WIDTH = 40

# Ids produced by `workflow/autonomous.py::_task_id` start with this prefix;
# it is the sole discriminator between auto-generated and user-created tasks
# (spec FR-4 — `Task.source` is not reliable).
AUTO_ID_PREFIX = "auto-"

# `done/` can grow without bound; the board shows only the N most recently
# updated done tasks plus a `(+N more)` line (spec FR-7).
DONE_DISPLAY_CAP = 10

# Locations whose tasks may carry a recorded terminal reason (park/fail,
# spec FR-3). The handler reads it best-effort; absence is not an error.
TERMINAL_LOCATIONS = frozenset({TaskStatus.PARKED.value, TaskStatus.FAILED.value})


class TaskOrigin(Enum):
    """Who created a task, classified by id alone (spec FR-4)."""
    AUTO = "auto"
    USER = "user"


def classify_origin(task_id: str) -> TaskOrigin:
    """`auto-…` ids are auto-generated; everything else is user-created."""
    return (TaskOrigin.AUTO if task_id.startswith(AUTO_ID_PREFIX)
            else TaskOrigin.USER)


@dataclass(frozen=True)
class BoardTask:
    """One task as the board sees it, collected by the handler.

    `last_updated` is the `task.json` timestamp string, empty when the task
    has no readable `task.json` or the file records no timestamp. Ordering
    reads it (spec §6) and treats the empty value as "no timestamp" — never
    as the epoch.

    `state_readable` is True only when a `task.json` was found and parsed as
    an object; a missing or corrupt one renders as `state: unknown` (FR-7).

    `mtime` is the entry's filesystem modification time (0.0 when it could
    not be stat'ed), the display fallback when `task.json` records no
    timestamp; it is deliberately not a sort key.

    `stage` and `checkpointed_stages` are the `task.json` values echoed
    verbatim (edge data: older files may hold names no current enum member
    covers); both are empty unless `state_readable`.

    `owner` is the claim-ownership sidecar value, set only for `claimed/`
    entries (`OWNER_UNKNOWN` renders as `?`). `reason` is a recorded
    park/fail reason for `parked/`/`failed/` entries, empty when none was
    found. `stats` is the task's collapsed session line, None when the
    stats store holds no rows for it.
    """
    task_id: str
    origin: TaskOrigin
    last_updated: str = ""
    mtime: float = 0.0
    state_readable: bool = False
    stage: str = ""
    checkpointed_stages: tuple[str, ...] = ()
    owner: str = ""
    reason: str = ""
    stats: "TaskStats | None" = None


@dataclass(frozen=True)
class TaskStats:
    """One task's session rows collapsed into the board's single line (FR-3).

    `sessions` is the row count, `tokens` the sum of `peak_tokens`,
    `duration_s` the sum of `duration_s`, and `last_verdict` the `verdict`
    of the newest row — a canonical `Verdict` value when the row holds one,
    otherwise the row's string verbatim (edge data from the model).
    """
    sessions: int
    tokens: int
    duration_s: float
    last_verdict: str


@dataclass(frozen=True)
class LocationBoard:
    """One lifecycle location and the tasks sitting in it (count = len)."""
    location: str
    tasks: tuple[BoardTask, ...] = ()


@dataclass(frozen=True)
class StatsAggregate:
    """The board's one-line aggregate over every session row in the store.

    `pass_rate` and `reject_rate` are fractions of *decided* sessions
    (see DECIDED_OUTCOMES), not of all sessions. `total_tokens` sums
    `peak_tokens` over all rows, decided or not.
    """
    sessions: int
    pass_rate: float
    reject_rate: float
    total_tokens: int


@dataclass(frozen=True)
class BoardSummary:
    """Everything slice 1's renderer prints, collected by the handler.

    `locations` is one entry per queue location in lifecycle order.
    `claims_warning` is the pre-formatted stranded-claims line (the handler
    owns the warning text; the renderer only places it), or None when
    `claimed/` is empty. `stats` is None when the store holds nothing the
    percentages can be computed from, and the aggregate line is omitted.
    """
    locations: tuple[LocationBoard, ...]
    claims_warning: str | None = None
    stats: StatsAggregate | None = None


def aggregate_stats(rows: list[dict]) -> StatsAggregate | None:
    """Collapse session rows into the board's aggregate line.

    Returns None when there is nothing to rate — no rows at all, or no row
    with a decided outcome — so the renderer omits the line instead of
    dividing by zero. An unreadable `peak_tokens` counts as 0, never a crash.
    """
    if not rows:
        return None
    decided = passed = rejected = 0
    for row in rows:
        outcome = Verdict.parse(str(row.get("outcome", "")))
        if outcome is None or outcome not in DECIDED_OUTCOMES:
            continue
        decided += 1
        if outcome == Verdict.PASS:
            passed += 1
        elif outcome in REJECTED_OUTCOMES:
            rejected += 1
    if not decided:
        return None
    return StatsAggregate(
        sessions=len(rows),
        pass_rate=passed / decided,
        reject_rate=rejected / decided,
        total_tokens=sum(int(r.get("peak_tokens", 0) or 0) for r in rows),
    )


def _parse_timestamp(raw: str) -> datetime | None:
    """Parse a `task.json` ISO timestamp; anything unparseable is 'no timestamp'."""
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _sort_key(task: BoardTask) -> tuple[int, float, str]:
    """Spec §6: `last_updated` descending, tasks without one last, id ascending.

    The negated timestamp makes the default ascending sort produce descending
    recency. A task with no parseable timestamp sorts after every task with
    one (leading 1) and is never compared as if its absence were the epoch.
    """
    ts = _parse_timestamp(task.last_updated)
    if ts is None:
        return (1, 0.0, task.task_id)
    return (0, -ts.timestamp(), task.task_id)


def sort_tasks(tasks: tuple[BoardTask, ...] | list[BoardTask]) -> tuple[BoardTask, ...]:
    """The board's deterministic task order within one location (§6)."""
    return tuple(sorted(tasks, key=_sort_key))


def _row_recency(row: dict, index: int) -> tuple[float, int]:
    """'Newest row' key: a parseable `ts` beats every row without one,
    otherwise append order decides (spec FR-3: order by timestamp, then
    append order, newest first)."""
    ts = _parse_timestamp(str(row.get("ts") or ""))
    return (ts.timestamp() if ts is not None else -1.0, index)


def collapse_task_stats(rows: list[dict]) -> TaskStats | None:
    """Collapse a task's session rows into its one board line (spec FR-3).

    None when the task has no rows — such a task shows none of these fields.
    Unreadable `peak_tokens`/`duration_s` count as 0, never a crash. The
    last verdict is the newest row's `verdict`, canonicalised through
    `Verdict` when it names a known member.
    """
    if not rows:
        return None
    tokens = 0
    duration_s = 0.0
    for row in rows:
        try:
            tokens += int(row.get("peak_tokens", 0) or 0)
        except (TypeError, ValueError):
            pass
        try:
            duration_s += float(row.get("duration_s", 0.0) or 0.0)
        except (TypeError, ValueError):
            pass
    newest = max(range(len(rows)), key=lambda i: _row_recency(rows[i], i))
    raw_verdict = str(rows[newest].get("verdict") or "")
    parsed = Verdict.parse(raw_verdict)
    return TaskStats(
        sessions=len(rows),
        tokens=tokens,
        duration_s=duration_s,
        last_verdict=parsed.value if parsed else (raw_verdict or "unknown"),
    )


def _owner_label(owner: str) -> str:
    """An unknown claim owner renders `?` (spec FR-3), a known one verbatim."""
    return "?" if owner == OWNER_UNKNOWN else owner


def _updated_label(task: BoardTask) -> str:
    """`task.json`'s timestamp, else the entry's mtime as UTC ISO, else none."""
    if task.last_updated:
        return task.last_updated
    if task.mtime > 0.0:
        return datetime.fromtimestamp(task.mtime, tz=timezone.utc).isoformat(
            timespec="seconds")
    return ""


def _render_task_line(task: BoardTask) -> str:
    """One task row: id, origin tag, and the state fields it has (FR-3).

    Unreadable state says so (FR-7). Color is slice 4.
    """
    line = f"  {task.task_id} [{task.origin.value}]"
    if not task.state_readable:
        line += "  state: unknown"
    if task.state_readable and task.stage:
        line += (f"  stage={task.stage}"
                 f" done:[{','.join(task.checkpointed_stages)}]")
    updated = _updated_label(task)
    if updated:
        line += f"  updated={updated}"
    if task.owner:
        line += f"  owner={_owner_label(task.owner)}"
    if task.stats is not None:
        line += (f"  sessions={task.stats.sessions} tokens={task.stats.tokens}"
                 f" time={task.stats.duration_s:.0f}s"
                 f" last verdict={task.stats.last_verdict}")
    if task.reason:
        line += f"  reason={task.reason}"
    return line


def _render_location(loc: LocationBoard) -> list[str]:
    header = f"── {loc.location} ({len(loc.tasks)}) "
    lines = [header + "─" * max(0, _SECTION_RULE_WIDTH - len(header))]
    tasks = sort_tasks(loc.tasks)
    hidden = 0
    if loc.location == TaskStatus.DONE.value and len(tasks) > DONE_DISPLAY_CAP:
        hidden = len(tasks) - DONE_DISPLAY_CAP
        tasks = tasks[:DONE_DISPLAY_CAP]
    if not tasks:
        lines.append(f"  {EMPTY_COLUMN_MARKER}")
    for task in tasks:
        lines.append(_render_task_line(task))
    if hidden:
        lines.append(f"  (+{hidden} more)")
    return lines


def _render_summary_lines(summary: BoardSummary) -> list[str]:
    lines = ["=== harness board ==="]
    lines.append(" · ".join(f"{c.location} {len(c.tasks)}"
                            for c in summary.locations))
    if summary.claims_warning:
        lines.append(summary.claims_warning)
    if summary.stats is not None:
        s = summary.stats
        lines.append(f"sessions {s.sessions} · "
                     f"pass {s.pass_rate * 100:.0f}% · "
                     f"reject/kickout {s.reject_rate * 100:.0f}% · "
                     f"tokens {s.total_tokens}")
    return lines


def render_board(summary: BoardSummary) -> str:
    """Render the executive summary plus one stacked section per location.

    The section order is the order of `summary.locations`, which the handler
    builds from QUEUE_LOCATIONS_ALL; task order within a section is
    `sort_tasks`, with `done/` capped at DONE_DISPLAY_CAP entries plus a
    `(+N more)` line. The side-by-side wide layout is a later slice.
    """
    lines = _render_summary_lines(summary)
    lines.append("")
    for loc in summary.locations:
        lines.extend(_render_location(loc))
    return "\n".join(lines)
