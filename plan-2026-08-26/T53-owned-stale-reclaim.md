# T53 — Make stale and operator reclaim ownership-aware

**Depends:** T12, T51 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- harness/core/providers.py
- harness/cli/handlers.py

## Do
Create the new file: `tests/test_claim_reclaim.py`.

Automatic stale reclaim skips foreign and unknown/corrupt ownership. Explicit requeue-claims force mode names the recorded owner. Test old-owned, old-foreign, unknown and corrupt metadata.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_claim_reclaim -v
```
Global Gate must pass.

## Out of scope
No run-command ownership generation, locking, PID checks, or real queue.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
