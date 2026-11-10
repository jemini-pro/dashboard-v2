# Dashboard server and device agents

The front end in the repo root is static. This directory is the runtime that serves it:
a stdlib Python API, a Caddy entry point, and one-shot agents that push device metrics.

## Layout

| Path | What |
|---|---|
| `server/app.py` | the whole API, stdlib only, no pip, no build step |
| `server/docker-compose.yml` | `caddy` (only published port) + `dash` |
| `server/Caddyfile` | `:8448` → `dash:8000`, plain HTTP behind Tailscale |
| `agents/dash-agent.py` | Linux/macOS agent, posts every 60 s |
| `agents/dash-agent.ps1` | Windows agent, PowerShell 5+ |
| `agents/com.jem.dashagent.plist` | macOS LaunchAgent, token from `~/dash/.token` |
| `agents/install-windows-agent.ps1` | Windows installer, hidden via `wscript` |
| `agents/pve-host-agent.py` | Proxmox host agent (host metrics + guest list) |
| `agents/install-pve-host-agent.sh` | installs the above as a systemd timer |

## Endpoints

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/health` | GET | `{ok, pve}` |
| `/api/bookmarks` | GET | the bookmark store |
| `/api/stats` | GET | devices array, cached 5 s |
| `/api/push/<name>` | POST | agent heartbeat, needs `X-Dash-Token` |

Reads are open on the tailnet. Only pushes are authenticated — a leaked token can fake
a metric, nothing more.

## Running it

```bash
cd server
cp .env.example .env        # fill DASH_TOKEN
docker compose up -d
```

`/opt/stack/dash/data/bookmarks.json` is the single source of truth for bookmarks, and is
read per request: edit it, reload the page, no restart.

## Gotchas worth knowing before changing anything

- **`/proc` inside this container describes the host, not the container.** Keep `cgroup: host`
  on the `dash` service and read `/sys/fs/cgroup`. Subtract `inactive_file` from
  `memory.current`, or page cache makes an idle 2 GB box read 94% full.
- **`docker compose restart` does not re-read `env_file`.** Use `up -d` to recreate.
  Restart *is* right for a bind-mounted file like the Caddyfile.
- **The container cannot reach the Proxmox API** when the host firewall runs
  `policy_in: DROP`. That is why `agents/pve-host-agent.py` exists: the host pushes,
  which needs no firewall hole.
- **Agents go offline after 150 s** without a push, so they run every 60 s.
