"""
Shared configuration management for battery-monitor.
Reads from ~/.config/battery-monitor/battery-monitor.conf with fallback defaults.
"""

import json
import os
from pathlib import Path


# ── Canonical paths ──────────────────────────────────────────────────────────

APP_NAME = "battery-monitor"

CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
DATA_DIR   = Path(os.environ.get("XDG_DATA_HOME",   Path.home() / ".local" / "share")) / APP_NAME
LOG_DIR    = DATA_DIR / "logs"

CONFIG_FILE = CONFIG_DIR / "battery-monitor.conf"
DB_FILE     = DATA_DIR  / "battery_monitor.db"
LOG_FILE    = LOG_DIR   / "daemon.log"

# Bundled default config shipped with the app
_BUNDLE_DEFAULT = Path(__file__).resolve().parents[2] / "config" / "battery-monitor.conf"

# ── Defaults (used when keys are missing from the file) ─────────────────────

_DEFAULTS = {
    "collection_interval_seconds": 30,
    "history_minutes": 30,
    "top_processes_count": 10,
    "spike_threshold_watts": 5.0,
    "high_drain_threshold_watts": 20.0,
    "notify_on_spike": True,
    "notify_on_high_drain": True,
    "show_top_n_in_widget": 5,
    "always_on_top": False,
    "dark_theme": True,
    "protected_process_names": [
        "systemd", "init", "kthreadd", "ksoftirqd", "kworker",
        "migration", "rcu_sched", "watchdog", "sshd", "dbus-daemon",
        "NetworkManager", "wpa_supplicant", "polkitd", "accounts-daemon",
        "gdm", "gdm3", "Xorg", "gnome-shell", "pulseaudio", "pipewire",
        "systemd-journald", "systemd-udevd", "systemd-logind",
        "battery-monitor-daemon",
    ],
    "user_kill_preferences": {},
}


class Config:
    """Merges defaults → bundled config → user config, then exposes values."""

    def __init__(self) -> None:
        self._data: dict = dict(_DEFAULTS)
        self._load_file(_BUNDLE_DEFAULT)   # bundled defaults from the package
        self._load_file(CONFIG_FILE)       # user overrides

    # ── Loading ──────────────────────────────────────────────────────────────

    def _load_file(self, path: Path) -> None:
        try:
            with open(path) as fh:
                overrides = json.load(fh)
            self._data.update(overrides)
        except FileNotFoundError:
            pass
        except json.JSONDecodeError as exc:
            print(f"[config] Warning: could not parse {path}: {exc}")

    # ── Attribute-style access ────────────────────────────────────────────────

    def __getattr__(self, key: str):
        try:
            return self._data[key]
        except KeyError:
            raise AttributeError(f"No config key '{key}'") from None

    def get(self, key: str, default=None):
        return self._data.get(key, default)

    # ── Persistence ──────────────────────────────────────────────────────────

    def save(self) -> None:
        """Write current settings back to the user config file."""
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w") as fh:
            json.dump(self._data, fh, indent=4)

    def set(self, key: str, value) -> None:
        self._data[key] = value
        self.save()

    # ── Helpers ──────────────────────────────────────────────────────────────

    def ensure_dirs(self) -> None:
        """Create all required application directories."""
        for d in (CONFIG_DIR, DATA_DIR, LOG_DIR):
            d.mkdir(parents=True, exist_ok=True)


# Module-level singleton — import this everywhere.
config = Config()
