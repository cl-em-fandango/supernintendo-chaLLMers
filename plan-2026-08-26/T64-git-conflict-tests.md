# T64 — Test conflict cleanup and dirty refusal

**Depends:** T05, T72 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- external/git_cli.py
- tests/test_checkpoint_state.py

## Do
Create the new file: `tests/test_git_conflict.py`.

Temp-repo tests for unmerged-index cleanup, known merge-added path removal, no conflict markers, unchanged HEAD, and dirty revert refusal.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_git_conflict -v
```
Global Gate must pass.

## Out of scope
No happy merge, gate recognition, branch setup, or real repo.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
