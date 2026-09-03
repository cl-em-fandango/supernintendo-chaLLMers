"""Demo deployer core: publish an artifact directory to `docs/` on the
demo deploy branch (FR-5), in a dedicated checkout, with serialization.

This is the git boundary for the `snes-demo` feature (CODING_STANDARDS §4:
all subprocess calls live in `external/`). It contains no LLM calls, no npm
builds and no GitHub API calls — it takes a caller-supplied artifact
directory and publishes it, following the FR-5 sequence:

  a. fetch origin;
  b. refresh the trunk ref from the *harness workdir's local repository*
     (FR-5.2.b — `merge_to_trunk` never pushes trunk to origin, so a plain
     `fetch origin` would miss the just-merged app source);
  c. check out the deploy branch (tracking origin; created from the
     refreshed trunk when it exists on neither side);
  d. rebase it onto the refreshed trunk — conflicts touching only the docs
     directory resolve to trunk's side (the docs are regenerated below
     anyway, FR-5.4); any other conflict is a hard failure;
  e. wipe the docs directory and replace it with exactly the supplied
     artifacts (edge case 4: a stale `docs/` never ships);
  f. commit;
  g. push **only** the deploy branch with `--force-with-lease`.

Nothing else is ever pushed. A failure at any step before the push leaves
the previous deployment on origin intact (FR-8.1), and raises
`DemoDeployError` naming the step that failed. The checkout is reset and
cleaned before use: the optional in-checkout builder (FR-7.3) writes
residue (`node_modules/`, lock files, artifact dirs) into the tracked
tree, and a dirty checkout makes every later checkout/rebase fail, so
residue from a previous deploy must never poison the next one
(FR-8.4, spec edge case 3). Deployments are serialized
by an exclusive file lock inside the deploy checkout (FR-8.4); a second
concurrent deploy blocks until the lock frees.
"""
from __future__ import annotations

import fcntl
import os
import shutil
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable

GIT_AUTHOR_EMAIL = "pi@harness.local"
GIT_AUTHOR_NAME = "pi-harness"

# Name of the exclusive lock file inside the deploy checkout (FR-8.4).
LOCKFILE_NAME = ".deploy.lock"

# Upper bound on rebase-conflict resolution rounds; a rebase that keeps
# conflicting this often is broken, not resolvable.
_MAX_REBASE_ROUNDS = 100

# `git status --porcelain` XY codes that mean "unmerged" (all eight;
# omitting `UU` would let the most common conflict — both sides edited —
# fall through to the empty-patch `rebase --skip` path and vanish).
_UNMERGED_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


def _no_log(message: str) -> None:
    """Default `log`: discard."""


class DeployStep(Enum):
    """The demo pipeline's failure-reportable steps (FR-8.1).

    The FR-5 publication sequence below, plus the two generation-time
    steps (`scaffold`, `content`) whose failures carry the same
    `Demo deployment failed at <step>: <reason>` comment shape."""

    SCAFFOLD = "scaffold"
    CONTENT = "content"
    INIT = "init"
    CLEAN = "clean"
    FETCH_ORIGIN = "fetch-origin"
    REFRESH_TRUNK = "refresh-trunk"
    CHECKOUT = "checkout"
    REBASE = "rebase"
    BUILD = "build"
    REPLACE_DOCS = "replace-docs"
    COMMIT = "commit"
    PUSH = "push"


class DemoDeployError(RuntimeError):
    """Hard deploy failure. `step` names the FR-5 step that failed so the
    caller can comment `Demo deployment failed at <step>: <reason>`."""

    def __init__(self, step: DeployStep, message: str):
        super().__init__(f"{step.value}: {message}")
        self.step = step
        self.message = message  # raw reason, without the step prefix


@dataclass(frozen=True)
class DemoDeployRequest:
    """Everything `publish_artifacts` needs, as one explicit parameters object.

    `harness_repo` is the harness workdir whose local `trunk_branch` carries
    the just-merged app source; `deploy_dir` is the dedicated checkout and
    must never be inside `harness_repo`'s task areas (spec §6).
    """

    harness_repo: Path
    deploy_dir: Path
    origin_url: str
    deploy_branch: str
    trunk_branch: str
    docs_dir: str
    artifacts_dir: Path | None = None
    # Optional in-checkout build (FR-7.3): called with the checkout path
    # after the rebase and must return the artifact directory to publish.
    # Takes precedence over `artifacts_dir`; exactly one of the two must
    # be supplied. The deployer itself stays build-tool-agnostic — it only
    # invokes the callable and attributes its failures to BUILD.
    builder: Callable[[Path], Path] | None = None
    # FR-8.3: every deploy step logs through the caller's sink (the
    # composition root passes the harness `LogSink`); the default
    # discards, so unwired callers keep the old silence.
    log: Callable[[str], None] = _no_log


@dataclass(frozen=True)
class DeployOutcome:
    """Result of one successful publication."""

    branch: str
    commit: str
    changed: bool


def origin_url_from_repo(repo: Path) -> str:
    """The `origin` remote URL of an existing clone.

    Lets the composition root derive the deploy checkout's origin from the
    harness repo instead of configuring it twice. Raises RuntimeError when
    the repo has no origin.
    """
    proc = _run(Path(repo), "remote", "get-url", "origin", check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise RuntimeError(f"{repo} has no origin remote")
    return proc.stdout.strip()


@contextmanager
def deploy_lock(deploy_dir: Path):
    """Exclusive lock serializing demo deployments (FR-8.4).

    The lock lives in the deploy checkout, so every deployer targeting the
    same checkout contends on the same file. Acquisition blocks; a second
    concurrent deploy defers until the lock frees.
    """
    deploy_dir = Path(deploy_dir)
    deploy_dir.mkdir(parents=True, exist_ok=True)
    lock_path = deploy_dir / LOCKFILE_NAME
    with open(lock_path, "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def publish_artifacts(request: DemoDeployRequest) -> DeployOutcome:
    """Publish `artifacts_dir` as the sole content of `docs/` on the deploy
    branch of origin (FR-5). Serialized by `deploy_lock`.

    Raises `DemoDeployError` naming the failed step; any raise before the
    push leaves origin exactly as it was.
    """
    note = request.log
    with deploy_lock(request.deploy_dir):
        note(f"  demo deploy: lock held; publishing "
             f"{request.deploy_branch} from {request.harness_repo}")
        checkout = _ensure_checkout(request)
        _clean_checkout(checkout)
        _fetch_origin(checkout)
        _refresh_trunk(checkout, request)
        _checkout_deploy_branch(checkout, request)
        _rebase_onto_trunk(checkout, request)
        note(f"  demo deploy: rebased {request.deploy_branch} onto "
             f"{request.trunk_branch}")
        artifacts = _resolve_artifacts(checkout, request)
        note(f"  demo deploy: artifacts ready in {artifacts}")
        _replace_docs(checkout, request, artifacts)
        note(f"  demo deploy: {request.docs_dir}/ replaced with the "
             f"active app's artifacts")
        commit, changed = _commit_docs(checkout)
        verdict = (f"committed {commit[:12]}" if changed
                   else "no tree change (idempotent re-deploy)")
        note(f"  demo deploy: {verdict}")
        outcome = _push(checkout, request, commit, changed)
        note(f"  demo deploy: pushed {request.deploy_branch} to origin")
        return outcome


# --- step implementations -------------------------------------------------

def _ensure_checkout(request: DemoDeployRequest) -> Path:
    """Create the dedicated deploy checkout (with an `origin`) if absent."""
    checkout = Path(request.deploy_dir)
    try:
        if not (checkout / ".git").exists():
            checkout.mkdir(parents=True, exist_ok=True)
            _run(checkout, "init", "-b", request.deploy_branch,
                 step=DeployStep.INIT)
            _run(checkout, "remote", "add", "origin", request.origin_url,
                 step=DeployStep.INIT)
        else:
            _run(checkout, "remote", "set-url", "origin", request.origin_url,
                 step=DeployStep.INIT)
    except DemoDeployError:
        raise
    except OSError as exc:
        raise DemoDeployError(DeployStep.INIT, str(exc))
    return checkout


def _clean_checkout(checkout: Path) -> None:
    """Return the harness-owned checkout to a pristine tree (FR-8.4).

    The builder runs inside this checkout and npm writes residue into
    the tracked tree (`package-lock.json`, `node_modules/`, artifact
    dirs). Left in place, that residue turns the *next* deploy's
    checkout or rebase into `cannot rebase: You have unstaged changes`
    or `untracked working tree files would be overwritten`. The checkout
    is harness-owned, so discarding everything not committed is always
    safe; the lock file is excluded — it is held open by `deploy_lock`
    and must survive for concurrent deployers to contend on.
    """
    # `reset --hard` fails on a freshly initialised repo without commits
    # (there is nothing to reset); the failure is benign, `clean` follows.
    _run(checkout, "reset", "--hard", check=False)
    _run(checkout, "clean", "-ffd", "-e", LOCKFILE_NAME,
         step=DeployStep.CLEAN)


def _fetch_origin(checkout: Path) -> None:
    _run(checkout, "fetch", "--prune", "origin", step=DeployStep.FETCH_ORIGIN)


def _refresh_trunk(checkout: Path, request: DemoDeployRequest) -> None:
    """Point the local trunk ref at the harness workdir's trunk (FR-5.2.b).

    Force-updated from the local repository, not from origin: the normal
    merge-to-trunk flow only ever advances trunk inside the harness workdir.
    """
    harness_repo = Path(request.harness_repo)
    if not (harness_repo / ".git").exists():
        raise DemoDeployError(
            DeployStep.REFRESH_TRUNK,
            f"harness repo {harness_repo} is not a git repository")
    trunk = request.trunk_branch
    # Read the trunk SHA with `rev-parse` rather than `fetch`: a fetch into
    # `refs/heads/<trunk>` is refused when the source repo has that branch
    # checked out ("refusing to fetch into branch ... checked out at ..."),
    # and the harness workdir always has *some* branch checked out. Reading
    # the object database directly works regardless of the source's HEAD.
    sha = _run(harness_repo, "rev-parse", "--verify", "--quiet",
               f"refs/heads/{trunk}", check=False)
    if sha.returncode != 0:
        raise DemoDeployError(
            DeployStep.REFRESH_TRUNK,
            f"harness repo {harness_repo} has no branch {trunk}")
    commit = sha.stdout.strip()
    # Point the checkout's trunk ref at it. When the checkout already holds
    # the object (origin carried it, or an earlier refresh did) this is a
    # pure ref update; otherwise the objects must be fetched from the
    # harness repo into a neutral refspace first, because the same
    # checked-out-branch refusal blocks the plain refspec fetch.
    probe = _run(checkout, "cat-file", "-e", f"{commit}^{{commit}}",
                 check=False)
    if probe.returncode != 0:
        _run(checkout, "fetch", str(harness_repo),
             f"+refs/heads/{trunk}:refs/harness-trunk/{trunk}",
             step=DeployStep.REFRESH_TRUNK)
        _run(checkout, "update-ref", f"refs/heads/{trunk}",
             f"refs/harness-trunk/{trunk}", step=DeployStep.REFRESH_TRUNK)
        return
    _run(checkout, "update-ref", f"refs/heads/{trunk}", commit,
         step=DeployStep.REFRESH_TRUNK)


def _checkout_deploy_branch(checkout: Path, request: DemoDeployRequest) -> None:
    """Check out the deploy branch tracking origin, or create it from the
    refreshed trunk when it exists on neither side (§5.1)."""
    branch = request.deploy_branch
    remote_ref = f"refs/remotes/origin/{branch}"
    probe = _run(checkout, "rev-parse", "--verify", "--quiet", remote_ref,
                 check=False)
    if probe.returncode == 0:
        _run(checkout, "checkout", "-B", branch, "--track", remote_ref,
             step=DeployStep.CHECKOUT)
    else:
        _run(checkout, "checkout", "-B", branch, request.trunk_branch,
             step=DeployStep.CHECKOUT)


def _rebase_onto_trunk(checkout: Path, request: DemoDeployRequest) -> None:
    """Rebase the deploy branch onto the refreshed trunk (FR-5.2.d).

    Conflicts confined to the docs directory resolve to trunk's side and
    the rebase continues — the docs are wiped and regenerated right after,
    so artifact-only history is worthless (FR-5.4). Any conflict outside
    docs aborts the rebase and is a hard failure.
    """
    trunk = request.trunk_branch
    result = _run(checkout, "rebase", trunk, check=False)
    rounds = 0
    while result.returncode != 0:
        rounds += 1
        if rounds > _MAX_REBASE_ROUNDS:
            _abort_rebase(checkout)
            raise DemoDeployError(
                DeployStep.REBASE,
                f"rebase of {request.deploy_branch} onto {trunk} did not "
                f"settle after {_MAX_REBASE_ROUNDS} rounds")
        unmerged = _unmerged_paths(checkout)
        if unmerged:
            outside = [p for p in unmerged
                       if not _under_docs(p, request.docs_dir)]
            if outside:
                _abort_rebase(checkout)
                raise DemoDeployError(
                    DeployStep.REBASE,
                    "conflict outside the docs directory (source divergence): "
                    f"{', '.join(sorted(outside)[:5])}")
            for path in unmerged:
                _resolve_docs_conflict(checkout, path)
            result = _run(checkout, "rebase", "--continue", check=False)
        elif _rebase_in_progress(checkout):
            # Conflict resolution emptied the patch (a docs-only commit
            # trunk already supersedes): drop it and move on.
            result = _run(checkout, "rebase", "--skip", check=False)
        else:
            _abort_rebase(checkout)
            raise DemoDeployError(
                DeployStep.REBASE,
                f"rebase of {request.deploy_branch} onto {trunk} failed: "
                f"{_tail(result)}")


def _rebase_in_progress(checkout: Path) -> bool:
    """True while a rebase sequencer state directory exists."""
    git_dir = _run(checkout, "rev-parse", "--git-dir", check=False).stdout.strip()
    if not git_dir:
        return False
    root = Path(git_dir) if os.path.isabs(git_dir) else checkout / git_dir
    return (root / "rebase-merge").is_dir() or (root / "rebase-apply").is_dir()


def _unmerged_paths(checkout: Path) -> list[str]:
    """Paths git reports as unmerged in the worktree status."""
    status = _run(checkout, "status", "--porcelain", check=False).stdout
    paths: list[str] = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        code, path = line[:2], line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        if code in _UNMERGED_CODES:
            paths.append(path.strip('"'))
    return paths


def _resolve_docs_conflict(checkout: Path, path: str) -> None:
    """Resolve one docs-only conflict by taking trunk's ('ours') side.

    During a rebase `--ours` is the onto-branch (trunk). Where trunk has no
    version of the path (deleted or never present), the path is removed.
    Either way the final `docs/` content comes from the regeneration step.
    """
    kept = _run(checkout, "checkout", "--ours", "--", path, check=False)
    if kept.returncode == 0:
        _run(checkout, "add", "--", path, step=DeployStep.REBASE)
    else:
        _run(checkout, "rm", "-f", "--", path, check=False)


def _abort_rebase(checkout: Path) -> None:
    _run(checkout, "rebase", "--abort", check=False)


def _resolve_artifacts(checkout: Path, request: DemoDeployRequest) -> Path:
    """The directory to publish: the builder's verdict, else the supplied
    artifacts directory. A builder failure is a BUILD-step failure (FR-8.1)
    — it runs after the rebase and before `docs/` is touched, so the
    previous deployment is still untouched on origin."""
    if request.builder is not None:
        try:
            return Path(request.builder(checkout))
        except DemoDeployError:
            raise
        except Exception as exc:  # noqa: BLE001 - step-named failure (FR-8.1)
            raise DemoDeployError(DeployStep.BUILD, str(exc))
    if request.artifacts_dir is None:
        raise DemoDeployError(
            DeployStep.REPLACE_DOCS,
            "request supplies neither artifacts_dir nor builder")
    return Path(request.artifacts_dir)


def _replace_docs(checkout: Path, request: DemoDeployRequest,
                  artifacts: Path) -> None:
    """Wipe `docs/` and make it exactly the supplied artifacts (FR-5.2.f)."""
    if not artifacts.is_dir():
        raise DemoDeployError(
            DeployStep.REPLACE_DOCS,
            f"artifact directory {artifacts} does not exist")
    docs_path = checkout / request.docs_dir
    try:
        if docs_path.is_symlink() or docs_path.is_file():
            docs_path.unlink()
        elif docs_path.is_dir():
            shutil.rmtree(docs_path)
        shutil.copytree(artifacts, docs_path)
    except OSError as exc:
        raise DemoDeployError(DeployStep.REPLACE_DOCS, str(exc))
    _run(checkout, "add", "-A", "--", request.docs_dir,
         step=DeployStep.REPLACE_DOCS)


def _commit_docs(checkout: Path) -> tuple[str, bool]:
    """Commit the staged docs. Returns (head sha, whether a commit was made);
    an unchanged tree (idempotent re-deploy) commits nothing."""
    staged = _run(checkout, "diff", "--cached", "--quiet", check=False)
    changed = staged.returncode != 0
    if changed:
        _run(checkout, "commit", "-m", "Deploy demo app artifacts to docs/",
             step=DeployStep.COMMIT)
    return _run(checkout, "rev-parse", "HEAD", step=DeployStep.COMMIT).stdout.strip(), changed


def _push(checkout: Path, request: DemoDeployRequest,
          commit: str, changed: bool) -> DeployOutcome:
    """Push only the deploy branch (FR-5.3). `--force-with-lease` is safe
    here: only the harness writes this branch."""
    result = _run(checkout, "push", "--force-with-lease", "origin",
                  request.deploy_branch, check=False)
    if result.returncode != 0:
        raise DemoDeployError(
            DeployStep.PUSH,
            f"push of {request.deploy_branch} failed: {_tail(result)}")
    return DeployOutcome(branch=request.deploy_branch,
                         commit=commit,
                         changed=changed)


# --- subprocess plumbing ---------------------------------------------------

def _run(cwd: Path, *args: str, check: bool = True,
         step: DeployStep = DeployStep.INIT):
    """Run one git command with a fixed identity and a non-interactive editor.

    Returns the CompletedProcess; with `check=True` a non-zero exit becomes a
    `DemoDeployError` attributed to `step`.
    """
    cmd = ["git", "-c", f"user.email={GIT_AUTHOR_EMAIL}",
           "-c", f"user.name={GIT_AUTHOR_NAME}", *args]
    env = {**os.environ, "GIT_EDITOR": "true", "LC_ALL": "C"}
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          env=env)
    if check and proc.returncode != 0:
        raise DemoDeployError(step, f"git {' '.join(args)} failed: {_tail(proc)}")
    return proc


def _tail(proc) -> str:
    """The last 300 chars of the most informative stream."""
    text = (proc.stderr or proc.stdout or "").strip()
    return text[-300:]


def _under_docs(path: str, docs_dir: str) -> bool:
    """True when a repo-relative path lies inside the docs directory."""
    docs = docs_dir.strip("/")
    return path == docs or path.startswith(docs + "/")
