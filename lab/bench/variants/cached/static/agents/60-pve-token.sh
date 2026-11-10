#!/usr/bin/env bash
# Step 60 — mint a Proxmox API token for the dashboard and wire it in.
#
# Run as root on pve:   bash /home/hermes/.hermes/homelab-build/60-pve-token.sh
#
# Idempotent: a token with the same id is deleted and re-minted, so a lost
# secret costs nothing. The secret never leaves this shell.
set -euo pipefail

if ! command -v pct >/dev/null 2>&1; then
  echo "This must run on the PVE host (pct not found here)."
  echo "Currently on: $(hostname) — if this is the container, exit and run it on pve."
  exit 1
fi

USER=dash@pve
TOKENID=dashboard
ROLE=PVEAuditor
CTID=200
ENVF=/opt/stack/dash/.env

echo "== 1. user =="
if pveum user list --output-format json | grep -q "\"userid\":\"${USER}\""; then
  echo "${USER} already exists"
else
  pveum user add "${USER}" --comment "read-only dashboard monitoring"
  echo "${USER} created"
fi

echo
echo "== 2. role on / =="
# PVEAuditor is read-only: status, config, no changes, no console.
pveum acl modify / --users "${USER}" --roles "${ROLE}"
echo "granted ${ROLE} on / to ${USER}"

echo
echo "== 3. token =="
if pvesh get "/access/users/${USER}/token/${TOKENID}" --output-format json >/dev/null 2>&1; then
  echo "token ${TOKENID} exists but its secret is unrecoverable — re-minting"
  pvesh delete "/access/users/${USER}/token/${TOKENID}"
fi
OUT=$(pvesh create "/access/users/${USER}/token/${TOKENID}" --privsep 0 --output-format json)
VAL=$(printf '%s' "${OUT}" | sed -n 's/.*"value":"\([^"]*\)".*/\1/p')
if [ -z "${VAL}" ]; then
  echo "could not read the secret out of:"
  echo "${OUT}"
  exit 1
fi
echo "minted ${USER}!${TOKENID}  (secret length ${#VAL})"

echo
echo "== 4. does the API accept it? (local, so the guest bridge is irrelevant) =="
curl -sk -m 8 -H "Authorization: PVEAPIToken=${USER}!${TOKENID}=${VAL}" \
  https://127.0.0.1:8006/api2/json/version | head -c 200
echo

echo
echo "== 5. writing it into CT ${CTID} =="
pct exec "${CTID}" -- sed -i "s|^PVE_TOKEN=.*|PVE_TOKEN=${USER}!${TOKENID}=${VAL}|" "${ENVF}"
pct exec "${CTID}" -- sh -c "grep -c '^PVE_TOKEN=dash@pve!dashboard=' ${ENVF}"
echo "env file updated"

echo
echo "== 6. recreating the dashboard (a restart does not re-read .env) =="
pct exec "${CTID}" -- sh -c 'cd /opt/stack && docker compose up -d dash' 2>&1 | tail -3

echo
echo "== done. The pve card still needs the container to reach the host; =="
echo "== that is the firewall question, separate from this token.        =="
