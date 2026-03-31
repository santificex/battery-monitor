"""
Battery Monitor Daemon
======================
Systemd-managed background service that collects power data every N seconds
and writes it to the shared SQLite database.

Run directly:
    python3 battery_daemon.py

Or via systemd (user service):
    systemctl --user start battery-monitor
"""

import logging
import os
import signal
import sys
import time
from pathlib import Path

# Make sure sibling packages are importable when run directly
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.shared.config import config, LOG_FILE
from src.daemon.database import DatabaseManager
from src.daemon.data_collector import (
    collect_battery_info,
    collect_process_stats,
    collect_component_info,
)
from src.daemon.power_attribution import (
    estimate_component_watts,
    attribute_process_power,
    detect_spike,
)
from src.daemon.process_classifier import ProcessClassifier

# ── Logging setup ─────────────────────────────────────────────────────────────

config.ensure_dirs()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("battery-daemon")

# ── Globals ───────────────────────────────────────────────────────────────────

_running = True


def _handle_signal(signum, _frame):
    global _running
    log.info(f"Received signal {signum}, shutting down gracefully…")
    _running = False


# ── Collection cycle ──────────────────────────────────────────────────────────

def run_collection_cycle(db: DatabaseManager, classifier: ProcessClassifier) -> None:
    """One full data-collection and storage cycle."""
    try:
        # 1. Battery state
        battery = collect_battery_info()
        log.debug(
            "Battery: %(battery_percent)s%% %(battery_status)s "
            "%(discharge_rate_watts)sW",
            battery,
        )

        # 2. Save snapshot and get its ID
        snap_id = db.save_snapshot(
            battery_percent      = battery["battery_percent"],
            battery_status       = battery["battery_status"],
            discharge_rate_watts = battery["discharge_rate_watts"],
            voltage_volts        = battery["voltage_volts"],
            time_remaining_min   = battery["time_remaining_min"],
        )

        # 3. Hardware components
        comp_info = collect_component_info()
        components = estimate_component_watts(comp_info)
        db.save_component_stats(snap_id, components)

        # 4. Processes
        processes = collect_process_stats()
        classifier.enrich_processes(processes)
        attribute_process_power(
            processes,
            components,
            battery["discharge_rate_watts"],
        )
        # Sort by estimated watts descending, keep top N for storage efficiency
        processes.sort(key=lambda p: p["estimated_watts"], reverse=True)
        db.save_process_stats(snap_id, processes[:config.top_processes_count * 2])

        # 5. Spike detection + notifications
        _check_spikes(db, processes)

        # 6. High drain notification
        _check_high_drain(battery.get("discharge_rate_watts"))

        # 7. Periodic cleanup
        db.purge_old_data()

        log.info(
            "Cycle done — snap_id=%d  battery=%.1f%%  %.2fW  top=%s",
            snap_id,
            battery["battery_percent"] or 0,
            battery["discharge_rate_watts"] or 0,
            processes[0]["name"] if processes else "n/a",
        )

    except Exception:
        log.exception("Error during collection cycle")


# ── Notification helpers ──────────────────────────────────────────────────────

_spike_notified: set[str] = set()


def _check_spikes(db: DatabaseManager, processes: list[dict]) -> None:
    global _spike_notified
    if not config.notify_on_spike:
        return
    for proc in processes[:config.top_processes_count]:
        name = proc.get("name", "")
        watts = proc.get("estimated_watts", 0)
        history = db.get_process_history(name, minutes=10)
        if detect_spike(name, watts, history, config.spike_threshold_watts):
            if name not in _spike_notified:
                _send_notification(
                    "Power Spike Detected",
                    f"{name} is consuming an unusually high {watts:.1f}W",
                    "battery-caution",
                )
                _spike_notified.add(name)
        else:
            _spike_notified.discard(name)


_high_drain_notified = False


def _check_high_drain(discharge_w: float | None) -> None:
    global _high_drain_notified
    if not config.notify_on_high_drain or discharge_w is None:
        return
    if discharge_w > config.high_drain_threshold_watts:
        if not _high_drain_notified:
            _send_notification(
                "High Battery Drain",
                f"Battery is discharging at {discharge_w:.1f}W",
                "battery-low",
            )
            _high_drain_notified = True
    else:
        _high_drain_notified = False


def _send_notification(title: str, body: str, icon: str) -> None:
    """Send a desktop notification via libnotify; swallow all errors."""
    try:
        import gi
        gi.require_version("Notify", "0.7")
        from gi.repository import Notify  # type: ignore
        if not Notify.is_initted():
            Notify.init("Battery Monitor")
        n = Notify.Notification.new(title, body, icon)
        n.show()
    except Exception as exc:
        log.debug("Could not send notification: %s", exc)


# ── Main loop ─────────────────────────────────────────────────────────────────

def main() -> None:
    log.info("Battery Monitor Daemon starting (interval=%ds)",
             config.collection_interval_seconds)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT,  _handle_signal)

    db         = DatabaseManager()
    classifier = ProcessClassifier()

    # Run first cycle immediately so UI has data right away
    run_collection_cycle(db, classifier)

    while _running:
        # Sleep in short chunks so SIGTERM is handled quickly
        elapsed = 0
        interval = config.collection_interval_seconds
        while _running and elapsed < interval:
            time.sleep(min(1, interval - elapsed))
            elapsed += 1

        if _running:
            run_collection_cycle(db, classifier)

    log.info("Daemon stopped.")


if __name__ == "__main__":
    main()
