# T70 — Add merge checkpoint and resume routing

**Depends:** T26 · **Leaf ticket**

## Context
This is one recursively-sliced behavior with one fixture class.

## Read first
- harness/core/enums.py
- harness/workflow/pipeline.py

## Do
Create the new file: `tests/test_merge_checkpoint.py`.

Add MERGE after SLICES; checkpoint after successful merge and before complete; when already checkpointed skip merge and complete. Test both paths with a stubbed merge function.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_merge_checkpoint -v
```
Global Gate must pass.

## Out of scope
No branch deletion or git implementation changes.

## Done when
The named behavior and failure path are proven by the dedicated test and no out-of-scope file changed.
