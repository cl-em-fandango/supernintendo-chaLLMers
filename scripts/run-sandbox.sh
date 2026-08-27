#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <work_directory> [command...]"
    echo "Example: $0 /home/donald/work python3 /opt/harness-frozen/harness.py status"
    echo "         $0 /home/donald/work python3 /opt/harness-frozen/harness.py report"
    exit 1
fi

WORK_DIR="$(cd "$1" && pwd)"
shift || true

ENGINE_CMD="podman"
if ! command -v podman >/dev/null 2>&1; then
    if command -v docker >/dev/null 2>&1; then
        ENGINE_CMD="docker"
    else
        echo "Error: Neither podman nor docker found in PATH." >&2
        exit 1
    fi
fi

IMAGE_TAG="${IMAGE_TAG:-harness-sandbox:frozen-latest}"

echo "==> Executing in sandboxed container..."
echo "    Engine:          ${ENGINE_CMD}"
echo "    Work Directory:  ${WORK_DIR}"
echo "    Image:           ${IMAGE_TAG}"

# Ensure container has access to local host LLM services (host.containers.internal / host.docker.internal)
# Mount WORK_DIR both at ${WORK_DIR} (to match config.json absolute paths) and /workspace
EXTRA_FLAGS=()
if [ "${ENGINE_CMD}" = "podman" ]; then
    EXTRA_FLAGS+=(
        "--userns=keep-id"
        "--add-host=host.containers.internal:host-gateway"
    )
else
    EXTRA_FLAGS+=(
        "--add-host=host.docker.internal:host-gateway"
    )
fi

# Pass through pi backend selection (e.g. openrouter for integration tests).
# Stage a writable copy of host pi agent config (auth.json etc.) so `pi` is
# authenticated in-container (user harnessuser, uid 1000) without mutating the
# host's ~/.pi (pi insists on writing an auth.json.lock next to auth.json).
EXTRA_FLAGS+=("-e" "HARNESS_PI_PROVIDER=${HARNESS_PI_PROVIDER:-llama-swap}")
if [ -f "${HOME}/.pi/agent/auth.json" ]; then
    PI_AGENT_STAGE="$(mktemp -d /tmp/harness-pi-agent.XXXXXX)"
    cp -r "${HOME}/.pi/agent/". "$PI_AGENT_STAGE"/ 2>/dev/null || true
    EXTRA_FLAGS+=("-v" "${PI_AGENT_STAGE}:/home/harnessuser/.pi/agent:z")
fi

DEFAULT_CMD=(python3 /opt/harness-frozen/harness.py status)
RUN_CMD=("${@:-${DEFAULT_CMD[@]}}")

${ENGINE_CMD} run --rm -i \
    --cap-add=NET_ADMIN \
    --cap-add=NET_RAW \
    -v "${WORK_DIR}:${WORK_DIR}:z" \
    -v "${WORK_DIR}:/workspace:z" \
    -w "${WORK_DIR}" \
    "${EXTRA_FLAGS[@]}" \
    "${IMAGE_TAG}" \
    "${RUN_CMD[@]}"
_rc=$?
[ -n "${PI_AGENT_STAGE:-}" ] && rm -rf "$PI_AGENT_STAGE"
exit $_rc
