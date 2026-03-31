"""
Battery Monitor — Plain-Text Floating Overlay
==============================================
A borderless, always-on-top, semi-transparent window that shows
battery status and top power consumers as plain text.

This is a separate, lightweight companion to battery_widget.py.
It is meant to run all the time in the background of your desktop.
The full-featured widget (battery_widget.py) is opened on demand.

Controls:
  • Drag anywhere to move it around the screen
  • Right-click → Refresh / Quit

Launch manually:
    battery-monitor-overlay

Autostarted on login via:
    ~/.config/autostart/battery-monitor-overlay.desktop
"""

import sys
import logging
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gtk, GLib, Gdk  # type: ignore
import cairo  # type: ignore  (provided by python3-gi-cairo)

from src.shared.config import config
from src.daemon.database import DatabaseManager
from src.daemon.process_classifier import safety_label

log = logging.getLogger(__name__)
logging.basicConfig(level=logging.WARNING)

# ── Appearance constants ──────────────────────────────────────────────────────

FONT             = "Monospace 9"
PADDING          = 12
BG_R, BG_G, BG_B = 0.08, 0.08, 0.12   # dark navy background
BG_ALPHA         = 0.82                 # 82% opaque — adjust to taste
FG_R, FG_G, FG_B = 0.90, 0.90, 0.92   # near-white text
CORNER_RADIUS    = 8


# ── Text builder ──────────────────────────────────────────────────────────────

def _build_text(db: DatabaseManager) -> str:
    """Pull the latest snapshot from the DB and format it as plain text."""
    snap  = db.get_latest_snapshot()
    procs = db.get_latest_processes(limit=config.show_top_n_in_widget)

    lines = []

    # ── No data yet ───────────────────────────────────────────────────────────
    if snap is None:
        lines.append("Battery Monitor")
        lines.append("No data — daemon not running?")
        lines.append("  systemctl --user start battery-monitor")
        return "\n".join(lines)

    # ── Battery status line ───────────────────────────────────────────────────
    pct    = snap["battery_percent"]
    status = snap["battery_status"] or "Unknown"
    rate   = snap["discharge_rate_watts"]
    mins   = snap["time_remaining_min"]

    arrow  = "+" if status.lower() == "charging" else "-"
    pct_str  = f"{pct:.0f}%" if pct is not None else "?%"
    rate_str = f"{rate:.1f}W" if rate is not None else "?W"

    lines.append(f"Battery  {pct_str}  {status}")

    if mins is not None:
        h, m    = divmod(int(mins), 60)
        remain  = f"{h}h {m:02d}m" if h else f"{m}m"
        lines.append(f"{arrow} {rate_str}  ~{remain} left")
    else:
        lines.append(f"{arrow} {rate_str}")

    # ── Process list ──────────────────────────────────────────────────────────
    lines.append("")
    lines.append("Top consumers")
    lines.append("-" * 34)

    if procs:
        for i, proc in enumerate(procs, 1):
            name   = proc["name"][:18]
            watts  = proc["estimated_watts"] or 0.0
            safety = proc["kill_safety"] or "caution"
            label  = safety_label(safety)[:9]
            lines.append(f" {i}.  {name:<18}  {watts:>5.2f}W  {label}")
    else:
        lines.append("  (no process data yet)")

    lines.append("-" * 34)
    lines.append(f"  {datetime.now().strftime('%H:%M:%S')}")

    return "\n".join(lines)


# ── Overlay window ────────────────────────────────────────────────────────────

class OverlayWindow(Gtk.Window):
    """Borderless, always-on-top, draggable plain-text overlay."""

    def __init__(self) -> None:
        super().__init__()
        self._db          = DatabaseManager()
        self._drag_offset = None   # set while left-button is held

        # ── Transparency ──────────────────────────────────────────────────────
        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual and screen.is_composited():
            self.set_visual(visual)
        self.set_app_paintable(True)
        self.connect("draw", self._on_draw)

        # ── Window behaviour ──────────────────────────────────────────────────
        self.set_decorated(False)           # no title bar
        self.set_keep_above(True)           # float above everything
        self.set_skip_taskbar_hint(True)    # hide from taskbar
        self.set_skip_pager_hint(True)      # hide from alt-tab
        self.set_type_hint(Gdk.WindowTypeHint.NOTIFICATION)
        self.set_resizable(False)

        # ── Text label ────────────────────────────────────────────────────────
        self._label = Gtk.Label()
        self._label.set_justify(Gtk.Justification.LEFT)
        self._label.set_halign(Gtk.Align.START)
        self._label.set_valign(Gtk.Align.START)

        # Monospace font
        import gi.repository.Pango as Pango
        self._label.override_font(Pango.FontDescription.from_string(FONT))

        # Text colour
        fg = Gdk.RGBA()
        fg.red, fg.green, fg.blue, fg.alpha = FG_R, FG_G, FG_B, 1.0
        self._label.override_color(Gtk.StateFlags.NORMAL, fg)

        # Box adds padding around the label
        box = Gtk.Box()
        box.set_margin_top(PADDING)
        box.set_margin_bottom(PADDING)
        box.set_margin_start(PADDING)
        box.set_margin_end(PADDING)
        box.add(self._label)
        self.add(box)

        # ── Input events ──────────────────────────────────────────────────────
        self.add_events(
            Gdk.EventMask.BUTTON_PRESS_MASK
            | Gdk.EventMask.BUTTON_RELEASE_MASK
            | Gdk.EventMask.POINTER_MOTION_MASK
        )
        self.connect("button-press-event",   self._on_button_press)
        self.connect("button-release-event", self._on_button_release)
        self.connect("motion-notify-event",  self._on_motion)

        # ── First render + auto-refresh timer ─────────────────────────────────
        self._refresh()
        GLib.timeout_add_seconds(config.collection_interval_seconds, self._refresh)

    # ── Background drawing ────────────────────────────────────────────────────

    def _on_draw(self, _widget, cr: cairo.Context) -> None:
        """Draw the semi-transparent rounded rectangle background."""
        w, h = self.get_size()
        r    = CORNER_RADIUS

        cr.set_source_rgba(BG_R, BG_G, BG_B, BG_ALPHA)
        cr.set_operator(cairo.OPERATOR_SOURCE)

        # Rounded rectangle path
        cr.move_to(r, 0)
        cr.line_to(w - r, 0);   cr.arc(w - r, r,     r, -1.5708, 0)
        cr.line_to(w, h - r);   cr.arc(w - r, h - r, r,  0,      1.5708)
        cr.line_to(r, h);       cr.arc(r,     h - r, r,  1.5708, 3.1416)
        cr.line_to(0, r);       cr.arc(r,     r,     r,  3.1416, -1.5708)
        cr.close_path()
        cr.fill()

        cr.set_operator(cairo.OPERATOR_OVER)

    # ── Drag to move ──────────────────────────────────────────────────────────

    def _on_button_press(self, _widget, event: Gdk.EventButton) -> None:
        if event.button == 1:
            wx, wy = self.get_position()
            self._drag_offset = (event.x_root - wx, event.y_root - wy)
        elif event.button == 3:
            self._show_menu(event)

    def _on_button_release(self, _widget, _event) -> None:
        self._drag_offset = None

    def _on_motion(self, _widget, event: Gdk.EventMotion) -> None:
        if self._drag_offset and (event.state & Gdk.ModifierType.BUTTON1_MASK):
            ox, oy = self._drag_offset
            self.move(int(event.x_root - ox), int(event.y_root - oy))

    # ── Right-click menu ──────────────────────────────────────────────────────

    def _show_menu(self, event: Gdk.EventButton) -> None:
        menu = Gtk.Menu()

        item_refresh = Gtk.MenuItem(label="Refresh now")
        item_refresh.connect("activate", lambda _: self._refresh())
        menu.append(item_refresh)

        menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Quit overlay")
        item_quit.connect("activate", lambda _: Gtk.main_quit())
        menu.append(item_quit)

        menu.show_all()
        menu.popup_at_pointer(event)

    # ── Data refresh ──────────────────────────────────────────────────────────

    def _refresh(self) -> bool:
        """Reload data from DB and update the label. Returns True to keep timer."""
        self._label.set_text(_build_text(self._db))
        self.queue_draw()
        return True


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    win = OverlayWindow()
    win.show_all()

    # Default position: top-right corner with a small margin
    screen = Gdk.Screen.get_default()
    win.move(screen.get_width() - 310, 40)

    Gtk.main()


if __name__ == "__main__":
    main()
