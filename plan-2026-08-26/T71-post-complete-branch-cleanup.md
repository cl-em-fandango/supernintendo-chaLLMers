# T71 — Delete the feature branch only after completion

**Depends:** T70 · **Leaf ticket**

## Context
This is one recursively-sliced behavior with one fixture class.

## Read first
- external/git_cli.py
- harness/workflow/pipeline.py

## Do
Create the new file: `tests/test_branch_cleanup.py`.

Remove branch deletion from merge_to_trunk; add cleanup_branch; invoke only after complete succeeds; cleanup failure logs and cannot re-park/fail the completed task.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_branch_cleanup -v
```
Global Gate must pass.

## Out of scope
No checkpoint schema, merge/gate behavior, or lifecycle move changes.

## Done when
The named behavior and failure path are proven by the dedicated test and no out-of-scope file changed.
