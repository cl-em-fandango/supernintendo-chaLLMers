#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
HARNESS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

ENGINE_CMD="podman"
if ! command -v podman >/dev/null 2>&1; then
    if command -v docker >/dev/null 2>&1; then
        ENGINE_CMD="docker"
    else
        echo "Error: Neither podman nor docker found in PATH." >&2
        exit 1
    fi
fi

IMAGE_TAG="${1:-harness-sandbox:frozen-latest}"

echo "==> Building sandbox container image using ${ENGINE_CMD}..."
echo "    Image tag: ${IMAGE_TAG}"
echo "    Context:   ${HARNESS_DIR}"

${ENGINE_CMD} build \
    -t "${IMAGE_TAG}" \
    -f "${HARNESS_DIR}/docker/Dockerfile" \
    "${HARNESS_DIR}"

echo "==> Sandbox container built successfully: ${IMAGE_TAG}"
