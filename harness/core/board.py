"""Pure rendering of the `board` command: executive summary + kanban sections.

State collection lives in `cli/handlers.py`; this module turns collected data
into the rendered string (precedent: `stats.render_report`). It is a leaf: it
imports no workflow/cli code, mutates nothing, and its output is a
deterministic function of its input. Slice 2 added the task rows (id, origin
tag, ordering, `done/` cap); slice 3 added the per-task state fields (stage,
checkpoints, last updated, owner, collapsed stats, terminal reason); slice 4
adds ANSI color gated on the caller-supplied `RenderContext`, the wide
side-by-side column layout, truncation to the terminal width, and an
encoding-safe writer.
"""
from __future__ import annotations

import re
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

# ANSI escapes (spec FR-5). Applied only when the RenderContext says the
# stream is a TTY and NO_COLOR is unset; the escapes are the whole payload of
# the color decision, so stripping them yields the plain output byte-for-byte.
COLOR_RESET = "\033[0m"
COLOR_GREEN = "\033[32m"
COLOR_YELLOW = "\033[33m"
COLOR_RED = "\033[31m"
COLOR_MAGENTA = "\033[35m"

# At or above this terminal width the locations render as side-by-side
# columns; below it, as stacked sections (spec FR-6, ~120 cells).
WIDE_LAYOUT_MIN_WIDTH = 120

# Cells between adjacent columns in the wide layout.
_COLUMN_GAP = 1

# Cells a column keeps for itself when the columns must share the terminal.
# Below this a column header is unreadable, so the share stops here and cells
# truncate instead of shrinking further.
MIN_COLUMN_WIDTH = 14

# The separator `_task_line_text` puts between a task row's fields.
FIELD_SEPARATOR = "  "

# Where a wide-layout column cell may break: any run of spaces, so a row that
# does not fit its column wraps between words instead of mid-word (spec FR-6)
# and every field the stacked layout shows stays visible in the columns too.
WRAP_POINT = re.compile("( +)")

# Marks a column line that continues the task row above it.
COLUMN_CONTINUATION_INDENT = "  "

# Marks a line cut short by truncation (spec FR-6: truncate, never wrap).
TRUNCATION_MARKER = "…"


class TaskOrigin(Enum):
    """Who created a task, classified by id alone (spec FR-4)."""
    AUTO = "auto"
    USER = "user"


def classify_origin(task_id: str) -> TaskOrigin:
    """`auto-…` ids are auto-generated; everything else is user-created."""
    return (TaskOrigin.AUTO if task_id.startswith(AUTO_ID_PREFIX)
            else TaskOrigin.USER)


# Task rows carry their origin color: user green, auto magenta (spec FR-5).
ORIGIN_COLORS = {TaskOrigin.USER: COLOR_GREEN, TaskOrigin.AUTO: COLOR_MAGENTA}

# Summary/column-header accents: `failed`/`parked` read as warnings, `done`
# as green (spec FR-5). Other locations stay uncolored.
LOCATION_COLORS = {TaskStatus.FAILED.value: COLOR_RED,
                   TaskStatus.PARKED.value: COLOR_YELLOW,
                   TaskStatus.DONE.value: COLOR_GREEN}


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
class RenderContext:
    """The terminal facts the renderer needs, collected by the handler.

    `use_color` is the TTY-and-NO_COLOR decision (spec FR-6) — the renderer
    never inspects the environment or the stream itself. `width` is the
    terminal width in cells; lines longer than it are truncated, and the
    layout switches to columns at WIDE_LAYOUT_MIN_WIDTH.
    """
    use_color: bool = False
    width: int = 80


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


def _paint(text: str, color: str, context: RenderContext) -> str:
    """Wrap `text` in `color` when color is on; plain text otherwise."""
    if not context.use_color or not color or not text:
        return text
    return f"{color}{text}{COLOR_RESET}"


def _truncate(text: str, width: int) -> str:
    """Cut `text` to `width` cells, marking the cut (spec FR-6: truncate
    rather than wrap). A non-positive width means 'no limit'."""
    if width <= 0 or len(text) <= width:
        return text
    if width <= len(TRUNCATION_MARKER):
        return TRUNCATION_MARKER[:width]
    return text[:width - len(TRUNCATION_MARKER)] + TRUNCATION_MARKER


def _task_line_text(task: BoardTask) -> str:
    """One task row's plain text: id, origin tag, and the state fields it
    has (FR-3). Unreadable state says so (FR-7). No indent, no color — the
    layouts add both, so stacked and column output carry identical text.
    Fields are separated by FIELD_SEPARATOR, the boundary the wide layout
    wraps on.
    """
    line = f"{task.task_id} [{task.origin.value}]"
    if not task.state_readable:
        line += FIELD_SEPARATOR + "state: unknown"
    if task.state_readable and task.stage:
        line += (FIELD_SEPARATOR + f"stage={task.stage}"
                 + f" done:[{','.join(task.checkpointed_stages)}]")
    updated = _updated_label(task)
    if updated:
        line += FIELD_SEPARATOR + f"updated={updated}"
    if task.owner:
        line += FIELD_SEPARATOR + f"owner={_owner_label(task.owner)}"
    if task.stats is not None:
        line += (FIELD_SEPARATOR + f"sessions={task.stats.sessions}"
                 f" tokens={task.stats.tokens}"
                 f" time={task.stats.duration_s:.0f}s"
                 f" last verdict={task.stats.last_verdict}")
    if task.reason:
        line += FIELD_SEPARATOR + f"reason={task.reason}"
    return line


def _visible_tasks(loc: LocationBoard) -> tuple[list[BoardTask], int]:
    """The location's tasks in board order, capped for `done/` (FR-7).

    Returns the tasks to show and how many the cap hid.
    """
    tasks = list(sort_tasks(loc.tasks))
    hidden = 0
    if loc.location == TaskStatus.DONE.value and len(tasks) > DONE_DISPLAY_CAP:
        hidden = len(tasks) - DONE_DISPLAY_CAP
        tasks = tasks[:DONE_DISPLAY_CAP]
    return tasks, hidden


def _render_location(loc: LocationBoard, context: RenderContext) -> list[str]:
    header = f"── {loc.location} ({len(loc.tasks)}) "
    rule = header + "─" * max(0, _SECTION_RULE_WIDTH - len(header))
    lines = [_paint(_truncate(rule, context.width),
                    LOCATION_COLORS.get(loc.location, ""), context)]
    tasks, hidden = _visible_tasks(loc)
    if not tasks:
        lines.append(f"  {EMPTY_COLUMN_MARKER}")
    for task in tasks:
        lines.append(_paint(_truncate(f"  {_task_line_text(task)}",
                                      context.width),
                            ORIGIN_COLORS[task.origin], context))
    if hidden:
        lines.append(f"  (+{hidden} more)")
    return lines


@dataclass(frozen=True)
class _ColumnLine:
    """One plain line inside a wide-layout column, with its accent color."""
    text: str
    color: str = ""


@dataclass(frozen=True)
class _Column:
    """One location as the wide layout sees it: header plus body lines."""
    header: _ColumnLine
    lines: tuple[_ColumnLine, ...]


def _share_remainder(cells: int, weights: list[int]) -> list[int]:
    """Split `cells` across columns in proportion to `weights`.

    Integer shares, so the layout is deterministic; the cells left over by
    rounding go to the columns with the largest fractional part, ties by
    column order.
    """
    total = sum(weights)
    if total <= 0:
        base, rest = divmod(cells, len(weights))
        return [base + (1 if i < rest else 0) for i in range(len(weights))]
    shares = [cells * w // total for w in weights]
    order = sorted(range(len(shares)),
                   key=lambda i: (-((cells * weights[i]) % total), i))
    for index in order[:cells - sum(shares)]:
        shares[index] += 1
    return shares


def _column_widths(columns: list[_Column], total_width: int) -> list[int]:
    """Per-column cell widths for the wide layout.

    Each column would like its natural width (its longest line). When they
    do not all fit in `total_width`, every column keeps MIN_COLUMN_WIDTH and
    the rest of the terminal is shared out in proportion to how much each
    column wanted beyond that floor — a content-heavy column gets more cells
    than an empty one. The widths are fixed for the whole board, so one long
    field cannot shift it (spec FR-6); a line that still does not fit wraps
    on field boundaries instead of being cut.
    """
    count = len(columns)
    natural = [max(len(line.text) for line in (col.header, *col.lines))
               for col in columns]
    available = total_width - _COLUMN_GAP * (count - 1)
    if available <= 0:
        return [1] * count
    if sum(natural) <= available:
        return natural
    if available < MIN_COLUMN_WIDTH * count:
        return [max(1, available // count)] * count
    extra = _share_remainder(available - MIN_COLUMN_WIDTH * count,
                             [max(0, w - MIN_COLUMN_WIDTH) for w in natural])
    return [MIN_COLUMN_WIDTH + e for e in extra]


def _wrap_cell_text(text: str, width: int) -> list[str]:
    """Break one column line onto several lines of at most `width` cells.

    Breaks happen only at a WRAP_POINT (a run of spaces), never inside a word;
    continuation lines are indented so they read as part of the row above. A
    single word longer than the column is the one thing that still gets
    truncated (spec FR-6: truncate long fields to the available width).
    """
    if width <= 0 or len(text) <= width:
        return [text]
    limit = max(1, width - len(COLUMN_CONTINUATION_INDENT))
    lines: list[str] = []
    current = ""
    separator = ""
    for piece in WRAP_POINT.split(text):
        if not piece:
            continue
        if piece.isspace():
            separator = piece
            continue
        candidate = f"{current}{separator}{piece}" if current else piece
        if len(candidate) <= (limit if lines else width):
            current = candidate
            separator = ""
            continue
        if current:
            lines.append(current)
        separator = ""
        current = _truncate(piece, limit)
    if current:
        lines.append(current)
    return lines


def _wrapped_body(col: _Column, width: int) -> tuple[_ColumnLine, ...]:
    """A column's body lines as they physically sit in a `width`-cell column."""
    body: list[_ColumnLine] = []
    for line in col.lines:
        for index, physical in enumerate(_wrap_cell_text(line.text, width)):
            text = (physical if index == 0
                    else COLUMN_CONTINUATION_INDENT + physical)
            body.append(_ColumnLine(text, line.color))
    return tuple(body)


def _render_column_cell(line: _ColumnLine | None, width: int,
                        context: RenderContext) -> str:
    """One padded, truncated, colored cell of the wide layout. A missing
    line (the column is shorter than the tallest) is blank padding."""
    if line is None:
        return " " * width
    plain = _truncate(line.text, width)
    return _paint(plain, line.color, context) + " " * (width - len(plain))


def _render_columns(summary: BoardSummary, context: RenderContext) -> list[str]:
    """The wide layout: one side-by-side column per location (spec FR-6).

    Same information as the stacked sections — same header counts, same
    task lines, same empty markers and `(+N more)` lines — laid out across
    the terminal, a task row that does not fit its column wrapped on field
    boundaries rather than cut short.
    """
    columns = []
    for loc in summary.locations:
        tasks, hidden = _visible_tasks(loc)
        body = ([_ColumnLine(EMPTY_COLUMN_MARKER)] if not tasks else
                [_ColumnLine(_task_line_text(task),
                             ORIGIN_COLORS[task.origin]) for task in tasks])
        if hidden:
            body.append(_ColumnLine(f"(+{hidden} more)"))
        columns.append(_Column(
            header=_ColumnLine(f"{loc.location} ({len(loc.tasks)})",
                               LOCATION_COLORS.get(loc.location, "")),
            lines=tuple(body)))
    widths = _column_widths(columns, context.width)
    bodies = [_wrapped_body(col, width)
              for col, width in zip(columns, widths)]
    gap = " " * _COLUMN_GAP
    lines = [gap.join(_render_column_cell(col.header, width, context)
                      for col, width in zip(columns, widths))]
    for row in range(max(len(body) for body in bodies)):
        lines.append(gap.join(
            _render_column_cell(
                body[row] if row < len(body) else None,
                width, context)
            for body, width in zip(bodies, widths)))
    return lines


def _render_summary_lines(summary: BoardSummary,
                          context: RenderContext) -> list[str]:
    lines = ["=== harness board ==="]
    lines.append(" · ".join(
        _paint(f"{c.location} {len(c.tasks)}",
               LOCATION_COLORS.get(c.location, ""), context)
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


def render_board(summary: BoardSummary,
                 context: RenderContext = RenderContext()) -> str:
    """Render the executive summary plus the board body.

    The section order is the order of `summary.locations`, which the handler
    builds from QUEUE_LOCATIONS_ALL; task order within a section is
    `sort_tasks`, with `done/` capped at DONE_DISPLAY_CAP entries plus a
    `(+N more)` line. At `context.width` >= WIDE_LAYOUT_MIN_WIDTH the body
    is side-by-side columns; below it, stacked sections. Both layouts carry
    identical information; color follows `context.use_color` only.
    """
    lines = _render_summary_lines(summary, context)
    lines.append("")
    if context.width >= WIDE_LAYOUT_MIN_WIDTH:
        lines.extend(_render_columns(summary, context))
    else:
        for loc in summary.locations:
            lines.extend(_render_location(loc, context))
    return "\n".join(lines)


def write_board(text: str, stream) -> None:
    """Write rendered board text to `stream` without an encoding crash.

    The board uses box-drawing and `·` characters and task ids may hold any
    Unicode; on a non-UTF8 locale a direct write could raise
    `UnicodeEncodeError` (spec FR-7). Round-tripping through the stream's
    own encoding with `errors="replace"` substitutes what cannot be encoded
    and never raises, and is a no-op for UTF-8 streams. A stream that
    declares no encoding (a pure in-memory text stream) takes the text as
    written, untransformed.
    """
    encoding = getattr(stream, "encoding", None)
    if encoding:
        text = text.encode(encoding, errors="replace").decode(
            encoding, errors="replace")
    stream.write(text + "\n")
