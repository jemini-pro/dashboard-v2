#!/usr/bin/env bash
# Step 90 — make the pve host report itself to the dashboard.
#
# Run as root on pve:
#   curl -fsSL https://pve.tail73491d.ts.net:8448/agents/install-pve-host-agent.sh -o /root/install-pve-host-agent.sh \
#     && bash /root/install-pve-host-agent.sh
#
# Why this instead of the Proxmox API: the container cannot reach the host
# (host firewall, policy_in DROP). The host CAN reach the container, so the
# host pushes. No firewall hole needed.
set -euo pipefail

BASE=https://pve.tail73491d.ts.net:8448
CTID=200
ENVF=/opt/stack/dash/.env
FW=/etc/pve/firewall/host.fw

if ! command -v pct >/dev/null 2>&1; then
  echo "Run this on the PVE host (hostname: $(hostname))."
  exit 1
fi

echo "== 1. agent binary =="
curl -fsSL "${BASE}/agents/pve-host-agent.py" -o /usr/local/bin/dash-pve-agent.py
chmod 755 /usr/local/bin/dash-pve-agent.py
python3 -m py_compile /usr/local/bin/dash-pve-agent.py && echo "agent installed and compiles"

echo
echo "== 2. token, read from the container (never printed) =="
TOKEN=$(pct exec "${CTID}" -- sed -n 's/^DASH_TOKEN=//p' "${ENVF}")
if [ -z "${TOKEN}" ]; then echo "no DASH_TOKEN in ${ENVF}"; exit 1; fi
umask 077
printf 'DASH_URL=http://10.10.10.20:8448\nDASH_TOKEN=%s\nDASH_SKIP=docker01\n' "${TOKEN}" > /etc/dash-pve-agent.env
chmod 600 /etc/dash-pve-agent.env
echo "wrote /etc/dash-pve-agent.env"

echo
echo "== 3. systemd unit + 60s timer =="
cat > /etc/systemd/system/dash-pve-agent.service <<'UNIT'
[Unit]
Description=Push pve host stats to the homelab dashboard
After=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/dash-pve-agent.env
ExecStart=/usr/bin/python3 /usr/local/bin/dash-pve-agent.py
UNIT

cat > /etc/systemd/system/dash-pve-agent.timer <<'UNIT'
[Unit]
Description=Push pve host stats every minute

[Timer]
OnBootSec=30
OnUnitActiveSec=60

[Install]
WantedBy=timers.target
UNIT

systemctl daemon-reload
systemctl enable --now dash-pve-agent.timer
systemctl start dash-pve-agent.service || true
echo "timer: $(systemctl is-active dash-pve-agent.timer)"

echo
echo "== 4. first push =="
journalctl -u dash-pve-agent.service -n 6 --no-pager 2>&1 | tail -6

echo
echo "== 5. retiring the Proxmox API path =="
echo "   the container still cannot reach the host, so the API collector only"
echo "   produced an error card. Token is parked, not deleted."
pct exec "${CTID}" -- sh -c "cp ${ENVF} ${ENVF}.bak 2>/dev/null || true"
pct exec "${CTID}" -- sed -i 's|^PVE_TOKEN=.*|PVE_TOKEN=|' "${ENVF}"
pct exec "${CTID}" -- sed -i 's|^PVE_HOST=.*|PVE_HOST=|' "${ENVF}"
pct exec "${CTID}" -- sh -c 'cd /opt/stack && docker compose up -d dash' 2>&1 | tail -2

echo
echo "== 6. removing the firewall rule that never worked =="
if grep -qF 'IN ACCEPT -source 10.10.10.20 -p tcp -dport 8006' "${FW}" 2>/dev/null; then
  cp -a "${FW}" "/root/host.fw.bak.$(date +%F-%H%M%S)"
  sed -i '\|^IN ACCEPT -source 10.10.10.20 -p tcp -dport 8006|d' "${FW}"
  pve-firewall restart || true
  echo "rule removed, backup written to /root"
else
  echo "rule not present, nothing to remove"
fi

echo
echo "Done. The pve card should fill within 10 seconds."
