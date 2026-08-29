# plan-2026-08-26 — DONE

Executable cards from `plan-2026-08-26/` that have been actioned (code landed on
`pi/trunk` and the global Gate is green), plus the archived parent/epic contracts
whose leaves have all landed (T04) or whose leaves are still open but which the
enqueue guard refuses as `DO NOT EXECUTE` parents (T25, T37, T41, T42). Cards still open, and the
parent/epic archives with leaves still open (T27, T33, T40, T46),
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

## Blocked — moved unactioned (1)

- **T61 — queue-audit CLI, NOT executed.** Its whole `Do` is dispatch plus report persistence over
  `audit_queue(cfg)` / `render_audit(cfg)`, and that report is not in the tree: `harness/workflow/queue_audit.py`
  is created by T76 and extended by T77, and both leaves are still open in `plan-2026-08-26/`.
  Verified by grep at move time — no `queue_audit` module, no `audit_queue`/`render_audit`
  definition, no `cmd_queue_audit`, no `tests/test_queue_audit_inventory.py` or
  `tests/test_queue_audit_artifacts.py`. T61's own *Out of scope* ("No anomaly logic") rules out
  writing the audit here, so actioning it would be three features in one session. Unlike the epic
  archives above this is a real leaf, so the note added to the file deliberately avoids the
  `DO NOT EXECUTE` phrase the enqueue guard scans for: the card stays enqueueable and becomes
  actionable once T76 → T77 land. The requirement text is otherwise unmodified.

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

- **T42 EPIC — archived here, NOT executed.** Parent/DO-NOT-EXECUTE contract: line 3 reads "sliced
  into executable leaves T48 → T49 → T74 → T75", so the file is a requirement archive, not a unit of
  work, and `harness/core/enqueue_guard.py` refuses any body carrying that marker. Its four leaves are
  four separate features — T48 the in-stream trip in `external/pi_cli.py`, T49 the propagation through
  `PiSessionResult`/`SessionResult` and the stats note, T74 the `OverContextBudget` raise/park routing
  in `Pipeline`, T75 the handoff rendering — each owning its own test module
  (`test_pi_over_cap_stream`, `test_over_cap_session`, `test_over_cap_park`, `test_over_cap_handoff`),
  so the epic's single Verify block is the union of four cards' gates and actioning it would be four
  features in one session. None of the four had landed at move time — verified by grep: no
  `over_budget_limit` in `harness/core/config.py`, no `max_context_tokens` on `run_pi_session()`, no
  `over_context_budget`/`context_limit` on `PiSessionResult` or `SessionResult`, no `OverContextBudget`
  in `harness/workflow/pipeline.py`, no handoff parameter on `TaskLifecycle.park()`, and none of those
  four test modules exists. The four leaves stay open in `plan-2026-08-26/`, untouched and in order.
  The `Do` list is additionally stale relative to the re-slice — it still has one leaf owning park
  *and* rendering, which `SLICING-MAP.md` records as rejected. Moved out of `plan-2026-08-26/` because
  `implement-dir.sh` globs `*.md` and was handing a refused parent to fresh sessions — same treatment
  as T25, T37 and T41. The only edit to the file is a note recording why it is not actionable; the
  note keeps its extra ticket ids outside the guard's 10-line header window, so a refusal still names
  exactly T48 → T49 → T74 → T75. The prose reference in `T32`'s *Read first* was re-pointed here.

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
