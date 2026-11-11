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


def load_unix_agent():
    spec = importlib.util.spec_from_file_location(
        "dash_agent_under_test", AGENTS / "dash-agent.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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
                mock.patch.object(self.agent, "disks", return_value=[
                    {"mount": "/", "label": "System", "total": 100, "used": 10, "pct": 10.0},
                    {"mount": "/mnt/data", "label": "Data", "total": 100, "used": 99, "pct": 99.0},
                ]):
            payload = self.agent.collect()

        self.assertEqual(payload["disk_pct"], 99.0)
        self.assertEqual(len(payload["disks"]), 2)


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
