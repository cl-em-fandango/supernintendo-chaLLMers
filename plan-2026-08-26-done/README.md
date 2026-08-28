# plan-2026-08-26 — DONE

Executable cards from `plan-2026-08-26/` that have been actioned (code landed on
`pi/trunk` and the global Gate is green), plus the archived parent/epic contracts
whose leaves have all landed (T04). Cards still open, and the parent/epic archives
with leaves still open (T25, T27, T33, T36, T37, T40, T41, T42, T46), remain in
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
- **T04 EPIC — archived here, NOT executed.** Parent/DO-NOT-EXECUTE contract. Its
  leaves (T72, T73) both landed, so the epic's `[tag]` condition is met; the parent's
  own Verify block was re-run against current `external/git_cli.py` at move time and
  prints `merge abort ok`. It was moved out of `plan-2026-08-26/` so the directory
  driver stops handing a superseded parent to a fresh session — `implement-dir.sh`
  globs `*.md`. Prose path references in `T24` and `T36` were re-pointed at this path.
  The file content is unmodified: it stays the requirement archive and conflict
  reproduction.

## Archived references

- **`SLICING-MAP.md`** — the plan's parent → leaf slicing map, re-slice audit and enqueue rule.
  Not a card: no `Read first`/`Do`/Verify block, owns no code, and is not a leaf of any marked
  parent (every normalization it reports already landed — T33, T40, T48, T52, T64, T69 — and its
  enqueue rule is enforced by `harness/core/enqueue_guard.py`). `implement-dir.sh` globs `*.md`, so
  it was being handed to sessions as a card; it is archived here, with the prose references in
  `T25`, `T42`, `T50`, `T60` and the enqueue-guard docstrings re-pointed at this path.

## Note on the plan's task index

`historical_docs/PLAN-2026-08-26.md` (the "only cross-session status record") is
**stale**: it still marks T02 and T07–T16 as `[ ]` although their code has landed on
`pi/trunk`. The done-list above was derived from actual git history + code + a green
Gate, not from that index.
