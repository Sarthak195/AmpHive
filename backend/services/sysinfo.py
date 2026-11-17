"""
Host and process health snapshot for the admin console (GET /api/admin/health).

Computed on request, no background task. Every source degrades to None when
its file is missing or unreadable (non-Linux dev boxes, no cgroup v2), so the
endpoint never raises because of the host. Memory figures are MiB.
"""

import shutil
import time
from typing import Optional

_MIB = 1024 * 1024
_PROCESS_START = time.monotonic()


def _read(path: str) -> Optional[str]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()
    except (OSError, ValueError):
        return None


def _kib_fields(text: Optional[str]) -> dict:
    """Parse '/proc/meminfo' / '/proc/self/status' style 'Key:   123 kB' lines."""
    out: dict = {}
    for line in (text or "").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if parts and parts[0].isdigit():
            out[key.strip()] = int(parts[0])
    return out


def parse_meminfo(text: Optional[str]) -> Optional[dict]:
    f = _kib_fields(text)
    if "MemTotal" not in f or "MemAvailable" not in f:
        return None
    swap_total = f.get("SwapTotal", 0)
    return {
        "total_mb": round(f["MemTotal"] / 1024, 1),
        "available_mb": round(f["MemAvailable"] / 1024, 1),
        "swap_total_mb": round(swap_total / 1024, 1),
        "swap_used_mb": round((swap_total - f.get("SwapFree", 0)) / 1024, 1),
    }


def parse_cgroup(current: Optional[str], limit: Optional[str]) -> Optional[dict]:
    """cgroup v2 memory.current / memory.max ('max' means unlimited -> None)."""
    try:
        cur = int((current or "").strip())
    except ValueError:
        return None
    lim = (limit or "").strip()
    return {
        "current_mb": round(cur / _MIB, 1),
        "limit_mb": round(int(lim) / _MIB, 1) if lim.isdigit() else None,
    }


def parse_rss_mb(text: Optional[str]) -> Optional[float]:
    rss = _kib_fields(text).get("VmRSS")
    return round(rss / 1024, 1) if rss is not None else None


def disk_usage(path: str = "/") -> Optional[dict]:
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    return {
        "total_gb": round(u.total / 1024**3, 2),
        "free_gb": round(u.free / 1024**3, 2),
        "used_pct": round(100 * u.used / u.total, 1) if u.total else None,
    }


def host_uptime_s() -> Optional[int]:
    parts = (_read("/proc/uptime") or "").split()
    try:
        return int(float(parts[0]))
    except (IndexError, ValueError):
        return None


def collect() -> dict:
    return {
        "memory": parse_meminfo(_read("/proc/meminfo")),
        "cgroup": parse_cgroup(
            _read("/sys/fs/cgroup/memory.current"), _read("/sys/fs/cgroup/memory.max")
        ),
        "disk": disk_usage("/"),
        "backend_rss_mb": parse_rss_mb(_read("/proc/self/status")),
        "host_uptime_s": host_uptime_s(),
        "process_uptime_s": int(time.monotonic() - _PROCESS_START),
    }
