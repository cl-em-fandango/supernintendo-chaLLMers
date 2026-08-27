# T69 — Add the executable local gate script

**Depends:** T40 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- PLAN-2026-08-26.md
- CODING_STANDARDS.md

## Do
Create the new files: `scripts/gate.sh`, `tests/test_gate_script.py`.

Implement the documented three-step gate with fail-fast named errors and no git mutation. Test in a copied/temp fixture where practical.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_gate_script -v
```
Global Gate must pass.

## Out of scope
No pyproject, ruff, README, CI, hooks, commits, or tags.

## Done when
The stated behavior is covered by the named test, the Gate passes, and no out-of-scope file changed.
