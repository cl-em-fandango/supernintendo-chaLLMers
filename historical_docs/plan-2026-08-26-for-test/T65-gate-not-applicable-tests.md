# T65 — Test gate-not-applicable is pre-mutation

**Depends:** T24 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- external/git_cli.py
- tests/test_checkpoint_state.py

## Do
Create the new file: `tests/test_gate_not_applicable.py`.

In a temp non-harness repo assert GateNotApplicable, unchanged HEAD/status/index, and no git write before refusal.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_gate_not_applicable -v
```
Global Gate must pass.

## Out of scope
No gate design, merge success, conflicts, or real repo.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
