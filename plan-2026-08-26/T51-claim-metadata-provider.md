# T51 — Add atomic claim ownership metadata to the provider

**Depends:** T09 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- harness/core/providers.py

## Do
Create the new file: `tests/test_claim_ownership.py`.

Add a Claim dataclass and owner metadata sidecar. Claim rename plus metadata creation is rollback-safe. Normal requeue requires matching owner. Test two owners and metadata-write rollback.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_claim_ownership -v
```
Global Gate must pass.

## Out of scope
No CLI owner generation, stale policy, force mode, or real queue.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
