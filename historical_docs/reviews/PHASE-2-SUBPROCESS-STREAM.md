# Phase 2: External Subprocess, Pi CLI & Context Budget Enforcement

## Mission
Audit the `pi` subprocess interaction layer, real-time context usage stream monitoring, watchdog timers, stderr non-blocking drain, and verdict extraction logic.

## Historical Reference Documents
- `historical_docs/AUDIT-2026-08-26.md`
- `historical_docs/HARDENING-ANALYSIS-2026-08-26.md` (Gaps: F1 immediate 60k stop; F2 stats row update; F3 over-cap handoff structured data; C4 parser vocabulary)
- `historical_docs/PLAN-2026-08-26.md`
- `historical_docs/plan-2026-08-26-done/SLICING-MAP.md`

## Cards in Scope
- **Subprocess & Stream**: `T17`, `T18`, `T35`
- **Verdict Parsing**: `T19`, `T20`, `T34`
- **Context Cap & Handoff**: `T32`, `T42` (Parent), `T48`, `T49`, `T74`, `T75`

## Target Production & Test Paths
- `external/pi_cli.py`
- `harness/core/session.py`
- `harness/core/stats.py`
- `harness/core/config.py`
- `tests/test_pi_subprocess.py`
- `tests/test_pi_verdict.py`
- `tests/test_stream_context_cap.py`
- `tests/test_over_cap_*.py`

## Verification & Audit Items

### 1. In-Flight Context Cap Enforcement (Hardening F1, T48, T49)
- Verify `external/pi_cli.py`:
  - Is context usage checked **in-flight during stream parsing** (e.g., on `message_end` / `agent_end` events)?
  - Is the subprocess terminated immediately when `total_tokens` / `peak_tokens` exceeds the configured cap (e.g. 60,000 tokens), rather than waiting for natural completion?
  - Does the runner raise an explicit, typed exception or structured signal (`OverContextBudget`)?

### 2. Over-Cap Stats & Structured Handoff (Hardening F2, F3, T74, T75)
- Verify stats and task routing on budget breach:
  - Is the stats row annotated with `over-cap` without breaking append-only store invariants?
  - Does the handoff artifact contain required structured data (stage, slice, iteration, last output path, checkpoints)?
  - Does the task get routed to `parked/` with explicit context-exhausted rationale?

### 3. Subprocess Watchdog & Non-Blocking Stderr Drain (T17, T18)
- Verify `external/pi_cli.py`:
  - Does stderr draining use non-blocking reads or separate thread/drain loop to avoid pipe buffer deadlocks?
  - Does the wallclock watchdog enforce hard timeouts and terminate runaway subprocesses cleanly?

### 4. Verdict Lexical Parsing vs Semantic Classification (Hardening C4, T19, T20, T34)
- Verify verdict handling:
  - Does `parse_verdict()` perform strict lexical extraction (returning raw parsed token or normalized string)?
  - Does `SessionRunner` / `SessionResult` map out-of-vocabulary or malformed tokens to `Verdict.UNKNOWN` without ambiguous fallbacks?
  - Are crash scenarios distinctly marked vs protocol/no-verdict scenarios?

## Expected Deliverable
Write report to `reviews/REPORT-PHASE-2.md` containing:
- Per-card status table (`[Card] -> [Implemented: YES/NO/PARTIAL] -> [Tests: PASS/FAIL] -> [Findings]`)
- Explicit assessment of Hardening items F1, F2, F3, and C4
- Verification of test suites under `tests/` covering subprocess and stream behaviors
