# T63 — Test successful squash merge and gate rollback

**Depends:** T05, T24, T71 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- external/git_cli.py
- tests/test_checkpoint_state.py

## Do
Create the new file: `tests/test_git_merge_gate.py`.

Temp-repo tests for successful gated merge, tag advancement, branch retention/cleanup, and failed-gate rollback.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_git_merge_gate -v
```
Global Gate must pass.

## Out of scope
No conflict merge, dirty refusal, gate-not-applicable, or real repo.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
