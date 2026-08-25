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