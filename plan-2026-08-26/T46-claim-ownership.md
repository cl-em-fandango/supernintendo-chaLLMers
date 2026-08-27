# T46 — Claim ownership epic (superseded)

> **DO NOT EXECUTE THIS FILE AS A CARD.** Execute T51 → T52 and T53. This file is retained only as
> the parent contract.

**Wave 2** · depends: T09, T10, T12 · finding: hardening review G4

## Context
Claims are currently identified only by filename and age. A cleanup path cannot distinguish its own claim from one held by another live invocation. This card adds minimal ownership metadata without introducing a daemon or distributed lock.

## Read first
- `harness/core/providers.py` — claim, list and requeue APIs
- `harness/cli/handlers.py` — run cleanup and stale requeue paths
- tests from T37

## Do
1. Introduce a named `Claim` dataclass containing the `Task`, original filename, owner id, claimed timestamp, and metadata path.
2. `fetch_pending(claim=True, owner=<non-empty id>)` writes an adjacent JSON metadata file atomically after the markdown rename. If metadata creation fails, move the markdown back and raise.
3. `requeue_claim(..., owner=...)` refuses an ownership mismatch. Explicit operator mode (`force=True`) is permitted only from `requeue-claims`, and must print the recorded owner.
4. Run commands generate one owner id per invocation and pass it through claim and finally-cleanup paths.
5. Stale requeue remains opt-in and may force only after the age threshold. Missing/corrupt metadata is listed as `owner=unknown` and requires explicit operator force; automatic reclaim skips it.
6. Add temp-directory tests with two owners proving owner A cannot requeue owner B's claim and failed metadata creation rolls the markdown back to pending.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_claim_ownership -v
python3 -m unittest tests.test_handlers -v
```
Gate must pass.

## Out of scope
`flock`, cross-host coordination, PID liveness checks, changing stale age defaults, mutating the real queue.

## Done when
Normal cleanup can requeue only claims from its own invocation; automatic stale recovery never takes an unknown/live foreign claim; operator force remains explicit and tested.
