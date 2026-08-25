"""Git helpers for the harness: feature branches + squash merge to trunk."""
from __future__ import annotations

import subprocess
from pathlib import Path


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def _has(cwd: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{ref}"],
        cwd=cwd).returncode == 0


def ensure_branch(workdir: Path, task_id: str, trunk: str) -> str:
    """Ensure repo exists, trunk exists, and we're on the task's feature branch."""
    workdir = Path(workdir)
    branch = f"pi/{task_id}"
    if not (workdir / ".git").exists():
        _git(workdir, "init", "-b", trunk)
        _git(workdir, "add", "-A")
        _git(workdir, "-c", "user.email=pi@harness.local",
             "-c", "user.name=pi-harness", "commit", "-m", "harness: initial commit",
             check=False)
    if not _has(workdir, trunk):
        _git(workdir, "branch", trunk)
    if _has(workdir, branch):
        _git(workdir, "checkout", branch)
    else:
        _git(workdir, "checkout", "-b", branch, trunk)
    return branch


def merge_to_trunk(workdir: Path, task_id: str, trunk: str, title: str) -> None:
    workdir = Path(workdir)
    branch = f"pi/{task_id}"
    _git(workdir, "checkout", trunk)
    _git(workdir, "merge", "--squash", branch)
    _git(workdir, "-c", "user.email=pi@harness.local",
         "-c", "user.name=pi-harness",
         "commit", "-m", f"feat({task_id}): {title}\n\nSquash-merged from {branch}.")
    _git(workdir, "branch", "-d", branch, check=False)
