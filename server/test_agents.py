"""Stdlib tests for the agents' disk reporting.

The Windows agent cannot run here (no PowerShell), so that file is checked at
the source level. The Unix collector is exercised directly with its mount
table and disk usage stubbed: the interesting rules are which mounts count as
drives, deduplication and the summary percentage.
"""
import importlib.util
import types
import unittest
from pathlib import Path
from unittest import mock

AGENTS = Path(__file__).resolve().parent.parent / "agents"


def load_agent(filename, module_name):
    spec = importlib.util.spec_from_file_location(module_name, AGENTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_unix_agent():
    return load_agent("dash-agent.py", "dash_agent_under_test")


class UnixDiskReportingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agent = load_unix_agent()

    def collect_disks(self, table, dev_ids, sizes, labels=None):
        """Run dash-agent's disks() against a stubbed mount table."""
        def usage(path):
            total = sizes[path]
            return types.SimpleNamespace(total=total, used=total // 2, free=total // 2)

        with mock.patch.object(self.agent, "mounts", return_value=iter(table)), \
                mock.patch.object(self.agent, "volume_labels", return_value=labels or {}), \
                mock.patch("os.stat", side_effect=lambda p: types.SimpleNamespace(st_dev=dev_ids[p])), \
                mock.patch.object(self.agent.shutil, "disk_usage", side_effect=usage):
            return self.agent.disks()

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

        result = self.collect_disks(table, dev_ids, sizes, {"/dev/sdb1": "Data"})

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
        with mock.patch.object(self.agent.platform, "system", return_value="Darwin"), \
                mock.patch.object(self.agent, "run", return_value=lines):
            parsed = list(self.agent.mounts())

        self.assertEqual(parsed, [
            ("/dev/disk3s1s1", "/", "apfs"),
            ("/dev/disk4s1", "/Volumes/My (Backup) Drive", "exfat"),
        ])

    def test_summary_uses_the_fullest_volume(self):
        with mock.patch.object(self.agent, "cpu_pct", return_value=1.0), \
                mock.patch.object(self.agent, "mem", return_value=(1, 2)), \
                mock.patch.object(self.agent, "uptime_s", return_value=3), \
                mock.patch.object(self.agent, "load_average",
                                  return_value=(None, None, None)), \
                mock.patch.object(self.agent, "swap", return_value=(None, None)), \
                mock.patch.object(self.agent, "net_rates",
                                  return_value=(None, None)), \
                mock.patch.object(self.agent, "cpu_extra",
                                  return_value=(None, None)), \
                mock.patch.object(self.agent, "disks", return_value=[
                    {"mount": "/", "label": "System", "total": 100, "used": 10, "pct": 10.0},
                    {"mount": "/mnt/data", "label": "Data", "total": 100, "used": 99, "pct": 99.0},
                ]):
            payload = self.agent.collect()

        self.assertEqual(payload["disk_pct"], 99.0)
        self.assertEqual(len(payload["disks"]), 2)


class UnixSignalReportingTest(unittest.TestCase):
    """Load, swap, network and CPU pressure on Linux/macOS.

    The collectors are pure enough to exercise with stubbed counters and files,
    which is the only way to test both platform branches here.
    """

    @classmethod
    def setUpClass(cls):
        cls.agent = load_unix_agent()

    def test_linux_swap_reads_swap_total_and_free(self):
        info = {"SwapTotal": 1000, "SwapFree": 400}
        with mock.patch.object(self.agent, "meminfo", return_value=info):
            self.assertEqual(self.agent.swap(), (600, 1000))

    def test_macos_swap_parses_sysctl_sizes(self):
        out = "total = 2.00G  used = 512.00M  free = 1.50G"
        with mock.patch.object(self.agent.platform, "system", return_value="Darwin"), \
                mock.patch.object(self.agent, "run", return_value=out):
            self.assertEqual(
                self.agent.swap(), (512 * 1024 ** 2, 2 * 1024 ** 3)
            )

    def test_load_average_rounds_and_reports_missing_as_none(self):
        with mock.patch.object(self.agent.os, "getloadavg",
                               return_value=(0.1234, 0.5, 1.0)):
            self.assertEqual(self.agent.load_average(), (0.12, 0.5, 1.0))
        with mock.patch.object(self.agent.os, "getloadavg", side_effect=OSError):
            self.assertEqual(
                self.agent.load_average(), (None, None, None)
            )

    def test_net_rates_are_a_delta_over_elapsed_time(self):
        with mock.patch.object(self.agent, "net_counters",
                               side_effect=[(1000, 500), (1600, 800)]), \
                mock.patch.object(self.agent.time, "time",
                                  side_effect=[10.0, 11.0]), \
                mock.patch.object(self.agent.time, "sleep"):
            self.assertEqual(self.agent.net_rates(), (600, 300))

    def test_net_rates_ignore_a_counter_reset(self):
        with mock.patch.object(self.agent, "net_counters",
                               side_effect=[(2000, 500), (100, 600)]), \
                mock.patch.object(self.agent.time, "time",
                                  side_effect=[10.0, 11.0]), \
                mock.patch.object(self.agent.time, "sleep"):
            self.assertEqual(self.agent.net_rates(), (None, None))

    def test_collect_includes_present_signals_and_omits_missing_ones(self):
        with mock.patch.object(self.agent, "cpu_pct", return_value=1.0), \
                mock.patch.object(self.agent, "mem", return_value=(1, 2)), \
                mock.patch.object(self.agent, "uptime_s", return_value=3), \
                mock.patch.object(self.agent, "disks", return_value=[]), \
                mock.patch.object(self.agent, "load_average",
                                  return_value=(0.5, 0.4, 0.3)), \
                mock.patch.object(self.agent, "swap", return_value=(100, 400)), \
                mock.patch.object(self.agent, "net_rates",
                                  return_value=(10, 20)), \
                mock.patch.object(self.agent, "cpu_extra",
                                  return_value=(1.0, 0.5)):
            payload = self.agent.collect()

        self.assertEqual(payload["load_1"], 0.5)
        self.assertEqual(payload["load_15"], 0.3)
        self.assertEqual(payload["swap_used"], 100)
        self.assertEqual(payload["swap_total"], 400)
        self.assertEqual(payload["net_rx_bps"], 10)
        self.assertEqual(payload["cpu_steal"], 0.5)

        with mock.patch.object(self.agent, "cpu_pct", return_value=1.0), \
                mock.patch.object(self.agent, "mem", return_value=(1, 2)), \
                mock.patch.object(self.agent, "uptime_s", return_value=3), \
                mock.patch.object(self.agent, "disks", return_value=[]), \
                mock.patch.object(self.agent, "load_average",
                                  return_value=(None, None, None)), \
                mock.patch.object(self.agent, "swap", return_value=(None, None)), \
                mock.patch.object(self.agent, "net_rates",
                                  return_value=(None, None)), \
                mock.patch.object(self.agent, "cpu_extra",
                                  return_value=(None, None)):
            empty = self.agent.collect()

        for key in ("load_1", "swap_used", "net_rx_bps", "cpu_iowait"):
            self.assertNotIn(key, empty)


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


class PveHostDiskReportingTest(unittest.TestCase):
    """The Proxmox host reports its root volume; LXC guests have no volumes."""

    @classmethod
    def setUpClass(cls):
        cls.agent = load_agent("pve-host-agent.py", "pve_host_agent_under_test")

    def test_host_payload_carries_the_root_volume_and_guests_do_not(self):
        pushes = {}

        def capture(name, payload):
            pushes[name] = payload
            return 200

        volume = types.SimpleNamespace(total=1000, used=900, free=100)
        guest = {"name": "adguard", "vmid": 101, "cpu": 0.1, "mem": 1,
                 "maxmem": 2, "uptime": 3}
        with mock.patch.object(self.agent, "push", side_effect=capture), \
                mock.patch.object(self.agent, "cpu_pct", return_value=1.0), \
                mock.patch.object(self.agent, "mem", return_value=(1, 2)), \
                mock.patch.object(self.agent, "uptime", return_value=3), \
                mock.patch.object(self.agent, "guests", return_value=[guest]), \
                mock.patch.object(self.agent, "load_average",
                                  return_value=(0.5, 0.4, 0.3)), \
                mock.patch.object(self.agent, "swap", return_value=(100, 400)), \
                mock.patch.object(self.agent, "net_rates",
                                  return_value=(10, 20)), \
                mock.patch.object(self.agent, "cpu_extra",
                                  return_value=(1.0, 0.5)), \
                mock.patch.object(self.agent.shutil, "disk_usage",
                                  return_value=volume):
            self.agent.main()

        disks = pushes["pve"]["disks"]
        self.assertEqual(len(disks), 1)
        self.assertEqual(disks[0]["mount"], "/")
        self.assertEqual(disks[0]["total"], 1000)
        self.assertEqual(disks[0]["used"], 900)
        self.assertEqual(disks[0]["pct"], 90.0)
        self.assertEqual(pushes["pve"]["disk_pct"], 90.0)
        self.assertEqual(pushes["pve"]["load_1"], 0.5)
        self.assertEqual(pushes["pve"]["swap_total"], 400)
        self.assertEqual(pushes["pve"]["net_rx_bps"], 10)
        self.assertEqual(pushes["pve"]["cpu_iowait"], 1.0)
        # Guests have no host-level load/swap/network data to report.
        for key in ("disks", "load_1", "swap_total", "net_rx_bps"):
            self.assertNotIn(key, pushes["adguard"])
