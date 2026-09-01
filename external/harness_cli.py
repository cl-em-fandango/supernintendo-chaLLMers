"""Spawning a detached harness run — the subprocess boundary for `syncd`.

The daemon must not work the queue itself (spec FR-4.4); it starts a harness
run through the same entry point an operator runs by hand
(`harness.py run-task-loop`) and lets that process own the pipeline. The
child is started in its own session so a signal to the daemon (SIGINT/SIGTERM,
FR-4.6) finishes the daemon's current pass without killing the run it just
started.

This is the only place outside `external/pi_cli.py` / `external/git_cli.py`
that calls `subprocess` (CODING_STANDARDS §4); everything above it takes the
spawn as an injected callable.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# The repo root holds `harness.py`, the composition-root entry point.
_HARNESS_ENTRY = Path(__file__).resolve().parent.parent / "harness.py"


def spawn_harness_run_task_loop() -> int:
    """Start `harness.py run-task-loop` detached; return the child's PID.

    Stdio goes to DEVNULL: the child writes to the harness log sink itself,
    and a daemon has no terminal to hand. `start_new_session` detaches it
    from the daemon's process group (FR-4.6).
    """
    child = subprocess.Popen(
        [sys.executable, str(_HARNESS_ENTRY), "run-task-loop"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return child.pid
