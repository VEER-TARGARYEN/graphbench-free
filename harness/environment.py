"""Capture the measurement environment into every run manifest.

The brief requires the same client machine and region for every platform, and Raasveldt
et al. name non-reproducibility - undocumented hardware and versions - as pitfall #1.
Capturing this in code rather than prose means it cannot drift out of date, and it means
a reader can tell whether a sub-millisecond figure is meaningful on the machine that
produced it.

Two fields here matter more than the rest:

  timer.sleep_granularity_ms - Windows' default is ~15.6 ms, which silently caps any
      scheduled dispatch rate. The harness raises it to ~1 ms, but the measured value is
      recorded so the claim is checkable rather than asserted.

  client.egress_ip / region  - on a WAN-reached managed tier the round trip is mostly
      geography. A client in South Asia measuring an instance in us-east pays ~265 ms
      before the engine does any work, which swamps engine differences by two orders of
      magnitude. Where the client sits is a first-class experimental variable, not
      trivia.
"""
from __future__ import annotations

import json
import os
import platform
import sys
import urllib.request


def _cpu_count() -> dict:
    try:
        logical = os.cpu_count()
    except Exception:
        logical = None
    out = {"logical": logical}
    try:  # Python 3.13+; absent on 3.12
        out["usable"] = len(os.sched_getaffinity(0))  # type: ignore[attr-defined]
    except Exception:
        pass
    return out


def _total_ram_mb() -> int | None:
    """Total physical RAM, without adding a dependency for it."""
    try:
        if sys.platform == "win32":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            m = MEMORYSTATUSEX()
            m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
            return int(m.ullTotalPhys // (1024 * 1024))
        return int(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") // (1024 * 1024))
    except Exception:
        return None


def _egress(timeout: float = 6.0) -> dict:
    """Where the CLIENT is, as the internet sees it.

    Best-effort and explicitly allowed to fail: a benchmark that refuses to run because
    an IP-geolocation service was down would be a worse benchmark. Recorded as
    "unavailable" rather than guessed.
    """
    try:
        req = urllib.request.Request(
            "https://ipinfo.io/json", headers={"User-Agent": "graphbench-free/1.0"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
        return {
            "ip": d.get("ip"),
            "city": d.get("city"),
            "region": d.get("region"),
            "country": d.get("country"),
            "org": d.get("org"),
        }
    except Exception as e:
        return {"unavailable": type(e).__name__}


def capture(include_network: bool = True) -> dict:
    from .scheduler import set_timer_resolution

    try:
        import neo4j

        driver_version = neo4j.__version__
    except Exception:
        driver_version = None

    env = {
        "python": sys.version.split()[0],
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
        },
        "cpu": _cpu_count(),
        "ram_total_mb": _total_ram_mb(),
        "neo4j_driver": driver_version,
        "timer": set_timer_resolution(),
    }
    if include_network:
        env["client_location"] = _egress()
    return env


def summarise(env: dict) -> str:
    p, cpu = env.get("platform", {}), env.get("cpu", {})
    loc = env.get("client_location", {})
    where = ", ".join(
        str(loc[k]) for k in ("city", "region", "country") if loc.get(k)
    ) or "location unavailable"
    return (
        f"{p.get('system')} {p.get('release')} / {p.get('machine')} / "
        f"{cpu.get('logical')} logical cores / {env.get('ram_total_mb')} MB RAM / "
        f"Python {env.get('python')} / neo4j driver {env.get('neo4j_driver')} / "
        f"timer granularity {env.get('timer', {}).get('sleep_granularity_ms')} ms / "
        f"client in {where}"
    )
