# T61 — Wire queue-audit output and report persistence

**Depends:** T77 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below. Its suite is named for
the behavior (`tests/test_queue_audit_cli.py`) so no sibling leaf shares a file with it — T52 and
this card both claimed `tests/test_handlers.py`, which no longer reverts independently.

## Read first
- harness/cli/parser.py
- harness/cli/handlers.py

## Do
Create the new file: `tests/test_queue_audit_cli.py`.

Add queue-audit dispatch; print core report and write one new dated report under logs. Test with temp workDir and unchanged temp queue hash.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_queue_audit_cli -v
```
Global Gate must pass.

## Out of scope
No anomaly logic, queue writes, or operator action execution.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
