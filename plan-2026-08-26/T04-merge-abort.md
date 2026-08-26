# T04 — A failed `merge --squash` must not leave the repo mid-merge

**Wave 0** · depends: T03 · `[tag]` · finding: F6b

## Context
`merge_to_trunk` (git_cli.py:46) runs `git merge --squash <branch>` with `check=True`. On a
conflict `_git` raises immediately, leaving trunk checked out with a live MERGE_STATE and a
dirty index. The next task's `ensure_branch`/`merge_to_trunk` then operates on that wreck, and
`_revert_to_last_good` is never reached because the raise happens before the gate.

## Read first
- `external/git_cli.py` — `merge_to_trunk`, `_git`, `verify_harness`
- `harness/workflow/pipeline.py` — `stage_holistic` (the only caller; it catches `Exception` and parks)

## Do
1. In `merge_to_trunk`, run the squash **without** `check=True` and inspect the result:
   on non-zero rc, call a new `abort_merge(workdir)` helper (`git merge --abort`, itself
   best-effort/`check=False`) and then raise `RuntimeError` whose message includes the git
   stderr tail (last ~300 chars) and the words `merge conflict`.
2. Apply the same treatment to the `commit` step that follows the squash: if the commit fails,
   `git merge --abort` if a merge is still in progress, else `git reset --hard <trunk-before-merge>`
   is NOT allowed here (that is T05's guard) — instead raise with the stderr tail and leave the
   tree untouched so a human can see it. Say so in the docstring.
3. Record the starting trunk sha (`git rev-parse <trunk>`) before touching anything, and include
   it in every error message, so recovery is a copy-paste away.
4. Add a helper `merge_in_progress(workdir) -> bool` (`(git-dir)/MERGE_HEAD` exists).

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, subprocess, tempfile, pathlib
sys.path.insert(0, '.')
from external import git_cli as G
d = pathlib.Path(tempfile.mkdtemp())
def g(*a, check=True):
    r = subprocess.run(["git", *a], cwd=d, capture_output=True, text=True)
    if check and r.returncode: raise AssertionError((a, r.stderr))
    return r
g("init","-b","pi/trunk"); (d/"f.txt").write_text("base\n"); g("add","-A")
g("-c","user.email=t@t","-c","user.name=t","commit","-m","base")
g("checkout","-b","pi/conflict"); (d/"f.txt").write_text("feature\n"); g("add","-A")
g("-c","user.email=t@t","-c","user.name=t","commit","-m","feat")
g("checkout","pi/trunk"); (d/"f.txt").write_text("trunk\n"); g("add","-A")
g("-c","user.email=t@t","-c","user.name=t","commit","-m","trunk-change")
try:
    G.merge_to_trunk(d, "conflict", "pi/trunk", "title"); raise SystemExit("expected RuntimeError")
except RuntimeError as e:
    assert "merge conflict" in str(e), str(e)[:200]
assert not G.merge_in_progress(d), "repo left mid-merge"
assert "nothing to commit" in g("status","--short","--porcelain") or True
assert subprocess.run(["git","status","--porcelain"],cwd=d,capture_output=True,text=True).stdout.strip() == "", "dirty index left behind"
print("merge abort ok")
PY
python3 -m unittest discover -s tests
```
Both must pass, plus the Gate.

## Out of scope
Dirty-tree guard for `reset --hard` (T05), tag lookup (done in T03), verify gate contents
(T24), branch deletion policy (T27), any pipeline change.

## Done when
The conflict repro prints `merge abort ok`, leaves `git status --porcelain` empty and no
`MERGE_HEAD`; a clean-merge temp repo still squash-merges exactly as before.
