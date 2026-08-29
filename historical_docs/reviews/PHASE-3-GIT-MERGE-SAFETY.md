# Phase 3: Git Operations, Workspace Isolation & Merge Safety

## Mission
Audit all Git execution layers for repository boundary safety, merge/squash rollback guarantees, branch lifecycle management, and pre/post-merge cleanliness.

## Historical Reference Documents
- `historical_docs/AUDIT-2026-08-26.md`
- `historical_docs/HARDENING-ANALYSIS-2026-08-26.md` (Gaps: F8 pre-merge cleanliness snapshot/untracked cleanup; F9 branch deletion timing)
- `historical_docs/PLAN-2026-08-26.md`
- `historical_docs/plan-2026-08-26-done/SLICING-MAP.md`

## Cards in Scope
- **Git Basics & CLI Wrappers**: `T03`, `T05`, `T06`, `T36` (Parent)
- **Squash Failure Cleanup**: `T04` (Parent), `T72`, `T73`
- **Queue Boundaries & Gates**: `T23`, `T24`
- **Merge Checkpoint & Branch Cleanup**: `T27` (Parent), `T70`, `T71`
- **Git Test Suites**: `T62`, `T63`, `T64`, `T65`

## Target Production & Test Paths
- `external/git_cli.py`
- `harness/core/gitops.py`
- `tests/test_git_*.py`
- `tests/test_merge_checkpoint.py`
- `tests/test_squash_*.py`

## Verification & Audit Items

### 1. Workspace Boundaries & Guarding (T05, T06, T23)
- Verify repository boundary protections:
  - Does the queue directory strictly prevent initializing or nesting a `.git` directory (`T23`)?
  - Does the breaker use git CLI properly and check for dirty trees before performing mutations?
  - Are operations isolated such that untracked queue files never leak into production tree commits?

### 2. Squash Abort & Untracked Path Cleanup (Hardening F8, T04, T72, T73)
- Verify squash failure and conflict recovery:
  - When `git merge --squash` fails or conflicts arise, is cleanup done safely without running destructive `git reset --hard`?
  - Does untracked path cleanup use a pre-merge snapshot/delta mechanism rather than naively wiping all untracked files in the workspace?
  - Are conflict states (`T72`) and commit failure states (`T73`) handled as distinct recovery steps?

### 3. Gate Verification Prior to Merge (T24)
- Verify merge preconditions:
  - Does the workflow refuse to squash-merge if the verification gate has not passed or is unknown/missing?
  - Is fail-closed behavior strictly maintained if gate scripts return nonzero or are absent?

### 4. Post-Completion Branch Cleanup (Hardening F9, T27, T70, T71)
- Verify branch lifecycle:
  - Is checkpoint routing performed cleanly across slice iterations (`T70`)?
  - Is feature branch deletion executed **strictly after** the task reaches terminal `complete()` (`T71`), with clear error boundary so branch cleanup failures do not fail completed tasks?

## Expected Deliverable
Write report to `reviews/REPORT-PHASE-3.md` containing:
- Per-card status table (`[Card] -> [Implemented: YES/NO/PARTIAL] -> [Tests: PASS/FAIL] -> [Findings]`)
- Explicit assessment of Hardening items F8, F9, and safety of git operations
- Test results from running Git test suites in temp/fake fixtures
