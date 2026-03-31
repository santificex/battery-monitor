# Battery Monitor

A lightweight Ubuntu/Pop!_OS application that continuously watches which processes and hardware components are draining your battery the most, and lets you act on that information.

The system has three parts that work together:

| Part | What it does | When it runs |
|---|---|---|
| **Daemon** | Collects power data every 60 seconds | Always, in the background |
| **Overlay** | Floating plain-text display on your desktop | Autostarted on every login |
| **Full widget** | Detailed view with kill controls | Only when you open it |

All three share a single SQLite database. The daemon writes to it; both UIs read from it.

---

## How it looks

**Overlay** (always visible, nearly transparent, top-right corner):
```
Battery  67%  Charging
+ 13.4W  ~1h 30m left

Top consumers
-------------------------------------
 1.  claude            36.6%  0.63W
 2.  py:battery_daemon 26.8%  0.32W
 3.  chromium           8.1%  0.21W
 4.  pcloud.bin         0.0%  0.08W
 5.  xdg-desktop-port   0.0%  0.05W
-------------------------------------
  14:22:05
```

**Full widget** (opened on demand): a dark-themed card with checkboxes, kill buttons, CSV export, and an expandable process list.

---

## Installation

### 1. System packages (requires your password once)

```bash
sudo apt-get install -y \
    python3-gi python3-gi-cairo \
    gir1.2-gtk-3.0 gir1.2-notify-0.7 \
    upower python3-dbus libnotify-bin
```

### 2. Python packages

```bash
pip3 install --user psutil
```

### 3. Run the install script

```bash
cd ~/projects/battery-monitor
bash scripts/install.sh
```

This will:
- Create all needed directories under `~/.config/` and `~/.local/`
- Write a default config to `~/.config/battery-monitor/battery-monitor.conf`
- Create launcher commands in `~/.local/bin/`
- Register and start the daemon as a systemd user service (autostart on login)
- Register the overlay to autostart on login via GNOME

> If `~/.local/bin` is not in your PATH, add this line to your `~/.bashrc` or `~/.zshrc`:
> ```bash
> export PATH="$HOME/.local/bin:$PATH"
> ```

---

## Usage

### Overlay (always-on-top floating text)

Starts automatically when you log in. To start it manually:

```bash
battery-monitor-overlay
```

- **Drag** it anywhere on screen by clicking and holding
- **Right-click** for a small menu (Refresh / Quit)

### Full widget

```bash
battery-monitor-ui
```

Features: top 10 processes with checkboxes, kill selected (SIGTERM or SIGKILL), safety labels, expand/collapse, CSV export, pin-on-top toggle.

### CLI tool (`bmon`)

Quick terminal inspection without opening any window:

```bash
bmon                  # battery status + top consumers
bmon top 15           # show top 15 processes
bmon history          # discharge chart for the last 30 minutes
bmon kill 1234        # terminate process by PID (with confirmation)
bmon export data.csv  # export history to CSV
```

### Daemon management

```bash
systemctl --user status battery-monitor    # is it running?
systemctl --user restart battery-monitor   # restart after config changes
systemctl --user stop battery-monitor      # stop it
systemctl --user disable battery-monitor   # remove from autostart
journalctl --user -u battery-monitor -f    # watch live logs
```

---

## Configuration

Edit `~/.config/battery-monitor/battery-monitor.conf` and restart the daemon for changes to take effect.

```json
{
    "collection_interval_seconds": 60,
    "history_minutes": 30,
    "top_processes_count": 10,
    "show_top_n_in_widget": 5,
    "spike_threshold_watts": 5.0,
    "high_drain_threshold_watts": 20.0,
    "notify_on_spike": true,
    "notify_on_high_drain": true,
    "always_on_top": false
}
```

| Setting | What it does |
|---|---|
| `collection_interval_seconds` | How often the daemon collects data (seconds) |
| `history_minutes` | How much history to keep in the database |
| `top_processes_count` | How many processes to store per snapshot |
| `show_top_n_in_widget` | How many processes to display in the overlay and widget |
| `spike_threshold_watts` | Notify when a process suddenly uses this many extra watts |
| `high_drain_threshold_watts` | Notify when total discharge exceeds this value |
| `notify_on_spike` | Enable/disable spike notifications |
| `notify_on_high_drain` | Enable/disable high-drain notifications |
| `always_on_top` | Start the full widget pinned above other windows |

---

## How power estimation works

Direct per-process wattage measurement requires root access on Linux. Instead, the app uses a layered heuristic:

1. **Read total discharge** from `/sys/class/power_supply/BAT*/power_now` — this is the real number your battery reports.
2. **Subtract component overhead** — screen brightness, WiFi activity, USB devices, keyboard backlight, and a base kernel cost.
3. **Distribute the remaining watts** to processes proportionally to their share of total CPU usage.
4. **Add a memory contribution** — a small amount per MB of RAM used (DRAM leakage proxy).

This won't be as precise as `powertop` (which needs root), but it's accurate enough to rank consumers and catch spikes.

### Process safety levels

Every process in the list is labelled:

| Label | Meaning |
|---|---|
| `SAFE` | Owned by you — safe to terminate |
| `CAUTION` | A system service that may restart or affect your session |
| `PROTECTED` | Core OS process — blocked from termination |

Protected processes (kernel threads, display server, audio daemon, NetworkManager, etc.) are never shown in the overlay and can never be killed through the app.

### Python3 process names

Instead of showing `python3` for every Python script, the app inspects the command line and resolves the real name:

| What's running | What you see |
|---|---|
| `python3 battery_daemon.py` | `py:battery_daemon` |
| `python3 -m http.server` | `py:http.server` |
| `python3 -c "..."` | `py:(inline)` |

---

## File structure

```
battery-monitor/
├── src/
│   ├── shared/
│   │   └── config.py           # reads ~/.config/battery-monitor/*.conf
│   ├── daemon/
│   │   ├── battery_daemon.py   # main daemon loop + notifications
│   │   ├── data_collector.py   # reads /sys, upower, psutil
│   │   ├── power_attribution.py # watts-per-process heuristics
│   │   ├── process_classifier.py # safe / caution / protected labels
│   │   └── database.py         # SQLite read/write layer
│   └── ui/
│       ├── overlay_widget.py   # plain-text always-on floating overlay
│       ├── battery_widget.py   # full-featured GTK widget (on demand)
│       └── process_killer.py   # safe SIGTERM/SIGKILL logic
├── scripts/
│   ├── install.sh              # one-command installer
│   └── bmon                    # CLI tool
├── systemd/
│   └── battery-monitor.service # template (install.sh writes the real one)
├── config/
│   └── battery-monitor.conf    # default configuration
└── requirements.txt
```

### Where data lives at runtime

| Path | Contents |
|---|---|
| `~/.config/battery-monitor/battery-monitor.conf` | Your personal settings |
| `~/.local/share/battery-monitor/battery_monitor.db` | SQLite database |
| `~/.local/share/battery-monitor/logs/daemon.log` | Daemon log file |
| `~/.config/systemd/user/battery-monitor.service` | Systemd service |
| `~/.config/autostart/battery-monitor-overlay.desktop` | Overlay autostart |
| `~/.local/bin/battery-monitor-{ui,overlay,daemon}` | Launcher commands |

---

## Uninstalling

```bash
# Stop and disable the daemon
systemctl --user stop battery-monitor
systemctl --user disable battery-monitor

# Remove autostart entries
rm ~/.config/autostart/battery-monitor-overlay.desktop
rm ~/.config/systemd/user/battery-monitor.service

# Remove launchers and desktop entries
rm ~/.local/bin/battery-monitor-{ui,overlay,daemon}
rm ~/.local/bin/bmon
rm ~/.local/share/applications/battery-monitor*.desktop

# Remove data (optional — only if you want to erase all history)
rm -rf ~/.local/share/battery-monitor
rm -rf ~/.config/battery-monitor
```
