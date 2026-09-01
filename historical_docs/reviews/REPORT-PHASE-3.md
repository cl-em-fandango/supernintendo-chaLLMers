# Phase 3 Audit Report: Git Operations, Workspace Isolation & Merge Safety

## Executive Summary
This audit evaluated all Git operations, repository boundary protections, merge/squash rollback mechanisms, gate enforcement, and branch lifecycle management across `external/git_cli.py`, `harness/core/gitops.py`, `harness/workflow/pipeline.py`, and related test suites. All 121 git-specific unit and integration tests passed without error. Hardening items F8 and F9 are fully and correctly implemented.

---

## 1. Per-Card Status Table

| Card | Implemented | Tests | Findings & Audit Summary |
|---|---|---|---|
| **T03** (Git Tag Lookup) | YES | PASS (`test_git_refs.py`) | `has_tag()` and `has_branch()` explicitly probe `refs/tags/` and `refs/heads/` respectively. `LAST_GOOD_TAG` (`pi/last-good`) lookups correctly resolve tags. |
| **T04** (Merge Abort Parent Epic) | YES (via T72, T73) | PASS (`test_git_conflict.py`, `test_git_commit_failure.py`) | Superseded parent epic. Cleaned up without blanket `reset --hard` or broad untracked deletion. |
| **T05** (Dirty Tree Guard) | YES | PASS (`test_git_refs.py`, `test_git_conflict.py`, `test_supervisor_breaker.py`) | `_require_clean()` checks `dirty_paths()` (porcelain status) before destructive operations (`revert_to_last_good`, cross-branch checkout, branch deletion). Refuses mutations if uncommitted paths exist. |
| **T06** (Breaker via Git CLI) | YES | PASS (`test_supervisor_breaker.py`) | `supervisor.py` contains zero raw git subprocess invocations, delegating all rollback logic through `external.git_cli.revert_to_last_good`. Revert target (`tag:pi/last-good` or `HEAD~1`) is returned and logged. |
| **T23** (No Git Init in Queue) | YES | PASS (`test_queue_git_guard.py`, `test_git_refs.py`) | `is_under_queue()` uses path resolution to prevent prefix collisions. `pipeline.py` checks workdir before `ensure_branch` and parks the task if workdir is inside `queue_dir` or non-existent, preventing `.git` creation in the queue tree. |
| **T24** (Refuse Merge Without Gate) | YES | PASS (`test_gate_not_applicable.py`) | `gate_applies()` validates harness files (`harness.py`, `harness/composition.py`) before executing any git write. Non-applicable repositories raise `GateNotApplicable` fail-closed, leaving trunk and feature branches untouched. |
| **T27** (Merge Checkpoint Epic) | YES (via T70, T71) | PASS (`test_merge_checkpoint.py`, `test_branch_cleanup.py`) | Superseded parent epic. Slices and merge stages checkpointed independently; branch cleanup occurs only post-completion. |
| **T36** (Git Boundary Tests Epic) | YES (via T62–T65) | PASS (`test_git_*.py`, `test_gate_not_applicable.py`) | Superseded parent epic; all sub-test suites fully implemented and verified. |
| **T62** (Git Ref & Branch Tests) | YES | PASS (`test_git_refs.py`) | 17 tests covering branch/tag differentiation, idempotent `ensure_branch`, and `is_under_queue` path boundary isolation. |
| **T63** (Merge & Gate Rollback Tests) | YES | PASS (`test_git_merge_gate.py`) | 18 tests covering happy-path squash merge, `pi/last-good` tag advancement, failed-gate rollback to tag or `HEAD~1`, and branch cleanup. |
| **T64** (Conflict Cleanup & Dirty Revert) | YES | PASS (`test_git_conflict.py`) | 24 tests validating pre-merge delta recording (`_added_paths`), targeted untracked removal (`_discard_added`), symlink safety, directory pruning, and dirty tree revert refusal. |
| **T65** (Gate-Not-Applicable Tests) | YES | PASS (`test_gate_not_applicable.py`) | 24 tests verifying pre-mutation refusal, no merge residue, no git subprocess writes, and preservation of feature branches. |
| **T70** (Merge Checkpoint Routing) | YES | PASS (`test_merge_checkpoint.py`) | 5 tests ensuring `CheckpointStage.MERGE` is persisted upon successful merge, preventing duplicate merge attempts upon pipeline resume. |
| **T71** (Post-Complete Branch Cleanup) | YES | PASS (`test_branch_cleanup.py`) | 15 tests ensuring `pi/<task_id>` deletion occurs strictly after `lifecycle.complete()`, with an exception boundary preventing cleanup errors from failing completed tasks. |
| **T72** (Squash Conflict Cleanup) | YES | PASS (`test_git_conflict.py`) | Verifies `abort_merge` resets index (`git reset -q`), restores worktree (`git checkout -q -- .`), and purges only branch-added paths. |
| **T73** (Squash Commit Failure Cleanup) | YES | PASS (`test_git_commit_failure.py`) | 3 tests verifying recovery and evidence preservation when squash succeeds but `git commit` fails. |

---

## 2. Assessment of Hardening Items & Git Operation Safety

### Hardening Item F8: Pre-Merge Cleanliness Snapshot & Untracked Cleanup
- **Mechanism**:
  1. `_require_clean(workdir)` ensures worktree is pristine before the merge begins.
  2. `_added_paths(workdir, trunk, branch)` records branch-introduced additions (`git diff --name-only --diff-filter=A trunk...branch`) prior to merge execution.
  3. In case of merge conflict or commit failure, `abort_merge(workdir, added)` executes `git reset -q` (clearing unmerged index entries without destructive `--hard`) and `git checkout -q -- .`.
  4. `_discard_added(workdir, added)` removes only files present in the pre-merge addition list that remain untracked, performing lexical boundary checks to prevent worktree directory escape and unlinking symlinks without traversal.
- **Safety Guarantee**: Naive sweeping of untracked files (`git clean -fd` or wiping `??` paths) is completely avoided. Unrelated untracked files are preserved.

### Hardening Item F9: Branch Lifecycle & Deletion Timing
- **Mechanism**:
  1. `merge_to_trunk()` never deletes the feature branch (`pi/<task_id>`). If the gate fails, trunk reverts to `pi/last-good` / `HEAD~1` while keeping the branch intact for forensic inspection or resumed execution.
  2. `_checkpoint_merge()` records `CheckpointStage.MERGE` in `task.json`.
  3. `pipeline.stage_holistic()` executes `lifecycle.complete()` before calling `_cleanup_branch()`.
  4. `_cleanup_branch()` wraps `cleanup_branch()` in a `try...except` block, logging warnings on failure without propagating exceptions or changing task state from `done`.
  5. `cleanup_branch()` uses `git branch -D` (handling squash commits where ancestry is not preserved) and checks out `trunk` first if HEAD is currently on the feature branch (guarded by `_require_clean`).
- **Safety Guarantee**: Branch deletion is crash-safe, idempotent, and isolated from task completion state.

### Queue Directory & Repository Boundaries (T23, T24)
- `is_under_queue()` uses absolute path containment checks (`queue_dir in workdir.parents or workdir == queue_dir`), preventing accidental prefix matches (e.g. `/queue-other`).
- `pipeline.py` enforces that workdirs inside `queue_dir` or nonexistent paths are rejected before `ensure_branch()` can run `git init`.
- `gate_applies()` enforces fail-closed gate verification prior to any checkout or merge operation.

---

## 3. Git Test Suite Verification Results

Execution command:
```bash
pytest tests/test_git_refs.py tests/test_git_merge_gate.py tests/test_git_conflict.py tests/test_git_commit_failure.py tests/test_gate_not_applicable.py tests/test_queue_git_guard.py tests/test_merge_checkpoint.py tests/test_branch_cleanup.py tests/test_supervisor_breaker.py -v
```

### Result Summary
- **Total Tests Collected**: 121
- **Total Passed**: 121 (100%)
- **Failed / Errored**: 0
- **Duration**: 1.83s

All test fixtures execute in isolated temporary directories using local git repositories without touching production workdirs or external resources.

---

## 4. Conclusion
The Git operation boundaries, queue isolation guards, squash abort rollbacks, gate enforcement, and branch lifecycle management conform to all specified hardening requirements and safety standards.
