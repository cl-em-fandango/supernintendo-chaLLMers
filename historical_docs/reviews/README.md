# Review Index & Parallel Dispatch Plan

This directory contains standalone phase review briefs for executing a full verification of the work executed against the scope defined in `historical_docs/AUDIT-2026-08-26.md`, `historical_docs/PLAN-2026-08-26.md`, `historical_docs/HARDENING-ANALYSIS-2026-08-26.md`, and the card folders (`historical_docs/plan-2026-08-26/`, `historical_docs/plan-2026-08-26-done/`, `historical_docs/plan-2026-08-26-for-test/`).

## Review Matrix

| Phase File | Focus Area | Primary Target Modules | Cards Governed |
|---|---|---|---|
| [`PHASE-1-WORKFLOW-LIFECYCLE.md`](./PHASE-1-WORKFLOW-LIFECYCLE.md) | Pipeline, Stages, Checkpoints, Resume, State, Enums | `harness/workflow/`, `harness/core/enums.py` | T01, T16, T21, T22, T26, T28, T29, T30, T31, T43, T45, T54, T55, T56, T57, T58 |
| [`PHASE-2-SUBPROCESS-STREAM.md`](./PHASE-2-SUBPROCESS-STREAM.md) | Subprocess, Watchdog, Context Cap, Parser, Handoff | `external/pi_cli.py`, `harness/core/session.py` | T17, T18, T19, T20, T32, T34, T35, T42, T48, T49, T74, T75 |
| [`PHASE-3-GIT-MERGE-SAFETY.md`](./PHASE-3-GIT-MERGE-SAFETY.md) | Git Ops, Workspace Boundaries, Squash Abort/Rollback, Gate | `external/git_cli.py`, `harness/core/gitops.py` | T03, T04, T05, T06, T23, T24, T27, T36, T62, T63, T64, T65, T70, T71, T72, T73 |
| [`PHASE-4-QUEUE-SUPERVISOR-AUDIT.md`](./PHASE-4-QUEUE-SUPERVISOR-AUDIT.md) | Supervisor, Claims, Ownership, Log Sinks, Queue Audit | `supervisor.py`, `harness/core/providers.py`, `harness/cli/` | T02, T07, T08, T09, T10, T11, T12, T13, T14, T15, T25, T33, T37, T38, T39, T44, T46, T51, T52, T53, T60, T61, T66, T67, T68, T76, T77 |
| [`PHASE-5-TEST-COVERAGE-HARDENING-MATRIX.md`](./PHASE-5-TEST-COVERAGE-HARDENING-MATRIX.md) | Verification Rigor, Test Suite, Gate Script, Hardening Gap Matrix | `tests/`, `pyproject.toml`, `scripts/gate.sh` | T40, T59, T69, G1-G5, F1-F9, C1-C6, All Test Suites |

## Execution Protocol for Sub-Agents

1. Each sub-agent receives exactly one `PHASE-*.md` file.
2. The agent reads the referenced historical documents and target code/test files.
3. The agent must verify actual code implementation and runtime test assertions (do not assume passing without checking logic).
4. Results are written to a corresponding report file: `reviews/REPORT-PHASE-<N>.md`.
