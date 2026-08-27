#!/usr/bin/env bash
set -e

# Initialize firewall rules (if running as root or with CAP_NET_ADMIN)
if [ "$(id -u)" -eq 0 ]; then
    /entrypoint-scripts/init-firewall.sh || true
    # If a non-root harnessuser exists and args were passed, switch to harnessuser unless requested otherwise
    if [ "$#" -gt 0 ] && [ "$1" != "root" ]; then
        exec su -s /bin/bash harnessuser -c "$*"
    fi
fi

exec "$@"
