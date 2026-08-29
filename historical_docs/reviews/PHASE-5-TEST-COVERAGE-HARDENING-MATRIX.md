# Phase 5: Test Coverage, Verification Rigor & Hardening Discrepancy Matrix

## Mission
Perform an exhaustive verification audit across the full test suite, packaging, gate scripts, and produce the authoritative audit findings & hardening discrepancy matrix.

## Historical Reference Documents
- `historical_docs/AUDIT-2026-08-26.md` (Findings F1 through F4)
- `historical_docs/HARDENING-ANALYSIS-2026-08-26.md` (Gaps G1-G5, Flaws F1-F9, Verification Concerns C1-C6)
- `historical_docs/PLAN-2026-08-26.md`
- `historical_docs/plan-2026-08-26-done/SLICING-MAP.md`

## Cards in Scope
- **Packaging & Gate**: `T40`, `T59`, `T69`
- **Global Regression Suite**: All tests in `tests/`
- **Full Trace Matrix**: Verification against all 77 planned cards and parent contracts.

## Target Production & Test Paths
- `pyproject.toml`
- `scripts/gate.sh`
- `tests/` (all test modules)
- `README.md`
- `config.json`

## Verification & Audit Items

### 1. Gate Execution & State Isolation (Hardening C6, T69)
- Run and analyze the gate script (`scripts/gate.sh` or unittest discovery):
  - Does the gate execute without mutating external operational state (e.g. creating real `work/` logs, stats, or queue entries)?
  - Are all tests in `tests/` passing cleanly? Record total test count and duration.

### 2. Verification Contract Rigor (Hardening C1, C2, C3, C4)
- Audit the unit test implementations:
  - Do tests execute real runtime behavior, or do they rely on trivial AST/source substring checks?
  - Do supervisor tests (`test_supervisor.py`) verify the actual decision logic and command-line construction without violating isolation rules?
  - Are all mock/fake fixtures (e.g. fake `pi`, fake git repos) properly isolated in temporary directories?

### 3. Comprehensive Hardening Discrepancy Matrix
Evaluate and score every finding from the original audit and hardening review:
- **Audit Findings**:
  - `F1`: Supervisor not wired to checkpoint/resume
  - `F2`: `claimed/` leak — tasks invisible
  - `F3`: All supervised harness output discarded
  - `F4`: `task.json` `status` never updated
- **Hardening Gaps & Flaws**:
  - `G1/G2/G3`: Spec assessor fail-open behavior & positive approval requirement
  - `G4`: Claim ownership & concurrency protection
  - `G5`: Terminal moves transactional safety
  - `F1/F2/F3`: In-flight 60k token limit stop & structured handoff data
  - `F4`: Circular intake/workdir resolution
  - `F5/F6`: Claimed-only blocked loop & progress snapshot identity
  - `F7`: Config logger warning emission
  - `F8/F9`: Squash untracked cleanup delta & post-completion branch cleanup
  - `C1-C6`: Runnable test contracts, gate side-effects, test fragmentation

### 4. Packaging, Configuration & Docs Sync (T40, T59)
- Verify `pyproject.toml` Python version requirements and dependencies.
- Verify documentation (`README.md`, docstrings) accurately mirrors current CLI options, config keys, and lifecycle rules.

## Expected Deliverable
Write report to `reviews/REPORT-PHASE-5.md` containing:
- Complete summary table mapping every finding (F1-F4, G1-G5, F1-F9, C1-C6) to: `[Status: FULLY_ADDRESSED / PARTIALLY_ADDRESSED / UNADDRESSED]`, `[Owning Cards]`, `[Test Module & Assertions]`, and `[Residual Risk Assessment]`.
- Summary of full test run results.
