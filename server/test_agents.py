"""Stdlib tests for the agents' shared wire protocol and host collectors.

There is one fake for the wire (`FakeTransport`) and one shared module under
test (`agents/dash_common.py`). The agents keep their wiring and
platform-specific extras; the failure-mode collectors deliberately live in
each agent until the planned move into `dash_common.py`, so the Python agents'
copies are exercised here directly. The Windows agent cannot run here (no
PowerShell), so that file is checked at the source level against the same
vocabulary.

Run from the repo root:
    python3 -m unittest discover -s server -p 'test_*.py' -v
"""
import importlib.util
import json
import os
import re
import sys
import tempfile
import types
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parent.parent / "agents"
SERVER = Path(__file__).resolve().parent
sys.path.insert(0, str(AGENTS))
sys.path.insert(0, str(SERVER))
import dash_common  # noqa: E402
import app  # noqa: E402


class FakeResponse:
    """Enough of an http.client response for urllib's context manager."""

    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeTransport:
    """The one fake wire: records pushes and serves /agents/<name> files."""

    def __init__(self, files=None, status=200, body='{"ok": true}', fail_names=()):
        self.files = files or {}
        self.status = status
        self.body = body
        self.fail_names = set(fail_names)
        self.posts = []
        self.gets = []
        self.post_error = None
        self.get_error = None

    def post(self, url, body, headers, timeout=15):
        if url.rsplit("/", 1)[-1] in self.fail_names:
            raise OSError("push failed")
        if self.post_error is not None:
            raise self.post_error
        self.posts.append({
            "url": url,
            "payload": json.loads(body),
            "headers": dict(headers),
        })
        return self.status, self.body

    def get(self, url, timeout=10):
        self.gets.append(url)
        if self.get_error is not None:
            raise self.get_error
        return self.files[url.rsplit("/agents/", 1)[-1]]


def load_agent(filename, module_name):
    spec = importlib.util.spec_from_file_location(module_name, AGENTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_unix_agent(module_name="dash_agent_under_test"):
    return load_agent("dash-agent.py", module_name)


class StubHost:
    """A platform adapter with fixed readings, for collect_host tests."""

    def __init__(self, volumes=None, missing=False):
        self._volumes = volumes or []
        self.missing = missing

    def cpu_pct(self):
        return 1.0

    def mem(self):
        return 1, 2

    def swap(self):
        return (None, None) if self.missing else (100, 400)

    def load_average(self):
        return (None, None, None) if self.missing else (0.5, 0.4, 0.3)

    def net_rates(self):
        return (None, None) if self.missing else (10, 20)

    def cpu_extra(self):
        return (None, None) if self.missing else (1.0, 0.5)

    def uptime_s(self):
        return 3

    def volumes(self):
        return self._volumes


class WireProtocolTest(unittest.TestCase):
    """push() is the one sender; the transport is the only thing swapped."""

    def test_push_posts_the_unchanged_payload_and_token_to_the_endpoint(self):
        fake = FakeTransport()
        payload = {"name": "mac", "kind": "mac", "cpu_pct": 1.0}

        status, body = dash_common.push(
            "mac", payload, "secret", "https://dash.example/", transport=fake
        )

        self.assertEqual((status, body), (200, '{"ok": true}'))
        self.assertEqual(len(fake.posts), 1)
        self.assertEqual(fake.posts[0]["url"], "https://dash.example/api/push/mac")
        self.assertEqual(fake.posts[0]["payload"], payload)
        self.assertEqual(fake.posts[0]["headers"]["X-Dash-Token"], "secret")
        self.assertEqual(fake.posts[0]["headers"]["Content-Type"], "application/json")

    def test_the_production_transport_is_used_when_none_is_passed(self):
        fake = FakeTransport()
        with mock.patch.object(dash_common, "TRANSPORT", fake):
            dash_common.push("pve", {"name": "pve"}, "secret", "http://dash:8448")

        self.assertEqual(fake.posts[0]["url"], "http://dash:8448/api/push/pve")


class TokenLoadingTest(unittest.TestCase):
    """Token lookup has one home: the environment, then a private file."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def token_file(self, text):
        path = Path(self.tmp.name) / ".token"
        path.write_text(text)
        return str(path)

    def test_the_environment_wins(self):
        path = self.token_file("from-file\n")
        self.assertEqual(
            dash_common.load_token(path=path, env={"DASH_TOKEN": " from-env "}),
            "from-env",
        )

    def test_a_plain_token_file_is_read(self):
        path = self.token_file("abc123\n")
        self.assertEqual(dash_common.load_token(path=path, env={}), "abc123")

    def test_an_env_style_file_can_hold_the_token(self):
        # The dashboard container ships /opt/stack/dash/.env, which is what
        # the hand-run container agent points at.
        path = self.token_file('DASH_URL=http://dash:8448\nDASH_TOKEN="abc123"\n')
        self.assertEqual(dash_common.load_token(path=path, env={}), "abc123")

    def test_a_missing_file_yields_an_empty_token(self):
        missing = str(Path(self.tmp.name) / "nope")
        self.assertEqual(dash_common.load_token(path=missing, env={}), "")


class AgentUpdateTest(unittest.TestCase):
    """Installed files are kept current from the served copies, never raising."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.agent_path = Path(self.tmp.name) / "dash-agent.py"
        self.common_path = Path(self.tmp.name) / "dash_common.py"
        self.agent_path.write_text("# installed old copy\n")
        self.common_path.write_text("# installed old common\n")
        self.files = {
            "dash-agent.py": "# new agent\n",
            "dash_common.py": "# new common\n",
        }

    def update(self, files, error=None):
        transport = FakeTransport(files=files)
        if error is not None:
            transport.get_error = error
        installed = [
            ("dash-agent.py", str(self.agent_path)),
            ("dash_common.py", str(self.common_path)),
        ]
        with mock.patch.object(dash_common, "TRANSPORT", transport):
            changed = dash_common.update_installed("https://dash.example", installed)
        return changed, transport

    def test_changed_served_files_replace_the_installed_copies(self):
        changed, transport = self.update(self.files)

        self.assertEqual(changed, ["dash-agent.py", "dash_common.py"])
        self.assertEqual(self.agent_path.read_text(), "# new agent\n")
        self.assertEqual(self.common_path.read_text(), "# new common\n")
        self.assertEqual(transport.gets, [
            "https://dash.example/agents/dash-agent.py",
            "https://dash.example/agents/dash_common.py",
        ])
        # Both have to stay executable or cron/systemd cannot run them.
        self.assertTrue(self.agent_path.stat().st_mode & 0o111)
        self.assertTrue(self.common_path.stat().st_mode & 0o111)

    def test_an_identical_file_is_left_untouched(self):
        self.common_path.write_text("# new common\n")
        changed, _ = self.update(self.files)

        self.assertEqual(changed, ["dash-agent.py"])
        self.assertEqual(self.agent_path.read_text(), "# new agent\n")
        self.assertEqual(self.common_path.read_text(), "# new common\n")

    def test_an_unreachable_server_keeps_the_installed_copies(self):
        changed, _ = self.update(self.files, error=OSError("offline"))

        self.assertEqual(changed, [])
        self.assertEqual(self.agent_path.read_text(), "# installed old copy\n")
        self.assertEqual(self.common_path.read_text(), "# installed old common\n")

    def test_a_development_copy_can_opt_out(self):
        with mock.patch.dict(os.environ, {"DASH_AGENT_UPDATE": "0"}):
            changed, transport = self.update(self.files)

        self.assertEqual(changed, [])
        self.assertEqual(transport.gets, [])
        self.assertEqual(self.agent_path.read_text(), "# installed old copy\n")


class LinuxCollectorTest(unittest.TestCase):
    """The Linux adapter is the only place /proc is read."""

    def setUp(self):
        self.host = dash_common.LinuxHost()

    def test_swap_reads_swap_total_and_free(self):
        info = {"SwapTotal": 1000, "SwapFree": 400}
        with mock.patch.object(dash_common, "meminfo", return_value=info):
            self.assertEqual(self.host.swap(), (600, 1000))

    def test_a_swap_disabled_host_reports_zero_rather_than_a_fake_full_disk(self):
        with mock.patch.object(dash_common, "meminfo",
                               return_value={"SwapTotal": 0}):
            self.assertEqual(self.host.swap(), (0, 0))

    def test_load_average_rounds_and_reports_missing_as_none(self):
        with mock.patch.object(dash_common.os, "getloadavg",
                               return_value=(0.1234, 0.5, 1.0)):
            self.assertEqual(self.host.load_average(), (0.12, 0.5, 1.0))
        with mock.patch.object(dash_common.os, "getloadavg", side_effect=OSError):
            self.assertEqual(self.host.load_average(), (None, None, None))

    def test_net_rates_are_a_delta_over_elapsed_time(self):
        with mock.patch.object(self.host, "net_counters",
                               side_effect=[(1000, 500), (1600, 800)]), \
                mock.patch.object(dash_common.time, "time",
                                  side_effect=[10.0, 11.0]), \
                mock.patch.object(dash_common.time, "sleep"):
            self.assertEqual(self.host.net_rates(), (600, 300))

    def test_net_rates_ignore_a_counter_reset(self):
        with mock.patch.object(self.host, "net_counters",
                               side_effect=[(2000, 500), (100, 600)]), \
                mock.patch.object(dash_common.time, "time",
                                  side_effect=[10.0, 11.0]), \
                mock.patch.object(dash_common.time, "sleep"):
            self.assertEqual(self.host.net_rates(), (None, None))

    def test_an_unreadable_counter_file_is_missing_data_not_zero_traffic(self):
        with mock.patch("builtins.open", side_effect=OSError):
            self.assertEqual(self.host.net_counters(), (None, None))

    def test_mount_table_escapes_are_decoded(self):
        table = (b"/dev/sda1 /mnt/my\\040disk ext4 rw 0 0\n"
                 b"/dev/sdb1 /mnt/tab\\011here xfs rw 0 0\n").decode()
        with mock.patch.object(dash_common, "read_file", return_value=table), \
                mock.patch.object(dash_common.platform, "system",
                                  return_value="Linux"):
            parsed = list(dash_common.mounts())

        self.assertEqual(parsed, [
            ("/dev/sda1", "/mnt/my disk", "ext4"),
            ("/dev/sdb1", "/mnt/tab\there", "xfs"),
        ])


class VolumeDiscoveryTest(unittest.TestCase):
    """Which mounts count as drives: one rule, exercised once."""

    def setUp(self):
        self.host = dash_common.LinuxHost()

    def collect_volumes(self, table, dev_ids, sizes, labels=None):
        def usage(path):
            total = sizes[path]
            return types.SimpleNamespace(total=total, used=total // 2, free=total // 2)

        with mock.patch.object(dash_common, "mounts", return_value=iter(table)), \
                mock.patch.object(dash_common, "volume_labels", return_value=labels or {}), \
                mock.patch.object(dash_common.os, "stat",
                                  side_effect=lambda p: types.SimpleNamespace(
                                      st_dev=dev_ids[p])), \
                mock.patch.object(dash_common.shutil, "disk_usage", side_effect=usage):
            return self.host.volumes()

    def test_reports_each_real_volume_once_with_root_first(self):
        table = [
            ("/dev/sda1", "/", "ext4"),
            ("/dev/sda1", "/mnt/bind", "ext4"),           # same device, bind mount
            ("/dev/sdb1", "/mnt/data", "ext4"),
            ("/dev/sdc1", "/media/External Drive", "exfat"),
            ("tmpfs", "/run", "tmpfs"),                  # pseudo filesystem
            ("/dev/loop0", "/snap/core", "squashfs"),    # snap loop
            ("/dev/sda2", "/boot/efi", "vfat"),          # below the 1 GiB floor
        ]
        dev_ids = {
            "/": 1, "/mnt/bind": 1, "/mnt/data": 2,
            "/media/External Drive": 3, "/run": 4,
            "/snap/core": 5, "/boot/efi": 6,
        }
        sizes = {
            "/": 1000 << 30, "/mnt/bind": 1000 << 30, "/mnt/data": 1000 << 30,
            "/media/External Drive": 2000 << 30, "/boot/efi": 512 << 20,
        }

        result = self.collect_volumes(table, dev_ids, sizes, {"/dev/sdb1": "Data"})

        self.assertEqual(
            [d["mount"] for d in result],
            ["/", "/media/External Drive", "/mnt/data"],
        )
        self.assertEqual(
            [d["label"] for d in result],
            ["System", "External Drive", "Data"],
        )
        self.assertEqual(result[0]["pct"], 50.0)

    def test_macos_mount_lines_handle_spaces_and_brackets_in_mount_points(self):
        lines = "\n".join([
            "/dev/disk3s1s1 on / (apfs, sealed, local, read-only, journaled)",
            "/dev/disk4s1 on /Volumes/My (Backup) Drive (exfat, local, nodev, nosuid)",
        ])
        with mock.patch.object(dash_common.platform, "system", return_value="Darwin"), \
                mock.patch.object(dash_common, "run", return_value=lines):
            parsed = list(dash_common.mounts())

        self.assertEqual(parsed, [
            ("/dev/disk3s1s1", "/", "apfs"),
            ("/dev/disk4s1", "/Volumes/My (Backup) Drive", "exfat"),
        ])


class DarwinCollectorTest(unittest.TestCase):
    def setUp(self):
        self.host = dash_common.DarwinHost()

    def test_swap_parses_sysctl_sizes(self):
        out = "total = 2.00G  used = 512.00M  free = 1.50G"
        with mock.patch.object(dash_common, "run", return_value=out):
            self.assertEqual(self.host.swap(), (512 * 1024 ** 2, 2 * 1024 ** 3))

    def test_cpu_pressure_is_not_invented(self):
        self.assertEqual(self.host.cpu_extra(), (None, None))

    def test_uptime_parses_the_boot_time(self):
        with mock.patch.object(dash_common, "run",
                               return_value="{ sec = 1700000000, usec = 0 }"), \
                mock.patch.object(dash_common.time, "time", return_value=1700000060):
            self.assertEqual(self.host.uptime_s(), 60)

    def test_cpu_usage_comes_from_top(self):
        out = "CPU usage: 5.26% user, 10.52% sys, 84.21% idle"
        with mock.patch.object(dash_common, "run", return_value=out):
            self.assertEqual(self.host.cpu_pct(), 15.8)


class CollectHostTest(unittest.TestCase):
    """collect_host() is the one payload builder for the Python agents."""

    def test_includes_present_signals_and_omits_missing_ones(self):
        payload = dash_common.collect_host("mac", "mac", host=StubHost())

        self.assertEqual(payload["name"], "mac")
        self.assertEqual(payload["kind"], "mac")
        self.assertEqual(payload["load_1"], 0.5)
        self.assertEqual(payload["load_15"], 0.3)
        self.assertEqual(payload["swap_used"], 100)
        self.assertEqual(payload["swap_total"], 400)
        self.assertEqual(payload["net_rx_bps"], 10)
        self.assertEqual(payload["cpu_steal"], 0.5)

        empty = dash_common.collect_host("mac", "mac", host=StubHost(missing=True))
        for key in ("load_1", "swap_used", "net_rx_bps", "cpu_iowait"):
            self.assertNotIn(key, empty)

    def test_summary_totals_every_volume(self):
        host = StubHost(volumes=[
            {"mount": "/", "label": "System", "total": 100, "used": 10, "pct": 10.0},
            {"mount": "/mnt/data", "label": "Data", "total": 100, "used": 99, "pct": 99.0},
            {"mount": "/mnt/roomy", "label": "Roomy", "total": 800, "used": 0, "pct": 0.0},
        ])

        payload = dash_common.collect_host("pc", "pc", host=host)

        # 109 used of 1000 total, not the fullest volume.
        self.assertEqual(payload["disk_pct"], 10.9)
        self.assertEqual(len(payload["disks"]), 3)

    def test_an_unsized_volume_does_not_divide_by_zero(self):
        self.assertIsNone(dash_common.aggregate_disk_pct([]))
        self.assertIsNone(dash_common.aggregate_disk_pct([
            {"mount": "/", "label": "System", "total": 0, "used": 0, "pct": 0.0},
        ]))

    def test_an_explicit_volume_source_replaces_the_platform_one(self):
        host = StubHost(volumes=[
            {"mount": "/", "label": "System", "total": 100, "used": 900, "pct": 900.0},
        ])
        root_only = [{"mount": "/", "label": "System", "total": 1000,
                      "used": 900, "pct": 90.0}]

        payload = dash_common.collect_host("pve", "host", host=host,
                                           volumes_from=lambda: root_only)

        self.assertEqual(payload["disks"], root_only)
        self.assertEqual(payload["disk_pct"], 90.0)

    def test_the_host_root_volume_is_a_valid_volume_entry(self):
        usage = types.SimpleNamespace(total=1000, used=900, free=100)
        with mock.patch.object(dash_common.shutil, "disk_usage",
                               return_value=usage):
            volumes = dash_common.LinuxHost().root_volume()

        self.assertEqual(volumes, [{
            "mount": "/", "label": "System",
            "total": 1000, "used": 900, "pct": 90.0,
        }])
        with mock.patch.object(dash_common.shutil, "disk_usage",
                               return_value=types.SimpleNamespace(
                                   total=0, used=0, free=0)):
            self.assertEqual(dash_common.LinuxHost().root_volume(), [])

    def test_the_platform_seam_picks_a_collector(self):
        with mock.patch.object(dash_common.platform, "system",
                               return_value="Darwin"):
            self.assertIsInstance(dash_common.host_for(), dash_common.DarwinHost)
        with mock.patch.object(dash_common.platform, "system",
                               return_value="Linux"):
            self.assertIsInstance(dash_common.host_for(), dash_common.LinuxHost)


class DashAgentTest(unittest.TestCase):
    """dash-agent keeps the one-shot/loop wiring and the update path."""

    def setUp(self):
        self.agent = load_unix_agent()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "dash-agent.py"
        self.common_path = Path(self.tmp.name) / "dash_common.py"
        self.path.write_text("# installed old copy\n")
        self.common_path.write_text("# installed old common\n")

    def test_a_heartbeat_uses_the_shared_sender(self):
        fake = FakeTransport()
        with mock.patch.object(dash_common, "TRANSPORT", fake), \
                mock.patch.object(self.agent, "DASH_URL", "https://dash.example"), \
                mock.patch.object(self.agent, "NAME", "testmac"), \
                mock.patch.dict(os.environ, {"DASH_TOKEN": "secret"}):
            status, _ = self.agent.push({"name": "testmac", "cpu_pct": 1.0})

        self.assertEqual(status, 200)
        self.assertEqual(fake.posts[0]["url"], "https://dash.example/api/push/testmac")
        self.assertEqual(fake.posts[0]["headers"]["X-Dash-Token"], "secret")

    def test_a_rejected_token_explains_how_to_fix_it(self):
        fake = FakeTransport()
        fake.post_error = urllib.error.HTTPError(
            "https://dash.example/api/push/testmac", 401, "Unauthorized", {}, None
        )
        with mock.patch.object(dash_common, "TRANSPORT", fake), \
                mock.patch.object(self.agent, "DASH_URL", "https://dash.example"), \
                mock.patch.object(self.agent, "NAME", "testmac"), \
                mock.patch.dict(os.environ, {"DASH_TOKEN": "short"}):
            with self.assertRaises(SystemExit) as caught:
                self.agent.push({"name": "testmac"})

        self.assertIn("401", str(caught.exception))
        self.assertIn("~/dash/.token", str(caught.exception))

    def test_a_changed_served_agent_replaces_both_installed_files(self):
        fake = FakeTransport(files={
            "dash-agent.py": "# new agent\n",
            "dash_common.py": "# new common\n",
        })
        with mock.patch.object(dash_common, "TRANSPORT", fake), \
                mock.patch.object(self.agent, "DASH_URL", "https://dash.example"), \
                mock.patch.object(self.agent, "__file__", str(self.path)), \
                mock.patch.object(dash_common, "__file__", str(self.common_path)):
            changed = self.agent.self_update()

        self.assertTrue(changed)
        self.assertEqual(self.path.read_text(), "# new agent\n")
        self.assertEqual(self.common_path.read_text(), "# new common\n")
        self.assertEqual(fake.gets, [
            "https://dash.example/agents/dash-agent.py",
            "https://dash.example/agents/dash_common.py",
        ])

    def test_an_identical_agent_is_left_untouched(self):
        self.path.write_text("# same agent\n")
        self.common_path.write_text("# same common\n")
        fake = FakeTransport(files={
            "dash-agent.py": "# same agent\n\n",
            "dash_common.py": "# same common\n\n",
        })
        with mock.patch.object(dash_common, "TRANSPORT", fake), \
                mock.patch.object(self.agent, "DASH_URL", "https://dash.example"), \
                mock.patch.object(self.agent, "__file__", str(self.path)), \
                mock.patch.object(dash_common, "__file__", str(self.common_path)):
            changed = self.agent.self_update()

        self.assertFalse(changed)
        self.assertEqual(self.path.read_text(), "# same agent\n")

    def test_an_unreachable_server_keeps_the_heartbeat_working(self):
        fake = FakeTransport()
        fake.get_error = OSError("offline")
        with mock.patch.object(dash_common, "TRANSPORT", fake), \
                mock.patch.object(self.agent, "DASH_URL", "https://dash.example"), \
                mock.patch.object(self.agent, "__file__", str(self.path)), \
                mock.patch.object(dash_common, "__file__", str(self.common_path)):
            changed = self.agent.self_update()

        self.assertFalse(changed)
        self.assertEqual(self.path.read_text(), "# installed old copy\n")

    def test_a_development_copy_can_opt_out(self):
        fake = FakeTransport(files={"dash-agent.py": "# new agent\n"})
        with mock.patch.dict(os.environ, {"DASH_AGENT_UPDATE": "0"}), \
                mock.patch.object(dash_common, "TRANSPORT", fake), \
                mock.patch.object(self.agent, "DASH_URL", "https://dash.example"), \
                mock.patch.object(self.agent, "__file__", str(self.path)), \
                mock.patch.object(dash_common, "__file__", str(self.common_path)):
            changed = self.agent.self_update()

        self.assertFalse(changed)
        self.assertEqual(fake.gets, [])
        self.assertEqual(self.path.read_text(), "# installed old copy\n")

    def test_an_old_install_bootstraps_the_missing_shared_module(self):
        # A device installed before dash_common existed replaces itself with
        # the new agent, which imports siblings. The first run must fetch the
        # module rather than crash.
        opener = mock.Mock(return_value=FakeResponse(b"# fetched common\n"))
        with mock.patch.object(self.agent, "__file__", str(self.path)), \
                mock.patch.object(self.agent, "DASH_URL", "https://dash.example"), \
                mock.patch.object(self.agent.urllib.request, "urlopen", opener):
            fetched = self.agent.bootstrap_common()

        self.assertTrue(fetched)
        self.assertEqual(self.common_path.read_text(), "# fetched common\n")
        self.assertEqual(
            opener.call_args[0][0],
            "https://dash.example/agents/dash_common.py",
        )


class PveHostAgentTest(unittest.TestCase):
    """The pve agent keeps only its guest logic and uses the Linux adapter."""

    def setUp(self):
        self.agent = load_agent("pve-host-agent.py", "pve_host_agent_under_test")

    def run_main(self, guest=None, fake=None, health=None):
        fake = fake or FakeTransport()
        volume = {"mount": "/", "label": "System", "total": 1000,
                  "used": 900, "pct": 90.0}
        host = StubHost(volumes=[volume])
        host.root_volume = lambda: [volume]
        with mock.patch.object(dash_common, "TRANSPORT", fake), \
                mock.patch.object(self.agent, "HOST", host), \
                mock.patch.object(self.agent, "guests",
                                  return_value=[guest] if guest else []), \
                mock.patch.object(self.agent, "self_update", return_value=False), \
                mock.patch.object(self.agent, "collect_health",
                                  return_value=health or {}), \
                mock.patch.object(self.agent, "URL", "http://dash:8448"), \
                mock.patch.dict(os.environ, {"DASH_TOKEN": "secret"}):
            self.agent.main()
        return fake

    def test_host_payload_carries_health_blocks_and_guests_do_not(self):
        guest = {"name": "adguard", "vmid": 101, "cpu": 0.1, "mem": 1,
                 "maxmem": 2, "uptime": 3}
        health = {"smart": [{"device": "/dev/sda", "health": "PASSED"}],
                  "zfs": [{"pool": "tank", "state": "ONLINE"}]}

        fake = self.run_main(guest=guest, health=health)
        by_name = {post["url"].rsplit("/", 1)[-1]: post["payload"]
                   for post in fake.posts}

        self.assertEqual(by_name["pve"]["smart"], health["smart"])
        self.assertEqual(by_name["pve"]["zfs"], health["zfs"])
        for key in ("smart", "zfs"):
            self.assertNotIn(key, by_name["adguard"])

    def test_host_payload_carries_the_root_volume_and_guests_do_not(self):
        guest = {"name": "adguard", "vmid": 101, "cpu": 0.1, "mem": 1,
                 "maxmem": 2, "uptime": 3}

        fake = self.run_main(guest=guest)
        by_name = {post["url"].rsplit("/", 1)[-1]: post["payload"]
                   for post in fake.posts}

        disks = by_name["pve"]["disks"]
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0]["mount"], "/")
        self.assertEqual(disks[0]["total"], 1000)
        self.assertEqual(disks[0]["used"], 900)
        self.assertEqual(disks[0]["pct"], 90.0)
        self.assertEqual(by_name["pve"]["disk_pct"], 90.0)
        self.assertEqual(by_name["pve"]["load_1"], 0.5)
        self.assertEqual(by_name["pve"]["swap_total"], 400)
        self.assertEqual(by_name["pve"]["net_rx_bps"], 10)
        self.assertEqual(by_name["pve"]["cpu_iowait"], 1.0)
        # Guests have no host-level load/swap/network data to report.
        for key in ("disks", "load_1", "swap_total", "net_rx_bps"):
            self.assertNotIn(key, by_name["adguard"])

    def test_pve_runs_the_local_smart_collector_for_the_host(self):
        def run(argv, timeout=4):
            if "--scan-open" in argv:
                return "/dev/sda -d sat\n"
            return json.dumps({"smart_status": {"passed": True},
                               "temperature": {"current": 30}})

        with mock.patch.object(self.agent.shutil, "which",
                               side_effect=lambda name: (
                                   "/usr/bin/" + name if name == "smartctl" else None)), \
                mock.patch.object(self.agent, "_run", side_effect=run), \
                mock.patch.object(self.agent.os.path, "exists",
                                  return_value=False), \
                mock.patch.dict(os.environ, {"DASH_HEALTH": "1"}):
            blocks = self.agent.collect_health()

        self.assertEqual(blocks["smart"], [{
            "device": "/dev/sda", "health": "PASSED", "temperature_c": 30,
        }])

    def test_a_guest_reports_its_size_and_root_volume(self):
        guest = {"name": "agent", "vmid": 210, "status": "running", "cpu": 0.1,
                 "maxcpu": 4, "mem": 1000, "maxmem": 4000, "uptime": 60,
                 "disk": 500, "maxdisk": 2000}

        payload = self.agent.guest_payload(guest)

        self.assertEqual(payload["state"], "running")
        self.assertEqual(payload["vmid"], 210)
        self.assertEqual(payload["vcpu"], 4)
        self.assertEqual(payload["disks"], [{
            "mount": "/", "label": "rootfs", "total": 2000, "used": 500,
            "pct": 25.0,
        }])
        self.assertEqual(payload["disk_pct"], 25.0)
        # What a guest runs is a deployment fact the dashboard owns; the host
        # agent reports measurements only.
        self.assertNotIn("role", payload)

    def test_a_guest_with_no_size_or_volume_invents_neither(self):
        guest = {"name": "mystery", "vmid": 199, "cpu": 0, "mem": 1, "maxmem": 2}

        payload = self.agent.guest_payload(guest)

        self.assertNotIn("disks", payload)
        self.assertNotIn("disk_pct", payload)
        self.assertNotIn("vcpu", payload)
        # A missing sample stays missing; the page drops the line rather than
        # printing a made-up "up 0s".
        self.assertIsNone(payload["uptime_s"])

    def test_a_failing_guest_does_not_stop_the_host_heartbeat(self):
        guest = {"name": "adguard", "vmid": 101, "cpu": 0.1, "mem": 1,
                 "maxmem": 2, "uptime": 3}
        fake = FakeTransport(fail_names={"adguard"})

        with self.assertRaises(SystemExit) as caught:
            self.run_main(guest=guest, fake=fake)

        self.assertEqual(caught.exception.code, 1)
        self.assertEqual([p["url"].rsplit("/", 1)[-1] for p in fake.posts], ["pve"])


class ContainerAgentTest(unittest.TestCase):
    """The hand-run container agent shares the wire, not a deployment path."""

    def setUp(self):
        self.agent = load_agent("docker-container-agent.py",
                                "container_agent_under_test")

    def test_one_push_per_running_container(self):
        fake = FakeTransport()
        measured = {
            "dash": {"cpu_pct": 1.0, "mem_pct": 2.0,
                     "mem_used": 3, "mem_total": 4},
        }
        with mock.patch.object(dash_common, "TRANSPORT", fake), \
                mock.patch.object(self.agent, "stats", return_value=measured), \
                mock.patch.object(self.agent, "describe",
                                  return_value={"state": "running",
                                                "role": "dashboard"}), \
                mock.patch.object(self.agent, "URL", "http://dash:8448"), \
                mock.patch.dict(os.environ, {"DASH_TOKEN": "secret"}):
            code = self.agent.main()

        self.assertEqual(code, 0)
        self.assertEqual(fake.posts[0]["url"], "http://dash:8448/api/push/dash")
        self.assertEqual(fake.posts[0]["payload"]["kind"], "container")
        self.assertEqual(fake.posts[0]["payload"]["role"], "dashboard")


class WindowsSelfUpdateTest(unittest.TestCase):
    def setUp(self):
        self.src = (AGENTS / "dash-agent.ps1").read_text()

    def test_the_scheduled_copy_pulls_the_served_agent_and_swaps_itself(self):
        self.assertIn('"$DashUrl/agents/dash-agent.ps1"', self.src)
        self.assertIn("$PSCommandPath", self.src)
        self.assertIn("Move-Item", self.src)
        self.assertIn('$env:DASH_AGENT_UPDATE -ne "0"', self.src)

    def test_only_a_different_agent_is_written(self):
        # Comparing the text also keeps this from rewriting the file every
        # minute, which Defender would not enjoy.
        self.assertRegex(self.src, r"\$served\.Trim\(\) -ne \$current\.Trim\(\)")

    def test_a_failed_fetch_never_aborts_the_heartbeat(self):
        block = self.src[self.src.index("Invoke-WebRequest"):self.src.index("$os = Get-CimInstance")]
        self.assertIn("catch", block)


class ServedAgentsTest(unittest.TestCase):
    def test_agent_files_need_no_browser_password(self):
        # Machines cannot answer Caddy's basic-auth prompt, and an installed
        # agent re-reads its own copy from here to stay current.
        caddy = (AGENTS.parent / "server" / "Caddyfile").read_text()
        self.assertRegex(caddy, r"@agents path /agents/\*")

    def test_the_shared_module_is_published_beside_the_agents(self):
        deploy = (AGENTS.parent / "server" / "deploy.sh").read_text()
        self.assertRegex(deploy, r"dash_common\.py")
        installer = (AGENTS / "install-pve-host-agent.sh").read_text()
        self.assertIn("dash_common.py", installer)


class WindowsDiskReportingTest(unittest.TestCase):
    def test_reports_fixed_and_removable_drives_not_only_c(self):
        # No PowerShell runtime here, so guard the two rules that make the
        # Windows payload plural: enumerate Win32_LogicalDisk and keep drive
        # types 2 (removable) and 3 (fixed).
        src = (AGENTS / "dash-agent.ps1").read_text()

        self.assertIn("Win32_LogicalDisk", src)
        self.assertIn("$_.DriveType -eq 2 -or $_.DriveType -eq 3", src)
        self.assertNotIn("Get-PSDrive -Name C", src)
        self.assertRegex(src, r"disks\s*=\s*\$disks")
        self.assertRegex(src, r"disk_pct\s*=\s*\$diskPct")
        self.assertIn("-Depth 4", src)

    def test_only_drives_with_a_disk_behind_them_count(self):
        # Google Drive for desktop mounts accounts as virtual fixed drives whose
        # size and free space are the cloud quota. They have no partition, so
        # the partition association is the filter. If that query fails, the
        # DriveType rule alone still reports the machine's real disks.
        src = (AGENTS / "dash-agent.ps1").read_text()

        self.assertIn("Win32_LogicalDiskToPartition", src)
        self.assertIn("$hasDiskBehind", src)
        self.assertRegex(src, r"\$backed\.Count -eq 0 -or \$backed\.ContainsKey")

    def test_the_summary_gauge_totals_the_volumes(self):
        src = (AGENTS / "dash-agent.ps1").read_text()

        self.assertRegex(src, r"Measure-Object -Property total -Sum")
        self.assertRegex(src, r"Measure-Object -Property used -Sum")
        self.assertRegex(src, r"100\.0 \* \$diskUsedBytes / \$diskTotalBytes")


class WindowsSignalReportingTest(unittest.TestCase):
    """Windows has no load average; page file and network are best-effort.

    PowerShell is not available in this environment, so these are source-level
    assertions. The server side is covered with synthetic payloads.
    """

    def setUp(self):
        self.src = (AGENTS / "dash-agent.ps1").read_text()

    def test_page_file_becomes_swap_only_when_present(self):
        self.assertIn("Win32_PageFileUsage", self.src)
        self.assertRegex(self.src, r"if \(\$pageFile\)")
        self.assertIn('$body["swap_used"]', self.src)
        self.assertIn('$body["swap_total"]', self.src)

    def test_network_rates_are_best_effort_and_optional(self):
        self.assertIn("Bytes Received/sec", self.src)
        self.assertIn("Bytes Sent/sec", self.src)
        self.assertRegex(self.src, r"if \(\$net\)")
        self.assertIn('$body["net_rx_bps"]', self.src)
        self.assertIn('$body["net_tx_bps"]', self.src)

    def test_no_fabricated_load_average_or_iowait(self):
        # Windows exposes neither, so the agent must not invent a zero.
        self.assertNotIn("load_1", self.src)
        self.assertNotIn("cpu_iowait", self.src)


class WindowsVocabularyTest(unittest.TestCase):
    """PowerShell cannot import Python; the shared vocabulary is pinned here."""

    def test_the_payload_keys_come_from_the_shared_list(self):
        src = (AGENTS / "dash-agent.ps1").read_text()
        keys = set(re.findall(r"^\s{4}([a-z_]+)\s*=", src, re.M))
        keys |= set(re.findall(r'\$body\["([a-z_]+)"\]', src))
        # Failure-mode blocks are optional top-level additions; the server's
        # own list is the pin, not a second hardcoded copy.
        allowed = (set(dash_common.CORE_METRICS)
                   | set(dash_common.EXTRA_METRICS)
                   | set(app.HEALTH_KINDS))

        self.assertTrue(keys)
        self.assertLessEqual(keys, allowed)

    def test_the_source_says_where_the_vocabulary_lives(self):
        src = (AGENTS / "dash-agent.ps1").read_text()
        self.assertIn("dash_common.py", src)


if __name__ == "__main__":
    unittest.main()


class HealthCollectorTest(unittest.TestCase):
    """The new collectors are best-effort and local to each agent script."""

    def setUp(self):
        self.agent = load_agent("dash-agent.py", "health_agent_under_test")

    def run_with(self, tools, run, env=None, exists=False):
        environ = {key: value for key, value in os.environ.items()
                   if not key.startswith("DASH_")}
        environ.update(env or {})

        def which(name):
            return "/usr/bin/" + name if name in tools else None

        with mock.patch.object(self.agent.shutil, "which", side_effect=which), \
                mock.patch.object(self.agent, "_run", side_effect=run), \
                mock.patch.object(self.agent.os.path, "exists",
                                  return_value=exists), \
                mock.patch.dict(os.environ, environ, clear=True):
            return self.agent.collect_health()

    def test_missing_commands_omit_every_block_instead_of_failing(self):
        self.assertEqual(self.run_with(set(), lambda argv, timeout=4: ""), {})

    def test_smart_parses_ata_health_attributes_and_power_on_hours(self):
        payload = {
            "smart_status": {"passed": True},
            "temperature": {"current": 34},
            "ata_smart_attributes": {"table": [
                {"id": 5, "raw": {"value": 2}},
                {"id": 197, "raw": {"value": 1}},
            ]},
            "power_on_time": {"hours": 1234},
        }

        def run(argv, timeout=4):
            if "--scan-open" in argv:
                return "/dev/sda -d sat # /dev/sda, ATA device\n"
            return json.dumps(payload)

        blocks = self.run_with({"smartctl"}, run)
        self.assertEqual(blocks["smart"], [{
            "device": "/dev/sda", "health": "PASSED", "temperature_c": 34,
            "reallocated": 2, "pending": 1, "power_on_hours": 1234,
        }])

    def test_smart_failure_is_reported_as_failed(self):
        def run(argv, timeout=4):
            if "--scan-open" in argv:
                return "/dev/sda -d sat\n"
            return json.dumps({"smart_status": {"passed": False}})

        blocks = self.run_with({"smartctl"}, run)
        self.assertEqual(blocks["smart"], [{"device": "/dev/sda",
                                           "health": "FAILED"}])

    def test_zfs_pools_carry_state_capacity_scrub_age_and_errors(self):
        def run(argv, timeout=4):
            if argv[:3] == ["zpool", "list", "-H"]:
                return "tank\tONLINE\t42\nbackup\tDEGRADED\t81\n"
            if argv[:2] == ["zpool", "status"]:
                return (
                    "  scan: scrub repaired 0B in 00:10:00 with 0 errors "
                    "on Sun Nov 10 12:00:00 2024\n"
                    "  errors: No known data errors\n"
                )
            return ""

        blocks = self.run_with({"zpool"}, run)
        self.assertEqual([pool["pool"] for pool in blocks["zfs"]],
                         ["tank", "backup"])
        self.assertEqual(blocks["zfs"][0]["state"], "ONLINE")
        self.assertEqual(blocks["zfs"][0]["capacity_pct"], 42.0)
        self.assertEqual(blocks["zfs"][0]["errors"], 0)
        self.assertGreater(blocks["zfs"][0]["scrub_age_s"], 0)
        self.assertEqual(blocks["zfs"][1]["state"], "DEGRADED")

    def test_zfs_permanent_errors_without_a_count_are_still_nonzero(self):
        def run(argv, timeout=4):
            if argv[:3] == ["zpool", "list", "-H"]:
                return "tank\tONLINE\t10\n"
            if argv[:2] == ["zpool", "status"]:
                return ("  errors: Permanent errors have been detected in the "
                        "following files:\n")
            return ""

        blocks = self.run_with({"zpool"}, run)
        self.assertGreaterEqual(blocks["zfs"][0]["errors"], 1)

    def test_diskutil_not_supported_is_not_a_smart_failure(self):
        def run(argv, timeout=4):
            if argv[:2] == ["diskutil", "list"]:
                return "/dev/disk0 (internal, physical):\n"
            return "   SMART Status: Not Supported\n"

        with mock.patch.object(self.agent.platform, "system",
                               return_value="Darwin"):
            self.assertNotIn("smart", self.run_with({"diskutil"}, run))

        def failing(argv, timeout=4):
            if argv[:2] == ["diskutil", "list"]:
                return "/dev/disk0 (internal, physical):\n"
            return "   SMART Status: Failing\n"

        with mock.patch.object(self.agent.platform, "system",
                               return_value="Darwin"):
            blocks = self.run_with({"diskutil"}, failing)
        self.assertEqual(blocks["smart"],
                         [{"device": "/dev/disk0", "health": "FAILED"}])

    def test_ups_is_only_collected_when_a_name_is_configured(self):
        def run(argv, timeout=4):
            return ("ups.status: OB DISCHRG\nbattery.charge: 12\n"
                    "battery.runtime: 120\nups.load: 30\n")

        self.assertNotIn("ups", self.run_with({"upsc"}, run))
        blocks = self.run_with({"upsc"}, run, env={"DASH_UPS_NAME": "rack"})
        self.assertEqual(blocks["ups"], {
            "status": "OB DISCHRG", "charge_pct": 12.0,
            "runtime_s": 120.0, "load_pct": 30.0, "battery_low": False,
        })

        def low(argv, timeout=4):
            return "ups.status: OB LB\nbattery.charge: 5\n"

        low_blocks = self.run_with({"upsc"}, low,
                                   env={"DASH_UPS_NAME": "rack"})
        self.assertTrue(low_blocks["ups"]["battery_low"])

    def test_backup_accepts_an_opt_in_timestamp_file_or_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            stamp = Path(tmp) / "last-backup"
            stamp.write_text("1700000000\n")
            blocks = self.run_with(
                set(), lambda argv, timeout=4: "",
                env={"DASH_BACKUP_TS_FILE": str(stamp),
                     "DASH_BACKUP_TOOL": "restic"},
            )
            self.assertEqual(blocks["backup"],
                             {"last_success_ts": 1700000000, "tool": "restic"})

        self.agent._run_shell = mock.Mock(
            return_value='{"last_success_ts": 1700000100}'
        )
        with mock.patch.dict(os.environ,
                             {"DASH_BACKUP_CMD": "backup-status"},
                             clear=False):
            self.assertEqual(self.agent.collect_backup(),
                             {"last_success_ts": 1700000100})

    def test_updates_count_apt_upgrades_and_a_required_reboot(self):
        def run(argv, timeout=4):
            return (
                "Inst libssl3 [3.0.1] (3.0.2 Debian-Security:12/stable-security [amd64])\n"
                "Inst vim [9.0] (9.1 Debian:12/stable [amd64])\n"
            )

        blocks = self.run_with({"apt-get"}, run, exists=True)
        self.assertEqual(blocks["updates"], {
            "pending": 2, "security": 1, "reboot_required": True,
        })

    def test_collect_merges_health_without_replacing_the_core_payload(self):
        with mock.patch.object(self.agent.common, "collect_host",
                               return_value={"name": "mac", "cpu_pct": 1.0}), \
                mock.patch.object(self.agent, "collect_health",
                                  return_value={"smart": [{"device": "/dev/sda"}]}):
            payload = self.agent.collect()
        self.assertEqual(payload["name"], "mac")
        self.assertEqual(payload["smart"][0]["device"], "/dev/sda")

    def test_both_python_agents_keep_the_optional_fields_in_their_source(self):
        for filename in ("dash-agent.py", "pve-host-agent.py"):
            source = (AGENTS / filename).read_text()
            for token in ('"smart"', '"zfs"', '"ups"', '"backup"', '"updates"',
                          "DASH_UPS_NAME", "DASH_BACKUP_TS_FILE",
                          "DASH_BACKUP_CMD", "reboot-required"):
                self.assertIn(token, source, filename + ": " + token)
            self.assertIn("timeout", source)
            self.assertIn("except Exception", source)


class WindowsHealthReportingTest(unittest.TestCase):
    """PowerShell cannot run here, so its health contract is checked at source."""

    def setUp(self):
        self.source = (AGENTS / "dash-agent.ps1").read_text()

    def test_windows_reports_smart_through_the_storage_cmdlets(self):
        self.assertIn("Get-PhysicalDisk", self.source)
        self.assertIn("Get-StorageReliabilityCounter", self.source)
        self.assertIn('"PASSED"', self.source)
        self.assertIn('$body["smart"]', self.source)
        self.assertIn("temperature_c", self.source)
        self.assertIn("power_on_hours", self.source)
        # Each hardware query is best effort: a failure omits the block.
        self.assertIn("try {", self.source)
        self.assertIn("catch { }", self.source)

    def test_windows_updates_are_opt_in_to_the_module_and_reboot_is_reported(self):
        self.assertIn("Get-Command Get-WindowsUpdate", self.source)
        self.assertIn('$body["updates"]', self.source)
        self.assertIn("reboot_required", self.source)
        self.assertIn("WindowsUpdate\\Auto Update\\RebootRequired", self.source)
        self.assertIn("Get-WindowsUpdate", self.source)
