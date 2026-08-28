# plan-2026-08-26 — DONE

Executable cards from `plan-2026-08-26/` that have been actioned (code landed on
`pi/trunk` and the global Gate is green), plus the archived parent/epic contracts
whose leaves have all landed (T04) or whose leaves are still open but which the
enqueue guard refuses as `DO NOT EXECUTE` parents (T25, T37, T41). Cards still open, and the
parent/epic archives with leaves still open (T27, T33, T40, T42, T46),
remain in `plan-2026-08-26/`.

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

- **T37 EPIC — archived here, NOT executed.** Parent/DO-NOT-EXECUTE contract: line 3 reads
  "Claim handlers are T66, run cleanup is T67, parser/dispatch is T68, and autonomous read-only
  counting is tested with T58", so the file is a requirement archive, not a unit of work, and
  `harness/core/enqueue_guard.py` refuses any body carrying that marker. Its four leaves each own a
  *different* test module (`tests/test_handlers_claims.py`, `tests/test_handlers_run.py`,
  `tests/test_cli_surface.py`, `tests/test_autonomous_count.py`) rather than the single
  `tests/test_handlers.py` the epic names, so no single feature could be implemented from it; and its
  case **h** asserts `provider.count_pending()`, which is not in the tree until T58 lands. Those four
  leaves are still open in `plan-2026-08-26/` and were left there untouched — executing the sequence
  would be four features in one session. Moved out of `plan-2026-08-26/` because
  `implement-dir.sh` globs `*.md` and was handing a refused parent to fresh sessions. The only edit
  to the file is a note recording why it is not actionable; the cases (a–h) are untouched and remain
  the requirement archive for T58 and T66–T68.

- **T25 EPIC — archived here, NOT executed.** Parent/DO-NOT-EXECUTE contract: line 3 reads
  "Execute T76 → T77 → T61", so the file is a requirement archive, not a unit of work, and
  `harness/core/enqueue_guard.py` refuses any body carrying that marker. Its leaves are still open
  in `plan-2026-08-26/` and were left there untouched — executing the sequence would be three
  features in one session. Moved out of `plan-2026-08-26/` because `implement-dir.sh` globs `*.md`
  and was handing a refused parent to fresh sessions. File content is unmodified.

- **T41 EPIC — archived here, NOT executed.** Parent/DO-NOT-EXECUTE contract: line 3 reads "Its five
  independent behaviors are T54, T55, T56, T57, and T58", so the file is a requirement archive, not a
  unit of work, and `harness/core/enqueue_guard.py` refuses any body carrying that marker. Its five
  items are five independent features, each owned by its own still-open leaf (T54 `resume --fresh`,
  T55 the implementer-model fix, T56 the `-review.md` note path, T57 `AllAttemptsCrashed`, T58
  `count_pending()`), all left untouched in `plan-2026-08-26/` — actioning the epic would be five
  features in one session, which its own Context section forbids. Item 4 is additionally blocked on
  T42, also still open: `OverContextBudget` is not in `harness/workflow/pipeline.py`, so there is no
  sibling exception shape for it to follow. None of the five behaviors had landed at move time —
  verified by grep: `--fresh` on `run-task` only, `resume_task()` without a `fresh` parameter,
  `_review_loop` still choosing `self.cfg.implementer if kind is ReviewKind.TECH else
  self.cfg.model`, one shared `artifacts/progress/slice-{sid}.md` path, `autonomous.py` still calling
  `fetch_pending()`, and no `count_pending` on either provider. Moved out of `plan-2026-08-26/`
  because `implement-dir.sh` globs `*.md` and was handing a refused parent to fresh sessions. The
  only edit to the file is a note recording why it is not actionable; the five items are untouched
  and remain the requirement archive for T54–T58. That note keeps its extra ticket ids outside the
  guard's 10-line header window, so a refusal still names exactly T54–T58.

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
