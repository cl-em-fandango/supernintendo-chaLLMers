# T69 — Add the executable local gate script

**Depends:** T40 · **Leaf ticket**

## Context
This ticket is one recursively-sliced leaf. It owns only the behavior below.

## Read first
- historical_docs/PLAN-2026-08-26.md — "The Gate (every card, before commit)" (the plan moved from
  the repository root to `historical_docs/`, so the root-level path is stale)
- CODING_STANDARDS.md

## Do
Create the new files: `scripts/gate.sh`, `tests/test_gate_script.py`.

Implement the documented three-step gate (unittest discovery, the module import sweep,
`harness.py status` returning 0) with one named fail-fast error per step and no git mutation. The
script takes one optional argument, the directory to run in, defaulting to its own repository root.
`tests/test_gate_script.py` always runs it against a temp directory holding a stub `tests/` package
and a stub `harness.py`: exit 0 when all three steps pass, non-zero naming the failing step when one
fails. The temp copy is the only fixture — the test never runs the script against this working tree.

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
