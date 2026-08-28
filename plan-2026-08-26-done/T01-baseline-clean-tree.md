# T01 — Get the working tree clean and pin the baseline

**Wave 0** · depends: none (run first) · `[tag]` · findings: F12, F13 · blocks: everything

## Context
`git status` shows ` M README.md`, ` M config.json`, ` D supervisor.sh`. The harness's own
rollback mechanism is `git reset --hard pi/last-good`, and wave 0 adds a dirty-tree guard
that *refuses* destructive git ops on a dirty tree. With these three edits outstanding,
both the rollback and the guard are meaningless. Decide and clean, once, now.

## Read first
- `git status --short`, `git diff -- config.json README.md` (read the actual diff, all of it)
- `README.md` §"Token budget", `config.json` (`tokenBudget`, `modelContext`)
- `AUDIT-2026-08-26.md` §F12, §F10 (last two bullets)

## Do
1. Resolve **[D1]** with the human. Default if no human is available: **commit** all three
   as-is — do not discard someone else's in-flight edit, and do not "fix" them here.
2. `config.json`: add a trailing newline only (no value changes; value semantics are T32's job).
3. `README.md`: add a one-line `> STATUS: partly out of date — see PLAN-2026-08-26.md §Open decisions`
   note under the title. Do **not** rewrite the README (T33 owns that).
4. Confirm `supervisor.sh` has a real replacement before committing its deletion:
   `python3 supervisor.py status` must run and print a status line. If it errors, STOP + handover.
5. Record the baseline in this file's task index: put `BASELINE=<sha>` under the T01 line.

## Verify
```bash
cd /home/donald/work/harness
git status --short                      # only AUDIT-*.md / PLAN-*.md / plan-*/ may remain untracked
python3 supervisor.py status ; echo rc=$?   # rc=0
git rev-parse --short pi/last-good
```
Gate from `PLAN-2026-08-26.md` must pass. Then `git tag -f pi/last-good pi/trunk`.

## Out of scope
README rewrite, config value/semantics changes, supervisor.log rotation (T02), queue
cleanup (T25), any code change at all.

## Done when
`git status --short` is clean except audit/plan markdown; `pi/last-good` == `pi/trunk`;
`BASELINE=<sha>` recorded in `PLAN-2026-08-26.md`.
