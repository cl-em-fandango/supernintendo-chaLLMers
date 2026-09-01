# Phase 4: Queue Management, Claims, Supervisor & Audit Tooling

## Mission
Audit queue operations, claim metadata and ownership invariants, supervisor orchestration loops, logging sinks, and the read-only queue audit command.

## Historical Reference Documents
- `historical_docs/AUDIT-2026-08-26.md` (Findings: F1 supervisor wiring; F2 claimed/ leak; F3 supervised output discarded)
- `historical_docs/HARDENING-ANALYSIS-2026-08-26.md` (Gaps: G4 claim ownership/concurrency; F5 inaccessible claims as work; F6 progress snapshot identity; F7 config logger)
- `historical_docs/PLAN-2026-08-26.md`
- `historical_docs/plan-2026-08-26-done/SLICING-MAP.md`

## Cards in Scope
- **Logging & Sinks**: `T02`, `T07`, `T08`
- **Claim Visibility & Recovery**: `T09`, `T10`, `T11`, `T12`
- **Claim Ownership & Concurrency**: `T46` (Parent), `T51`, `T52`, `T53`
- **Supervisor Loops & Decisions**: `T13`, `T14`, `T15`, `T38`, `T44`, `T47`
- **Read-Only Queue Audit**: `T25` (Parent), `T60` (Parent), `T61`, `T76`, `T77`
- **Handler Test Suites & Units**: `T33`, `T37` (Parent), `T39`, `T66`, `T67`, `T68`

## Target Production & Test Paths
- `supervisor.py`
- `harness/core/providers.py`
- `harness/cli/handlers.py`
- `harness/cli/parser.py`
- `harness/composition.py`
- `tests/test_supervisor.py`
- `tests/test_handlers.py`
- `tests/test_claim_*.py`
- `tests/test_queue_audit_*.py`

## Verification & Audit Items

### 1. Claim Visibility, Leakage & Ownership (Audit F2, Hardening G4, T09-T12, T51-T53)
- Verify `queue/claimed/`:
  - Does `harness status` display `claimed/` items so they are never invisible?
  - Does `fetch_pending(claim=True)` prevent task loss or orphan leaks if a run terminates prematurely?
  - Does claim metadata attach a concrete `run_owner_id` (T51/T52) to avoid cross-process claim hijacking?
  - Are stale claims safely reclaimed only by owner or operator policies (T53)?

### 2. Supervisor Loop, Output Sinks & Wiring (Audit F1, F3, Hardening F5, F6, T08, T13-T15, T44, T47)
- Verify `supervisor.py` and child execution:
  - Is child output directed to real log sinks (`work/logs/harness.log` with rotation) rather than `DEVNULL` (T08)?
  - Is supervisor correctly wired to run `--continue` / `run-task-loop` rather than restarting or skipping in-flight work (Audit F1)?
  - Does the cycle decision properly identify `claimed-only` blocked state (T44, Hardening F5) instead of looping indefinitely on `WORK`?
  - Does the progress snapshot evaluate task identity (T47, Hardening F6) rather than superficial count tuples to determine backoff?

### 3. Read-Only Queue Audit Tooling (Hardening C6, T25, T60, T61, T76, T77)
- Verify `harness queue-audit`:
  - Is the audit strictly read-only without modifying files, moving tasks, or changing repository state?
  - Does `T76` verify inventory and basic structure (counts, directory structure, `.git` absence)?
  - Does `T77` detect duplicate slugs, artifact anomalies, and malformed state files?

## Expected Deliverable
Write report to `reviews/REPORT-PHASE-4.md` containing:
- Per-card status table (`[Card] -> [Implemented: YES/NO/PARTIAL] -> [Tests: PASS/FAIL] -> [Findings]`)
- Explicit assessment of Audit findings F1, F2, F3, and Hardening items G4, F5, F6
- Verification of test suites under `tests/` covering supervisor, handlers, and claims
