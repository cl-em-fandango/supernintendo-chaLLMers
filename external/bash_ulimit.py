"""Wrap a shell command string so it runs under nproc/vmem `ulimit`s (FR-4.3).

One responsibility: turn a bare shell command into a `bash -c` argv whose first
act is lowering the shell's own resource limits, so every child the command
spawns inherits them. The limits are soft limits — lowering a soft limit never
fails, and a hard limit below the requested value only warns on stderr without
aborting the command (bash continues past a failed builtin unless `set -e`).

Enforcement (running the wrapped argv under timeout/group-kill/output caps)
lives in `external/hardened_process.py`, which imports these defaults.
"""
from __future__ import annotations

# Max processes per shell (FR-4.3 default; config key `toolUlimitNproc`).
DEFAULT_ULIMIT_NPROC = 50

# Max virtual memory per shell in KiB, ~8 GiB (FR-4.3 default; config key
# `toolUlimitVmemKB`).
DEFAULT_ULIMIT_VMEM_KB = 8_388_608


def ulimit_prelude(nproc: int, vmem_kb: int) -> str:
    """The `ulimit` line executed before the real command."""
    return f"ulimit -u {int(nproc)} -v {int(vmem_kb)}"


def wrap_bash_command(
    command: str,
    *,
    nproc: int = DEFAULT_ULIMIT_NPROC,
    vmem_kb: int = DEFAULT_ULIMIT_VMEM_KB,
) -> list[str]:
    """Return a `bash -c` argv running `command` under the given ulimits.

    The prelude and the command are separate lines of one script so a trailing
    comment or an unterminated construct in `command` cannot swallow the
    `ulimit` line.
    """
    script = f"{ulimit_prelude(nproc, vmem_kb)}\n{command}"
    return ["bash", "-c", script]
