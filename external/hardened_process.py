"""Run harness-launched subprocesses under guardrails (FR-4).

Every process the harness starts shares the same mechanics:

- launched in its own session (`start_new_session=True`) so the whole process
  tree is one `killpg` away;
- a hard timeout: SIGTERM the process group first, `terminate_grace_s` of
  grace, then SIGKILL the group, then reap — the same stop order as
  `external/pi_cli._terminate_reap`, so a child always gets the chance to
  close its own streams;
- stdout/stderr read with a hard per-stream byte cap: data beyond the cap is
  discarded (never buffered) and the result flags the truncation, so a spewing
  child cannot exhaust the harness's memory.

`run()` is the complete guardrail set for fire-and-forget commands (shell
helpers, cleanup, `git clean`); `spawn()` and `terminate_process_group()` are
the same primitives for callers that stream the child's stdout themselves
(`external/pi_cli.py`) and therefore cannot use `run()`'s buffered capture.
`run_bash()` adds the FR-4.3 `ulimit` wrapper from `external/bash_ulimit.py`.

A timed-out run is *reported*, never swallowed: `CommandResult.timed_out` is
set and `rc` carries the termination, so the caller decides policy.
"""
from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from external.bash_ulimit import (
    DEFAULT_ULIMIT_NPROC,
    DEFAULT_ULIMIT_VMEM_KB,
    wrap_bash_command,
)

# Hard timeout for harness-run shell helpers (config key `toolTimeout`).
DEFAULT_TOOL_TIMEOUT_S = 60.0

# Per-stream capture cap in bytes (config key `maxOutputBytes`).
DEFAULT_MAX_OUTPUT_BYTES = 2 * 1024 * 1024

# SIGTERM-then-SIGKILL grace for the group stop (matches pi_cli).
DEFAULT_TERMINATE_GRACE_S = 5.0

# Read block size for the capped drainers.
_DRAIN_CHUNK_CHARS = 65536


@dataclass
class GuardrailLimits:
    """The guardrail knobs for one launch, from `config.json` via `Config`.

    `timeout_s` is the hard wall-clock cap for the command; the ulimit fields
    apply only on the `run_bash()` path, where the shell command is wrapped.
    """
    timeout_s: float = DEFAULT_TOOL_TIMEOUT_S
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES
    terminate_grace_s: float = DEFAULT_TERMINATE_GRACE_S
    ulimit_nproc: int = DEFAULT_ULIMIT_NPROC
    ulimit_vmem_kb: int = DEFAULT_ULIMIT_VMEM_KB


@dataclass
class CommandResult:
    """Outcome of one hardened run.

    `stdout`/`stderr` hold at most `max_output_bytes` characters each; the
    matching `*_truncated` flag says whether bytes beyond the cap were
    discarded. `timed_out` distinguishes a guardrail kill from the child's own
    exit.
    """
    rc: int
    stdout: str
    stderr: str
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    timed_out: bool = False


def spawn(
    cmd: list[str],
    *,
    cwd: str | Path | None = None,
    text: bool = True,
) -> subprocess.Popen:
    """Start `cmd` in its own session with piped stdout/stderr.

    `start_new_session=True` makes the child a group leader, which is what
    lets `terminate_process_group()` take the whole tree — including
    grandchildren — down with one signal.
    """
    return subprocess.Popen(
        cmd,
        cwd=str(cwd) if cwd is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=text,
        start_new_session=True,
    )


def _signal_group(proc: subprocess.Popen, sig: signal.Signals) -> None:
    """Signal the child's process group, or the child alone if it is not the
    group leader (a non-leader shares our group — killing it would kill us)."""
    try:
        if os.getpgid(proc.pid) == proc.pid:
            os.killpg(proc.pid, sig)
            return
    except (ProcessLookupError, PermissionError, OSError):
        pass
    try:
        os.kill(proc.pid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def terminate_process_group(
    proc: subprocess.Popen,
    *,
    grace_s: float = DEFAULT_TERMINATE_GRACE_S,
) -> None:
    """Stop a running child's whole group and reap it.

    SIGTERM the group first so the child can close its own streams; SIGKILL
    the group only after `grace_s` for a child that ignores SIGTERM. Reaping
    here is what makes a later `wait()` return at once. A child that already
    exited is not an error.

    The group is only signalled when the child *leads* it (i.e. it was started
    by `spawn()` with `start_new_session=True`). A child that shares our
    process group would take this whole process down with it, so for those
    the signal goes to the child alone.
    """
    if proc.poll() is not None:
        try:
            proc.wait()
        except (ChildProcessError, OSError):
            pass
        return
    _signal_group(proc, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while proc.poll() is None and time.monotonic() < deadline:
        time.sleep(0.05)
    if proc.poll() is None:
        _signal_group(proc, signal.SIGKILL)
    proc.wait()


def _drain_capped(stream, cap: int) -> tuple[str, bool]:
    """Read `stream` to EOF, keeping at most `cap` characters.

    Bytes beyond the cap are read and discarded — stopping the read would
    block the child on a full pipe buffer, which is the deadlock the stderr
    drainer in `pi_cli` exists to prevent. Returns the kept text and whether
    anything was discarded. Only ever called on the drainer's own local
    objects, so no lock is needed.
    """
    chunks: list[str] = []
    kept = 0
    truncated = False
    while True:
        chunk = stream.read(_DRAIN_CHUNK_CHARS)
        if not chunk:
            break
        keep = chunk[:max(0, cap - kept)]
        if len(keep) < len(chunk):
            truncated = True
        if keep:
            chunks.append(keep)
            kept += len(keep)
    try:
        stream.close()
    except OSError:
        pass
    return "".join(chunks), truncated


def run(
    cmd: list[str],
    *,
    cwd: str | Path | None = None,
    limits: GuardrailLimits | None = None,
) -> CommandResult:
    """Run `cmd` under the full guardrail set and return the capped capture.

    Both streams are drained concurrently from the moment the pipes exist, so
    a child that floods stderr (or stdout) never stalls on the OS pipe buffer
    while we wait. On timeout the group is stopped with the shared
    SIGTERM-grace-SIGKILL path and the result is flagged `timed_out`.
    """
    limits = limits or GuardrailLimits()
    proc = spawn(cmd, cwd=cwd)

    boxes: dict[str, tuple[str, bool]] = {}

    def drain(name: str, stream) -> None:
        boxes[name] = _drain_capped(stream, limits.max_output_bytes)

    readers = [
        threading.Thread(target=drain, args=(name, stream), daemon=True)
        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr))
    ]
    for t in readers:
        t.start()

    timed_out = False
    try:
        proc.wait(timeout=limits.timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        terminate_process_group(proc, grace_s=limits.terminate_grace_s)
    finally:
        # The child is reaped by now, so both pipes are at EOF and the joins
        # return promptly; the join timeout is only the escape hatch for a
        # double-forked grandchild that escaped the group and still holds a
        # pipe open.
        for t in readers:
            t.join(timeout=limits.terminate_grace_s + 2.0)

    stdout, stdout_truncated = boxes.get("stdout", ("", False))
    stderr, stderr_truncated = boxes.get("stderr", ("", False))
    return CommandResult(
        rc=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout,
        stderr=stderr,
        stdout_truncated=stdout_truncated,
        stderr_truncated=stderr_truncated,
        timed_out=timed_out,
    )


def run_bash(
    command: str,
    *,
    cwd: str | Path | None = None,
    limits: GuardrailLimits | None = None,
) -> CommandResult:
    """Run a shell string under the guardrails *and* the configured ulimits.

    The command executes inside `bash -c 'ulimit -u N -v M; <command>'` (see
    `external/bash_ulimit.py`), so the shell and everything it spawns carry
    the nproc/vmem limits from `GuardrailLimits`.
    """
    limits = limits or GuardrailLimits()
    wrapped = wrap_bash_command(
        command,
        nproc=limits.ulimit_nproc,
        vmem_kb=limits.ulimit_vmem_kb,
    )
    return run(wrapped, cwd=cwd, limits=limits)
