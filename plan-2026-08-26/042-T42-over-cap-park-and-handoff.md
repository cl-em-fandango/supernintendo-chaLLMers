# T42 — Over-cap hardening epic (superseded)

> **DO NOT EXECUTE THIS FILE AS A CARD.** It is retained as the complete contract and is recursively
> sliced into executable leaves T48 → T49 → T50. Those three leaves jointly own every criterion below.

**Wave 8** · depends: T17, T18, T20, T26, T32 · finding: F10 + decision D2

## Context
D2 requires an immediate stop when context usage goes over 60,000 tokens. Checking `peak_tokens` after `run_pi_session()` returns is too late: the model may continue consuming context for the rest of the session. Enforcement must occur while JSON usage events are read. The same structured result must carry enough data for stats and the handoff.

## Read first
- `external/pi_cli.py` — event loop, watchdog termination, `PiSessionResult`
- `harness/core/session.py` — stats record and `SessionResult`
- `harness/core/config.py` — `max_prompt_tokens`
- `harness/workflow/pipeline.py` — `_run`, `process`
- `harness/workflow/task_lifecycle.py` — `park`, `_exec_summary`

## Do
1. Add `over_budget_limit` to `Config`, equal to `max_prompt_tokens`. There is one threshold only.
2. Add optional `max_context_tokens: int | None = None` to `run_pi_session()`.
3. While parsing every `message_end` and `agent_end` usage value, update `peak_tokens`. On the first value strictly greater than the limit:
   - set `over_context_budget = True`;
   - set an error describing measured peak and limit;
   - terminate the child process immediately using one shared terminate/reap helper also usable by the watchdog;
   - stop consuming further model work and return after the process is reaped.
   Exactly 60,000 does not trip; 60,001 does.
4. Add `over_context_budget: bool = False` and `context_limit: int | None = None` to `PiSessionResult` and `SessionResult`. This flag is distinct from a crash; preserve the actual return code separately.
5. `SessionRunner.run()` passes `cfg.over_budget_limit` into `run_pi_session()`. Because it owns the stats write, it appends `over-cap peak=<n> limit=<n>` to that same row's notes before recording it. Do not append a duplicate stats row and do not rewrite JSONL.
6. In `Pipeline._run`, inspect `result.over_context_budget` before crash retry logic. Raise one `OverContextBudget` exception carrying task id, stage, slice id, iteration, peak, limit and `out_file`. Never retry an over-cap result and never route on its partial verdict.
7. `Pipeline.process()` catches the exception once and parks with a structured handoff object. Extend `TaskLifecycle.park()` with an optional handoff dataclass, not a parsed reason string.
8. The existing review markdown gains, only for this case:
   - `## Handoff` with stage, slice, iteration, peak, cap, output path, `checkpointed_stages`, and `checkpointed_slices`;
   - `## Next agent should` stating that work must be re-split or context reduced before resume.
9. Add `tests/test_over_cap_trip.py` using a fake `pi` that emits a 60,001-token usage event and then sleeps. Assert the process is terminated promptly, only one runner call occurs, the single stats row contains `over-cap`, the task parks, and the handoff contains every required field. Add the exact-60,000 boundary case.

## Verify
```bash
cd /home/donald/work/harness
python3 -m unittest tests.test_over_cap_trip -v
python3 -m unittest tests.test_pi_subprocess -v
```
Gate must pass.

## Out of scope
Changing the 60,000 cap, per-model caps, context summarization, automatic resume/unpark, report rendering, all-attempts-crashed behavior (T41).

## Done when
A streamed 60,001 usage event terminates the child without waiting for natural exit; no retry occurs; exactly one stats row is written with an over-cap note; the task parks with a complete markdown handoff. A 60,000 event does not trip.
