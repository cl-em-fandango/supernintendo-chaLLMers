"""The npm boundary: the only place the harness spawns npm (spec §6).

Demo app builds run `npm install` / `npm run build` in the app directory;
everything else in the codebase goes through the two functions here. The
arguments are argv fragments (no shell, no interpolation), so ticket- or
model-derived strings can never become commands — the command is fixed by
the stack plan and only boring, fixed fragments are appended.
"""
from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

# A build is a network-and-disk job; a hung npm must not hold the deploy
# lock forever. Generous, but bounded.
NPM_TIMEOUT_S = 1800.0


@dataclass(frozen=True)
class NpmResult:
    """The outcome of one npm invocation: exit code and captured streams."""

    rc: int
    stdout: str
    stderr: str


def npm_available() -> bool:
    """Whether an `npm` executable is reachable on PATH."""
    return shutil.which("npm") is not None


def run_npm(args: tuple[str, ...], cwd: Path,
            timeout_s: float = NPM_TIMEOUT_S) -> NpmResult:
    """Run one `npm <args>` invocation in `cwd` and return its outcome.

    `args` are npm's own argv fragments (e.g. `("run", "build")`); the
    program is always `npm` and no shell is involved. A timeout is
    reported as rc 124 with a note on stderr rather than raising, so the
    caller's failure handling stays a single rc check.
    """
    command = ["npm", *args]
    try:
        proc = subprocess.run(command, cwd=str(cwd), capture_output=True,
                              text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return NpmResult(rc=124, stdout="",
                         stderr=f"npm {' '.join(args)} timed out "
                                f"after {timeout_s}s")
    return NpmResult(rc=proc.returncode, stdout=proc.stdout or "",
                     stderr=proc.stderr or "")
