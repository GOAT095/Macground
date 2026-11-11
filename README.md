<div align="center">

# 🎬 Macground

**High-performance 4K video (`.mp4`, `.mov`, `.m4v`) and animated `.gif` live wallpapers for macOS Desktop & Lock Screen.**

[![macOS](https://img.shields.io/badge/macOS-Sonoma%20%7C%20Sequoia%20%7C%20Tahoe%20(14%2B)-000000?style=for-the-badge&logo=apple&logoColor=white)](#)
[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](#)
[![Zero Dependencies](https://img.shields.io/badge/Dependencies-Zero%20(Native%20AVFoundation)-2EA44F?style=for-the-badge)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](#license)
[![Architecture diagram](https://gitdiagram.com/diagram-badge.svg)](https://gitdiagram.com/goat095/macground?utm_source=readme&utm_medium=badge)

</div>

---

## ✨ Features

- **🖥️ True Desktop Video & GIF Layer (`--mode desktop`):** Compiles a lightweight native macOS `Cocoa` + `AVFoundation` helper that pins a borderless, click-through video (`AVPlayerLooper`) or animated GIF (`NSImageView`) window strictly behind your desktop icons (`kCGDesktopWindowLevel`) across all Spaces and monitors at full native 60 FPS. Features **intelligent auto-pause** when windows/fullscreen apps completely cover the desktop, on display sleep, or on screen lock, dropping CPU to 0.0% and conserving battery.
- **🔋 Battery Saver & Low Power Mode:**
  - **Auto-Pause on Battery (`--pause-on-battery`):** Automatically halts video rendering when your MacBook is unplugged from AC power, freezing on a clean frame to maximize battery life.
  - **Low Power Mode Integration (`--no-pause-on-low-power` to override):** Automatically pauses whenever macOS Low Power Mode is active.
  - **Status Bar Toggles:** Toggle battery and low power behaviors on the fly directly from the menu bar.
- **🔄 Playlist & Shuffle Rotation (`--interval`, `--shuffle`):**
  - Point Macground to a single file or an entire folder of wallpapers (`python3 Macground.py ~/Wallpapers --interval 15m --shuffle`).
  - Automatically rotates wallpapers on customizable timers (1m, 5m, 15m, 30m, 1h).
  - One-click **Next Wallpaper** (`n`) and **Previous Wallpaper** (`b`) menu controls.
- **🔁 Seamless Looping & Ping-Pong Bounce (`--ping-pong`, `--crossfade`):**
  - **Ping-Pong Mode (`--ping-pong`):** Plays video forward and then in reverse back to the start, creating a natural seamless bounce loop with zero jump cut.
  - **Crossfade (`--crossfade <frames>`):** Smoothly dissolves loop boundaries so looping animations feel infinite.
- **🖥️ Native macOS Menu Bar Controls (`NSStatusItem`):** Uses an Apple HIG-compliant monochrome template SF Symbol (`play.desktopcomputer`) in the top-right menu bar:
  - **Current File Indicator:** Displays the active wallpaper and playlist position (`1/8`).
  - **Next / Previous / Shuffle Controls:** Instant playlist navigation.
  - **Change Wallpaper...:** Launches a native macOS file dialog (`NSOpenPanel`) to browse and swap wallpapers directly from Finder. Automatically syncs to **both** your Desktop and Lock Screen in real time.
  - **Recent Wallpapers:** Fast one-click switching between your recently used wallpapers.
  - **Energy & Battery Saver Submenu:** One-click toggles for "Pause on Battery Power" and "Pause in Low Power Mode".
  - **Playback Speed Control:** Toggle between 0.25x, 0.5x, 0.75x, 1.0x, 1.25x, 1.5x, and 2.0x on the fly.
  - **Pause / Resume:** Instant playback toggle with real-time status indication.
  - **Stop Wallpaper:** Cleanly exits the background process.
- **📦 Standalone `.app` Bundle (`--build-app` / `--install-app`):**
  - Package Macground into a native double-clickable `Macground.app` with an embedded app icon.
  - Configured with `LSUIElement=true` to run cleanly as a menu bar agent with zero Dock clutter.
- **🚀 Launch at Login (`--startup` / `--no-startup`):** Auto-starts your chosen desktop wallpaper upon user login using a lightweight native user LaunchAgent (`~/Library/LaunchAgents/com.macground.desktop.plist`), completely independent of terminal sessions.
- **🔒 Native 4K @ 60 FPS Lock Screen Engine (`--mode lockscreen`):** Encodes your videos at native 4K UHD resolution (`3840x2160`) at a silky-smooth **60 FPS** with high-bitrate HEVC compression (~50 Mbps target). Features **Direct GPU Buffer Transfer** (zero CPU memcpy) matching Apple's native `4KSDR240FPS` Aerial format, hardlinking (`os.link`) the result across all Aerial slots so it uses 0 extra disk space and encodes in seconds.
- **🪶 Zero `pip` Dependencies:** Uses only the Python standard library and macOS's built-in Xcode Command Line Tools (`xcrun swiftc`).
- **♻️ One-Command Backup & Restore:** Automatically backs up original Apple `.mov` assets and your `Index.plist` before making changes, so you can revert to stock macOS settings at any time with `--restore-lockscreen`.

---

## 🚀 Quick Start

### 1. Clone the Repository

```bash
git clone https://github.com/yourusername/Macground.git
cd Macground
chmod +x Macground.py
```

*(Prerequisite: macOS 14 Sonoma, macOS 15 Sequoia, or macOS 26/27 Tahoe with Xcode Command Line Tools installed. If you don't have Command Line Tools yet, run `xcode-select --install`.)*

### 2. Set a Video or GIF on Both Desktop & Lock Screen (Default)

```bash
sudo python3 Macground.py /path/to/your/video_or_animation.mp4
```

Or with an animated `.gif`:

```bash
sudo python3 Macground.py /path/to/your/animation.gif
```

### 3. Rotate a Folder of Wallpapers on Desktop

```bash
python3 Macground.py ~/Wallpapers/ --mode desktop --interval 15m --shuffle --pause-on-battery
```

### 4. Build Standalone macOS App Bundle

```bash
python3 Macground.py --build-app
# Or install directly into /Applications
python3 Macground.py --install-app
```

---

## 🛠️ CLI Options & Reference

```text
usage: Macground.py [-h] [--mode {both,desktop,lockscreen}]
                    [--target-id TARGET_ID] [--fit] [--foreground]
                    [--speed SPEED] [--pause-on-battery]
                    [--no-pause-on-low-power] [--interval INTERVAL]
                    [--shuffle] [--ping-pong] [--crossfade CROSSFADE]
                    [--build-app] [--install-app] [--startup] [--no-startup]
                    [--stop] [--list-lockscreen-slots] [--restore-lockscreen]
                    [video]
```
[[https://gitdiagram.com/diagram-badge.svg]](https://gitdiagram.com/goat095/macground?utm_source=readme&utm_medium=badge)
| Flag | Description | Default | Requires `sudo`? |
| :--- | :--- | :---: | :---: |
| `video` | Path to a video file (`.mp4`, `.mov`, `.m4v`), GIF (`.gif`), or directory of wallpapers. | — | Mode dependent |
| `--mode {both,desktop,lockscreen}` | Wallpaper target. `desktop` requires no sudo. | `both` | Yes for lockscreen/both |
| `--pause-on-battery` | Auto-pause video playback when running on MacBook battery power. | `False` | No |
| `--no-pause-on-low-power` | Disable automatic pausing when macOS Low Power Mode is active. | Pauses on low power | No |
| `--interval <time>` | Auto-rotation interval for folders/playlists (e.g. `5m`, `15m`, `30m`, `1h`, `300s`). | `15m` for folders | No |
| `--shuffle` | Randomize the order of wallpapers in a folder or playlist. | `False` | No |
| `--ping-pong` | Seamless bounce loop (plays forward, then reverses back to start). | `False` | No |
| `--crossfade <frames>` | Crossfade frames at loop boundary to eliminate jump cuts. | `0` | No |
| `--build-app` | Package Macground into a standalone double-clickable `Macground.app`. | — | No |
| `--install-app` | Package and install `Macground.app` into `/Applications`. | — | No |
| `--speed <float>` | Playback speed multiplier (e.g. `0.5` for cinematic slow-mo). | `1.0` | No |
| `--startup` | Auto-start the desktop wallpaper on login via `~/Library/LaunchAgents`. | `False` | No |
| `--no-startup` | Remove auto-start LaunchAgent. | — | No |
| `--fit` | Fit media inside screen bounds instead of aspect filling. | `False` | No |
| `--foreground` | Keep player attached to terminal instead of background daemon. | `False` | No |
| `--stop` | Stop the running background desktop wallpaper process. | — | No |
| `--list-lockscreen-slots` | List installed macOS Aerial `.mov` slots. | — | No |
| `--restore-lockscreen` | Restore stock Apple Aerial videos and original `Index.plist`. | — | Yes |

---

## 📄 License

Distributed under the **MIT License**.
