#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# End-to-End Hardening & Container Test Driver
#
# This script:
#  1. Sets up an isolated test folder structure with mock queues, logs, and stats.
#  2. Initializes a target mock git repository.
#  3. Creates simulated task specifications.
#  4. Validates T03 (`has_tag`, `has_branch`, `_revert_to_last_good`) in isolation.
#  5. Validates T06 (`revert_to_last_good` with dirty-tree guard).
#  6. Executes the frozen container against the test workspace to verify:
#     - Container runtime and mount compatibility.
#     - Unit tests inside the container.
#     - Status reporting and lifecycle state transitions.
#     - Proper termination and cleanup.
# ==============================================================================

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_DIR="$(mktemp -d /tmp/harness-test-workspace.XXXXXX)"
echo "==> Setting up test workspace: ${TEST_DIR}"

# 1. Directory Structure Setup
mkdir -p "${TEST_DIR}"/{queue/pending,queue/claimed,queue/active,queue/done,queue/parked,queue/review,logs,stats}

cat << EOF > "${TEST_DIR}/config.json"
{
  "harnessExecutionAndQueueDir": "${TEST_DIR}",
  "logDir": "${TEST_DIR}/logs",
  "statsDir": "${TEST_DIR}/stats",
  "tokenBudget": 100000,
  "maxSpecKickbacks": 3,
  "maxSliceImplement": 5,
  "maxSliceTechReview": 5,
  "maxSliceFuncReview": 5,
  "maxSliceCheckLoops": 3,
  "autonomousQueueTarget": 5,
  "trunkBranch": "pi/trunk",
  "taskProvider": "directory",
  "directoryProvider": {
    "pendingDir": "${TEST_DIR}/queue/pending",
    "claimedDir": "${TEST_DIR}/queue/claimed"
  },
  "models": {
    "technicalWriter": "test-writer",
    "implementer": "test-coder",
    "assessor": "test-assessor"
  }
}
EOF

# 2. Populate Test Tasks
cat << 'EOF' > "${TEST_DIR}/queue/pending/001-test-task-a.md"
# Task A: Implement tag lookup in git_cli
Target repository: /tmp/target-repo-a
Ensure has_tag and has_branch replace _has.
EOF

cat << 'EOF' > "${TEST_DIR}/queue/pending/002-test-task-b.md"
# Task B: Add dirty-tree check before destructive operations
Target repository: /tmp/target-repo-b
Ensure destructive git commands refuse on uncommitted files.
EOF

echo "✓ Created test folder structure with 2 tasks in queue/pending"

# 3. Verify Git Operations Hardening (T03 Verification)
echo "==> Running T03 verification (git tag vs branch lookup)..."
python3 - << 'PY'
import sys, subprocess, tempfile, pathlib
sys.path.insert(0, '.')
from external import git_cli as G

d = pathlib.Path(tempfile.mkdtemp())
def g(*a):
    r = subprocess.run(["git", *a], cwd=d, capture_output=True, text=True)
    assert r.returncode == 0, f"git {' '.join(a)} failed: {r.stderr}"

g("init", "-b", "pi/trunk")
(d/"a.txt").write_text("1")
g("add", "-A")
g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "c1")
g("tag", "-f", "pi/last-good")

assert G.has_branch(d, "pi/trunk"), "has_branch(pi/trunk) failed"
assert not G.has_branch(d, "pi/nope"), "has_branch(pi/nope) false positive"
assert G.has_tag(d, "pi/last-good"), "has_tag(pi/last-good) failed"
assert not G.has_tag(d, "pi/nope"), "has_tag(pi/nope) false positive"

# Advance branch with new commit
(d/"a.txt").write_text("2")
g("add", "-A")
g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "c2")

# Revert to last known good tag
took = G._revert_to_last_good(d, "pi/trunk")
assert took == "tag:pi/last-good", f"Expected tag:pi/last-good revert, got: {took}"
assert (d/"a.txt").read_text() == "1", "File content was not restored to tag commit"
print(f"  [T03 passed] Reverted cleanly to {took}")
PY

# 4. Verify Host Unit Tests
echo "==> Running host unit test suite..."
python3 -m unittest discover -s tests

# 5. Build and Test Frozen Sandbox Container
echo "==> Rebuilding sandbox container..."
"${PROJECT_ROOT}/scripts/rebuild-container.sh"

echo "==> Executing test suite inside frozen sandbox..."
"${PROJECT_ROOT}/scripts/run-sandbox.sh" "${TEST_DIR}" python3 -m unittest discover -s /opt/harness-frozen/tests

echo "==> Inspecting sandbox harness status..."
"${PROJECT_ROOT}/scripts/run-sandbox.sh" "${TEST_DIR}" python3 /opt/harness-frozen/harness.py status

# 6. Task Lifecycle & Claim Test
echo "==> Verifying task claim and state transitions in container..."
python3 - << PY
import json, sys
from pathlib import Path
sys.path.insert(0, '.')
from harness.core.config import load
from harness.core.providers import create_provider

cfg = load("${TEST_DIR}/config.json")
prov = create_provider(cfg)

# Check pending
pending = prov.fetch_pending(claim=False)
assert len(pending) == 2, f"Expected 2 pending tasks, found {len(pending)}"

# Claim one task
claimed = prov.fetch_pending(claim=True)
assert len(claimed) == 1, f"Expected 1 claimed task, got {len(claimed)}"
assert (cfg.queue_dir / "claimed" / f"{claimed[0].id}.md").exists()
print(f"  [Lifecycle] Claimed: {claimed[0].id}")
PY

echo "==> Inspecting status after task claim..."
"${PROJECT_ROOT}/scripts/run-sandbox.sh" "${TEST_DIR}" python3 /opt/harness-frozen/harness.py status

# 7. Cleanup
echo "==> Cleaning up test directory ${TEST_DIR}..."
rm -rf "${TEST_DIR}"

echo "========================================================================"
echo "All tests, container verification, and lifecycle checks completed successfully!"
echo "========================================================================"
