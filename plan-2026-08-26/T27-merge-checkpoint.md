# T27 — Checkpoint the merge and stop deleting the branch too early

**Wave 6** · depends: T26 · finding: F8 · `[tag]`

## Context
`holistic` is deliberately not checkpointed. `stage_holistic` does `pass` → `merge_to_trunk(...)` →
`complete(...)`, and `merge_to_trunk` ends with `tag -f pi/last-good trunk` **and
`branch -d pi/<id>`**. If the process dies in the window between a successful squash-merge and
`complete()`, the task is still in `active/` with no merge record; a resume re-enters
`stage_holistic`, runs `merge --squash pi/<id>` again — and fails, because the branch was deleted
after the first merge. Result: a spurious park of work that is already on trunk. That is F8's second
half, and it is unrecoverable by hand without reading git reflog.

## Read first
- `external/git_cli.py` — `merge_to_trunk` end-to-end, especially the `tag -f` / `branch -d` pair at
  the end, and `_revert_to_last_good`
- `harness/workflow/pipeline.py` — `stage_holistic` (the `pass` → merge → `complete` sequence and its
  `except` → park)
- `harness/workflow/task_lifecycle.py` — `checkpoint()`, `complete()`, `_parse_stages`
- `harness/core/enums.py` — `CheckpointStage` + `CHECKPOINT_ORDER` (holistic is absent on purpose)

## Do
1. Add `CheckpointStage.MERGE = "merge"` **and** append it to `CHECKPOINT_ORDER` after `SLICES`.
   `_parse_stages` drops unknown names with a warning, so old `task.json` files are unaffected.
2. In `stage_holistic`, after `merge_to_trunk` returns successfully and **before** `complete()`, call
   `checkpoint(task_id, CheckpointStage.MERGE)`. If that write fails, log loudly and still call
   `complete()` — losing a checkpoint is better than leaving a merged task in `active/`.
3. At the top of `stage_holistic`: if `CheckpointStage.MERGE in state.checkpointed_stages`, skip the
   merge entirely, log `already merged, completing`, and go straight to `complete()`.
4. Remove `branch -d pi/<id>` from `merge_to_trunk`. Move branch deletion into `complete()` (or into
   a new `TaskLifecycle`-adjacent helper the pipeline calls after `complete()` succeeds — pick one,
   name it `cleanup_branch(workdir, task_id, trunk)` in `external/git_cli.py`, and keep the deletion
   best-effort: a failed delete logs and does not raise).
5. Keep `tag -f pi/last-good trunk` exactly where it is (in `merge_to_trunk`, after the gate passes).

## Verify
```bash
cd /home/donald/work/harness
python3 - <<'PY'
import sys, tempfile, subprocess, pathlib; sys.path.insert(0,'.')
from harness.core.enums import CheckpointStage
assert CheckpointStage.MERGE.value == "merge"
assert CheckpointStage.CHECKPOINT_ORDER[-1] is CheckpointStage.MERGE
src = pathlib.Path('external/git_cli.py').read_text()
mt = src.split('def merge_to_trunk')[1].split('\ndef ')[0]
assert 'branch -d' not in mt, "merge_to_trunk still deletes the branch"
assert 'def cleanup_branch' in src, "no explicit post-complete cleanup"
# end-to-end on a temp repo: merge leaves the branch alive; cleanup removes it
r = pathlib.Path(tempfile.mkdtemp())
subprocess.run(["git","init","-q","-b","trunk",str(r)], check=True)
(r/"harness.py").write_text("print('ok')\n"); (r/"harness").mkdir()
subprocess.run(["git","-C",str(r),"add","-A"], check=True)
subprocess.run(["git","-C",str(r),"-c","user.email=t@t","-c","user.name=t","commit","-qm","init"], check=True)
subprocess.run(["git","-C",str(r),"checkout","-qb","pi/t1"], check=True)
(r/"f.md").write_text("work\n"); subprocess.run(["git","-C",str(r),"add","-A"], check=True)
subprocess.run(["git","-C",str(r),"-c","user.email=t@t","-c","user.name=t","commit","-qm","work"], check=True)
from external import git_cli as G
G.merge_to_trunk(r, "t1", "trunk", "title")
branches = subprocess.run(["git","-C",str(r),"branch","--format=%(refname:short)"],
                          capture_output=True,text=True).stdout.split()
assert "pi/t1" in branches, f"branch vanished before complete(): {branches}"
G.cleanup_branch(r, "t1", "trunk")
branches = subprocess.run(["git","-C",str(r),"branch","--format=%(refname:short)"],
                          capture_output=True,text=True).stdout.split()
assert "pi/t1" not in branches
print("merge checkpoint ok")
PY
```
Must pass, plus the Gate. Then `git tag -f pi/last-good pi/trunk`.

## Out of scope
Per-slice checkpoints (T26, already landed), what a per-repo verification gate should be (deferred by
**D3**; T24 is the current behaviour), the revert-path and abort fixes (T03–T05), checkpointing
`holistic` itself (still deliberately absent — MERGE is the marker, do not add HOLISTIC to
`CHECKPOINT_ORDER`), and any change to `complete()`'s directory move.

## Done when
`merge_to_trunk` leaves `pi/<id>` alive; `checkpointed_stages` contains `"merge"` after a successful
merge; a resume with `"merge"` present does not run `merge --squash` again; branch deletion happens
only after `complete()` and cannot fail the task.
