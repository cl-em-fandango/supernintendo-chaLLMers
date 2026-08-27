# T59 — Resynchronize operator and historical documentation

**Depends:** T12, T33, T50, T54, T61, T69 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- README.md
- historical_docs/REFACTOR_PLAN.md
- historical_docs/refactor-tasks/README.md

## Do
Document only landed commands/config/log paths; remove deleted paths and stale no-automation claims. Verify every documented command with --help.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest discover -s tests
```
Global Gate must pass.

## Out of scope
No Python, config, gate-script, or behavior changes.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
