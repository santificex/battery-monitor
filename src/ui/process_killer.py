"""
Process termination with safety checks.

Rules:
  - 'unsafe' processes are NEVER killed (raises PermissionError).
  - 'caution' processes require an explicit force flag.
  - 'safe' processes can be killed with a normal SIGTERM/SIGKILL.

All actions are logged and the user preference can be persisted.
"""

import logging
import os
import signal as _signal
from typing import Optional

import psutil

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from src.daemon.database import DatabaseManager
from src.daemon.process_classifier import UNSAFE, CAUTION, SAFE

log = logging.getLogger(__name__)


class KillResult:
    def __init__(self, pid: int, name: str, success: bool, message: str):
        self.pid     = pid
        self.name    = name
        self.success = success
        self.message = message

    def __repr__(self):
        status = "OK" if self.success else "FAIL"
        return f"KillResult({status} pid={self.pid} name={self.name!r}: {self.message})"


class ProcessKiller:
    """
    Safely terminate or force-kill processes after checking safety levels.
    """

    def __init__(self, db: Optional[DatabaseManager] = None) -> None:
        self._db = db or DatabaseManager()

    # ── Public API ────────────────────────────────────────────────────────────

    def kill(
        self,
        pid: int,
        name: str,
        kill_safety: str,
        force: bool = False,
        remember: bool = False,
    ) -> KillResult:
        """
        Attempt to terminate a process.

        Parameters
        ----------
        pid          : Process ID
        name         : Process name (for logging and preference storage)
        kill_safety  : Classification – 'safe', 'caution', or 'unsafe'
        force        : If True, send SIGKILL instead of SIGTERM.
                       Also required to kill 'caution' processes.
        remember     : Persist the user's decision to always allow killing
                       this process name in future sessions.
        """
        # Hard block on unsafe processes
        if kill_safety == UNSAFE:
            msg = f"{name} is a protected system process and cannot be terminated."
            log.warning("Blocked kill of protected process: pid=%d name=%r", pid, name)
            return KillResult(pid, name, False, msg)

        # Caution processes require explicit force flag
        if kill_safety == CAUTION and not force:
            msg = (f"{name} is a system service. "
                   "Enable 'Force Kill' to terminate it.")
            return KillResult(pid, name, False, msg)

        # Verify process still exists
        try:
            proc = psutil.Process(pid)
            if not proc.is_running():
                return KillResult(pid, name, False, "Process is no longer running.")
        except psutil.NoSuchProcess:
            return KillResult(pid, name, False, "Process no longer exists.")
        except psutil.AccessDenied:
            return KillResult(pid, name, False,
                              "Permission denied (process owned by another user).")

        # Verify name still matches (guard against PID recycling)
        try:
            actual_name = proc.name()
            if actual_name != name:
                log.warning(
                    "PID %d name changed from %r to %r — aborting kill",
                    pid, name, actual_name,
                )
                return KillResult(pid, name, False,
                                  f"PID {pid} is now running {actual_name!r}, not {name!r}.")
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return KillResult(pid, name, False, "Process disappeared before kill.")

        # Send signal
        sig = _signal.SIGKILL if force else _signal.SIGTERM
        try:
            os.kill(pid, sig)
            log.info("Sent %s to pid=%d name=%r (safety=%s)",
                     sig.name, pid, name, kill_safety)
            if remember:
                self._db.set_user_preference(name, "always_allow")
            return KillResult(pid, name, True,
                              f"Sent {sig.name} to {name} (PID {pid}).")
        except ProcessLookupError:
            return KillResult(pid, name, False, "Process disappeared during kill.")
        except PermissionError:
            return KillResult(pid, name, False,
                              "Permission denied — process may be owned by root.")
        except OSError as exc:
            return KillResult(pid, name, False, str(exc))

    def kill_many(
        self,
        processes: list[dict],
        force: bool = False,
        remember: bool = False,
    ) -> list[KillResult]:
        """Kill a list of process dicts. Returns one KillResult per process."""
        results = []
        for proc in processes:
            result = self.kill(
                pid          = proc["pid"],
                name         = proc["name"],
                kill_safety  = proc.get("kill_safety", CAUTION),
                force        = force,
                remember     = remember,
            )
            results.append(result)
        return results
