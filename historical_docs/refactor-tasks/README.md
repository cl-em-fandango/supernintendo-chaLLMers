# Refactor Tasks

> **STATUS: ALL SEVEN LANDED.** These seven tasks took the harness to compliance
> with `CODING_STANDARDS.md`; every chunk is committed on `pi/trunk` and the
> verification gate passes. Automation is no longer held back behind this list —
> `supervisor.py` drives the loop (see `REFACTOR_PLAN.md` §Rollback for how a bad
> chunk is reverted). This page is kept as the record of what each chunk did.

| # | Task | What it did | Risk |
|---|------|-------------|------|
| 1 | `01-enums.md` | Added `TaskStatus`, `Verdict`, `Stage` to `harness/core/enums.py` (additive) | none |
| 2 | `02-external-pi-cli.md` | Moved the pi subprocess into `external/pi_cli.py` | low |
| 3 | `03-external-git-cli.md` | Moved the git subprocess into `external/git_cli.py` | low |
| 4 | `04-core-subpackage.md` | Relocated the leaf modules into `harness/core/` (pure move) | low |
| 5 | `05-workflow-split.md` | Split the monolithic pipeline module into `harness/workflow/` + `StageContext` | medium |
| 6 | `06-cli-split.md` | Split `harness.py` into `harness/cli/` + `harness/composition.py` | medium |
| 7 | `07-e2e-verification.md` | Proved runtime works (no code change) | n/a |

## Rules for executing a task (as run at the time)
1. Read the task's "Read first" files.
2. Make the changes described.
3. Run the "Verify (the gate)" block — every line must pass.
4. Commit with the given message; advance the `pi/last-good` tag.
5. If the gate fails, STOP and report — do not paper over it.

## The gate today

Every change to the harness ends with the same two lines passing:

```bash
cd work/harness
python3 -c "import harness"
python3 harness.py status
```
