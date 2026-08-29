# T67 — Test run and run-one clean only their own claims

**Depends:** T10, T52 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- harness/cli/handlers.py
- harness/core/providers.py

## Do
Create the new file: `tests/test_handlers_run.py`.

Test cmd_run continues after one exception, run-one processes one, own claims return, and foreign claims remain.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_handlers_run -v
```
Global Gate must pass.

## Out of scope
No status, parser surface, autonomous, stale policy, or real queue.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
