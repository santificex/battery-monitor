"""
Power Attribution Engine
========================
Estimates watts consumed by each process and by hardware components.

Since per-process wattage can't be measured directly without root/eBPF,
we use a layered heuristic:

  total_discharge_W   = read from battery sysfs (ground truth)
  component_overhead  = screen + wifi + usb + keyboard backlight
  process_pool_W      = total - overhead - base_kernel_W
  per_process_W       = process_pool_W × (cpu% / total_cpu%)
                       + memory_contribution

This won't be as accurate as powertop, but it's useful for ranking.
"""

import logging
from typing import Optional

log = logging.getLogger(__name__)

# ── Tunable constants ─────────────────────────────────────────────────────────

# Fixed overhead estimates (watts) when a component is fully active
SCREEN_MAX_W    = 4.0   # modern IPS at 100% brightness
SCREEN_MIN_W    = 0.5   # OLED at 0% (still needs some power)
WIFI_ACTIVE_W   = 1.5
WIFI_IDLE_W     = 0.3
USB_PER_DEVICE_W = 0.5
KB_BACKLIGHT_W  = 0.3
BASE_KERNEL_W   = 2.0   # firmware, kernel threads, irq overhead
MEMORY_MW_PER_MB = 0.8  # ~0.8 mW per 1 MB RSS (DRAM leakage proxy)

# Fallback total discharge when battery reading is unavailable
FALLBACK_TOTAL_W = 10.0


def estimate_component_watts(component_info: dict) -> list[dict]:
    """
    Given the raw dict from data_collector.collect_component_info(),
    return a list of {"component": str, "estimated_watts": float} dicts.
    """
    components = []

    # Screen
    brightness = component_info.get("screen_brightness_pct")
    if brightness is not None:
        screen_w = SCREEN_MIN_W + brightness * (SCREEN_MAX_W - SCREEN_MIN_W)
    else:
        screen_w = SCREEN_MAX_W * 0.5  # assume 50% if unknown
    components.append({"component": "Screen", "estimated_watts": round(screen_w, 2)})

    # WiFi / Ethernet
    wifi_w = WIFI_ACTIVE_W if component_info.get("wifi_active") else WIFI_IDLE_W
    # Boost slightly for high network activity
    net_bps = component_info.get("net_bytes_per_sec", 0)
    if net_bps > 1_000_000:   # > 1 MB/s
        wifi_w = min(wifi_w * 1.5, 3.0)
    components.append({"component": "WiFi/Network", "estimated_watts": round(wifi_w, 2)})

    # USB devices
    usb_count = component_info.get("usb_device_count", 0)
    usb_w = usb_count * USB_PER_DEVICE_W
    components.append({"component": f"USB ({usb_count} devices)",
                        "estimated_watts": round(usb_w, 2)})

    # Keyboard backlight
    kb_pct = component_info.get("kb_backlight_pct")
    if kb_pct is not None and kb_pct > 0:
        kb_w = kb_pct * KB_BACKLIGHT_W
        components.append({"component": "Keyboard Backlight",
                            "estimated_watts": round(kb_w, 2)})

    # Base kernel / firmware overhead
    components.append({"component": "Kernel/Firmware",
                        "estimated_watts": round(BASE_KERNEL_W, 2)})

    return components


def attribute_process_power(
    processes: list[dict],
    component_watts: list[dict],
    total_discharge_w: Optional[float],
) -> list[dict]:
    """
    Assign an estimated_watts value to each process dict (in-place + return).

    Strategy:
      1. Sum all component overhead watts.
      2. Remaining watts go to processes (CPU-proportional) + memory.
      3. Floor total at FALLBACK_TOTAL_W so we always show something sensible.
    """
    if not processes:
        return processes

    # Total system power
    total_w = total_discharge_w if (total_discharge_w and total_discharge_w > 0.5) \
              else FALLBACK_TOTAL_W

    # Component pool already spent
    component_total = sum(c["estimated_watts"] for c in component_watts)
    process_pool_w  = max(0.0, total_w - component_total)

    # Sum of all CPU percentages across processes
    total_cpu = sum(p.get("cpu_percent", 0) for p in processes)

    for proc in processes:
        cpu_pct = proc.get("cpu_percent", 0)
        mem_mb  = proc.get("memory_mb", 0)

        # CPU contribution
        if total_cpu > 0:
            cpu_share_w = process_pool_w * (cpu_pct / total_cpu)
        else:
            cpu_share_w = 0.0

        # Memory contribution (DRAM leakage proxy)
        mem_w = (mem_mb * MEMORY_MW_PER_MB) / 1000.0

        proc["estimated_watts"] = round(cpu_share_w + mem_w, 3)

    # Normalise so sum of all processes ≤ process_pool_w (avoids over-attribution)
    total_attributed = sum(p["estimated_watts"] for p in processes)
    if total_attributed > process_pool_w and total_attributed > 0:
        scale = process_pool_w / total_attributed
        for proc in processes:
            proc["estimated_watts"] = round(proc["estimated_watts"] * scale, 3)

    return processes


def detect_spike(
    process_name: str,
    current_watts: float,
    history: list,  # list of sqlite3.Row with (estimated_watts, timestamp)
    threshold_w: float = 5.0,
) -> bool:
    """
    Return True if the process's current watts represent a significant spike
    relative to its recent average.
    """
    if len(history) < 3:
        return False
    avg = sum(row["estimated_watts"] for row in history) / len(history)
    return current_watts > avg + threshold_w and current_watts > 1.0
