"""
Battery Monitor – GTK3 Desktop Widget
======================================
A floating card-style window that shows the top battery-draining processes
and lets the user safely terminate them.

Launch:
    python3 battery_widget.py

Requires:
    python3-gi  (PyGObject / GTK 3)
    libnotify   (for notifications)
"""

import csv
import os
import sys
import time
import logging
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import gi
gi.require_version("Gtk",    "3.0")
gi.require_version("Notify", "0.7")
from gi.repository import Gtk, GLib, Gdk, Notify  # type: ignore

from src.shared.config import config, DATA_DIR
from src.daemon.database import DatabaseManager
from src.daemon.process_classifier import safety_label, UNSAFE, CAUTION, SAFE
from src.ui.process_killer import ProcessKiller, KillResult

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")

# ── CSS Stylesheet ────────────────────────────────────────────────────────────

_CSS = b"""
window {
    background-color: #1e1e2e;
}
.card {
    background-color: #2a2a3e;
    border-radius: 12px;
    padding: 0;
}
.header-bar {
    background-color: #313149;
    border-radius: 12px 12px 0 0;
    padding: 10px 14px;
}
.title-label {
    color: #cdd6f4;
    font-weight: bold;
    font-size: 14px;
}
.battery-status {
    color: #a6e3a1;
    font-size: 12px;
    padding: 6px 14px;
}
.battery-status.charging {
    color: #f9e2af;
}
.battery-status.low {
    color: #f38ba8;
}
.section-header {
    color: #6c7086;
    font-size: 11px;
    font-weight: bold;
    padding: 4px 14px 2px 14px;
    letter-spacing: 1px;
}
.process-row {
    padding: 5px 10px;
    border-bottom: 1px solid #313149;
}
.process-row:hover {
    background-color: #313149;
}
.process-name {
    color: #cdd6f4;
    font-size: 12px;
    font-weight: bold;
}
.process-detail {
    color: #6c7086;
    font-size: 10px;
}
.watts-label {
    color: #89b4fa;
    font-size: 12px;
    font-weight: bold;
    min-width: 52px;
}
.badge-safe {
    background-color: #26403a;
    color: #a6e3a1;
    border-radius: 4px;
    padding: 1px 5px;
    font-size: 10px;
    font-weight: bold;
}
.badge-caution {
    background-color: #403826;
    color: #f9e2af;
    border-radius: 4px;
    padding: 1px 5px;
    font-size: 10px;
    font-weight: bold;
}
.badge-unsafe {
    background-color: #402626;
    color: #f38ba8;
    border-radius: 4px;
    padding: 1px 5px;
    font-size: 10px;
    font-weight: bold;
}
.action-bar {
    background-color: #252535;
    border-radius: 0 0 12px 12px;
    padding: 8px 14px;
}
.kill-button {
    background-color: #f38ba8;
    color: #1e1e2e;
    font-weight: bold;
    border-radius: 6px;
    padding: 4px 10px;
    border: none;
    font-size: 11px;
}
.kill-button:hover {
    background-color: #eba0ac;
}
.kill-button:disabled {
    background-color: #45475a;
    color: #6c7086;
}
.force-kill-button {
    background-color: #313149;
    color: #f38ba8;
    font-weight: bold;
    border-radius: 6px;
    padding: 4px 10px;
    border: 1px solid #f38ba8;
    font-size: 11px;
}
.icon-button {
    background: transparent;
    border: none;
    color: #6c7086;
    padding: 2px 6px;
    border-radius: 4px;
}
.icon-button:hover {
    background-color: #313149;
    color: #cdd6f4;
}
.no-data-label {
    color: #6c7086;
    font-size: 12px;
    padding: 20px;
}
.last-updated {
    color: #45475a;
    font-size: 10px;
    padding: 0 14px 6px 14px;
}
"""


# ── Process row widget ────────────────────────────────────────────────────────

class ProcessRow(Gtk.ListBoxRow):
    """One row in the process list — checkbox, name, watts, badge."""

    def __init__(self, proc: dict) -> None:
        super().__init__()
        self.proc = proc
        self.get_style_context().add_class("process-row")

        outer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        outer.set_margin_start(4)
        outer.set_margin_end(4)

        # Checkbox
        self.check = Gtk.CheckButton()
        outer.pack_start(self.check, False, False, 0)

        # Name + detail column
        info_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=1)
        info_box.set_hexpand(True)

        name_label = Gtk.Label(label=proc["name"])
        name_label.set_halign(Gtk.Align.START)
        name_label.get_style_context().add_class("process-name")

        detail = f"PID {proc['pid']}  CPU {proc.get('cpu_percent', 0):.1f}%  " \
                 f"Mem {proc.get('memory_mb', 0):.0f}MB  " \
                 f"user: {proc.get('username', '?')}"
        detail_label = Gtk.Label(label=detail)
        detail_label.set_halign(Gtk.Align.START)
        detail_label.get_style_context().add_class("process-detail")

        info_box.pack_start(name_label,   False, False, 0)
        info_box.pack_start(detail_label, False, False, 0)
        outer.pack_start(info_box, True, True, 0)

        # Watts
        watts_label = Gtk.Label(label=f"{proc.get('estimated_watts', 0):.2f}W")
        watts_label.set_halign(Gtk.Align.END)
        watts_label.get_style_context().add_class("watts-label")
        outer.pack_start(watts_label, False, False, 0)

        # Safety badge
        safety = proc.get("kill_safety", "caution")
        badge  = Gtk.Label(label=safety_label(safety))
        badge.set_halign(Gtk.Align.CENTER)
        badge.get_style_context().add_class(f"badge-{safety}")
        outer.pack_start(badge, False, False, 4)

        self.add(outer)
        self.show_all()

    def is_checked(self) -> bool:
        return self.check.get_active()


# ── Main widget window ────────────────────────────────────────────────────────

class BatteryWidget(Gtk.Window):

    def __init__(self) -> None:
        super().__init__(title="Battery Monitor")

        self._db      = DatabaseManager()
        self._killer  = ProcessKiller(self._db)
        self._rows: list[ProcessRow] = []
        self._show_all_processes = False
        self._expanded = False

        # ── Apply CSS ─────────────────────────────────────────────────────────
        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS)
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        # ── Window properties ─────────────────────────────────────────────────
        self.set_default_size(460, -1)
        self.set_resizable(True)
        self.set_keep_above(config.always_on_top)
        self.set_decorated(True)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("delete-event", Gtk.main_quit)

        # ── Root container ────────────────────────────────────────────────────
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        root.get_style_context().add_class("card")
        self.add(root)

        # ── Header bar ────────────────────────────────────────────────────────
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        header.get_style_context().add_class("header-bar")

        title = Gtk.Label(label="⚡ Battery Monitor")
        title.get_style_context().add_class("title-label")
        title.set_hexpand(True)
        title.set_halign(Gtk.Align.START)
        header.pack_start(title, True, True, 0)

        # Refresh button
        btn_refresh = Gtk.Button(label="↺")
        btn_refresh.set_tooltip_text("Refresh now")
        btn_refresh.get_style_context().add_class("icon-button")
        btn_refresh.connect("clicked", self._on_refresh_clicked)
        header.pack_start(btn_refresh, False, False, 0)

        # Export button
        btn_export = Gtk.Button(label="⬇")
        btn_export.set_tooltip_text("Export CSV")
        btn_export.get_style_context().add_class("icon-button")
        btn_export.connect("clicked", self._on_export_clicked)
        header.pack_start(btn_export, False, False, 0)

        # Pin / always-on-top toggle
        self._btn_pin = Gtk.ToggleButton(label="📌")
        self._btn_pin.set_tooltip_text("Keep window on top")
        self._btn_pin.set_active(config.always_on_top)
        self._btn_pin.get_style_context().add_class("icon-button")
        self._btn_pin.connect("toggled", self._on_pin_toggled)
        header.pack_start(self._btn_pin, False, False, 0)

        root.pack_start(header, False, False, 0)

        # ── Battery status bar ────────────────────────────────────────────────
        self._status_label = Gtk.Label(label="Waiting for data…")
        self._status_label.set_halign(Gtk.Align.START)
        self._status_label.get_style_context().add_class("battery-status")
        root.pack_start(self._status_label, False, False, 0)

        # ── Section header ────────────────────────────────────────────────────
        sec_hdr = Gtk.Label(label="TOP POWER CONSUMERS")
        sec_hdr.set_halign(Gtk.Align.START)
        sec_hdr.get_style_context().add_class("section-header")
        root.pack_start(sec_hdr, False, False, 2)

        # ── Process list ──────────────────────────────────────────────────────
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_min_content_height(80)
        scroll.set_max_content_height(420)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll.add(self._list_box)
        root.pack_start(scroll, True, True, 0)

        # ── Last updated label ────────────────────────────────────────────────
        self._updated_label = Gtk.Label(label="")
        self._updated_label.set_halign(Gtk.Align.END)
        self._updated_label.get_style_context().add_class("last-updated")
        root.pack_start(self._updated_label, False, False, 0)

        # ── Action bar ────────────────────────────────────────────────────────
        action_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        action_bar.get_style_context().add_class("action-bar")

        self._btn_kill = Gtk.Button(label="⚠ Kill Selected")
        self._btn_kill.get_style_context().add_class("kill-button")
        self._btn_kill.set_sensitive(False)
        self._btn_kill.connect("clicked", lambda _: self._kill_selected(force=False))
        action_bar.pack_start(self._btn_kill, False, False, 0)

        self._btn_force = Gtk.Button(label="☠ Force Kill")
        self._btn_force.get_style_context().add_class("force-kill-button")
        self._btn_force.set_sensitive(False)
        self._btn_force.set_tooltip_text("Send SIGKILL (immediate, no cleanup)")
        self._btn_force.connect("clicked", lambda _: self._kill_selected(force=True))
        action_bar.pack_start(self._btn_force, False, False, 0)

        # Spacer
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        action_bar.pack_start(spacer, True, True, 0)

        # Expand / collapse toggle
        self._btn_expand = Gtk.Button(label="Show More ▾")
        self._btn_expand.get_style_context().add_class("icon-button")
        self._btn_expand.connect("clicked", self._on_expand_clicked)
        action_bar.pack_start(self._btn_expand, False, False, 0)

        root.pack_start(action_bar, False, False, 0)

        # ── Notify init ───────────────────────────────────────────────────────
        try:
            if not Notify.is_initted():
                Notify.init("Battery Monitor")
        except Exception:
            pass

        # ── Initial data load + auto-refresh timer ────────────────────────────
        self._refresh_data()
        GLib.timeout_add_seconds(config.collection_interval_seconds, self._auto_refresh)

    # ── Data refresh ──────────────────────────────────────────────────────────

    def _refresh_data(self) -> None:
        """Pull latest data from DB and update all widgets."""
        snap   = self._db.get_latest_snapshot()
        procs  = self._db.get_latest_processes(limit=50)
        comps  = self._db.get_latest_components()
        self._update_status(snap, comps)
        self._rebuild_process_list(procs)
        self._updated_label.set_text(
            f"Last updated: {datetime.now().strftime('%H:%M:%S')}"
        )

    def _auto_refresh(self) -> bool:
        """Called by GLib timer; return True to keep the timer alive."""
        self._refresh_data()
        return True

    def _on_refresh_clicked(self, _btn) -> None:
        self._refresh_data()

    # ── Status bar update ─────────────────────────────────────────────────────

    def _update_status(self, snap, components) -> None:
        if snap is None:
            self._status_label.set_text("⚠ Daemon not running — no data yet.")
            return

        pct    = snap["battery_percent"]
        status = snap["battery_status"] or "Unknown"
        rate   = snap["discharge_rate_watts"]
        mins   = snap["time_remaining_min"]

        icon = "🔋"
        if status.lower() == "charging":
            icon = "🔌"
        elif pct and pct < 20:
            icon = "🪫"

        parts = [f"{icon} {pct:.0f}%" if pct is not None else f"{icon} ?%"]
        parts.append(status)
        if rate is not None:
            parts.append(f"{rate:.1f}W")
        if mins is not None:
            h, m = divmod(int(mins), 60)
            parts.append(f"~{h}h {m:02d}m remaining" if h else f"~{m}m remaining")

        self._status_label.set_text("  ·  ".join(parts))

        # Colour the status based on level
        ctx = self._status_label.get_style_context()
        for cls in ("charging", "low"):
            ctx.remove_class(cls)
        if status.lower() == "charging":
            ctx.add_class("charging")
        elif pct and pct < 20:
            ctx.add_class("low")

    # ── Process list ──────────────────────────────────────────────────────────

    def _rebuild_process_list(self, procs) -> None:
        # Remove old rows
        for child in self._list_box.get_children():
            self._list_box.remove(child)
        self._rows.clear()

        if not procs:
            lbl = Gtk.Label(label="No data — is the daemon running?")
            lbl.get_style_context().add_class("no-data-label")
            self._list_box.add(lbl)
            self._list_box.show_all()
            return

        limit = None if self._expanded else config.show_top_n_in_widget
        display_procs = [dict(p) for p in procs[:limit]]

        for proc in display_procs:
            row = ProcessRow(proc)
            row.check.connect("toggled", self._on_check_toggled)
            self._list_box.add(row)
            self._rows.append(row)

        self._list_box.show_all()
        self._update_kill_buttons()

    def _on_check_toggled(self, _check) -> None:
        self._update_kill_buttons()

    def _update_kill_buttons(self) -> None:
        checked = self._get_checked_procs()
        has_killable = any(
            p.get("kill_safety") in (SAFE, CAUTION) for p in checked
        )
        self._btn_kill.set_sensitive(bool(checked))
        self._btn_force.set_sensitive(bool(checked))

    def _get_checked_procs(self) -> list[dict]:
        return [row.proc for row in self._rows if row.is_checked()]

    # ── Expand / collapse ─────────────────────────────────────────────────────

    def _on_expand_clicked(self, _btn) -> None:
        self._expanded = not self._expanded
        self._btn_expand.set_label(
            "Show Less ▴" if self._expanded else "Show More ▾"
        )
        self._refresh_data()

    # ── Kill actions ──────────────────────────────────────────────────────────

    def _kill_selected(self, force: bool = False) -> None:
        selected = self._get_checked_procs()
        if not selected:
            return

        # Show confirmation dialog
        if not self._confirm_kill(selected, force):
            return

        # Ask if user wants to remember choices
        remember = self._ask_remember(selected)

        # Execute kills
        results = self._killer.kill_many(selected, force=force, remember=remember)

        # Show result summary
        self._show_kill_results(results)

        # Refresh list (killed processes should be gone)
        GLib.timeout_add(800, self._refresh_data)

    def _confirm_kill(self, procs: list[dict], force: bool) -> bool:
        sig_name = "SIGKILL (force)" if force else "SIGTERM (graceful)"
        names    = "\n".join(
            f"  • {p['name']} (PID {p['pid']})  [{safety_label(p.get('kill_safety','?'))}]"
            for p in procs
        )

        # Warn about caution / unsafe
        warnings = [p for p in procs if p.get("kill_safety") == CAUTION]
        unsafe   = [p for p in procs if p.get("kill_safety") == UNSAFE]

        msg = f"Send {sig_name} to:\n\n{names}"
        if unsafe:
            unames = ", ".join(p["name"] for p in unsafe)
            msg += f"\n\n⛔ Protected processes will be skipped: {unames}"
        if warnings:
            wnames = ", ".join(p["name"] for p in warnings)
            msg += f"\n\n⚠ System services selected: {wnames}"

        dialog = Gtk.MessageDialog(
            transient_for = self,
            flags         = Gtk.DialogFlags.MODAL,
            message_type  = Gtk.MessageType.WARNING,
            buttons       = Gtk.ButtonsType.OK_CANCEL,
            text          = "Confirm Process Termination",
        )
        dialog.format_secondary_text(msg)
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.OK

    def _ask_remember(self, procs: list[dict]) -> bool:
        """Ask if kill preference should be remembered. Only for 'safe' procs."""
        safe_procs = [p for p in procs if p.get("kill_safety") == SAFE]
        if not safe_procs:
            return False

        dialog = Gtk.MessageDialog(
            transient_for = self,
            flags         = Gtk.DialogFlags.MODAL,
            message_type  = Gtk.MessageType.QUESTION,
            buttons       = Gtk.ButtonsType.YES_NO,
            text          = "Remember this preference?",
        )
        dialog.format_secondary_text(
            "Should Battery Monitor always allow killing these processes\n"
            "without asking for confirmation next time?"
        )
        response = dialog.run()
        dialog.destroy()
        return response == Gtk.ResponseType.YES

    def _show_kill_results(self, results: list[KillResult]) -> None:
        success = [r for r in results if r.success]
        failed  = [r for r in results if not r.success]

        lines = []
        for r in success:
            lines.append(f"✓ {r.name}: {r.message}")
        for r in failed:
            lines.append(f"✗ {r.name}: {r.message}")

        msg_type = Gtk.MessageType.INFO if not failed else Gtk.MessageType.WARNING
        dialog = Gtk.MessageDialog(
            transient_for = self,
            flags         = Gtk.DialogFlags.MODAL,
            message_type  = msg_type,
            buttons       = Gtk.ButtonsType.OK,
            text          = f"Kill Results ({len(success)} succeeded, {len(failed)} failed)",
        )
        dialog.format_secondary_text("\n".join(lines))
        dialog.run()
        dialog.destroy()

    # ── Pin / always-on-top ───────────────────────────────────────────────────

    def _on_pin_toggled(self, btn) -> None:
        active = btn.get_active()
        self.set_keep_above(active)
        config.set("always_on_top", active)

    # ── Export CSV ────────────────────────────────────────────────────────────

    def _on_export_clicked(self, _btn) -> None:
        dialog = Gtk.FileChooserDialog(
            title         = "Export Battery History to CSV",
            parent        = self,
            action        = Gtk.FileChooserAction.SAVE,
        )
        dialog.add_buttons(
            Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
            Gtk.STOCK_SAVE,   Gtk.ResponseType.OK,
        )
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        dialog.set_current_name(f"battery_history_{ts}.csv")
        dialog.set_current_folder(str(Path.home()))

        filt = Gtk.FileFilter()
        filt.set_name("CSV files")
        filt.add_pattern("*.csv")
        dialog.add_filter(filt)

        response = dialog.run()
        if response == Gtk.ResponseType.OK:
            filepath = dialog.get_filename()
            dialog.destroy()
            count = self._db.export_csv(filepath)
            info = Gtk.MessageDialog(
                transient_for=self,
                flags=Gtk.DialogFlags.MODAL,
                message_type=Gtk.MessageType.INFO,
                buttons=Gtk.ButtonsType.OK,
                text="Export Complete",
            )
            info.format_secondary_text(
                f"Exported {count} snapshots to:\n{filepath}"
            )
            info.run()
            info.destroy()
        else:
            dialog.destroy()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    widget = BatteryWidget()
    widget.show_all()
    Gtk.main()


if __name__ == "__main__":
    main()
