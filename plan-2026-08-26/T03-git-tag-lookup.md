# T03 — `_has()` must find tags, or the last-good revert never happens

**Wave 0** · depends: T01 · `[tag]` · finding: F6a

## Context
`external/git_cli.py:22` `_has()` verifies `refs/heads/<ref>`. `LAST_GOOD_TAG = "pi/last-good"`
is a **tag**. So `_revert_to_last_good()` (git_cli.py:101) always takes the `else` branch and
runs `git reset --hard HEAD~1` — the safety net silently does the wrong thing on every failed
verification gate. Verified in the audit: `refs/heads/pi/last-good` rc=1, `refs/tags/...` rc=0.

## Read first
- `external/git_cli.py` — whole file, it is 108 lines

## Do
1. Replace `_has(cwd, ref)` with two explicit predicates that take no implicit guesswork:
   - `has_branch(cwd, ref) -> bool` — probes `refs/heads/<ref>`
   - `has_tag(cwd, ref) -> bool` — probes `refs/tags/<ref>`
2. `ensure_branch` uses `has_branch` for both trunk and the feature branch (same semantics as today).
3. `_revert_to_last_good` uses `has_tag(workdir, LAST_GOOD_TAG)`; keep the `HEAD~1` fallback and
   make it loud: the function returns the string it reverted to (`"tag:<ref>"` or `"HEAD~1"`),
   so the caller can log which path was taken.
4. Do not change any git command beyond the ref probes.

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, subprocess, tempfile, pathlib
sys.path.insert(0, '.')
from external import git_cli as G
d = pathlib.Path(tempfile.mkdtemp())
def g(*a): assert subprocess.run(["git", *a], cwd=d, capture_output=True).returncode == 0, a
g("init", "-b", "pi/trunk"); (d/"a.txt").write_text("1"); g("add","-A")
g("-c","user.email=t@t","-c","user.name=t","commit","-m","c1")
g("tag","-f","pi/last-good")
assert G.has_branch(d, "pi/trunk") and not G.has_branch(d, "pi/nope")
assert G.has_tag(d, "pi/last-good") and not G.has_tag(d, "pi/nope")
g("add","-A"); g("-c","user.email=t@t","-c","user.name=t","commit","-m","c2")
took = G._revert_to_last_good(d, "pi/trunk")
assert took.startswith("tag:"), f"fell back to HEAD~1: {took}"
assert (d/"a.txt").exists() and "c1" in subprocess.run(["git","log","--oneline"],cwd=d,capture_output=True,text=True).stdout
print("revert-to-tag ok:", took)
PY
python3 -m unittest discover -s tests
```
Both must pass, plus the Gate.

## Out of scope
Merge abort (T04), dirty-tree guard (T05), supervisor breaker (T06), verify_harness (T24),
`merge_to_trunk` flow.

## Done when
A temp repo with only a `pi/last-good` **tag** reverts to that tag (not `HEAD~1`);
`_has` no longer exists; the two new predicates are used everywhere they should be
(`grep -n "_has(" external/git_cli.py` is empty).
