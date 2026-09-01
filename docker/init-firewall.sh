#!/usr/bin/env bash
set -euo pipefail

if [ "${DISABLE_FIREWALL:-0}" = "1" ]; then
    echo "[firewall] Firewall disabled by DISABLE_FIREWALL=1"
    exit 0
fi

# Configure outbound isolation using iptables
# If running without root / NET_ADMIN capabilities, log a warning and proceed gracefully
if ! command -v iptables >/dev/null 2>&1; then
    echo "[firewall] iptables command not available, continuing without local kernel packet filter"
    exit 0
fi

# Attempt to configure iptables rules if permitted
if iptables -L -n >/dev/null 2>&1; then
    echo "[firewall] Configuring network egress rules..."

    # Allow loopback
    iptables -A OUTPUT -o lo -j ACCEPT
    iptables -A INPUT -i lo -j ACCEPT

    # Allow established and related connections
    iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
    iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

    # Allow DNS (port 53 UDP/TCP)
    iptables -A OUTPUT -p udp --dport 53 -j ACCEPT
    iptables -A OUTPUT -p tcp --dport 53 -j ACCEPT

    # Allow communication to host / local LLM services (default bridge gateway / typical local networks)
    iptables -A OUTPUT -d 10.0.0.0/8 -j ACCEPT
    iptables -A OUTPUT -d 172.16.0.0/12 -j ACCEPT
    iptables -A OUTPUT -d 192.168.0.0/16 -j ACCEPT

    # Allow HTTP / HTTPS outbound for package managers (PyPI / mirrors)
    iptables -A OUTPUT -p tcp --dport 80 -j ACCEPT
    iptables -A OUTPUT -p tcp --dport 443 -j ACCEPT
    iptables -A OUTPUT -p tcp --dport 8000 -j ACCEPT

    # Allow Git SSH outbound if needed (port 22)
    iptables -A OUTPUT -p tcp --dport 22 -j ACCEPT

    # Set default drop for any other unexpected outbound traffic
    iptables -P OUTPUT DROP

    echo "[firewall] Egress filter active: Loopback, DNS, Local Subnets (LLM backends), HTTP/HTTPS, SSH permitted."
else
    echo "[firewall] CAP_NET_ADMIN not available; running under user-namespace network boundary."
fi
