"""Git operations for the harness.

This module contains all subprocess calls to git (CODING_STANDARDS §4).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

LAST_GOOD_TAG = "pi/last-good"


def _git(cwd: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
    return proc.stdout


def has_branch(cwd: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{ref}"],
        cwd=cwd).returncode == 0


def has_tag(cwd: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/tags/{ref}"],
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
    if not has_branch(workdir, trunk):
        _git(workdir, "branch", trunk)
    if has_branch(workdir, branch):
        _git(workdir, "checkout", branch)
    else:
        _git(workdir, "checkout", "-b", branch, trunk)
    return branch


def merge_to_trunk(workdir: Path, task_id: str, trunk: str, title: str) -> None:
    """Squash-merge a feature branch to trunk, then verify the result.

    If the verification gate fails, trunk is reset to the last known-good tag
    and the feature branch is left intact (for inspection) and a RuntimeError
    is raised so the pipeline parks the task.
    """
    workdir = Path(workdir)
    branch = f"pi/{task_id}"
    _git(workdir, "checkout", trunk)
    _git(workdir, "merge", "--squash", branch)
    _git(workdir, "-c", "user.email=pi@harness.local",
         "-c", "user.name=pi-harness",
         "commit", "-m", f"feat({task_id}): {title}\n\nSquash-merged from {branch}.")

    # --- verification gate ---
    ok, detail = verify_harness(workdir)
    if not ok:
        # revert trunk to last known-good; keep the feature branch for inspection
        _revert_to_last_good(workdir, trunk)
        raise RuntimeError(
            f"verification gate FAILED for {task_id}: {detail}. "
            f"trunk reverted to {LAST_GOOD_TAG}; feature branch {branch} kept.")

    # gate passed: advance the last-good tag and drop the feature branch
    _git(workdir, "tag", "-f", LAST_GOOD_TAG, trunk)
    _git(workdir, "branch", "-d", branch, check=False)


def verify_harness(workdir: Path) -> tuple[bool, str]:
    """Smoke-test the harness at workdir: imports cleanly and the CLI runs.

    Returns (ok, detail). These are the two things that, if broken, wedge the
    supervisor: a failed import, or a CLI that won't execute.
    """
    workdir = Path(workdir)
    # 1. the package must import (current module layout after the workflow/ + core/ split)
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, '.'); "
         "import harness, harness.workflow.pipeline, harness.workflow.autonomous, "
         "harness.core.session, harness.core.providers, harness.core.gitops, harness.core.stats"],
        cwd=workdir, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        return False, f"import failed: {r.stderr.strip()[-300:]}"
    # 2. the CLI must actually run (status touches config + stats + queue)
    r = subprocess.run(
        [sys.executable, "harness.py", "status"],
        cwd=workdir, capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        return False, f"harness.py status failed rc={r.returncode}: {r.stderr.strip()[-300:]}"
    return True, "ok"


def _revert_to_last_good(workdir: Path, trunk: str) -> str:
    """Reset trunk to the last known-good tag. If no tag exists yet, reset to
    trunk's parent (undo the single bad merge commit). Returns the target reverted to."""
    workdir = Path(workdir)
    if has_tag(workdir, LAST_GOOD_TAG):
        _git(workdir, "reset", "--hard", LAST_GOOD_TAG)
        return f"tag:{LAST_GOOD_TAG}"
    else:
        # no last-good tag: undo the merge commit we just made
        _git(workdir, "reset", "--hard", "HEAD~1")
        return "HEAD~1"