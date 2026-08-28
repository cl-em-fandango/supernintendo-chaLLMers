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
    """The VERDICT a session emits. Values match the strings in prompts.py."""
    PASS = "pass"
    FAIL = "fail"
    KICKBACK = "kickback"
    DONE = "done"
    PROGRESS = "progress"
    RESLICED = "resliced"
    INFEASIBLE = "infeasible"
    REJECTED = "rejected"


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


class Stage(str, Enum):
    """Pipeline stage names, used in stats and logs."""
    SPEC = "spec_author"
    FEASIBILITY = "feasibility"
    SLICING = "slicing"
    SLICE_FIT = "slice_fit"
    IMPLEMENT = "implement"
    TECH_REVIEW = "tech_review"
    FUNC_REVIEW = "func_review"
    FIX_TECH = "fix_tech"
    FIX_FUNC = "fix_func"
    HOLISTIC = "holistic_review"
    AUTONOMOUS_SUGGEST = "autonomous_suggest"
    AUTONOMOUS_REVIEW = "autonomous_review"