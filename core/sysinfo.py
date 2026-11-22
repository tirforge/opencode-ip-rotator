"""Host metrics scraped from Prometheus ``node_exporter``.

``node_exporter`` is the de-facto standard single-binary host collector
(https://github.com/prometheus/node_exporter). We install it as a systemd
service bound to loopback and parse its ``/metrics`` text here with
``prometheus_client``'s own parser - no metric format is hand-rolled.

If the exporter is not reachable the API simply reports ``available: false``
so the dashboard keeps working.
"""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.request import Request, urlopen

from prometheus_client.parser import text_string_to_metric_families

log = logging.getLogger("core.sysinfo")

NODE_EXPORTER_URL = os.environ.get(
    "NODE_EXPORTER_URL", "http://127.0.0.1:9100/metrics"
).strip()
CACHE_SECONDS = 2.0

_cache_lock = threading.Lock()
_cache: Dict[str, Any] = {"at": 0.0, "data": None, "error_logged_at": 0.0}
_prev_lock = threading.Lock()
_prev: Dict[str, Any] = {"cpu": None, "net": None}

Labels = Dict[str, str]


def _sample_value(sample: Any) -> float:
    value = getattr(sample, "value", 0.0)
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _sum(samples: List[Any]) -> float:
    return float(sum(_sample_value(sample) for sample in samples))


def _pick(
    samples: List[Any], **wanted: str
) -> Optional[float]:
    for sample in samples:
        labels = getattr(sample, "labels", {}) or {}
        if all(labels.get(key) == value for key, value in wanted.items()):
            return _sample_value(sample)
    return None


def _labels_of(samples: List[Any]) -> Labels:
    for sample in samples:
        labels = getattr(sample, "labels", None)
        if labels:
            return dict(labels)
    return {}


def _scrape() -> Tuple[Dict[str, List[Any]], float]:
    request = Request(
        NODE_EXPORTER_URL,
        headers={"Accept": "text/plain", "User-Agent": "opencode-proxy-sysinfo/1.0"},
    )
    with urlopen(request, timeout=4) as response:
        raw = response.read().decode("utf-8", errors="replace")

    groups: Dict[str, List[Any]] = {}
    for family in text_string_to_metric_families(raw):
        for sample in family.samples:
            groups.setdefault(sample.name, []).append(sample)
    return groups, time.time()


def _cpu_snapshot(
    groups: Dict[str, List[Any]], now: Optional[float] = None
) -> Dict[str, Any]:
    samples = groups.get("node_cpu_seconds_total") or []
    idle = 0.0
    total = 0.0
    cores = set()
    for sample in samples:
        labels = getattr(sample, "labels", {}) or {}
        mode = labels.get("mode", "")
        value = _sample_value(sample)
        total += value
        if mode in ("idle", "iowait"):
            idle += value
        if labels.get("cpu"):
            cores.add(labels["cpu"])

    now = time.time() if now is None else now
    with _prev_lock:
        previous = _prev["cpu"]
        _prev["cpu"] = (idle, total, now)

    percent: Optional[float] = None
    since_boot = True
    if previous:
        prev_idle, prev_total, prev_at = previous
        delta_total = total - prev_total
        delta_idle = idle - prev_idle
        if delta_total > 0 and now - prev_at > 0.2:
            percent = max(0.0, min(100.0, 100.0 * (1.0 - delta_idle / delta_total)))
            since_boot = False
    if percent is None and total > 0:
        percent = max(0.0, min(100.0, 100.0 * (1.0 - idle / total)))

    return {
        "percent": round(percent, 1) if percent is not None else None,
        "cores": len(cores) or os.cpu_count() or 1,
        "since_boot": since_boot,
    }


def _net_snapshot(
    groups: Dict[str, List[Any]], now: Optional[float] = None
) -> Dict[str, Any]:
    rx = tx = 0.0
    for name, direction in (
        ("node_network_receive_bytes_total", "rx"),
        ("node_network_transmit_bytes_total", "tx"),
    ):
        for sample in groups.get(name) or []:
            device = (getattr(sample, "labels", {}) or {}).get("device", "")
            if device in ("lo",) or device.startswith(("veth", "docker", "br-")):
                continue
            if direction == "rx":
                rx += _sample_value(sample)
            else:
                tx += _sample_value(sample)

    now = time.time() if now is None else now
    with _prev_lock:
        previous = _prev["net"]
        _prev["net"] = (rx, tx, now)

    rx_rate = tx_rate = 0.0
    if previous:
        prev_rx, prev_tx, prev_at = previous
        span = now - prev_at
        if span > 0.2:
            rx_rate = max(0.0, (rx - prev_rx) / span)
            tx_rate = max(0.0, (tx - prev_tx) / span)

    return {
        "rx_total": int(rx),
        "tx_total": int(tx),
        "rx_per_sec": round(rx_rate, 1),
        "tx_per_sec": round(tx_rate, 1),
    }


def _filesystem(groups: Dict[str, List[Any]]) -> Dict[str, Any]:
    sizes = groups.get("node_filesystem_size_bytes") or []
    avail = groups.get("node_filesystem_avail_bytes") or []

    def pick_for(mount: str) -> Optional[Tuple[float, float]]:
        size = _pick(sizes, mountpoint=mount, fstype="ext4") or _pick(
            sizes, mountpoint=mount
        )
        free = _pick(avail, mountpoint=mount, fstype="ext4") or _pick(
            avail, mountpoint=mount
        )
        if size is None or free is None:
            return None
        return size, free

    chosen = pick_for("/") or pick_for("/var") or pick_for("/home")
    if not chosen:
        # Fall back to the largest real filesystem.
        best: Optional[Tuple[float, float]] = None
        for sample in sizes:
            labels = getattr(sample, "labels", {}) or {}
            fstype = labels.get("fstype", "")
            if fstype in ("tmpfs", "overlay", "squashfs", "proc", "sysfs"):
                continue
            size = _sample_value(sample)
            free = _pick(avail, mountpoint=labels.get("mountpoint", ""))
            if free is None:
                continue
            if best is None or size > best[0]:
                best = (size, free)
        chosen = best
    if not chosen:
        return {"available": False}

    size, free = chosen
    used = max(0.0, size - free)
    return {
        "available": True,
        "total": int(size),
        "used": int(used),
        "free": int(free),
        "percent": round(used / size * 100, 1) if size else 0.0,
        "mount": "/",
    }


def _build(groups: Dict[str, List[Any]], scraped_at: float) -> Dict[str, Any]:
    def first(name: str, default: float = 0.0) -> float:
        samples = groups.get(name) or []
        return _sample_value(samples[0]) if samples else default

    mem_total = first("node_memory_MemTotal_bytes")
    mem_available = first("node_memory_MemAvailable_bytes")
    if not mem_available:
        mem_available = first("node_memory_MemFree_bytes") + first(
            "node_memory_Cached_bytes"
        )
    swap_total = first("node_memory_SwapTotal_bytes")
    swap_free = first("node_memory_SwapFree_bytes")

    mem_used = max(0.0, mem_total - mem_available)
    swap_used = max(0.0, swap_total - swap_free)

    cpu = _cpu_snapshot(groups, scraped_at)
    uname = _labels_of(groups.get("node_uname_info") or [])
    boot_time = first("node_boot_time_seconds")
    exporter_version = _labels_of(
        groups.get("node_exporter_build_info") or []
    ).get("version", "")

    procs_running = first("node_procs_running")
    procs_blocked = first("node_procs_blocked")
    processes = 0
    for sample in groups.get("node_processes_state") or []:
        processes += int(_sample_value(sample))
    if not processes:
        processes = int(first("node_process_count", 0))

    fds_allocated = first("node_filefd_allocated")
    fds_max = first("node_filefd_maximum")

    return {
        "available": True,
        "scraped_at": scraped_at,
        "host": {
            "nodename": uname.get("nodename") or socket.gethostname(),
            "kernel": uname.get("release", ""),
            "machine": uname.get("machine", ""),
            "boot_time": int(boot_time) if boot_time else 0,
            "uptime_seconds": int(max(0, time.time() - boot_time)) if boot_time else 0,
            "exporter_version": exporter_version,
        },
        "cpu": {
            **cpu,
            "load1": round(first("node_load1"), 2),
            "load5": round(first("node_load5"), 2),
            "load15": round(first("node_load15"), 2),
        },
        "memory": {
            "total": int(mem_total),
            "used": int(mem_used),
            "available": int(mem_available),
            "percent": round(mem_used / mem_total * 100, 1) if mem_total else 0.0,
            "swap_total": int(swap_total),
            "swap_used": int(swap_used),
            "swap_percent": round(swap_used / swap_total * 100, 1) if swap_total else 0.0,
        },
        "disk": _filesystem(groups),
        "network": _net_snapshot(groups, scraped_at),
        "processes": {
            "running": int(procs_running),
            "blocked": int(procs_blocked),
            "total": processes,
        },
        "files": {
            "allocated": int(fds_allocated),
            "maximum": int(fds_max),
        },
    }


def snapshot() -> Dict[str, Any]:
    """Cached host snapshot. Never raises."""
    now = time.time()
    with _cache_lock:
        cached = _cache["data"]
        if cached is not None and now - float(_cache["at"]) < CACHE_SECONDS:
            return cached

    try:
        groups, scraped_at = _scrape()
        data = _build(groups, scraped_at)
    except Exception as exc:  # noqa: BLE001 - dashboard must degrade gracefully
        with _cache_lock:
            if now - float(_cache["error_logged_at"]) > 60:
                _cache["error_logged_at"] = now
                log.warning("node_exporter unreachable (%s): %s", NODE_EXPORTER_URL, exc)
            data = {
                "available": False,
                "error": str(exc),
                "scraped_at": now,
            }

    with _cache_lock:
        _cache["data"] = data
        _cache["at"] = now
    return data
