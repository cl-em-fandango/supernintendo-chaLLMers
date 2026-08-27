#!/usr/bin/env bash
set -euo pipefail

# implement-dir.sh — for each feature file in a directory (alphabetic order),
# run one non-interactive pi session that implements that feature, then exit.
#
#   scripts/implement-dir.sh <feature_dir>
#
# Env: MODEL, PROVIDER, GLOB, LOG_DIR, TIMEOUT (seconds), PI_BIN

FEATURE_DIR="${1:?usage: implement-dir.sh <feature_dir>}"
[ -d "$FEATURE_DIR" ] || { echo "not a directory: $FEATURE_DIR" >&2; exit 1; }

MODEL="${MODEL:-$(jq -r '.models.implementer // empty' config.json 2>/dev/null || true)}"
PROVIDER="${PROVIDER:-${HARNESS_PI_PROVIDER:-llama-swap}}"
GLOB="${GLOB:-*.md}"
LOG_DIR="${LOG_DIR:-.pi-implement-dir}"
TIMEOUT="${TIMEOUT:-3600}"
PI_BIN="${PI_BIN:-pi}"
mkdir -p "$LOG_DIR"

# Alphabetic order. Sort the NUL-separated list so names with spaces survive.
mapfile -d '' -t FILES < <(find "$FEATURE_DIR" -maxdepth 1 -type f -name "$GLOB" -print0 | LC_ALL=C sort -z)
[ "${#FILES[@]}" -gt 0 ] || { echo "no files matching '$GLOB' in $FEATURE_DIR" >&2; exit 1; }

for spec in "${FILES[@]}"; do
    name="$(basename "$spec")"
    log="$LOG_DIR/$name.out"
    echo "=== $name"

    # </dev/null is mandatory: pi's print mode reads stdin and merges it into
    # the prompt, so an inherited pipe/TTY with no EOF stalls the session with
    # zero output. tee keeps stdout visible AND on disk. timeout --kill-after
    # because a trapped SIGTERM alone does not kill node.
    # shellcheck disable=SC2086
    if timeout --kill-after=30s "$TIMEOUT" "$PI_BIN" \
            --provider "$PROVIDER" \
            ${MODEL:+--model "$MODEL"} \
            --no-session --mode json -p \
            "Implement exactly one feature in this repository: read the file $spec and do what it asks. Do not start any other feature. Follow CODING_STANDARDS.md if present, run the tests, commit your work, and end your final message with a line 'VERDICT: done' (or 'VERDICT: failed' with the reason)." \
            </dev/null 2>&1 | tee "$log"
    then
        echo "--- $name ok"
    else
        echo "--- $name FAILED (rc=${PIPESTATUS[0]:-?}, see $log)" >&2
        exit 1
    fi
done
