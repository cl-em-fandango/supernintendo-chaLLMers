#!/usr/bin/env bash
set -e

# Initialize firewall rules unless disabled or in host networking mode
if [ "${DISABLE_FIREWALL:-0}" != "1" ] && command -v iptables >/dev/null 2>&1; then
    /entrypoint-scripts/init-firewall.sh 2>/dev/null || true
fi

exec "$@"
