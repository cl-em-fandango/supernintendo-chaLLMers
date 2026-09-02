"""The GitHub label vocabulary the queue sync owns. See CODING_STANDARDS.md §3.

Two fixed, non-configurable label families (spec §4):

* trigger labels — an instruction from GitHub to the *inbound* sync
  (ingest / halt / delete);
* state labels — the mirror of a task's queue location, owned by the
  *outbound* sync, exactly one per issue.

The `snes-` prefix marks a label as ours: a label without the prefix and
not equal to the bare `snes` is never added or removed by the sync. The
strings live here as Enum values and only ever travel to/from the GitHub
API as `.value` (strings at the edge, enums inside).

`snes-parked` is deliberately both a trigger (`TriggerLabel.PARK`) and the
parked state label (`StateLabel.PARKED`): applying it as state is harmless
because the inbound park is idempotent (spec FR-2.4).
"""
from __future__ import annotations

from enum import Enum

# Marks a label as owned by the sync. Labels outside this family belong
# to humans and are never touched (spec §4).
HARNESS_LABEL_PREFIX = "snes-"


class TriggerLabel(str, Enum):
    """What an issue's labels instruct the inbound sync to do (spec FR-1).

    `INGEST` (the bare `snes`) doubles as a subscription marker: outbound
    sync never removes it (spec FR-2.4). `DEMO` (`snes-demo`, demo spec
    FR-1) is a second subscription marker with the same rule: it ingests
    with the demo flag, and outbound sync never removes it either.
    """
    INGEST = "snes"
    DEMO = "snes-demo"
    PARK = "snes-parked"
    DELETE = "snes-deleted"


# Exactly one action per issue when several triggers are present
# (demo spec FR-1.5): delete > park > demo > ingest.
TRIGGER_PRECEDENCE: tuple[TriggerLabel, ...] = (
    TriggerLabel.DELETE,
    TriggerLabel.PARK,
    TriggerLabel.DEMO,
    TriggerLabel.INGEST,
)


class StateLabel(str, Enum):
    """One issue label mirroring the task's queue location (spec FR-2.4).

    Values are `snes-<location>` for the seven synced locations.
    """
    PENDING = "snes-pending"
    CLAIMED = "snes-claimed"
    ACTIVE = "snes-active"
    REVIEW = "snes-review"
    PARKED = "snes-parked"
    FAILED = "snes-failed"
    DONE = "snes-done"


def trigger_for(name: str) -> TriggerLabel | None:
    """The trigger label `name` spells, else None."""
    for member in TRIGGER_PRECEDENCE:
        if member.value == name:
            return member
    return None


def state_for(name: str) -> StateLabel | None:
    """The state label `name` spells, else None."""
    for member in StateLabel:
        if member.value == name:
            return member
    return None


def is_harness_label(name: str) -> bool:
    """True when the sync owns `name` (our prefix, or the bare `snes`).

    Everything else is a human label: never added, never removed.
    """
    return name.startswith(HARNESS_LABEL_PREFIX) or name == TriggerLabel.INGEST.value


def is_state_label(name: str) -> bool:
    """True when `name` is one of the `snes-<state>` labels outbound owns.

    Narrower than `is_harness_label`: the bare `snes` and any future
    non-state `snes-*` label are excluded, so a label cleanup can remove
    stale state labels without ever touching the subscription marker.
    """
    return state_for(name) is not None
