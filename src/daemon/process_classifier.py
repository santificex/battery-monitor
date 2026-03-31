"""
Process Safety Classifier
=========================
Assigns one of three safety levels to every process:

  unsafe   – system / root-owned critical processes; never kill
  caution  – system services that may restart or affect user session
  safe     – user-owned, non-essential processes; may be terminated

The classification uses a layered rule system:
  1. Protected names list (from config)
  2. Username rules (root → unsafe, current user → potentially safe)
  3. Heuristic name patterns
"""

import os
import re
import logging
from typing import Optional

import psutil

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.shared.config import config

log = logging.getLogger(__name__)

# ── Kill-safety constants ─────────────────────────────────────────────────────

UNSAFE  = "unsafe"
CAUTION = "caution"
SAFE    = "safe"

# Process names that are ALWAYS protected regardless of owner
_ALWAYS_UNSAFE: frozenset[str] = frozenset({
    # Core OS
    "systemd", "init", "kthreadd", "kswapd0", "ksoftirqd",
    "kworker", "migration", "rcu_sched", "rcu_bh",
    "watchdog", "cpuhp", "khugepaged", "kcompactd",
    # Display & session
    "Xorg", "X", "gnome-shell", "mutter", "kwin", "kwin_wayland",
    "plasmashell", "sddm", "gdm", "gdm3", "lightdm",
    # Audio
    "pulseaudio", "pipewire", "pipewire-pulse", "wireplumber",
    # System services
    "dbus-daemon", "dbus-broker", "systemd-journald", "systemd-udevd",
    "systemd-logind", "systemd-resolved", "systemd-networkd",
    "NetworkManager", "wpa_supplicant", "avahi-daemon",
    "polkitd", "accounts-daemon", "rtkit-daemon",
    # Security
    "sshd", "gpg-agent", "ssh-agent", "keyring-daemon",
    # Our own daemon
    "battery-monitor-daemon", "battery_daemon",
})

# Patterns (regex) that map to CAUTION
_CAUTION_PATTERNS: list[re.Pattern] = [re.compile(p, re.I) for p in [
    r"^gvfs",          # GNOME virtual filesystem
    r"^gnome-",        # GNOME background services
    r"^xdg-",          # XDG portals
    r"^dconf",         # settings daemon
    r"^at-spi",        # accessibility
    r"^ibus",          # input method
    r"^fcitx",
    r"^tracker",       # file indexer (annoying but user might want)
    r"^evolution",     # mail background sync
    r"^goa-",          # GNOME online accounts
    r"^gsd-",          # GNOME settings daemon components
    r"^update-manager",
    r"^apt",
    r"^dpkg",
    r"^snap",
    r"^flatpak",
]]

# Current user at startup
_CURRENT_USER: str = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"


class ProcessClassifier:
    """Stateless classifier — call classify() for each process."""

    def __init__(self) -> None:
        # Merge config-level protected names into our set
        self._protected = _ALWAYS_UNSAFE | frozenset(
            config.protected_process_names
        )

    def classify(self, proc: dict) -> str:
        """Return 'safe', 'caution', or 'unsafe' for the given process dict."""
        name     = (proc.get("name") or "").lower()
        username = (proc.get("username") or "").lower()
        pid      = proc.get("pid", 0)

        # PID 1 is always init/systemd
        if pid == 1:
            return UNSAFE

        # Name in protected set
        if proc.get("name") in self._protected or name in {
            n.lower() for n in self._protected
        }:
            return UNSAFE

        # Root-owned → unsafe (root services should never be killed by user)
        if username in ("root", "daemon", "messagebus", "avahi", "polkitd",
                        "rtkit", "systemd-network", "systemd-resolve",
                        "syslog", "_apt"):
            return UNSAFE

        # Kernel thread (no real user, runs in kernel space)
        if username in ("", "unknown") and pid < 1000:
            return UNSAFE

        # Caution patterns
        for pattern in _CAUTION_PATTERNS:
            if pattern.search(name):
                return CAUTION

        # Owned by current user → generally safe to kill
        if username == _CURRENT_USER:
            return SAFE

        # Other system users → caution
        return CAUTION

    def enrich_processes(self, processes: list[dict]) -> list[dict]:
        """Add 'kill_safety' field to each process dict (in-place + return)."""
        for proc in processes:
            proc["kill_safety"] = self.classify(proc)
        return processes


def safety_label(level: str) -> str:
    """Human-readable label for display."""
    return {"safe": "SAFE", "caution": "CAUTION", "unsafe": "PROTECTED"}.get(level, level.upper())
