# T62 — Test git ref lookup and branch setup

**Depends:** T03, T23 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- external/git_cli.py
- tests/test_checkpoint_state.py

## Do
Create the new file: `tests/test_git_refs.py`.

Temp-repo tests for has_tag/has_branch, tag revert, ensure_branch idempotence and queue predicate.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_git_refs -v
```
Global Gate must pass.

## Out of scope
No merges, gates, conflicts, dirty cleanup, or real git repo.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
