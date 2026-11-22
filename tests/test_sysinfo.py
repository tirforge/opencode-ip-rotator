import unittest
from unittest.mock import patch

from prometheus_client.parser import text_string_to_metric_families

import core.sysinfo as sysinfo

SAMPLE = """# HELP node_cpu_seconds_total Seconds the CPUs spent in each mode.
# TYPE node_cpu_seconds_total counter
node_cpu_seconds_total{cpu="0",mode="idle"} 8000
node_cpu_seconds_total{cpu="0",mode="user"} 1000
node_cpu_seconds_total{cpu="0",mode="system"} 500
node_cpu_seconds_total{cpu="0",mode="iowait"} 100
node_cpu_seconds_total{cpu="1",mode="idle"} 8100
node_cpu_seconds_total{cpu="1",mode="user"} 900
node_cpu_seconds_total{cpu="1",mode="system"} 400
node_cpu_seconds_total{cpu="1",mode="iowait"} 80
# TYPE node_memory_MemTotal_bytes gauge
node_memory_MemTotal_bytes 1073741824
node_memory_MemAvailable_bytes 536870912
node_memory_SwapTotal_bytes 2147483648
node_memory_SwapFree_bytes 1610612736
# TYPE node_filesystem_size_bytes gauge
node_filesystem_size_bytes{fstype="ext4",mountpoint="/"} 42949672960
node_filesystem_avail_bytes{fstype="ext4",mountpoint="/"} 21474836480
node_load1 0.42
node_load5 0.31
node_load15 0.22
node_boot_time_seconds 1700000000
node_network_receive_bytes_total{device="eth0"} 123456
node_network_transmit_bytes_total{device="eth0"} 654321
node_network_receive_bytes_total{device="lo"} 999
node_procs_running 3
node_procs_blocked 0
node_filefd_allocated 512
node_filefd_maximum 65536
node_uname_info{nodename="oracle-vps",release="6.14.0-1009-oracle",machine="x86_64"} 1
node_exporter_build_info{version="1.9.1"} 1
"""


def _groups(text=SAMPLE):
    groups = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            groups.setdefault(sample.name, []).append(sample)
    return groups


class SysinfoTests(unittest.TestCase):
    def setUp(self):
        sysinfo._prev.update({"cpu": None, "net": None})  # noqa: SLF001

    def test_build_reports_cpu_memory_disk_and_host(self):
        data = sysinfo._build(_groups(), 1700000100.0)  # noqa: SLF001
        self.assertTrue(data["available"])
        self.assertEqual(data["host"]["nodename"], "oracle-vps")
        self.assertEqual(data["host"]["kernel"], "6.14.0-1009-oracle")
        self.assertEqual(data["host"]["exporter_version"], "1.9.1")
        self.assertEqual(data["cpu"]["cores"], 2)
        self.assertTrue(data["cpu"]["since_boot"])
        self.assertAlmostEqual(data["cpu"]["percent"], 14.7, places=1)
        self.assertEqual(data["cpu"]["load1"], 0.42)
        self.assertEqual(data["memory"]["total"], 1073741824)
        self.assertEqual(data["memory"]["used"], 536870912)
        self.assertAlmostEqual(data["memory"]["percent"], 50.0, places=1)
        self.assertAlmostEqual(data["memory"]["swap_percent"], 25.0, places=1)
        self.assertTrue(data["disk"]["available"])
        self.assertAlmostEqual(data["disk"]["percent"], 50.0, places=1)
        self.assertEqual(data["network"]["rx_total"], 123456)
        self.assertEqual(data["network"]["tx_total"], 654321)
        self.assertEqual(data["processes"]["running"], 3)
        self.assertEqual(data["files"]["maximum"], 65536)

    def test_second_scrape_uses_delta_for_cpu(self):
        sysinfo._build(_groups(), 1700000100.0)  # noqa: SLF001 - baseline
        busy = SAMPLE.replace('mode="idle"} 8000', 'mode="idle"} 8001').replace(
            'mode="idle"} 8100', 'mode="idle"} 8100'
        )
        data = sysinfo._build(_groups(busy), 1700000110.0)  # noqa: SLF001
        self.assertFalse(data["cpu"]["since_boot"])

    def test_snapshot_degrades_when_exporter_is_down(self):
        with patch.object(sysinfo, "_scrape", side_effect=OSError("connection refused")):
            data = sysinfo.snapshot()
        self.assertFalse(data["available"])
        self.assertIn("connection refused", data["error"])


if __name__ == "__main__":
    unittest.main()
