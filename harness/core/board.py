"""Pure rendering of the `board` command: executive summary + kanban sections.

State collection lives in `cli/handlers.py`; this module turns collected data
into the rendered string (precedent: `stats.render_report`). It is a leaf: it
imports no workflow/cli code, mutates nothing, and its output is a
deterministic function of its input. Slice 1 renders the executive summary
and empty location headers; task rows, color and the wide layout arrive in
later slices.
"""
from __future__ import annotations

from dataclasses import dataclass

from .enums import Verdict

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


@dataclass(frozen=True)
class LocationCount:
    """One lifecycle location and how many tasks sit in it."""
    location: str
    count: int


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
    locations: tuple[LocationCount, ...]
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


def _render_summary_lines(summary: BoardSummary) -> list[str]:
    lines = ["=== harness board ==="]
    lines.append(" · ".join(f"{c.location} {c.count}" for c in summary.locations))
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
    """Render the executive summary plus one section per location.

    Slice 1: every section is an empty column (the marker line); task rows
    are later slices. The section order is the order of `summary.locations`,
    which the handler builds from QUEUE_LOCATIONS_ALL.
    """
    lines = _render_summary_lines(summary)
    lines.append("")
    for c in summary.locations:
        header = f"── {c.location} ({c.count}) "
        lines.append(header + "─" * max(0, _SECTION_RULE_WIDTH - len(header)))
        lines.append(f"  {EMPTY_COLUMN_MARKER}")
    return "\n".join(lines)
