"""The session-boundary stand-down check: one question, asked anywhere.

A run loop reaches a safe boundary (spec FR-6.1: the moment immediately
before spawning a new `pi` session) and needs one answer: stop taking work,
or continue. Asking it is a read of the interrupt state file plus, when a
request is pending, the `requested -> paused` acknowledgement — both owned by
`interrupt.py`. This module owns only the *decision* and its two log lines,
so every boundary in the harness (the CLI run loops, the pipeline's dispatch
gate, the autonomous attempt loop) answers the same question the same way.

The check is a callable object, so it can be handed to a workflow module as
`stand_down_check` without that module knowing about config, work dirs or the
state file.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .interrupt import acknowledge_interrupt

# Logged when a boundary answers "stop": the operator's contract for how a
# child reports its stand-down (spec FR-6.2).
STOOD_DOWN_LOG = "stood down at session boundary"

StandDownCheck = Callable[[], bool]


@dataclass(frozen=True)
class StandDownWatcher:
    """Asks the interrupt file whether the caller must stand down.

    `work_dir` is the directory holding `state/interrupt.json`; None means
    the wiring has no state file to read, so there is no interrupt and every
    boundary answers "continue". A pending request is acknowledged
    (`requested -> paused`) as part of answering "stop", which is what makes
    the stand-down visible to the supervisor.
    """
    work_dir: Optional[Path]
    log: Callable[[str], None] = print

    def __call__(self) -> bool:
        """True when an interrupt is active: stop taking new work now.

        Called where the caller would otherwise take work. A True answer
        means the request is already acknowledged, so the caller only has to
        unwind: no parking, no crash-retry, exit 0 (FR-6.2/FR-6.4/FR-6.5).
        """
        if self.work_dir is None:
            return False
        if acknowledge_interrupt(self.work_dir, log=self.log) is None:
            return False
        self.log(STOOD_DOWN_LOG)
        return True
