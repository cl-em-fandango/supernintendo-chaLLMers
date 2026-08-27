# T05 — Refuse destructive git ops on a dirty tree

**Wave 0** · depends: T03 · **blocks: T04** (its `abort_merge` cleanup is only safe under this guard) · `[tag]` · finding: F6c

## Context
Two paths destroy uncommitted human work today: `_revert_to_last_good` runs
`git reset --hard <tag>` and `merge_to_trunk` runs `git branch -d`. The audit found the
harness repo itself dirty (`config.json`, `README.md`, deleted `supervisor.sh`) — a circuit
breaker event right now would silently erase a person's edits. `git reset --hard` is only safe
when the tree is clean *of third-party changes*.

## Read first
- `external/git_cli.py` — `_git`, `merge_to_trunk`, `_revert_to_last_good`
- `AUDIT-2026-08-26.md` §F6 third bullet

## Do
1. Add `dirty_paths(workdir) -> list[str]` — `git status --porcelain` parsed to paths
   (include staged, unstaged and untracked).
2. Add a module-level guard used by every destructive call:
   `def _require_clean(workdir, what: str) -> None: paths = dirty_paths(workdir); if paths: raise RuntimeError(f"refusing {what}: {len(paths)} uncommitted paths, e.g. {paths[:5]}")`
3. Call it before: `git reset --hard` in `_revert_to_last_good`, and before the
   `git checkout <trunk>` in `merge_to_trunk` (a checkout over local changes is where git
   starts silently carrying them across branches). T04 lands **after** this card and its
   `abort_merge` runs `git reset -q` + `git checkout -q -- .` to clean a failed `--squash`; that
   cleanup is defensible only because this guard already proved the tree clean. Leave the guard
   reachable as a helper T04 can call, and do not narrow it to `reset --hard` alone.
4. Escape hatch: both `merge_to_trunk` and `_revert_to_last_good` take
   `allow_dirty: bool = False`. Default False. Nothing in the repo passes True — it exists for
   the human recovery path only. Say that in the docstring.
5. On refusal, the message must tell the human the exact command to inspect the damage
   (`git -C <workdir> status`).

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, subprocess, tempfile, pathlib
sys.path.insert(0, '.')
from external import git_cli as G
d = pathlib.Path(tempfile.mkdtemp())
def g(*a): assert subprocess.run(["git",*a],cwd=d,capture_output=True).returncode == 0, a
g("init","-b","pi/trunk"); (d/"a").write_text("1"); g("add","-A")
g("-c","user.email=t@t","-c","user.name=t","commit","-m","c1"); g("tag","-f","pi/last-good")
(d/"a").write_text("DIRTY"); (d/"untracked").write_text("u")
assert len(G.dirty_paths(d)) == 2, G.dirty_paths(d)
try:
    G._revert_to_last_good(d, "pi/trunk"); raise SystemExit("guard did not fire")
except RuntimeError as e:
    assert "refusing" in str(e) and "uncommitted" in str(e), str(e)
assert (d/"a").read_text() == "DIRTY", "dirty file was clobbered anyway"
G._revert_to_last_good(d, "pi/trunk", allow_dirty=True)   # documented escape hatch
assert (d/"a").read_text() == "1", "escape hatch broken"
print("dirty guard ok")
PY
python3 -m unittest discover -s tests
```
Both must pass, plus the Gate.

## Out of scope
Changing *when* the revert is chosen (T03/T06), auto-stash (explicitly rejected — silently
stashing human work is the same sin), supervisor-side breaker wiring (T06).

## Done when
Repro prints `dirty guard ok`; no `reset --hard` or cross-branch checkout in `git_cli.py`
is reachable without a prior `_require_clean`; the only `allow_dirty=True` in the tree is a
docstring/comment mention.
