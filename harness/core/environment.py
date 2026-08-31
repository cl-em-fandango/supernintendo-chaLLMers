"""Container detection and the bare-metal execution gate (FR-1).

One responsibility: answer "is this process running inside a container?"
and enforce the gate that refuses bare-metal harness runs. No subprocesses
are spawned; an unreadable `/proc` simply counts as "no markers present"
(FR-1.4). The `root` / `proc_root` / `env` parameters exist so unit tests
can inject fake filesystems and environments; production callers use the
defaults.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Mapping, Optional

# Escape hatch for headless unit tests only (FR-1.3). Only the exact
# value "1" counts as set.
ESCAPE_ENV_VAR = "HARNESS_ALLOW_HOST_UNSAFE"

# The runner the refusal message points users at (FR-1.2).
RUNNER_SCRIPT = "scripts/harness-run"

# Cgroup lines attributable to a container runtime.
_CGROUP_MARKER = re.compile(r"docker|kubepods|containerd|libpod")
# Best-effort fallback: a 64-hex container id inside a non-root cgroup path.
_CONTAINER_ID = re.compile(r"[0-9a-f]{64}")

# Module flag backing the "warning printed once" rule (FR-1.3).
_host_unsafe_warning_printed = False


def reset_host_unsafe_warning() -> None:
    """Clear the printed-once flag (test support only)."""
    global _host_unsafe_warning_printed
    _host_unsafe_warning_printed = False


def _cgroup_says_container(proc_root: Path) -> bool:
    """True if /proc/1/cgroup carries container-runtime evidence.

    Unreadable or missing `/proc` means "not containerized" and never
    raises (FR-1.4).
    """
    try:
        text = (proc_root / "1" / "cgroup").read_text(
            encoding="utf-8", errors="replace")
    except OSError:
        return False
    for line in text.splitlines():
        if _CGROUP_MARKER.search(line):
            return True
        # cgroup v2 lines look like "0::<path>"; a non-root path carrying
        # a full container id is attributable to a container runtime
        # (best effort — the marker check above takes precedence).
        path = line.split("::", 1)[-1]
        if path and path != "/" and _CONTAINER_ID.search(path):
            return True
    return False


def is_containerized(root: Path = Path("/"),
                     proc_root: Path = Path("/proc"),
                     env: Optional[Mapping[str, str]] = None) -> bool:
    """True if any container marker is present (FR-1.1).

    Markers, in order: `/.dockerenv`, `/run/.containerenv`, a non-empty
    `container` environment variable (podman and docker both set it), and
    container-runtime evidence in `/proc/1/cgroup`.
    """
    if env is None:
        env = os.environ
    if (root / ".dockerenv").exists():
        return True
    if (root / "run" / ".containerenv").exists():
        return True
    if env.get("container"):
        return True
    return _cgroup_says_container(proc_root)


def _warn_host_unsafe(entrypoint_name: str) -> None:
    global _host_unsafe_warning_printed
    if not _host_unsafe_warning_printed:
        _host_unsafe_warning_printed = True
        print(f"WARNING: {ESCAPE_ENV_VAR}=1 — {entrypoint_name} is running on "
              f"the bare host without container resource limits.",
              file=sys.stderr)


def assert_containerized(entrypoint_name: str,
                         root: Path = Path("/"),
                         proc_root: Path = Path("/proc"),
                         env: Optional[Mapping[str, str]] = None) -> None:
    """Refuse to run on bare metal (FR-1.2/FR-1.3).

    Exits 1 with instructions naming `scripts/harness-run` and the escape
    hatch unless the process is containerized or
    `HARNESS_ALLOW_HOST_UNSAFE` is exactly "1" (then a warning is printed
    once per process).
    """
    if env is None:
        env = os.environ
    if is_containerized(root=root, proc_root=proc_root, env=env):
        return
    if env.get(ESCAPE_ENV_VAR) == "1":
        _warn_host_unsafe(entrypoint_name)
        return
    print(
        f"{entrypoint_name} refuses to run outside a resource-bounded "
        f"container.\n"
        f"Run it through {RUNNER_SCRIPT}, or set {ESCAPE_ENV_VAR}=1 to allow "
        f"this bare-metal run (headless unit tests only).",
        file=sys.stderr,
    )
    sys.exit(1)
