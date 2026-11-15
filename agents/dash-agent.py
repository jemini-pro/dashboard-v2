#!/usr/bin/env python3
"""Dashboard agent for macOS and Linux.

Posts this machine's CPU, memory, every local volume and uptime to the homelab
dashboard.
Stdlib only. Run it every minute from cron/launchd, or with --loop.

    DASH_URL=https://pve.tail73491d.ts.net:8448 DASH_TOKEN=xxx ./dash-agent.py

The collectors and the wire live in dash_common.py, installed beside this
file. A copy installed before that module existed fetches it on first run.
"""
import os
import platform
import sys
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(globals().get("__file__") or "dash-agent.py"))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

try:
    import dash_common as common
except ImportError:  # an old install that predates the shared module
    common = None

DASH_URL = os.environ.get("DASH_URL", "https://pve.tail73491d.ts.net:8448").rstrip("/")
NAME = os.environ.get("DASH_NAME", platform.node() or "device")
KIND = os.environ.get("DASH_KIND", "mac" if platform.system() == "Darwin" else "pc")


def agent_path():
    """The path this script was loaded from, for self-update and bootstrap."""
    return os.path.abspath(globals().get("__file__") or "")


def collect():
    return common.collect_host(NAME, KIND)


def push(payload):
    token = common.load_token()
    try:
        return common.push(NAME, payload, token, DASH_URL)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise SystemExit(
                f"401: dashboard rejected the token (sent {len(token)} chars, expected 48).\n"
                "  Set it with:  printf '%s' '<token>' > ~/dash/.token && chmod 600 ~/dash/.token\n"
                "  Get it with:  pct exec 200 -- sed -n 's/^DASH_TOKEN=//p' /opt/stack/dash/.env"
            ) from None
        raise


def self_update():
    """Replace this file and dash_common.py with the served copies.

    Nobody revisits an installed agent: the cron job or LaunchAgent keeps
    running whatever the installer downloaded, so a fix on the server would
    otherwise only reach machines somebody logs into by hand. The served copy
    is the source of truth, including the shared module this script imports.
    Returns True when a file on disk changed.

    Never raises: an unreachable server or a read-only install must not stop
    the heartbeat. DASH_AGENT_UPDATE=0 opts a development copy out entirely.
    """
    if common is None:
        return False
    files = common.installed_files("dash-agent.py", agent_path())
    return bool(common.update_installed(DASH_URL, files))


def bootstrap_common():
    """Fetch dash_common.py beside this file when an old install lacks it.

    A device installed before the shared module existed self-updates to an
    agent that imports a sibling. Its first run must fetch that sibling
    instead of dying with ImportError. Returns True when the file was written.
    """
    path = agent_path()
    if not path:
        return False
    target = os.path.join(os.path.dirname(path), "dash_common.py")
    try:
        with urllib.request.urlopen(
            f"{DASH_URL}/agents/dash_common.py", timeout=10
        ) as response:
            served = response.read().decode()
    except Exception:  # noqa: BLE001 - offline is normal
        return False
    if not served.strip():
        return False
    tmp = target + ".new"
    try:
        with open(tmp, "w") as handle:
            handle.write(served)
        os.chmod(tmp, 0o755)
        os.replace(tmp, target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        return False
    return True


def restart():
    """Exec the file on disk, which self_update may just have replaced."""
    path = os.path.abspath(__file__)
    if common is not None:
        common.restart(path)
        return
    # Bootstrap runs before dash_common can be imported.
    os.execv(sys.executable, [sys.executable, path] + sys.argv[1:])


if __name__ == "__main__":
    if common is None:
        if bootstrap_common():
            restart()
        print("dash_common.py is missing beside the agent and could not be fetched",
              file=sys.stderr)
        raise SystemExit(1)
    if self_update():
        restart()
    if "--loop" not in sys.argv:
        print(push(collect()))
        sys.exit(0)
    while True:
        try:
            print(push(collect()), flush=True)
        except Exception as exc:  # noqa: BLE001
            print("push failed:", exc, flush=True)
        time.sleep(60)
        # A loop can run for weeks, so look for a new agent between pushes
        # rather than waiting for a reboot.
        if self_update():
            restart()
