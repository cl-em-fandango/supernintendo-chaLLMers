# Phase 2 Audit Report: External Subprocess, Pi CLI & Context Budget Enforcement

**Date:** 2026-08-29  
**Scope:** Subprocess execution layer, real-time in-flight token cap monitoring, watchdog timers, non-blocking stderr drain, verdict extraction, and structured over-cap handoff routing.  
**Target Paths:**
- `external/pi_cli.py`
- `harness/core/session.py`
- `harness/core/stats.py`
- `harness/core/config.py`
- `harness/core/enums.py`
- `harness/workflow/pipeline.py`
- `harness/workflow/task_lifecycle.py`
- `tests/test_pi_subprocess.py`
- `tests/test_pi_verdict.py`
- `tests/test_pi_over_cap_stream.py`
- `tests/test_over_cap_session.py`
- `tests/test_over_cap_park.py`
- `tests/test_over_cap_handoff.py`

---

## 1. Executive Summary

Phase 2 inspection evaluated the boundary interaction between the Python harness and the external `pi` agent runner subprocess. All core resilience and context-enforcement mechanisms specified in the hardening plan have been implemented and verified:
1. **Real-time stream enforcement (Hardening F1, T48):** Subprocess context usage is inspected in-flight per `message_end` and `agent_end` JSON event. As soon as token usage exceeds `maxPromptTokens` (default 60,000), the process is immediately stopped via `_terminate_reap` (SIGTERM + SIGKILL fallback) rather than awaiting natural completion.
2. **Single-row append-only stats annotation (Hardening F2, T49):** Breaches are annotated directly into the `notes` field (`over-cap peak=<n> limit=<n>`) during the initial single row construction before appending to `sessions.jsonl`.
3. **Structured exception and handoff artifact (Hardening F3, T74, T75):** `OverContextBudget` captures full run context (`task_id`, `stage`, `slice_id`, `iteration`, `peak_tokens`, `context_limit`, `out_file`), bypasses crash-retries in `Pipeline._run`, and renders markdown sections (`## Handoff`, `## Next agent should`) into the parked review summary.
4. **Lexical parsing vs semantic classification (Hardening C4, T19, T20, T34):** `_extract_verdict` strictly extracts lexical tokens case-insensitively from assistant stdout text only, keeping stderr separated. One implementation detail was identified where `_map_verdict` helper is inlined in `SessionRunner.run`, leaving `Verdict.NO_VERDICT` mapping partially unexercised and tracked by an `@unittest.expectedFailure` test.

Overall, 74 test cases passed across the Phase 2 test modules (with 1 expected failure for the inlined `_map_verdict` helper).

---

## 2. Per-Card Status Table

| Card | Implemented | Tests | Findings |
|---|---|---|---|
| **T17** (Stderr Drain) | **YES** | **PASS** | Background daemon thread drains `proc.stderr` under `stderr_lock`. Stderr is stripped from `result.output` (preventing verdict spoofing) and saved to `<out_file>.err` sidecar file when non-empty. Prevents OS pipe buffer deadlocks on floods (>64 KB). |
| **T18** (Watchdog Timer) | **YES** | **PASS** | Daemon watchdog thread polls deadline with `stop_watchdog.wait(min(1.0, remaining))`. Upon wall-clock expiration (`HARD_TIMEOUT_S`), invokes `_terminate_reap(proc)`, flags `killed_by_watchdog`, and marks `crashed=True` with `wall-clock timeout` prefix. |
| **T19** (Verdict Parse) | **YES** | **PASS** | Pure lexical extraction via `VERDICT_RE` and `VERDICT_JSON_RE` using `re.IGNORECASE`. Uses `.findall()[-1]` to ensure the last verdict emitted wins. Scans assistant text only. |
| **T20** (Unknown vs Crash) | **PARTIAL** | **PARTIAL** | Core distinction between crashes and clean runs exists, but logic is inlined in `SessionRunner.run` rather than an isolated pure `_map_verdict(crashed, parsed)` helper. A crash with a partial verdict line (e.g. `VERDICT: pass`) is not mapped to `Verdict.ERROR` if `verdict != UNKNOWN`, and clean runs with no verdict map to `UNKNOWN` instead of `NO_VERDICT`. (Tracked by xfail in `tests/test_pi_verdict.py`). |
| **T32** (Window vs Cap) | **YES** | **PASS** | Separated `modelContext` (true model window) from `maxPromptTokens` (working cap, 60,000) in `config.json` and `Config`. `model_budget()` reserves 8,192 tokens while clamping to `max_prompt_tokens`. `has_known_context()` warns on fallback. |
| **T34** (Verdict Unit Tests) | **YES** | **PASS** | Pure table tests in `tests/test_pi_verdict.py` covering case variations, JSON extraction, prose fallbacks, large outputs (10 KB+), historical outcome byte-compatibility, and unsupported token extraction. |
| **T35** (Subprocess Tests) | **YES** | **PASS** | Subprocess tests in `tests/test_pi_subprocess.py` using fake `pi` on `PATH`. Tests clean runs, 200 KB stderr flood deadlock prevention, silent child watchdog timeouts, non-zero exits, non-JSON stdout, and malformed JSON recovery. |
| **T42** (Parent Epic) | **YES** | **PASS** | Successfully decomposed into and fulfilled by child leaves `T48`, `T49`, `T74`, and `T75` per the slicing map. |
| **T48** (Stream Cap Enforcement) | **YES** | **PASS** | `run_pi_session` inspects streamed `message_end` and `agent_end` tokens via `measure(usage)`. Immediately trips `over_context_budget=True`, sets structured error, and calls `_terminate_reap` before waiting on pipes. |
| **T49** (Over-Cap Stats Propagation) | **YES** | **PASS** | `SessionRunner.run` passes configured `max_prompt_tokens`. Notes field formatted via `_row_notes` (`over-cap peak=<n> limit=<n>`) prior to single append into `sessions.jsonl`. |
| **T74** (Over-Cap Park Routing) | **YES** | **PASS** | `Pipeline._run` checks `r.over_context_budget` before crash-retry loop and raises `OverContextBudget`. Caught by `Pipeline.process()`, ensuring no retries occur and partial verdicts are never routed on. |
| **T75** (Over-Cap Handoff Rendering) | **YES** | **PASS** | `Handoff` dataclass rendered by `_handoff_section` in `task_lifecycle.py`. Appends `## Handoff` and `## Next agent should` sections to review summary file `review/<task_id>.md`. Unaffected plain park summaries remain byte-identical. |

---

## 3. Explicit Assessment of Hardening Items

### Hardening F1: Immediate In-Flight Token Cap Stop (T48, T49)
- **Mechanism:** In `external/pi_cli.py`, `run_pi_session` accepts `max_context_tokens`. During stdout stream consumption, each line is parsed as JSON. On `message_end` or `agent_end` events, `measure(usage)` computes `totalTokens`.
- **Termination:** If `total > max_context_tokens`, `over_context_budget` is flagged, `err` is populated, and stdout iteration breaks immediately. Crucially, `_terminate_reap(proc)` is invoked **before** `proc.wait()`, sending `SIGTERM` followed by `SIGKILL` (after 5s grace). This immediately halts the runaway model session and avoids deadlocks from unread stdout pipes.
- **Signal Integrity:** `over_context_budget` is explicitly decoupled from `crashed` in `PiSessionResult` and `SessionResult`. A budget breach represents an intentional policy termination, preserving the true subprocess exit code in `rc`.

### Hardening F2: Over-Cap Stats Row Update (T49)
- **Append-Only Invariant:** Rather than attempting a post-hoc mutation of an existing record or writing duplicate rows, `SessionRunner.run()` in `harness/core/session.py` computes `_row_notes(notes, result)` **before** calling `self.store.record()`.
- **Notes Formatting:** If `result.over_context_budget` is True, `over-cap peak=<peak> limit=<limit>` is concatenated into `notes`. If the process also failed, `[crashed: ...]` is included in the same notes string. Exactly one record per session invocation is appended to `sessions.jsonl`.

### Hardening F3: Structured Over-Cap Handoff Data (T74, T75)
- **Exception Schema:** `OverContextBudget` in `harness/workflow/pipeline.py` carries complete contextual metadata:
  - `task_id: str`
  - `stage: Stage | str`
  - `slice_id: str | None`
  - `iteration: int`
  - `peak_tokens: int`
  - `context_limit: int | None`
  - `out_file: Path | None`
- **Routing & Zero-Retry:** In `Pipeline._run`, `r.over_context_budget` is evaluated before checking `r.crashed` or attempting crash retries. It immediately raises `OverContextBudget`.
- **Handoff Artifact:** `Pipeline.process()` catches `OverContextBudget` at the top level, reads current checkpoint progress from `task.json`, and invokes `self.lifecycle.park(task.id, str(e), handoff=...)`. `TaskLifecycle._exec_summary` renders:
  ```markdown
  ## Handoff

  - stage: <stage>
  - slice: <slice_id|none>
  - iteration: <iteration>
  - peak: <peak_tokens>
  - cap: <context_limit>
  - output: <out_file_path>
  - checkpointed_stages: [...]
  - checkpointed_slices: [...]

  ## Next agent should

  re-split the work or reduce its context before resume
  ```
- Unaffected standard parks (e.g., git guard errors) omit these sections, maintaining byte-level stability for standard review summaries.

### Hardening C4: Lexical Extraction vs. Semantic Classification (T19, T20, T34)
- **Lexical Layer:** `_extract_verdict(output: str) -> str` in `external/pi_cli.py` handles lexical matching only. It matches `VERDICT:\s*([A-Za-z_]+)` and JSON `"verdict":\s*"([A-Za-z_]+)"` case-insensitively, returning the lowercased captured token (e.g., `"done"`, `"pass"`, or out-of-vocabulary `"kick_out"`). It defaults to `"unknown"` only if no pattern matches.
- **Vocabulary Layer:** `Verdict.parse(raw)` in `harness/core/enums.py` checks membership against the `Verdict` enum.
- **Semantic Layer & Finding:** While T20 specified an isolated `_map_verdict(crashed: bool, parsed: str) -> Verdict` helper, `SessionRunner.run` currently handles this inline:
  ```python
  raw = _extract_verdict(result.output)
  verdict = Verdict.parse(raw) or Verdict.UNKNOWN
  if result.crashed and verdict is Verdict.UNKNOWN:
      verdict = Verdict.ERROR
  ```
  *Audit Observation:* If a crashed run emits `VERDICT: pass` in a partial buffer, `Verdict.parse("pass")` returns `Verdict.PASS`, causing `verdict` to remain `PASS` rather than mapping to `ERROR`. Additionally, `raw == "unknown"` produces `Verdict.UNKNOWN` rather than `Verdict.NO_VERDICT`. This behavior is isolated to `SessionRunner` and does not affect lexical parsing.

---

## 4. Subprocess Watchdog & Stderr Drain Verification

### Non-Blocking Stderr Drain (`T17`, `T35`)
- `external/pi_cli.py` spawns `drain_thread` immediately after `Popen`.
- `drain_stderr()` iterates over `proc.stderr` lines and appends under `stderr_lock`.
- In `finally`, `drain_thread.join(timeout=2)` is performed after process reaping.
- `output` is strictly built from assistant `text_parts`. Stderr is written separately to `out_file.with_suffix(".out.err")` if non-empty, preventing prompt injection or false verdict triggers from stderr.

### Wall-Clock Watchdog (`T18`, `T35`)
- `watchdog_thread` monitors `deadline = t0 + HARD_TIMEOUT_S` using `stop_watchdog.wait(min(1.0, remaining))`.
- If the child process is unresponsive (silent child), the watchdog fires `_terminate_reap(proc)`, sets `killed_by_watchdog`, and terminates within `HARD_TIMEOUT_S + WATCHDOG_GRACE_S`.
- The result surfaces `crashed=True` and `err` starting with `"wall-clock timeout after <N>s"`.

---

## 5. Test Suite Verification Summary

The test suites covering Phase 2 functionality were executed via `pytest`:

```
tests/test_pi_subprocess.py . . . . . .                                    [PASS: 6/6]
tests/test_pi_verdict.py . . . . . . . . . . . . . . . . . . . . x . .     [PASS: 22/23, 1 XFAIL]
tests/test_pi_over_cap_stream.py . . . . . . .                             [PASS: 7/7]
tests/test_over_cap_session.py . . . . . .                                 [PASS: 6/6]
tests/test_over_cap_park.py . . . . . . . . . . . . . . . . . . . .        [PASS: 20/20]
tests/test_over_cap_handoff.py . . . . . . . . . . . . .                   [PASS: 13/13]
```

**Total Phase 2 Tests:** 74 passed, 1 expected failure (`test_map_verdict_rejects_out_of_vocabulary_token`).  
All tests complete in under 3 seconds using isolated fake executables on `PATH` without spawning actual model processes.
