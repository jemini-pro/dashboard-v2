#!/usr/bin/env bash
# Step 81 — why is an allowed port still blocked? Read-only.
# Run as root on pve:  bash /root/diag2.sh
set -uo pipefail

OUT=/home/hermes/.hermes/homelab-build/diag2.txt

{
  echo "=== date: $(date)"
  echo
  echo "=== does the compiler emit our rule?"
  pve-firewall compile 2>&1 | grep -n -E '10\.10\.10\.20|8006' | head -20
  echo
  echo "=== live PVEFW-INPUT"
  iptables-legacy -S PVEFW-INPUT 2>&1
  echo
  echo "=== live PVEFW-HOST-IN (full)"
  iptables-legacy -S PVEFW-HOST-IN 2>&1
  echo
  echo "=== IPSet membership for management"
  iptables-legacy -S 2>&1 | grep -E 'PVEFW-0-management|ipset' | head -10
  echo
  echo "--- ipset contents"
  ipset list PVEFW-0-management-v4 2>&1 | head -20
  echo
  echo "=== firewall service state"
  systemctl status pve-firewall --no-pager 2>&1 | head -14
  echo
  echo "=== recent firewall log"
  journalctl -u pve-firewall --since '-20 min' --no-pager 2>&1 | tail -25
  echo
  echo "=== bridge nf settings"
  sysctl -a 2>/dev/null | grep -E 'bridge-nf-call|br_netfilter' | head
  lsmod | grep -E '^br_netfilter|^bridge' 2>&1
  echo
  echo "=== can the HOST reach its own bridge address on 8006?"
  timeout 5 curl -sk -o /dev/null -w '127.0.0.1:8006 -> %{http_code}\n' https://127.0.0.1:8006/api2/json/version 2>&1
  timeout 5 curl -sk -o /dev/null -w '10.10.10.1:8006 -> %{http_code}\n' https://10.10.10.1:8006/api2/json/version 2>&1
  echo
  echo "=== from the container, with verbose failure"
  pct exec 200 -- timeout 6 curl -skv https://10.10.10.1:8006/api2/json/version 2>&1 | head -12
  echo
  echo "=== from the container to the host on other ports (all should fail if policy DROP rules)"
  for p in 22 8006 9119; do
    pct exec 200 -- timeout 4 bash -c "exec 3<>/dev/tcp/10.10.10.1/$p" 2>/dev/null \
      && echo "10.10.10.1:$p open" || echo "10.10.10.1:$p blocked"
  done
} > "${OUT}" 2>&1

chmod 644 "${OUT}"
echo "written: ${OUT}  ($(wc -l < "${OUT}") lines)"
