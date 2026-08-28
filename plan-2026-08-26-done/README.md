# plan-2026-08-26 — DONE

Executable cards from `plan-2026-08-26/` that have been actioned (code landed on
`pi/trunk` and the global Gate is green). Cards still open, and the non-executable
parent/epic archives (T04, T25, T27, T33, T36, T37, T40, T41, T42, T46), remain in
`plan-2026-08-26/`.

## Verified actioned (17)

| Card | Landed | Notes |
|---|---|---|
| T01 | `8517540` (re-pinned `22e5360`, `394ec3b`, `278eeed`) | baseline / clean tree `[tag]` |
| T02 | `225ca1e` | rotation code + tests landed; truncating the real 186 MB `supervisor.log` is still a **human operator step** |
| T03 | `c9d943c` | `has_branch`/`has_tag` `[tag]` |
| T05 | `aca3907` | `_require_clean` dirty-tree guard `[tag]` |
| T06 | `cb8cd35` | breaker reverts via `git_cli.revert_to_last_good` |
| T07 | `21de7da` | `harness/core/logsink.py` `LogSink` |
| T08 | `b04e981`, `2cc624a` | child output to `logs/children`, never `/dev/null` |
| T09 | `166c00c` | provider `list_claims`/`requeue_claim`/`requeue_all_claims` |
| T10 | `55f9ed3`, `7856d67` | `cmd_run` claims one task, releases its own |
| T11 | `839bef1` | `status` lists `claimed/` |
| T12 | `214afa1` | `requeue-claims` + loop-start reclaim |
| T13 | `b553798` | pure `decide_cycle_action` |
| T14 | `8489473` | supervisor drives `run-task-loop --continue` `[tag]` |
| T15 | `8ff81d3` | no-progress backoff |
| T16 | `0c29e12` | docstring truth |
| T72 | `fed8694`, tests `4b2632f` | squash-conflict cleanup (`tests/test_git_conflict.py`) |
| T73 | `fed8694` | squash-commit-failure cleanup (`tests/test_git_commit_failure.py`) |

Gate at move time: 120 tests OK, imports ok, `harness.py status` rc=0.

## Not moved — caveats

- **T17 — partial, NOT done.** The stderr-drainer code is present in
  `external/pi_cli.py`, but it is the in-flight edit T01 committed *as-is* after
  T17's session was killed at its 3600s timeout. T17 still owns that behaviour and
  its own Verify block has never passed (its boundary tests are T35, not yet
  written). Left in `plan-2026-08-26/`.
- **T04 EPIC — left in place.** Parent/DO-NOT-EXECUTE archive. Its leaves (T72, T73)
  are both done so the epic's `[tag]` condition is met, but the parent file itself is
  a requirement archive per `SLICING-MAP.md` and was never executed, so it stays with
  the other parent contracts.

## Note on the plan's task index

`historical_docs/PLAN-2026-08-26.md` (the "only cross-session status record") is
**stale**: it still marks T02 and T07–T16 as `[ ]` although their code has landed on
`pi/trunk`. The done-list above was derived from actual git history + code + a green
Gate, not from that index.
