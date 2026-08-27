# T04 — A failed `merge --squash` must not leave the repo half-merged

**Wave 0** · depends: T03, **T05 (its `_require_clean` guard is what makes this card's cleanup safe)** · `[tag]` · finding: F6b

## Context
`merge_to_trunk` (git_cli.py:46) runs `git merge --squash <branch>` with `check=True`. On a
conflict `_git` raises immediately and the repo is left with **unmerged index entries** and the
files the squash staged. The next task's `ensure_branch`/`merge_to_trunk` then operates on that
wreck, and `_revert_to_last_good` is never reached because the raise happens before the gate.

**Correct the obvious assumption before starting:** a squash merge leaves **no** `MERGE_HEAD`. On
this machine, a conflicting `git merge --squash` prints `Squash commit -- not updating HEAD`, writes
no `.git/MERGE_HEAD`, and a following `git merge --abort` exits 128 with *"There is no merge to abort
(MERGE_HEAD missing)"*. So "abort the merge" is not a fix here — it is a no-op that raises. The
residual state is provable with `git ls-files -u` (three stages per conflicted path) plus whatever
the squash staged, including files the branch *added* that survive as untracked.

## Read first
- `external/git_cli.py` — `merge_to_trunk`, `_git`, `verify_harness`
- `harness/workflow/pipeline.py` — `stage_holistic` (the only caller; it catches `Exception` and parks)

## Do
1. In `merge_to_trunk`, run the squash **without** `check=True` and inspect the result. On non-zero
   rc call a new `abort_merge(workdir)` helper, then raise `RuntimeError` whose message includes the
   git stderr tail (last ~300 chars), the pre-merge trunk sha, and the words `merge conflict`.
2. `abort_merge(workdir)` must handle both merge shapes, in this order, all `check=False`:
   - if `.git/MERGE_HEAD` exists (a plain merge) → `git merge --abort`;
   - always, because a squash leaves conflict stages behind: `git reset -q` (mixed reset to HEAD —
     this is what clears the unmerged index entries), then `git checkout -q -- .` to restore the
     worktree. **Neither is allowed to run unless T05's `_require_clean(workdir, ...)` proved the
     tree clean before the merge started** — that precondition is the only reason a worktree
     `checkout` cannot destroy someone's edits. Say so in the docstring.
   - finally remove untracked paths that appeared during the merge (`git status --porcelain` `??`
     entries). None of them can have existed beforehand, because the tree was proven clean; these
     are exactly the files the squash added.
3. Apply the same treatment to the `commit` step that follows the squash: on failure call
   `abort_merge`, and if anything is still dirty afterwards, raise with the stderr tail and leave it
   for a human. `git reset --hard` is never used here (T05's guard owns that).
4. Record the starting trunk sha (`git rev-parse <trunk>`) before touching anything, and include
   it in every error message, so recovery is a copy-paste away.
5. Add `merge_in_progress(workdir) -> bool` — true when `.git/MERGE_HEAD` exists **or**
   `git ls-files -u` is non-empty. `MERGE_HEAD` alone is the bug in disguise: for a squash it is
   always absent, so a `MERGE_HEAD`-only check reports a wrecked repo as clean.

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
g("checkout","-b","pi/conflict"); (d/"f.txt").write_text("feature\n")
(d/"added_by_feature.txt").write_text("new file from the branch\n"); g("add","-A")
g("-c","user.email=t@t","-c","user.name=t","commit","-m","feat")
g("checkout","pi/trunk"); (d/"f.txt").write_text("trunk\n"); g("add","-A")
g("-c","user.email=t@t","-c","user.name=t","commit","-m","trunk-change")
head = subprocess.run(["git","rev-parse","HEAD"],cwd=d,capture_output=True,text=True).stdout
try:
    G.merge_to_trunk(d, "conflict", "pi/trunk", "title"); raise SystemExit("expected RuntimeError")
except RuntimeError as e:
    assert "merge conflict" in str(e), str(e)[:200]
    assert head[:8] in str(e), "pre-merge sha missing from the error message"
out = subprocess.run(["git","ls-files","-u"],cwd=d,capture_output=True,text=True).stdout
assert out.strip() == "", f"unmerged index entries left behind:\n{out}"
assert not G.merge_in_progress(d), "repo still reported mid-merge"
st = subprocess.run(["git","status","--porcelain"],cwd=d,capture_output=True,text=True).stdout
assert st.strip() == "", f"dirty tree left behind: {st}"
assert not (d/"added_by_feature.txt").exists(), "merge-added file survived the cleanup"
assert "<<<<<<<" not in (d/"f.txt").read_text(), "conflict markers left in the worktree"
assert subprocess.run(["git","rev-parse","HEAD"],cwd=d,capture_output=True,text=True).stdout == head
print("merge abort ok")
PY
python3 -m unittest discover -s tests
```
Both must pass, plus the Gate.

Note for whoever runs this after T24 lands: T24 makes `merge_to_trunk` raise `GateNotApplicable` for
a repo without `harness.py` + `harness/composition.py`, which this temp repo has not got. Either add
those two stub files to the repro or monkeypatch `G.gate_applies` to return True — and say which you
did in the commit message, so the repro stays runnable.

## Out of scope
Dirty-tree guard for `reset --hard` (T05 — landed before this card, and used by it), tag lookup
(done in T03), verify gate contents (T24), branch deletion policy (T27), any pipeline change.

## Done when
The conflict repro prints `merge abort ok`: no unmerged index entries (`git ls-files -u` empty),
`git status --porcelain` empty, no merge-added file left in the worktree, HEAD unmoved; a clean-merge
temp repo still squash-merges exactly as before.
