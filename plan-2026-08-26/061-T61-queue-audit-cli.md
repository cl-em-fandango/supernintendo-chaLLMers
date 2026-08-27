# T61 — Wire queue-audit output and report persistence

**Depends:** T60 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- harness/cli/parser.py
- harness/cli/handlers.py

## Do
Create the new file: `tests/test_handlers.py`.

Add queue-audit dispatch; print core report and write one new dated report under logs. Test with temp workDir and unchanged temp queue hash.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_handlers -v
```
Global Gate must pass.

## Out of scope
No anomaly logic, queue writes, or operator action execution.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
