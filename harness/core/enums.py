"""Shared enums for discrete state. See CODING_STANDARDS.md §3."""
from __future__ import annotations
from enum import Enum


class TaskStatus(str, Enum):
    """Where a task lives in the queue lifecycle."""
    PENDING = "pending"
    ACTIVE = "active"
    DONE = "done"
    PARKED = "parked"
    FAILED = "failed"


class Verdict(str, Enum):
    """The VERDICT a session emits. Values match the strings in prompts.py.

    Values are byte-identical to the wire strings in `sessions.jsonl`: the
    stats report re-renders historical rows, so a value here is load-bearing
    history. `REJECT` (not `REJECTED`) is the value the data actually contains;
    `KICKOUT` is compared in `stage_feasibility`, `UNKNOWN`/`ERROR` are
    produced by `session.py`.

    `NO_VERDICT` is a new wire value: it appears in stats rows from T20 onward
    and in none of the 56 historical ones, so nothing re-renders differently.
    T20's `_map_verdict` returns it for "the process finished and said nothing
    decidable".
    """
    PASS = "pass"
    FAIL = "fail"
    KICKBACK = "kickback"
    DONE = "done"
    PROGRESS = "progress"
    RESLICED = "resliced"
    INFEASIBLE = "infeasible"
    REJECT = "reject"
    KICKOUT = "kickout"
    UNKNOWN = "unknown"
    ERROR = "error"
    NO_VERDICT = "no_verdict"

    @classmethod
    def parse(cls, raw: str) -> "Verdict | None":
        """Return the member whose value equals `raw`, else None."""
        for member in cls:
            if member.value == raw:
                return member
        return None


class CheckpointStage(str, Enum):
    """The checkpointable stages, in pipeline order.

    `holistic` is deliberately absent: it is terminal (success -> done/, failure
    -> parked/), so it is never recorded in `checkpointed_stages`.

    `merge` is the marker for "the squash-merge onto trunk succeeded". It lives
    outside `holistic` on purpose: the merge and the completion move are two
    separate steps, and a crash between them must not re-run the merge (F8).
    """
    SPEC = "spec"
    FEASIBILITY = "feasibility"
    SLICING = "slicing"
    SLICES = "slices"
    MERGE = "merge"


CHECKPOINT_ORDER: tuple[CheckpointStage, ...] = (
    CheckpointStage.SPEC,
    CheckpointStage.FEASIBILITY,
    CheckpointStage.SLICING,
    CheckpointStage.SLICES,
    CheckpointStage.MERGE,
)


class ReviewKind(str, Enum):
    """Which review a `_review_loop` pass is running.

    `kind` survives in `_review_loop` (prompt selection, iteration cap, model
    choice, log text), so it is an enum member rather than a bare string. It
    never reaches the wire: the stats `stage` column is a `Stage` value, and
    the review's own prompt text is built by `prompts.py`.
    """
    TECH = "tech"
    FUNC = "func"


class Stage(str, Enum):
    """Pipeline stage names, used in stats and logs.

    Values are byte-identical to the stage strings emitted by `pipeline.py`
    and `autonomous.py` and to the historical rows in `sessions.jsonl`.
    `IMPLEMENT`/`SLICE_FIT`/`HOLISTIC_REVIEW` are gone: they appear in neither
    the data nor the code.

    This enum is deliberately a subset of the data: the `smoke` and `smoke32k`
    rows are ad-hoc manual runs, not a stage any code path can produce, so
    they get no member.
    """
    SPEC_AUTHOR = "spec_author"
    SPEC_ASSESS_TW = "spec_assess_tw"
    SPEC_ASSESS_ORNITH = "spec_assess_ornith"
    FEASIBILITY = "feasibility"
    SLICING = "slicing"
    SLICE_CHECK = "slice_check"
    SLICE_IMPLEMENT = "slice_implement"
    TECH_REVIEW = "tech_review"
    FUNC_REVIEW = "func_review"
    SLICE_FIX = "slice_fix"
    HOLISTIC = "holistic"
    AUTONOMOUS_SUGGEST = "autonomous_suggest"
    AUTONOMOUS_REVIEW = "autonomous_review"

    @classmethod
    def parse(cls, raw: str) -> "Stage | None":
        """Return the member whose value equals `raw`, else None."""
        for member in cls:
            if member.value == raw:
                return member
        return None