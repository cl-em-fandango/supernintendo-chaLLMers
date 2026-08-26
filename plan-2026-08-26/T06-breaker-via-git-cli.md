# T06 — Circuit breaker must call `external/git_cli`, not raw git

**Wave 0** · depends: T05 · finding: F6d (and CODING_STANDARDS §4)

## Context
`supervisor.py:180-192` shells out `git reset --hard pi/last-good` and `git rev-parse` inline
via `subprocess.run`. That bypasses the T05 dirty-tree guard (so the breaker can still erase a
human's work), duplicates git knowledge, and violates "all subprocess lives in `external/`".

## Read first
- `supervisor.py` — the breaker block inside `run_loop()` (search `CIRCUIT BREAKER`)
- `external/git_cli.py` — `_revert_to_last_good`, `dirty_paths`, `LAST_GOOD_TAG` (as of T03–T05)

## Do
1. Add to `external/git_cli.py`: `revert_to_last_good(workdir, trunk) -> str` — a public, thin
   wrapper over `_revert_to_last_good` that returns what it reverted to (T03's return value)
   and raises `RuntimeError` on a dirty tree (T05's guard).
2. In `supervisor.py`, replace both inline `subprocess.run(["git", ...])` calls with
   `revert_to_last_good(...)` inside a `try/Exception`; log the returned ref string, and log
   `⚠ breaker refused: <err>` and **continue the loop** when it raises (never crash the supervisor).
3. Delete the now-unused `rev = subprocess.run(...)` line; log short sha from the returned string.
4. `supervisor.py` must end with **zero** `["git"` literals. (`grep -n '"git"' supervisor.py` → empty.)

## Verify
```bash
cd /home/donald/work/harness
! grep -qn '"git"' supervisor.py && echo "supervisor: no raw git ✓"
python3 - <<'PY'
import sys, inspect, subprocess, tempfile, pathlib
sys.path.insert(0, '.')
from external import git_cli as G
assert callable(G.revert_to_last_good)
d = pathlib.Path(tempfile.mkdtemp())
def g(*a): assert subprocess.run(["git",*a],cwd=d,capture_output=True).returncode == 0, a
g("init","-b","pi/trunk"); (d/"a").write_text("1"); g("add","-A")
g("-c","user.email=t@t","-c","user.name=t","commit","-m","c1"); g("tag","-f","pi/last-good")
(d/"a").write_text("DIRTY")
try:
    G.revert_to_last_good(d, "pi/trunk"); raise SystemExit("guard bypassed")
except RuntimeError as e:
    assert "refusing" in str(e)
print("breaker path guarded ok")
PY
```
Both must pass, plus the Gate. Do NOT run `supervisor.py run` to test this.

## Out of scope
The cycle decision (T13), `--continue` wiring (T14), child output (T08), changing FAIL_LIMIT semantics.

## Done when
No `git` subprocess literals remain in `supervisor.py`; a dirty tree makes the breaker refuse
and log rather than destroy; `git_cli.revert_to_last_good` is the single entry point for it.
