#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -lt 1 ]; then
    echo "Usage: $0 <work_directory> [command...]"
    echo "Example: $0 /home/donald/work python3 /opt/harness-frozen/harness.py status"
    echo "         $0 /home/donald/work python3 /opt/harness-frozen/harness.py report"
    exit 1
fi

# WORK_DIR is the host directory the operator wants the container to see. It is
# always mounted at itself and at /workspace; the config-declared directories
# (see below) are mounted *additionally* at their declared paths.
WORK_DIR="$(cd "$1" && pwd)"
shift || true

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

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

# SELinux relabel suffix. podman wants :z on every bind; docker rejects the flag.
SELINUX=""
if [ "${ENGINE_CMD}" = "podman" ]; then
    SELINUX=":z"
fi

echo "==> Executing in sandboxed container..."
echo "    Engine:          ${ENGINE_CMD}"
echo "    Work Directory:  ${WORK_DIR}"
echo "    Image:           ${IMAGE_TAG}"

# Ensure container has access to local host LLM services (host.containers.internal / host.docker.internal)
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

# The staged copy holds live pi credentials, so it must go away on EVERY exit
# path (engine failure, Ctrl-C, nonzero container exit under `set -e`). A plain
# `[ -n ... ] && rm -rf` line after the run never executes in those cases — the
# shell aborts before reaching it — and leaks a /tmp dir full of auth.json.
PI_AGENT_STAGE=""
cleanup_pi_auth_stage() {
    if [ -n "${PI_AGENT_STAGE:-}" ] && [ -d "${PI_AGENT_STAGE}" ]; then
        rm -rf "$PI_AGENT_STAGE"
    fi
    return 0
}
trap cleanup_pi_auth_stage EXIT

if [ -f "${HOME}/.pi/agent/auth.json" ]; then
    PI_AGENT_STAGE="$(mktemp -d /tmp/harness-pi-agent.XXXXXX)"
    cp -r "${HOME}/.pi/agent/". "$PI_AGENT_STAGE"/ 2>/dev/null || true
    EXTRA_FLAGS+=("-v" "${PI_AGENT_STAGE}:/home/harnessuser/.pi/agent${SELINUX}")
fi

# ---------------------------------------------------------------------------
# Mount table.
#
# The config names the directories the harness works in absolutely, and the
# harness reads them verbatim:
#   harnessExecutionAndQueueDir  where harness/, queue/, logs/, stats/ live
#   targetCodebaseDir            the repository that gets edited and committed
# Those paths are frequently symlinks on the host (e.g. /srv/pi-harness ->
# ~/work), and a bind mount of the *resolved* path does not make the declared
# path exist inside the container. So each declared directory gets its own
# mount at exactly the path the config names: the container's filesystem then
# mirrors the host layout the config was written against, symlink and all.
# ---------------------------------------------------------------------------
declare -a MOUNTS=() MNT_SRC=() MNT_DST=()

# add_mount <host_source> <container_target> — idempotent, and skips a mount
# that an earlier one already provides (same source subtree at the matching
# relative position under an earlier target).
add_mount() {
    local src="$1" dst="$2" i rel
    dst="${dst%/}"
    [ -n "$dst" ] || dst="/"
    for i in "${!MNT_DST[@]}"; do
        [ "${MNT_DST[$i]}" = "$dst" ] && return 0
    done
    for i in "${!MNT_SRC[@]}"; do
        case "${src}/" in
            "${MNT_SRC[$i]}"/*)
                rel="${src#${MNT_SRC[$i]}/}"
                rel="${rel#/}"
                if [ "${MNT_DST[$i]}/${rel}" = "$dst" ]; then
                    return 0
                fi
                ;;
        esac
    done
    MNT_SRC+=("$src")
    MNT_DST+=("$dst")
    MOUNTS+=("-v" "${src}:${dst}${SELINUX}")
    echo "    Mount:           ${src} -> ${dst}"
}

# mount_declared <declared_path> <label> — bind the host path a config entry
# names at that same path in the container. A declared path that does not exist
# on this host is reported and skipped: handing it to the engine would have the
# engine create it root-owned, and the harness could not write its queue there.
mount_declared() {
    local declared="$1" label="$2" src
    [ -n "$declared" ] || return 0
    if [ ! -e "$declared" ] && [ ! -L "$declared" ]; then
        echo "    !! ${label} '${declared}' does not exist on this host; not mounted." >&2
        return 0
    fi
    src="$(realpath "$declared" 2>/dev/null)" || {
        echo "    !! ${label} '${declared}' could not be resolved on this host; not mounted." >&2
        return 0
    }
    add_mount "$src" "$declared"
}

add_mount "$WORK_DIR" "$WORK_DIR"
add_mount "$WORK_DIR" "/workspace"

# Config handoff: the frozen image carries no config.json (it is gitignored), so
# the harness would fall back to <repo>/config.json == /opt/harness-frozen/config.json
# and die with FileNotFoundError. Resolve the config, hand it to the container as
# HARNESS_CONFIG, and mount the directories it declares.
CONFIG_FILE="${HARNESS_CONFIG:-}"
if [ -z "$CONFIG_FILE" ] && [ -r "${REPO_ROOT}/config.json" ]; then
    CONFIG_FILE="$(realpath "${REPO_ROOT}/config.json")"
fi

if [ -n "$CONFIG_FILE" ] && [ -r "$CONFIG_FILE" ]; then
    echo "    Config:          ${CONFIG_FILE}"

    # Same keys as harness.core.config.load().
    mapfile -t CFG_DIRS < <(python3 -c '
import json, sys
raw = json.load(open(sys.argv[1]))
print(raw.get("harnessExecutionAndQueueDir") or "")
print(raw.get("targetCodebaseDir") or "")
' "$CONFIG_FILE" 2>/dev/null || true)
    EXEC_DIR="${CFG_DIRS[0]:-}"
    TARGET_DIR="${CFG_DIRS[1]:-}"

    # The queue/logs/stats dirs are derived from the execution dir, so it is
    # mounted first; the target codebase usually sits inside it and is then
    # covered by this mount already.
    mount_declared "$EXEC_DIR" "harnessExecutionAndQueueDir"
    mount_declared "$TARGET_DIR" "targetCodebaseDir"

    if [ -n "$EXEC_DIR" ] && [ ! -d "${EXEC_DIR}/queue" ]; then
        echo "    !! No queue directory at '${EXEC_DIR}/queue'; the harness will create one as whatever user the container runs under." >&2
    fi

    if [[ "$CONFIG_FILE" == "${WORK_DIR}"/* ]]; then
        CONTAINER_CFG="$CONFIG_FILE"
    else
        CONTAINER_CFG="/run/harness-config.json"
        EXTRA_FLAGS+=("-v" "${CONFIG_FILE}:${CONTAINER_CFG}:ro${SELINUX}")
    fi
    EXTRA_FLAGS+=("-e" "HARNESS_CONFIG=${CONTAINER_CFG}")
else
    if [ -n "$CONFIG_FILE" ]; then
        echo "    !! Config '${CONFIG_FILE}' is not readable; the harness must find its own." >&2
    fi
fi

DEFAULT_CMD=(python3 /opt/harness-frozen/harness.py status)
RUN_CMD=("${@:-${DEFAULT_CMD[@]}}")

${ENGINE_CMD} run --rm -i \
    --cap-add=NET_ADMIN \
    --cap-add=NET_RAW \
    "${MOUNTS[@]}" \
    -w "${WORK_DIR}" \
    "${EXTRA_FLAGS[@]}" \
    "${IMAGE_TAG}" \
    "${RUN_CMD[@]}"
