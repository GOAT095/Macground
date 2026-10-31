<div align="center">

# 🎬 Macground

**Play any custom video (`.mp4`, `.mov`, `.m4v`) or animated `.gif` as your live macOS Desktop Wallpaper and Lock Screen.**

[![macOS](https://img.shields.io/badge/macOS-Sonoma%20%7C%20Sequoia%20%7C%20Tahoe%20(14%2B)-000000?style=for-the-badge&logo=apple&logoColor=white)](#)
[![Python](https://img.shields.io/badge/Python-3.9%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](#)
[![Zero Dependencies](https://img.shields.io/badge/Dependencies-Zero%20(Native%20AVFoundation)-2EA44F?style=for-the-badge)](#)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=for-the-badge)](#license)

</div>

---

## ✨ Why Macground?

Most macOS live wallpaper tools only work on the Desktop, require heavy third-party apps, go black/static after 2–3 screen locks on short `.mp4` clips, or break on newer macOS releases because Apple hardcodes the default **Golden Gate Sunset** (`4207734D-74FE-4F92-B5E1-6EC8DEE24A15`) wallpaper to a read-only system volume file (`/System/Library/Wallpapers/.default/Golden Gate.mov`).

**Macground** solves both Desktop and Lock Screen live wallpapers in a single, zero-dependency Python script:

- **🖥️ True Desktop Video & GIF Layer (`--mode desktop`):** Compiles a lightweight native macOS `Cocoa` + `AVFoundation` helper on the fly that pins a borderless, click-through video (`AVPlayerLooper`) or animated GIF (`NSImageView`) window strictly behind your desktop icons (`kCGDesktopWindowLevel`) across all Spaces and monitors at full native 60 FPS. Features **intelligent auto-pause** when windows/fullscreen apps completely cover the desktop, on display sleep, or on screen lock, dropping CPU to 0.0% and conserving battery.
- **🔒 Native 4K @ 60 FPS Lock Screen Engine (`--mode lockscreen`):** Encodes your videos at native 4K UHD resolution (`3840x2160`) at a silky-smooth **60 FPS** with high-bitrate HEVC compression (~50 Mbps target). Features **Direct GPU Buffer Transfer** (zero CPU memcpy) matching Apple's native `4KSDR240FPS` Aerial format, hardlinking (`os.link`) the result across all Aerial slots so it uses 0 extra disk space and encodes in seconds.
- **🎞️ Built-In Animated `.gif` Engine:** Renders `.gif` files natively via `AppKit` on the Desktop with zero conversion delay, and automatically converts `.gif` files into hardware-accelerated QuickTime `.mov` loops for the Lock Screen using native macOS `ImageIO` + `AVFoundation`.
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

> **💡 Lock Screen Tip:** Because macOS hardcodes **Golden Gate Sunset** to the read-only system bridge video, open **System Settings > Wallpaper** once, make sure **Show as screen saver** is enabled, and click **Tahoe Day** (right next to Golden Gate Sunset under *Landscape*), **Sequoia**, or **Yosemite**. Macground pre-populates all of those slots with your custom video so it plays immediately when you lock your screen (`Ctrl + Cmd + Q`).

---

## 🛠️ Usage & CLI Options

```text
usage: Macground.py [-h] [--mode {both,desktop,lockscreen}]
                    [--target-id TARGET_ID] [--unmute] [--fit]
                    [--foreground] [--stop]
                    [--list-lockscreen-slots] [--restore-lockscreen]
                    [video]
```

| Command / Flag | Description | Requires `sudo`? |
| :--- | :--- | :---: |
| `python3 Macground.py file.mp4 --mode desktop` | Play video or `.gif` on the Desktop background only (behind desktop icons, across all Spaces and monitors). | No |
| `sudo python3 Macground.py file.mp4` | Apply to **both** the Desktop background and the macOS Lock Screen (`--mode both` is default). | Yes |
| `sudo python3 Macground.py file.mp4 --mode lockscreen` | Install video or `.gif` into the native macOS Lock Screen Aerial engine only. | Yes |
| `--unmute` | Enable audio playback in Desktop mode (muted by default). | No |
| `--fit` | Letterbox/fit the video or GIF inside screen bounds instead of aspect-filling the screen. | No |
| `--foreground` | Keep the Desktop player attached to your terminal window (`Ctrl+C` to stop) instead of daemonizing in the background. | No |
| `python3 Macground.py --stop` | Stop the running background Desktop video/GIF wallpaper process. | No |
| `python3 Macground.py --list-lockscreen-slots` | List all installed/downloaded macOS Aerial `.mov` slots on your Mac. | No |
| `sudo python3 Macground.py --restore-lockscreen` | Restore original Apple Aerial videos and your original `Index.plist` Lock Screen configuration from backup. | Yes |

---

## 🧠 How It Works Under the Hood

macOS separates the user Desktop session from the Lock Screen (`loginwindow` / `WallpaperAerialsExtension` / `idleassetsd`):

1. **Desktop Engine (`AVQueuePlayer` + `AVPlayerLooper` / `NSImageView`):**
   On first run, Macground compiles a tiny native Swift binary into `.wallpaper_cache/desktop_video_player`. It creates a borderless `NSWindow` for every connected display at window level `kCGDesktopWindowLevel` with `.canJoinAllSpaces`, `.stationary`, and `ignoresMouseEvents = true`, plus `NSWorkspace.screensDidWakeNotification` and `com.apple.screenIsUnlocked` observers so playback never stalls after sleep or unlock.
2. **Sequential Compressed Sample Loop & `FastStart` Builder (`AVAssetReader` + `AVAssetWriter` + `AVAssetExportSession`):**
   Apple's native Lock Screen `.mov` files are 4-minute (`240s`), single-track (`tracks=1`), single-segment (`segments=1`), `moov`-first (`ftyp` -> `moov` -> `mdat`) QuickTime movies whose `stco` chunk byte offsets increase strictly monotonically from start to finish. If a file loops via multi-segment `elst` edit lists or non-monotonic `stco` chunk pointers, macOS's sandboxed Lock Screen hardware decoder fails on the 2nd lock with a black or static screen. Macground reads the compressed video samples once, strips audio, shifts B-frame timestamps to `0.0s`, writes 240 seconds of sequential compressed samples, and runs `AVAssetExportSession` with `shouldOptimizeForNetworkUse = true` so the resulting `.mov` matches Apple's native Aerial structure byte-for-byte.
3. **Hardlinked Aerial Injection & Hardcode Bypass:**
   On modern macOS, `WallpaperAerialsExtension` checks if the selected wallpaper ID is `4207734D-74FE-4F92-B5E1-6EC8DEE24A15` (*Golden Gate Sunset*) and forces `/System/Library/Wallpapers/.default/Golden Gate.mov` if so. Macground automatically switches your active `~/Library/Application Support/com.apple.wallpaper/Store/Index.plist` choice to non-hardcoded Landscape Aerial slots (`4C108785-A7BA-422E-9C79-B0129F1D5550` *Tahoe Day*, *Sequoia*, *Yosemite*, etc.), hardlinks (`os.link`) the prepared `.mov` across all Aerial directories so no extra disk space is used, clears `com.apple.quarantine` attributes and `ZPOINTSOFINTEREST` seek checkpoints in `Aerial.sqlite`, and restarts the wallpaper daemons.

---

## 🔍 Search Keywords & Topics

`macos-wallpaper` · `live-wallpaper-macos` · `video-wallpaper-mac` · `lockscreen-video-mac` · `gif-wallpaper-mac` · `macos-lock-screen` · `idleassetsd` · `aerial-wallpaper` · `macos-sonoma-wallpaper` · `macos-sequoia-wallpaper` · `macos-tahoe-wallpaper` · `avfoundation` · `python-macos`

---

## 📄 License

Distributed under the **MIT License**. Feel free to fork, customize, and share!
