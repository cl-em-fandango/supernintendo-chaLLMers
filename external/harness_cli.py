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

import datetime
import os
import subprocess
import sys
from pathlib import Path

# The repo root holds `harness.py`, the composition-root entry point.
_HARNESS_ENTRY = Path(__file__).resolve().parent.parent / "harness.py"

# The `pi` binary lives in /usr/local/bin, which a cron-started daemon's
# minimal PATH (`/usr/bin:/bin:/usr/sbin:/sbin`) does not carry. The spawned
# child inherits that PATH, so without this the child dies in
# `validate_models()` with `FileNotFoundError: 'pi'` — stderr is DEVNULL,
# so the only trace is a defunct child and a fresh spawn every pass.
_STANDARD_BIN_DIR = "/usr/local/bin"


def child_env(env: dict[str, str] | None = None) -> dict[str, str]:
    """The spawn environment: `env` (default `os.environ`) with
    `/usr/local/bin` guaranteed on PATH. Idempotent — an environment that
    already carries it is returned unchanged (as a copy)."""
    base = dict(os.environ if env is None else env)
    parts = [p for p in base.get("PATH", "").split(os.pathsep) if p]
    if _STANDARD_BIN_DIR not in parts:
        base["PATH"] = os.pathsep.join([_STANDARD_BIN_DIR, *parts])
    return base


def spawn_harness_run_task_loop(spawn_log: Path | None = None) -> int:
    """Start `harness.py run-task-loop` detached; return the child's PID.

    Stdin and stdout go to DEVNULL: the child writes its structured log to
    the harness log sink itself, and a daemon has no terminal to hand.
    `start_new_session` detaches it from the daemon's process group
    (FR-4.6). The environment is the daemon's with `/usr/local/bin` added
    to PATH (`child_env`), so a cron-minimal daemon environment still
    reaches the `pi` binary.

    `spawn_log`: when given, the child's stderr is appended there instead
    of DEVNULL, so a crash before (or outside) the child's own log sink —
    a traceback, an import failure — leaves a trace instead of an
    invisible defunct child. A timestamped header line is written first so
    entries correlate with the daemon's spawn log line. The dedicated file
    (not `harness.log`) keeps clear of the log sink's rotation, which
    would strand an append fd on the rotated generation. An unwritable
    log path degrades to DEVNULL rather than losing the spawn.
    """
    stderr: object = subprocess.DEVNULL
    header = (f"\n=== spawned {datetime.datetime.now().isoformat(timespec='seconds')} "
              f"(parent pid {os.getpid()}) ===\n")
    handle = None
    if spawn_log is not None:
        try:
            spawn_log.parent.mkdir(parents=True, exist_ok=True)
            handle = spawn_log.open("a", encoding="utf-8")
            handle.write(header)
            handle.flush()
            stderr = handle
        except OSError:
            if handle is not None:
                handle.close()
            handle = None
            stderr = subprocess.DEVNULL
    try:
        child = subprocess.Popen(
            [sys.executable, str(_HARNESS_ENTRY), "run-task-loop"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=stderr,
            start_new_session=True,
            env=child_env(),
        )
    finally:
        # Popen duplicated the fd into the child; the parent's copy must
        # not stay open or the file grows a leak per spawn.
        if handle is not None:
            handle.close()
    return child.pid
