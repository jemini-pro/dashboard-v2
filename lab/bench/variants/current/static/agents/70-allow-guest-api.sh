#!/usr/bin/env bash
# Step 70 — let the dashboard container reach the Proxmox API.
#
# Run as root on pve:  bash /root/allow-guest-api.sh
#
# It diagnoses first. If the PVE firewall is not the cause, it changes nothing
# and says so. If it is, it adds ONE rule (one source IP, one port, one
# direction), backs up the file it edits, and prints the diff.
set -euo pipefail

if ! command -v pct >/dev/null 2>&1; then
  echo "Run this on the PVE host, not inside a container (hostname: $(hostname))."
  exit 1
fi

GUEST=10.10.10.20
HOSTAPI=10.10.10.1
CTID=200
ENVF=/opt/stack/dash/.env
FW=/etc/pve/firewall/host.fw
RULE="IN ACCEPT -source ${GUEST} -p tcp -dport 8006 -log nolog"

echo "== 1. is the firewall actually running? =="
systemctl is-active pve-firewall || true
pve-firewall status || true
echo

echo "== 2. what is dropping guest traffic, if anything? =="
iptables-legacy -S INPUT 2>/dev/null | head -12 || echo "(no legacy INPUT rules)"
echo "---"
iptables -S INPUT 2>/dev/null | head -12 || echo "(no nft-backed INPUT rules)"
echo

echo "== 3. baseline: can CT ${CTID} reach the API right now? =="
if pct exec "${CTID}" -- curl -sk -m 5 -o /dev/null -w '' "https://${HOSTAPI}:8006/api2/json/version" 2>/dev/null; then
  echo "already reachable — no firewall change needed"
else
  echo "not reachable"
fi
echo

if ! systemctl is-active --quiet pve-firewall; then
  echo "STOP: the PVE firewall is not running, so it is not the cause."
  echo "Nothing was changed. Paste the section 2 output and I will look elsewhere."
  exit 0
fi

if ! grep -qE '^enable:[[:space:]]*1' /etc/pve/firewall/cluster.fw 2>/dev/null; then
  echo "STOP: pve-firewall runs but cluster.fw does not enable it."
  echo "Nothing was changed. Paste the section 2 output and I will look elsewhere."
  exit 0
fi

echo "== 4. firewall is enabled and is the likely cause — adding one rule =="
BAK="/root/host.fw.bak.$(date +%F-%H%M%S)"
cp -a "${FW}" "${BAK}"
echo "backup: ${BAK}"

if grep -qF "${RULE}" "${FW}"; then
  echo "rule already present, nothing to add"
else
  if grep -q '^\[RULES\]' "${FW}"; then
    sed -i "/^\[RULES\]/a ${RULE}" "${FW}"
  else
    printf '\n[RULES]\n%s\n' "${RULE}" >> "${FW}"
  fi
  echo "added: ${RULE}"
fi

echo
echo "== 5. the file now says =="
grep -nE '^\[RULES\]|8006' "${FW}" || true

pve-firewall restart || pve-firewall reload
sleep 2

echo
echo "== 6. retest from the container =="
pct exec "${CTID}" -- curl -sk -m 6 -H "Authorization: PVEAPIToken=$(pct exec ${CTID} -- sed -n 's/^PVE_TOKEN=//p' ${ENVF})" \
  "https://${HOSTAPI}:8006/api2/json/version" | head -c 200
echo

echo
echo "== 7. point the dashboard at the bridge address (no hairpin) =="
pct exec "${CTID}" -- sed -i "s|^PVE_HOST=.*|PVE_HOST=${HOSTAPI}|" "${ENVF}"
pct exec "${CTID}" -- grep '^PVE_HOST' "${ENVF}"
pct exec "${CTID}" -- sh -c 'cd /opt/stack && docker compose up -d dash' 2>&1 | tail -2

echo
echo "Done. If section 6 printed JSON, the card fills in within 5 seconds."
