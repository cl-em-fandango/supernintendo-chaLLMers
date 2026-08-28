# T57 — Park when every crash retry is exhausted

**Depends:** T20, T74 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- harness/workflow/pipeline.py

## Do
Create the new file: `tests/test_all_attempts_crashed.py`.

Raise AllAttemptsCrashed with task, stage and count after the final crashed attempt; catch once in process and park. Test exact attempt count and reason.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_all_attempts_crashed -v
```
Global Gate must pass.

## Out of scope
No over-cap handling, retry count changes, or non-crash verdict routing.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
