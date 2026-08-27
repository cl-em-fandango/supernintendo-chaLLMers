# T45 — Terminal moves must survive bookkeeping write failures

**Wave 5** · depends: T21 · finding: hardening review G5

## Context
A terminal directory move is the lifecycle authority. After T21, state and review-summary writes happen after that move. An I/O failure there must not make callers treat an already-moved task as still active or attempt a second terminal transition.

## Read first
- `harness/workflow/task_lifecycle.py` — `park`, `fail`, `complete`, `save_state`, `_exec_summary`
- tests added by T21

## Do
1. Extract one terminal-move helper used by `park`, `fail`, and `complete`.
2. Once `shutil.move` succeeds, failures updating `task.json` or writing the review summary are caught and logged; the terminal method does not raise.
3. Log the failed path and exception. Do not claim the state/summary was written.
4. If the move itself fails, propagate the exception and do not write terminal bookkeeping elsewhere.
5. Add tests that monkeypatch state write and summary write to raise after a successful move, asserting the directory remains in the requested terminal location and no exception escapes. Add one move-failure test asserting the exception does escape.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_terminal_move_failures -v
```
Gate must pass.

## Out of scope
Retries, filesystem transactions, queue locking, repairing historical task files, changing directory location as lifecycle authority.

## Done when
Post-move bookkeeping failures are observable but non-fatal, while a failed move remains fatal and creates no false terminal record.
