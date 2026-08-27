"""Git operations for the harness.

This module contains all subprocess calls to git (CODING_STANDARDS §4).
"""
from __future__ import annotations

import os
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
    """True when `refs/heads/<ref>` exists. Branches only — never a tag."""
    return subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{ref}"],
        cwd=cwd).returncode == 0


def has_tag(cwd: Path, ref: str) -> bool:
    """True when `refs/tags/<ref>` exists (F6a: `pi/last-good` is a tag, so the
    one probe that used to check both namespaces under `refs/heads/` never saw it)."""
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


def dirty_paths(workdir: Path) -> list[str]:
    """Every path git considers uncommitted: staged, unstaged and untracked."""
    out = subprocess.run(["git", "status", "--porcelain"], cwd=Path(workdir),
                         capture_output=True, text=True).stdout
    paths: list[str] = []
    for line in out.splitlines():
        if len(line) < 4:
            continue
        p = line[3:]
        if " -> " in p:                 # rename/copy: report the destination
            p = p.split(" -> ", 1)[1]
        paths.append(p.strip('"'))
    return paths


def _require_clean(workdir: Path, what: str) -> None:
    """Refuse a destructive git operation unless the worktree is clean.

    Every destructive call in this module (hard reset, cross-branch checkout,
    merge cleanup) sits behind this guard, so a harness failure can never
    silently erase uncommitted human work. The only way past it is an explicit
    ``allow_dirty=True``; nothing in the repo passes one — that is the human
    recovery path.
    """
    workdir = Path(workdir)
    paths = dirty_paths(workdir)
    if paths:
        raise RuntimeError(
            f"refusing {what}: {len(paths)} uncommitted paths, e.g. {paths[:5]}. "
            f"Inspect with: git -C {workdir} status"
        )


def _gitdir(workdir: Path) -> Path | None:
    gd = _git(workdir, "rev-parse", "--git-dir", check=False).strip()
    if not gd:
        return None
    return Path(gd) if os.path.isabs(gd) else Path(workdir) / gd


def merge_in_progress(workdir: Path) -> bool:
    """True when the repo holds merge residue.

    `MERGE_HEAD` alone is not enough: `git merge --squash` never writes it, so a
    wrecked squash would otherwise be reported as clean. Unmerged index entries
    (`git ls-files -u`) are checked too.
    """
    gitdir = _gitdir(Path(workdir))
    if gitdir is not None and (gitdir / "MERGE_HEAD").exists():
        return True
    return bool(_git(workdir, "ls-files", "-u", check=False).strip())


def _added_paths(workdir: Path, trunk: str, branch: str) -> list[str]:
    """Paths the branch adds relative to trunk — recorded *before* the merge."""
    out = _git(workdir, "diff", "--name-only", "--diff-filter=A",
               f"{trunk}...{branch}", check=False)
    return [line.strip() for line in out.splitlines() if line.strip()]


def _discard_added(workdir: Path, added: list[str]) -> None:
    """Delete only the recorded branch-added paths that are now untracked.

    Never a blanket `git status --porcelain` `??` sweep: a concurrent tool may
    have created an unrelated file after the cleanliness check. Paths that
    resolve outside the worktree, that git tracks, or that are not plain files
    are left alone; symlinks are unlinked, never followed. Directories emptied
    by the deletion are pruned.
    """
    workdir = Path(workdir)
    root = os.path.abspath(str(workdir))
    tracked = set(_git(workdir, "ls-files", check=False).splitlines())
    for rel in added:
        if not rel or os.path.isabs(rel):
            continue
        target = workdir / rel
        # containment is checked lexically: resolving symlinks here would let a
        # link that points out of the worktree hide an escape, or hide the fact
        # that we are only ever removing the link itself
        lex = os.path.normpath(os.path.abspath(str(target)))
        if not (lex == root or lex.startswith(root + os.sep)):
            continue                       # escapes the worktree
        if rel in tracked:
            continue                       # exists on trunk: not ours to delete
        try:
            # os.unlink on a symlink removes the link, never its target
            if os.path.islink(target) or target.is_file():
                os.unlink(target)
            else:
                continue
        except OSError:
            continue
        parent = target.parent
        while parent != workdir and parent.is_dir():
            try:
                next(parent.iterdir())
                break                      # not empty, stop pruning
            except StopIteration:
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent


def abort_merge(workdir: Path, added: list[str] | None = None) -> None:
    """Clear the residue of a failed merge: index, worktree, branch-added files.

    Handles both merge shapes, in order, all `check=False`:
      * a plain merge writes `.git/MERGE_HEAD` -> `git merge --abort`;
      * a squash writes no `MERGE_HEAD` but leaves conflict stages behind, so
        *always* `git reset -q` (mixed reset clears the unmerged index entries)
        followed by `git checkout -q -- .` to restore the worktree.

    PRECONDITION: the caller proved the worktree clean with `_require_clean`
    before the merge started (see `merge_to_trunk`). That proof is the only
    reason the worktree `checkout` cannot destroy uncommitted human work — this
    cleanup must never run on a tree nobody vouched for.
    """
    workdir = Path(workdir)
    gitdir = _gitdir(workdir)
    if gitdir is not None and (gitdir / "MERGE_HEAD").exists():
        _git(workdir, "merge", "--abort", check=False)
    _git(workdir, "reset", "-q", check=False)
    _git(workdir, "checkout", "-q", "--", ".", check=False)
    if added:
        _discard_added(workdir, added)


def merge_to_trunk(workdir: Path, task_id: str, trunk: str, title: str,
                   allow_dirty: bool = False) -> None:
    """Squash-merge a feature branch to trunk, then verify the result.

    If the verification gate fails, trunk is reset to the last known-good tag
    and the feature branch is left intact (for inspection) and a RuntimeError
    is raised so the pipeline parks the task.

    ``allow_dirty`` bypasses the uncommitted-work guard in front of the
    cross-branch checkout. Nothing in the repo passes True; it is the human
    recovery path only.
    """
    workdir = Path(workdir)
    branch = f"pi/{task_id}"
    if not allow_dirty:
        _require_clean(workdir, f"merge_to_trunk checkout {trunk}")
    pre_head = _git(workdir, "rev-parse", trunk, check=False).strip()
    added = _added_paths(workdir, trunk, branch)
    _git(workdir, "checkout", trunk)
    squash = subprocess.run(["git", "merge", "--squash", branch], cwd=workdir,
                            capture_output=True, text=True)
    if squash.returncode != 0:
        abort_merge(workdir, added)
        raise RuntimeError(
            f"merge conflict for {task_id}: "
            f"{(squash.stderr or squash.stdout).strip()[-300:]} "
            f"(trunk before merge: {pre_head})"
        )
    commit = subprocess.run(
        ["git", "-c", "user.email=pi@harness.local", "-c", "user.name=pi-harness",
         "commit", "-m", f"feat({task_id}): {title}\n\nSquash-merged from {branch}."],
        cwd=workdir, capture_output=True, text=True)
    if commit.returncode != 0:
        # Same cleanup as a conflict, then stop. Anything still uncommitted after
        # that is evidence: we do not reset, do not retry, do not delete.
        abort_merge(workdir, added)
        remaining = dirty_paths(workdir)
        msg = (f"squash commit FAILED for {task_id}: "
               f"{(commit.stderr or commit.stdout).strip()[-300:]} "
               f"(trunk before merge: {pre_head})")
        if remaining:
            msg += (f"; cleanup incomplete - {len(remaining)} path(s) still uncommitted, "
                    f"e.g. {remaining[:5]}. Left untouched for a human: "
                    f"git -C {workdir} status")
        raise RuntimeError(msg)

    # --- verification gate ---
    ok, detail = verify_harness(workdir)
    if not ok:
        # revert trunk to last known-good; keep the feature branch for inspection.
        # `reverted_to` names the ref that was *actually* rolled back to: the tag
        # when one exists, `HEAD~1` when there is none (T03 — reporting the tag
        # unconditionally hid the fact that the fallback had run).
        reverted_to = _revert_to_last_good(workdir, trunk)
        raise RuntimeError(
            f"verification gate FAILED for {task_id}: {detail}. "
            f"trunk reverted to {reverted_to}; feature branch {branch} kept.")

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
    trunk's parent (undo the single bad merge commit).

    Returns the target actually reverted to — `"tag:<ref>"` or `"HEAD~1"` — so the
    caller can log which path was taken instead of assuming the tag was there.
    """
    workdir = Path(workdir)
    if has_tag(workdir, LAST_GOOD_TAG):
        _git(workdir, "reset", "--hard", LAST_GOOD_TAG)
        return f"tag:{LAST_GOOD_TAG}"
    else:
        # no last-good tag: undo the merge commit we just made
        _git(workdir, "reset", "--hard", "HEAD~1")
        return "HEAD~1"