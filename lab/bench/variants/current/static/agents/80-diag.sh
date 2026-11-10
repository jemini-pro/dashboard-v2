#!/usr/bin/env bash
# Step 80 — collect the facts about guest -> host traffic into a file Kai can read.
# Read-only. Changes nothing.
#
# Run as root on pve:  bash /root/diag.sh
set -uo pipefail

OUT=/home/hermes/.hermes/homelab-build/diag.txt

{
  echo "=== date: $(date)"
  echo "=== hostname: $(hostname)"
  echo
  echo "=== pve-firewall status"
  pve-firewall status 2>&1 | head -40
  echo
  echo "=== firewall config files"
  for f in /etc/pve/firewall/*.fw; do
    echo "--- ${f}"
    cat "${f}" 2>&1
  done
  echo
  echo "=== guest firewall config"
  for f in /etc/pve/firewall/200.fw; do
    [ -f "${f}" ] && { echo "--- ${f}"; cat "${f}"; } || echo "--- ${f}: absent (no guest firewall)"
  done
  echo
  echo "=== iptables-legacy -S (first 60)"
  iptables-legacy -S 2>&1 | head -60
  echo
  echo "=== iptables -S (first 40)"
  iptables -S 2>&1 | head -40
  echo
  echo "=== nft list ruleset (first 80)"
  nft list ruleset 2>&1 | head -80
  echo
  echo "=== sysctl"
  sysctl net.ipv4.ip_forward net.ipv4.conf.all.rp_filter net.ipv4.conf.vmbr1.rp_filter \
         net.bridge.bridge-nf-call-iptables net.bridge.bridge-nf-call-ip6tables 2>&1
  echo
  echo "=== interfaces"
  ip -br a
  echo
  echo "=== vmbr1 details"
  ip -d link show vmbr1 2>&1 | head -12
  echo
  echo "=== routes"
  ip route
  echo
  echo "=== container network"
  pct exec 200 -- ip -br a 2>&1
  pct exec 200 -- ip route 2>&1
  echo
  echo "=== container -> host, both addresses (5s cap each)"
  pct exec 200 -- timeout 5 curl -sk -o /dev/null -w '10.10.10.1 -> %{http_code}\n' https://10.10.10.1:8006/api2/json/version 2>&1
  pct exec 200 -- timeout 5 curl -sk -o /dev/null -w '192.168.1.5 -> %{http_code}\n' https://192.168.1.5:8006/api2/json/version 2>&1
  echo
  echo "=== container ping"
  pct exec 200 -- ping -c2 -W1 10.10.10.1 2>&1 | tail -3
  echo
  echo "=== container -> another LAN device (router), 4s"
  pct exec 200 -- timeout 4 curl -s -o /dev/null -w 'router 192.168.1.254:80 -> %{http_code}\n' http://192.168.1.254/ 2>&1
  echo
  echo "=== pct config for 200"
  pct config 200 2>&1
} > "${OUT}" 2>&1

chmod 644 "${OUT}"
echo "written: ${OUT}  ($(wc -l < "${OUT}") lines)"
