# Refactor Tasks

Seven self-contained tasks that take the harness from its current state to full
compliance with `CODING_STANDARDS.md`. Feed them in **one at a time**, in order.
Each task ends with the verification gate passing and a commit; `pi/last-good`
advances only after the gate passes, so `git reset --hard pi/last-good` is the
rollback for any chunk.

| # | Task | What it does | Risk |
|---|------|--------------|------|
| 1 | `01-enums.md` | Add `TaskStatus`, `Verdict`, `Stage` enums (additive) | none |
| 2 | `02-external-pi-cli.md` | Move pi subprocess into `external/pi_cli.py` | low |
| 3 | `03-external-git-cli.md` | Move git subprocess into `external/git_cli.py` | low |
| 4 | `04-core-subpackage.md` | Relocate leaf modules into `core/` (pure move) | low |
| 5 | `05-workflow-split.md` | Split `pipeline.py` into `workflow/` + `StageContext` | medium |
| 6 | `06-cli-split.md` | Split `harness.py` into `cli/` + thin composition root | medium |
| 7 | `07-e2e-verification.md` | Prove runtime works (no code change) — green light for automation | n/a |

## Rules for executing a task
1. Read the task's "Read first" files.
2. Make the changes described.
3. Run the "Verify (the gate)" block — every line must pass.
4. Commit with the given message; advance `pi/last-good`.
5. If the gate fails, STOP and report — do not paper over it.

## Not started until
Task 7 passes. Only then is the supervisor allowed to run automation.
