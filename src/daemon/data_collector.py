"""
Data collection from all available system power sources.

Sources (in order of preference / accuracy):
  1. /sys/class/power_supply/   – battery voltage, current, energy
  2. upower (via subprocess)    – parsed for time-remaining
  3. psutil                     – per-process CPU, memory
  4. /sys/class/backlight/      – screen brightness
  5. /proc/net/dev               – network activity (WiFi proxy)
  6. powertop CSV (optional)    – requires root; skipped gracefully if absent

All methods return plain dicts so they are easily serialisable to SQLite.
"""

import os
import re
import glob
import logging
import subprocess
import time
from pathlib import Path
from typing import Optional

import psutil

log = logging.getLogger(__name__)

# ── Battery paths ─────────────────────────────────────────────────────────────

def _find_battery_path() -> Optional[Path]:
    """Return the first available BAT* sysfs path, or None if no battery."""
    for pattern in ("/sys/class/power_supply/BAT*",
                    "/sys/class/power_supply/battery"):
        matches = sorted(glob.glob(pattern))
        if matches:
            return Path(matches[0])
    return None


def _sysfs_read(path: Path, filename: str) -> Optional[str]:
    """Read a single-line sysfs file; return None on any error."""
    try:
        return (path / filename).read_text().strip()
    except (OSError, IOError):
        return None


def _sysfs_int(path: Path, filename: str) -> Optional[int]:
    val = _sysfs_read(path, filename)
    try:
        return int(val) if val is not None else None
    except ValueError:
        return None


# ── Battery info ──────────────────────────────────────────────────────────────

def collect_battery_info() -> dict:
    """
    Return a dict with:
      battery_percent, battery_status, discharge_rate_watts,
      voltage_volts, time_remaining_min
    All values may be None if unavailable.
    """
    result = {
        "battery_percent":      None,
        "battery_status":       None,
        "discharge_rate_watts": None,
        "voltage_volts":        None,
        "time_remaining_min":   None,
    }

    bat = _find_battery_path()
    if bat is None:
        log.debug("No battery found in sysfs")
        return result

    # Capacity (percent)
    cap = _sysfs_int(bat, "capacity")
    result["battery_percent"] = float(cap) if cap is not None else None

    # Status: Charging / Discharging / Full / Unknown
    result["battery_status"] = _sysfs_read(bat, "status") or "Unknown"

    # Voltage (µV → V)
    voltage_uv = _sysfs_int(bat, "voltage_now")
    if voltage_uv is not None:
        result["voltage_volts"] = voltage_uv / 1_000_000

    # Discharge rate – prefer power_now (µW), fall back to current_now × voltage
    power_uw = _sysfs_int(bat, "power_now")
    if power_uw is not None:
        result["discharge_rate_watts"] = power_uw / 1_000_000
    else:
        current_ua = _sysfs_int(bat, "current_now")
        if current_ua is not None and voltage_uv is not None and voltage_uv > 0:
            result["discharge_rate_watts"] = (
                (current_ua / 1_000_000) * (voltage_uv / 1_000_000)
            )

    # Time remaining – try upower first (more accurate), then calculate
    result["time_remaining_min"] = _get_time_remaining_upower()
    if result["time_remaining_min"] is None:
        result["time_remaining_min"] = _calculate_time_remaining(bat, result)

    return result


def _get_time_remaining_upower() -> Optional[float]:
    """Parse `upower -i ...` output for time-to-empty in minutes."""
    try:
        out = subprocess.check_output(
            ["upower", "-i", "/org/freedesktop/UPower/devices/battery_BAT0"],
            stderr=subprocess.DEVNULL, timeout=3
        ).decode()
    except (subprocess.CalledProcessError, FileNotFoundError,
            subprocess.TimeoutExpired, Exception):
        return None

    for line in out.splitlines():
        if "time to empty" in line.lower():
            # e.g. "     time to empty:          2.2 hours"
            m = re.search(r"([\d.]+)\s*(hour|minute)", line, re.I)
            if m:
                val = float(m.group(1))
                unit = m.group(2).lower()
                return val * 60 if unit.startswith("hour") else val
    return None


def _calculate_time_remaining(bat: Path, info: dict) -> Optional[float]:
    """
    Rough estimate: energy_now / discharge_rate.
    Falls back to charge_now / current_now for current-only batteries.
    """
    rate = info.get("discharge_rate_watts")
    if rate is None or rate <= 0:
        return None

    energy_uj = _sysfs_int(bat, "energy_now")
    if energy_uj is not None:
        energy_wh = energy_uj / 1_000_000 / 3600 * 1_000_000  # µWh → Wh
        # sysfs energy_now is in µWh on some kernels, µJ on others
        # Most modern kernels: energy_now in µWh
        energy_wh = _sysfs_int(bat, "energy_now") / 1_000_000  # µWh → Wh
        return (energy_wh / rate) * 60 if energy_wh else None

    # charge-based battery
    charge_now = _sysfs_int(bat, "charge_now")   # µAh
    voltage_v  = info.get("voltage_volts")
    if charge_now and voltage_v:
        energy_wh = (charge_now / 1_000_000) * voltage_v
        return (energy_wh / rate) * 60

    return None


# ── Process stats ─────────────────────────────────────────────────────────────

def collect_process_stats() -> list[dict]:
    """
    Return a list of dicts (one per running process) with:
      pid, name, username, cpu_percent, memory_mb, cmdline
    cpu_percent is the interval-averaged value (0–100 × num_cpus).
    """
    # First call primes the CPU counters; we call it twice with a short gap
    # so we get a real reading instead of 0.0.
    procs = []
    try:
        # Prime counters
        for p in psutil.process_iter(["pid", "name", "username", "cmdline",
                                       "cpu_percent", "memory_info", "status"]):
            pass
    except Exception:
        pass

    time.sleep(0.3)

    for proc in psutil.process_iter(["pid", "name", "username", "cmdline",
                                      "cpu_percent", "memory_info", "status"]):
        try:
            info = proc.info
            if info["status"] in (psutil.STATUS_ZOMBIE, psutil.STATUS_DEAD):
                continue

            mem_mb = 0.0
            if info.get("memory_info"):
                mem_mb = info["memory_info"].rss / 1_048_576  # bytes → MB

            cmdline = ""
            if info.get("cmdline"):
                cmdline = " ".join(info["cmdline"])[:200]

            procs.append({
                "pid":         info["pid"],
                "name":        info["name"] or "",
                "username":    info.get("username") or "unknown",
                "cpu_percent": info.get("cpu_percent") or 0.0,
                "memory_mb":   mem_mb,
                "cmdline":     cmdline,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    return procs


# ── Hardware component stats ──────────────────────────────────────────────────

def collect_component_info() -> dict:
    """
    Return a dict with raw data for component power estimation:
      screen_brightness_pct, wifi_active, usb_device_count, kb_backlight_pct,
      net_bytes_per_sec (proxy for wifi/eth activity)
    """
    return {
        "screen_brightness_pct": _read_screen_brightness(),
        "wifi_active":           _detect_wifi_active(),
        "usb_device_count":      _count_usb_devices(),
        "kb_backlight_pct":      _read_kb_backlight(),
        "net_bytes_per_sec":     _net_activity(),
    }


def _read_screen_brightness() -> Optional[float]:
    """Return brightness as a fraction 0–1."""
    for pattern in (
        "/sys/class/backlight/*/brightness",
        "/sys/class/backlight/*/actual_brightness",
    ):
        paths = sorted(glob.glob(pattern))
        if not paths:
            continue
        bl_path = Path(paths[0]).parent
        cur = _sysfs_int(bl_path, "actual_brightness") or _sysfs_int(bl_path, "brightness")
        max_ = _sysfs_int(bl_path, "max_brightness")
        if cur is not None and max_ and max_ > 0:
            return cur / max_
    return None


def _detect_wifi_active() -> bool:
    """True if any wireless interface is up."""
    try:
        ifaces = os.listdir("/sys/class/net")
        for iface in ifaces:
            wireless_path = Path(f"/sys/class/net/{iface}/wireless")
            if wireless_path.exists():
                operstate = Path(f"/sys/class/net/{iface}/operstate").read_text().strip()
                if operstate == "up":
                    return True
    except OSError:
        pass
    return False


def _count_usb_devices() -> int:
    """Count connected USB devices (excluding hubs)."""
    try:
        return len(glob.glob("/sys/bus/usb/devices/[0-9]*-[0-9]*"))
    except OSError:
        return 0


def _read_kb_backlight() -> Optional[float]:
    """Return keyboard backlight as 0–1, or None if absent."""
    for pattern in (
        "/sys/class/leds/*kbd*backlight*/brightness",
        "/sys/class/leds/*kbd*/brightness",
    ):
        paths = sorted(glob.glob(pattern))
        if not paths:
            continue
        led_path = Path(paths[0]).parent
        cur  = _sysfs_int(led_path, "brightness")
        max_ = _sysfs_int(led_path, "max_brightness")
        if cur is not None and max_ and max_ > 0:
            return cur / max_
    return None


_last_net_bytes: dict = {}
_last_net_time: float = 0.0


def _net_activity() -> float:
    """Return approximate bytes/sec across all interfaces (rolling average)."""
    global _last_net_bytes, _last_net_time
    try:
        counters = psutil.net_io_counters(pernic=True)
        now = time.monotonic()
        total = sum(
            c.bytes_sent + c.bytes_recv
            for iface, c in counters.items()
            if not iface.startswith("lo")
        )
        if _last_net_time and (now - _last_net_time) > 0:
            delta_bytes = total - sum(_last_net_bytes.values())
            delta_t     = now - _last_net_time
            rate = delta_bytes / delta_t
        else:
            rate = 0.0

        _last_net_bytes = {iface: c.bytes_sent + c.bytes_recv
                           for iface, c in counters.items()}
        _last_net_time = now
        return max(0.0, rate)
    except Exception:
        return 0.0
