# T72 — Clean a failed squash merge without broad untracked deletion

**Depends:** T03, T05 · **Leaf ticket**

## Context
This is one recursively-sliced behavior with one fixture class.

## Read first
- external/git_cli.py

## Do
Create the new file: `tests/test_git_conflict.py`.

Add merge_in_progress and abort_merge; capture branch-added paths before merge; on squash conflict clear index/worktree and remove only safe recorded added paths. Test clean HEAD/index/worktree and unrelated concurrent untracked-file preservation.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_git_conflict -v
```
Global Gate must pass.

## Out of scope
No commit-failure path, gate, revert, or branch cleanup.

## Done when
The named behavior and failure path are proven by the dedicated test and no out-of-scope file changed.
