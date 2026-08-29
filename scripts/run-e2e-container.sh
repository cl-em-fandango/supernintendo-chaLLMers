#!/usr/bin/env bash
set -euo pipefail

# ==============================================================================
# E2E Container Test Runner
#
# Orchestrates container creation, ephemeral snapshotting, and test execution:
#  1. Builds the base container image with the current version of the source.
#  2. Initializes the ephemeral workspace directory structure in a builder container.
#  3. Saves the container snapshot as "harness-e2e-startpoint:latest".
#  4. Runs the E2E pytest suite (spawning fresh ephemeral containers per test).
#  5. Cleans up containers on test completion.
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "==> Running E2E container tests via pytest e2e..."
cd "${HARNESS_DIR}"

pytest e2e -v -s "$@"
