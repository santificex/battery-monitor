# Changelog & Development Notes

This file tracks what was built, what changed, and ideas for the future.

---

## v1.2.0 — Overlay tweaks (2026-03-31)

### Changed
- **Background opacity**: overlay background reduced from 82% to 18% — nearly transparent so it blends into any wallpaper.
- **CPU% added**: each process row in the overlay now shows CPU usage alongside estimated watts (e.g. `claude  36.6%  0.63W`).
- **Protected processes hidden**: system processes classified as `PROTECTED` (kernel threads, display server, audio daemon, etc.) are no longer shown in the overlay — they can't be killed anyway, so there is no reason to list them. The app fetches 4× more rows from the database and filters after, so the top-N slots always fill up with actionable entries.
- **Smart Python3 naming**: instead of showing `python3` for every Python script, the app inspects the command line and resolves the real script or module name (e.g. `py:battery_daemon`, `py:http.server`, `py:(inline)`).

---

## v1.1.0 — Plain-text overlay as a separate app (2026-03-31)

### Added
- **`src/ui/overlay_widget.py`**: new always-on-top borderless overlay that shows battery info and top consumers as plain monospace text. Completely separate from the full widget — designed to stay running all day without being intrusive.
- **GNOME autostart**: the overlay is registered in `~/.config/autostart/` so it starts automatically on every login (5-second delay to let the desktop settle first).
- **Separate launcher**: `battery-monitor-overlay` command added to `~/.local/bin/`.
- **App menu entry**: the overlay appears in GNOME Activities search as "Battery Overlay".

### Changed
- **Collection interval**: daemon interval increased from 30 seconds to 60 seconds to reduce CPU and battery overhead. The daemon was the second-highest CPU consumer on the machine during testing.
- **`battery_widget.py` kept intact**: the full-featured widget with kill controls, CSV export, and safety badges is unchanged and still launched with `battery-monitor-ui`.

---

## v1.0.0 — Initial release (2026-03-31)

### What was built

**Background daemon** (`src/daemon/battery_daemon.py`)
- Runs as a systemd user service, survives logout/login cycles via `WantedBy=default.target`
- Collects one snapshot every N seconds (configurable, default 60)
- Sends desktop notifications for power spikes and high-drain events
- Capped at 5% CPU quota and 128 MB RAM by systemd to prevent self-impact

**Data collection** (`src/daemon/data_collector.py`)
- Battery stats from `/sys/class/power_supply/BAT*/` (percent, voltage, discharge rate, time remaining)
- Time-remaining parsed from `upower` output (more accurate than calculating from sysfs alone)
- Per-process CPU and memory via `psutil`
- Screen brightness from `/sys/class/backlight/*/`
- WiFi activity from `/sys/class/net/*/wireless` and `/sys/class/net/*/operstate`
- USB device count from `/sys/bus/usb/devices/`
- Keyboard backlight from `/sys/class/leds/*kbd*/`
- Network bytes/sec from `psutil.net_io_counters()` as a WiFi activity proxy

**Power attribution engine** (`src/daemon/power_attribution.py`)
- Estimates per-component watts: screen, WiFi, USB, keyboard backlight, kernel base
- Distributes remaining discharge across processes proportional to CPU share
- Adds a memory bandwidth contribution (~0.8 mW per MB of RSS)
- Normalises results so total attributed never exceeds the real discharge rate
- Spike detection: compares current watts against recent average for each process

**Process safety classifier** (`src/daemon/process_classifier.py`)
- Three levels: `safe`, `caution`, `unsafe` (shown as PROTECTED)
- Root-owned processes are always `unsafe`
- Core OS services (systemd, Xorg, gnome-shell, pulseaudio, NetworkManager, etc.) are hardcoded as `unsafe`
- Pattern matching for GNOME background services → `caution`
- Processes owned by the current user → `safe`

**SQLite database** (`src/daemon/database.py`)
- WAL mode so daemon writes never block UI reads
- Tables: `snapshots`, `process_stats`, `component_stats`, `user_preferences`
- Automatic purge of data older than the history window (default 30 minutes)
- CSV export of up to 7 days of snapshots

**Full GTK widget** (`src/ui/battery_widget.py`)
- Dark-themed floating card, CSS-styled with Catppuccin-inspired colours
- Auto-refreshes every `collection_interval_seconds`
- Checkboxes to select processes, "Kill Selected" (SIGTERM) and "Force Kill" (SIGKILL)
- Confirmation dialog before any kill, with extra warning for `caution` processes
- "Remember preference" option — persists `always_allow` to the database
- Expand/collapse to show more or fewer processes
- Pin button to keep window above all others
- CSV export via file chooser dialog

**Process killer** (`src/ui/process_killer.py`)
- Hard block on `unsafe` processes — raises an error, never sends a signal
- `caution` processes require explicit `force=True` flag
- PID recycling guard — verifies the process name still matches before killing
- Supports both `SIGTERM` (graceful) and `SIGKILL` (immediate)

**CLI tool** (`scripts/bmon`)
- `bmon` — battery status + top consumers
- `bmon top N` — top N processes
- `bmon history` — 30-minute discharge chart in the terminal
- `bmon kill PID` — kill by PID with confirmation prompt
- `bmon export FILE` — write history to CSV

---

## Known limitations

- **Power estimates are heuristic.** Per-process wattage measurement at full accuracy requires root access (via `powertop` or eBPF). The CPU-share heuristic is good for ranking consumers but not for precise watt figures.
- **GPU not measured.** Dedicated GPU (NVIDIA/AMD) power draw is not captured. Integrated GPU contributes indirectly through the CPU discharge share.
- **No desktop battery.** On a desktop PC with no battery the daemon starts but most readings will be `None`. The UIs show a "no battery" message gracefully.
- **Python name resolution is best-effort.** If a Python script hides its name (e.g. by modifying `sys.argv` at runtime), the app will fall back to showing `python3`.

---

## Ideas for future versions

These are not planned — just collected thoughts for when the project grows.

### Higher priority
- **Throttle instead of kill** — use `cpulimit` or `cgroups` to cap a process's CPU share without terminating it. Useful for apps you want to keep running but slow down.
- **GPU power tracking** — read NVIDIA power via `nvidia-smi --query-gpu=power.draw` or AMD via `/sys/class/drm/card*/device/hwmon/*/power1_average`. Attribute GPU usage to processes using `/proc/<pid>/fdinfo`.
- **Per-app historical chart** — a simple line graph in the full widget showing a selected process's power consumption over the last 30 minutes.
- **Auto-kill rules** — let the user define rules like "if battery < 15% and chrome uses > 3W, ask to kill it". Store rules in the config file.

### Medium priority
- **System tray icon** — replace the always-open full widget with a tray icon (using `AppIndicator3`) that shows a battery icon and opens the widget on click.
- **powertop integration** — optionally run `sudo powertop --csv` on a schedule and import the wakeup-count data for more accurate attributions. Requires a polkit rule so it doesn't need a password.
- **Wayland compatibility check** — the overlay uses `Gdk.WindowTypeHint.NOTIFICATION` which may behave differently under pure Wayland compositors. Test and adjust if needed.
- **Export to JSON** — alongside CSV, add a structured JSON export for easier scripting.

### Lower priority / nice to have
- **Config UI** — a simple settings screen inside the full widget instead of editing JSON manually.
- **Dark/light theme toggle** — auto-detect the system theme and switch the full widget's CSS accordingly.
- **Localisation** — translate the UI strings. The text is currently all in English.
- **Flatpak packaging** — bundle the app as a Flatpak for easier distribution on other distros.
- **Snap package** — same idea via the Snap store for Ubuntu users.
