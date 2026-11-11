#!/usr/bin/env python3

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import plistlib
import random
import shutil
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path

SWIFT_DESKTOP_PLAYER_SOURCE = r"""
import Cocoa
import AVFoundation
import UniformTypeIdentifiers
import IOKit.ps

func isRunningOnBattery() -> Bool {
    guard let snapshot = IOPSCopyPowerSourcesInfo()?.takeRetainedValue(),
          let sources = IOPSCopyPowerSourcesList(snapshot)?.takeRetainedValue() as? [CFTypeRef] else {
        return false
    }
    for source in sources {
        guard let desc = IOPSGetPowerSourceDescription(snapshot, source)?.takeUnretainedValue() as? [String: Any] else { continue }
        if let state = desc[kIOPSPowerSourceStateKey as String] as? String {
            if state == kIOPSBatteryPowerValue as String {
                return true
            }
        }
    }
    return false
}

func scanMediaFolder(dirURL: URL) -> [URL] {
    let supported = Set(["mp4", "mov", "m4v", "gif"])
    let fm = FileManager.default
    guard let files = try? fm.contentsOfDirectory(at: dirURL, includingPropertiesForKeys: nil, options: [.skipsHiddenFiles]) else {
        return []
    }
    return files.filter { supported.contains($0.pathExtension.lowercased()) }
        .sorted { $0.lastPathComponent.localizedStandardCompare($1.lastPathComponent) == .orderedAscending }
}

class WallpaperAppDelegate: NSObject, NSApplicationDelegate {
    var windows: [NSWindow] = []
    var players: [AVQueuePlayer] = []
    var loopers: [AVPlayerLooper] = []
    var imageViews: [NSImageView] = []
    var statusItem: NSStatusItem?
    var playPauseMenuItem: NSMenuItem?
    var userManuallyPaused = false
    var mediaURL: URL
    let fillScreen: Bool
    var isGIF: Bool
    var currentSpeed: Float

    var isScreenLocked = false
    var isDisplayAsleep = false

    // Battery / Energy Saver
    var pauseOnBattery: Bool = false
    var pauseOnLowPower: Bool = true
    var powerCheckTimer: Timer?

    // Playlist & Auto-Rotation
    var playlist: [URL] = []
    var playlistIndex: Int = 0
    var isShuffle: Bool = false
    var rotationInterval: TimeInterval = 0 // 0 = disabled
    var rotationTimer: Timer?

    var recentsURL: URL {
        let home = FileManager.default.homeDirectoryForCurrentUser
        return home.appendingPathComponent(".macground_recents.json")
    }

    init(mediaURL: URL, fillScreen: Bool, speed: Float, pauseOnBattery: Bool = false, pauseOnLowPower: Bool = true, playlist: [URL] = [], interval: TimeInterval = 0, shuffle: Bool = false) {
        self.mediaURL = mediaURL
        self.fillScreen = fillScreen
        self.currentSpeed = max(0.1, min(4.0, speed))
        self.isGIF = mediaURL.pathExtension.lowercased() == "gif"
        self.pauseOnBattery = pauseOnBattery
        self.pauseOnLowPower = pauseOnLowPower
        self.playlist = playlist.isEmpty ? [mediaURL] : playlist
        self.rotationInterval = interval
        self.isShuffle = shuffle
        if let idx = self.playlist.firstIndex(of: mediaURL) {
            self.playlistIndex = idx
        }
        super.init()
    }

    func loadRecents() -> [String] {
        guard let data = try? Data(contentsOf: recentsURL),
              let list = try? JSONDecoder().decode([String].self, from: data) else {
            return []
        }
        return list.filter { p in FileManager.default.fileExists(atPath: p) }
    }

    func saveRecent(path: String) {
        var list = loadRecents().filter { p in p != path }
        list.insert(path, at: 0)
        if list.count > 8 {
            list = Array(list.prefix(8))
        }
        if let data = try? JSONEncoder().encode(list) {
            try? data.write(to: recentsURL)
        }
    }

    func updateLaunchAgentIfPresent() {
        let plistURL = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/LaunchAgents/com.macground.desktop.plist")
        guard FileManager.default.fileExists(atPath: plistURL.path) else { return }
        guard let data = try? Data(contentsOf: plistURL),
              var plist = (try? PropertyListSerialization.propertyList(from: data, options: [], format: nil)) as? [String: Any] else {
            return
        }
        var newArgs: [String] = [CommandLine.arguments[0], mediaURL.path]
        if !fillScreen { newArgs.append("--fit") }
        if currentSpeed != 1.0 { newArgs.append(contentsOf: ["--speed", String(format: "%.2f", currentSpeed)]) }
        if pauseOnBattery { newArgs.append("--pause-on-battery") }
        if !pauseOnLowPower { newArgs.append("--no-pause-on-low-power") }
        plist["ProgramArguments"] = newArgs
        if let updatedData = try? PropertyListSerialization.data(fromPropertyList: plist, format: .xml, options: 0) {
            try? updatedData.write(to: plistURL)
        }
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        saveRecent(path: mediaURL.path)
        setupWindows()
        setupStatusBarItem()
        restartRotationTimer()

        NotificationCenter.default.addObserver(
            self,
            selector: #selector(screensDidChange),
            name: NSApplication.didChangeScreenParametersNotification,
            object: nil
        )
        NSWorkspace.shared.notificationCenter.addObserver(
            self,
            selector: #selector(sessionDidWake),
            name: NSWorkspace.screensDidWakeNotification,
            object: nil
        )
        NSWorkspace.shared.notificationCenter.addObserver(
            self,
            selector: #selector(sessionDidSleep),
            name: NSWorkspace.screensDidSleepNotification,
            object: nil
        )
        NSWorkspace.shared.notificationCenter.addObserver(
            self,
            selector: #selector(spaceDidChange),
            name: NSWorkspace.activeSpaceDidChangeNotification,
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(windowOcclusionDidChange),
            name: NSWindow.didChangeOcclusionStateNotification,
            object: nil
        )
        DistributedNotificationCenter.default().addObserver(
            self,
            selector: #selector(sessionDidUnlock),
            name: NSNotification.Name("com.apple.screenIsUnlocked"),
            object: nil
        )
        DistributedNotificationCenter.default().addObserver(
            self,
            selector: #selector(sessionDidLock),
            name: NSNotification.Name("com.apple.screenIsLocked"),
            object: nil
        )
        NotificationCenter.default.addObserver(
            self,
            selector: #selector(powerStateDidChange),
            name: Notification.Name.NSProcessInfoPowerStateDidChange,
            object: nil
        )

        powerCheckTimer = Timer.scheduledTimer(withTimeInterval: 5.0, repeats: true) { [weak self] _ in
            self?.evaluatePlaybackState()
        }
    }

    func setupStatusBarItem() {
        if statusItem == nil {
            statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        }
        if let button = statusItem?.button {
            button.title = ""
            let icon: NSImage? = {
                if let img = NSImage(systemSymbolName: "play.desktopcomputer", accessibilityDescription: "Macground") {
                    return img
                }
                if let img = NSImage(systemSymbolName: "display", accessibilityDescription: "Macground") {
                    return img
                }
                return NSImage(systemSymbolName: "photo.tv", accessibilityDescription: "Macground")
            }()
            if let icon = icon {
                icon.isTemplate = true
                button.image = icon
                button.imagePosition = .imageOnly
            } else {
                button.title = "🖥"
            }
            button.toolTip = "Macground: \(mediaURL.lastPathComponent)"
        }
        let menu = NSMenu()
        menu.autoenablesItems = false

        let titleItem = NSMenuItem(title: "Macground", action: nil, keyEquivalent: "")
        titleItem.isEnabled = false
        menu.addItem(titleItem)

        let subtitle = playlist.count > 1 ? "\(mediaURL.lastPathComponent) (\(playlistIndex + 1)/\(playlist.count))" : mediaURL.lastPathComponent
        let currentItem = NSMenuItem(title: "Current: \(subtitle)", action: nil, keyEquivalent: "")
        currentItem.isEnabled = false
        menu.addItem(currentItem)
        menu.addItem(NSMenuItem.separator())

        if playlist.count > 1 {
            let nextItem = NSMenuItem(title: "Next Wallpaper", action: #selector(playNextWallpaper), keyEquivalent: "n")
            nextItem.target = self
            nextItem.image = NSImage(systemSymbolName: "forward.fill", accessibilityDescription: nil)
            menu.addItem(nextItem)

            let prevItem = NSMenuItem(title: "Previous Wallpaper", action: #selector(playPreviousWallpaper), keyEquivalent: "b")
            prevItem.target = self
            prevItem.image = NSImage(systemSymbolName: "backward.fill", accessibilityDescription: nil)
            menu.addItem(prevItem)

            let shuffleItem = NSMenuItem(title: "Shuffle Playlist", action: #selector(toggleShuffle), keyEquivalent: "")
            shuffleItem.target = self
            shuffleItem.image = NSImage(systemSymbolName: "shuffle", accessibilityDescription: nil)
            shuffleItem.state = isShuffle ? .on : .off
            menu.addItem(shuffleItem)

            let rotateMenu = NSMenu()
            let intervals: [(String, TimeInterval)] = [
                ("Off (Manual)", 0),
                ("1 Minute", 60),
                ("5 Minutes", 300),
                ("15 Minutes", 900),
                ("30 Minutes", 1800),
                ("1 Hour", 3600)
            ]
            for (label, sec) in intervals {
                let it = NSMenuItem(title: label, action: #selector(selectRotationInterval(_:)), keyEquivalent: "")
                it.target = self
                it.representedObject = NSNumber(value: sec)
                if abs(rotationInterval - sec) < 1.0 {
                    it.state = .on
                }
                rotateMenu.addItem(it)
            }
            let rotateParent = NSMenuItem(title: "Auto-Rotate Interval", action: nil, keyEquivalent: "")
            rotateParent.image = NSImage(systemSymbolName: "timer", accessibilityDescription: nil)
            rotateParent.submenu = rotateMenu
            menu.addItem(rotateParent)

            menu.addItem(NSMenuItem.separator())
        }

        let changeItem = NSMenuItem(title: "Change Wallpaper...", action: #selector(openWallpaperFilePicker), keyEquivalent: "o")
        changeItem.target = self
        changeItem.image = NSImage(systemSymbolName: "photo.badge.plus", accessibilityDescription: nil)
        menu.addItem(changeItem)

        let recentsMenu = NSMenu()
        let recents = loadRecents()
        if recents.isEmpty {
            let emptyItem = NSMenuItem(title: "No Recent Wallpapers", action: nil, keyEquivalent: "")
            emptyItem.isEnabled = false
            recentsMenu.addItem(emptyItem)
        } else {
            for path in recents {
                let u = URL(fileURLWithPath: path)
                let item = NSMenuItem(title: u.lastPathComponent, action: #selector(selectRecentWallpaper(_:)), keyEquivalent: "")
                item.target = self
                item.representedObject = path
                item.toolTip = path
                if path == mediaURL.path {
                    item.state = .on
                }
                recentsMenu.addItem(item)
            }
            recentsMenu.addItem(NSMenuItem.separator())
            let clearItem = NSMenuItem(title: "Clear Recents", action: #selector(clearRecents), keyEquivalent: "")
            clearItem.target = self
            clearItem.image = NSImage(systemSymbolName: "trash", accessibilityDescription: nil)
            recentsMenu.addItem(clearItem)
        }
        let recentsParentItem = NSMenuItem(title: "Recent Wallpapers", action: nil, keyEquivalent: "")
        recentsParentItem.image = NSImage(systemSymbolName: "clock.arrow.circlepath", accessibilityDescription: nil)
        recentsParentItem.submenu = recentsMenu
        menu.addItem(recentsParentItem)

        menu.addItem(NSMenuItem.separator())

        let batteryMenu = NSMenu()
        let batteryToggleItem = NSMenuItem(title: "Pause on Battery Power", action: #selector(togglePauseOnBattery), keyEquivalent: "")
        batteryToggleItem.target = self
        batteryToggleItem.image = NSImage(systemSymbolName: "battery.50", accessibilityDescription: nil)
        batteryToggleItem.state = pauseOnBattery ? .on : .off
        batteryMenu.addItem(batteryToggleItem)

        let lowPowerToggleItem = NSMenuItem(title: "Pause in Low Power Mode", action: #selector(togglePauseOnLowPower), keyEquivalent: "")
        lowPowerToggleItem.target = self
        lowPowerToggleItem.image = NSImage(systemSymbolName: "leaf.fill", accessibilityDescription: nil)
        lowPowerToggleItem.state = pauseOnLowPower ? .on : .off
        batteryMenu.addItem(lowPowerToggleItem)

        let batteryParentItem = NSMenuItem(title: "Energy & Battery Saver", action: nil, keyEquivalent: "")
        batteryParentItem.image = NSImage(systemSymbolName: "bolt.battery", accessibilityDescription: nil)
        batteryParentItem.submenu = batteryMenu
        menu.addItem(batteryParentItem)

        menu.addItem(NSMenuItem.separator())

        playPauseMenuItem = NSMenuItem(
            title: userManuallyPaused ? "Resume Wallpaper" : "Pause Wallpaper",
            action: #selector(togglePlayPause),
            keyEquivalent: "p"
        )
        playPauseMenuItem?.target = self
        playPauseMenuItem?.image = NSImage(systemSymbolName: userManuallyPaused ? "play.fill" : "pause.fill", accessibilityDescription: nil)
        menu.addItem(playPauseMenuItem!)

        let speedMenu = NSMenu()
        let speedOptions: [(String, Float)] = [
            ("0.25x (Super Slow)", 0.25),
            ("0.5x (Slow-Mo)", 0.5),
            ("0.75x (Cinematic)", 0.75),
            ("1.0x (Normal)", 1.0),
            ("1.25x (Fast)", 1.25),
            ("1.5x (Very Fast)", 1.5),
            ("2.0x (Double Speed)", 2.0)
        ]
        for (label, s) in speedOptions {
            let item = NSMenuItem(title: label, action: #selector(selectSpeed(_:)), keyEquivalent: "")
            item.target = self
            item.representedObject = NSNumber(value: s)
            if abs(currentSpeed - s) < 0.05 {
                item.state = .on
            }
            speedMenu.addItem(item)
        }
        let speedParentItem = NSMenuItem(title: "Playback Speed (\(String(format: "%.2fx", currentSpeed)))", action: nil, keyEquivalent: "")
        speedParentItem.image = NSImage(systemSymbolName: "speedometer", accessibilityDescription: nil)
        speedParentItem.submenu = speedMenu
        if isGIF {
            speedParentItem.isEnabled = false
        }
        menu.addItem(speedParentItem)

        menu.addItem(NSMenuItem.separator())
        let quitItem = NSMenuItem(title: "Stop Wallpaper", action: #selector(quitApp), keyEquivalent: "q")
        quitItem.target = self
        quitItem.image = NSImage(systemSymbolName: "power", accessibilityDescription: nil)
        menu.addItem(quitItem)

        statusItem?.menu = menu
    }

    @objc func playNextWallpaper() {
        guard playlist.count > 1 else { return }
        if isShuffle {
            var nextIdx = Int.random(in: 0..<playlist.count)
            if nextIdx == playlistIndex { nextIdx = (nextIdx + 1) % playlist.count }
            playlistIndex = nextIdx
        } else {
            playlistIndex = (playlistIndex + 1) % playlist.count
        }
        switchMedia(url: playlist[playlistIndex])
    }

    @objc func playPreviousWallpaper() {
        guard playlist.count > 1 else { return }
        playlistIndex = (playlistIndex - 1 + playlist.count) % playlist.count
        switchMedia(url: playlist[playlistIndex])
    }

    @objc func toggleShuffle() {
        isShuffle.toggle()
        setupStatusBarItem()
    }

    @objc func selectRotationInterval(_ sender: NSMenuItem) {
        guard let secondsNum = sender.representedObject as? NSNumber else { return }
        rotationInterval = secondsNum.doubleValue
        restartRotationTimer()
        setupStatusBarItem()
    }

    func restartRotationTimer() {
        rotationTimer?.invalidate()
        rotationTimer = nil
        guard rotationInterval > 0, playlist.count > 1 else { return }
        rotationTimer = Timer.scheduledTimer(withTimeInterval: rotationInterval, repeats: true) { [weak self] _ in
            self?.playNextWallpaper()
        }
    }

    @objc func togglePauseOnBattery() {
        pauseOnBattery.toggle()
        evaluatePlaybackState()
        setupStatusBarItem()
    }

    @objc func togglePauseOnLowPower() {
        pauseOnLowPower.toggle()
        evaluatePlaybackState()
        setupStatusBarItem()
    }

    @objc func powerStateDidChange() {
        evaluatePlaybackState()
    }

    @objc func openWallpaperFilePicker() {
        NSApp.activate(ignoringOtherApps: true)
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.movie, .quickTimeMovie, .mpeg4Movie, .gif]
        panel.title = "Choose Desktop Wallpaper or Folder"
        panel.prompt = "Set Wallpaper"
        if panel.runModal() == .OK, let selectedURL = panel.url {
            var isDir: ObjCBool = false
            if FileManager.default.fileExists(atPath: selectedURL.path, isDirectory: &isDir), isDir.boolValue {
                let items = scanMediaFolder(dirURL: selectedURL)
                if !items.isEmpty {
                    self.playlist = items
                    self.playlistIndex = 0
                    if self.rotationInterval == 0 {
                        self.rotationInterval = 900 // default 15m for newly selected folder
                    }
                    restartRotationTimer()
                    switchMedia(url: items[0])
                }
            } else {
                self.playlist = [selectedURL]
                self.playlistIndex = 0
                restartRotationTimer()
                switchMedia(url: selectedURL)
            }
        }
    }

    @objc func selectRecentWallpaper(_ sender: NSMenuItem) {
        guard let path = sender.representedObject as? String else { return }
        let selectedURL = URL(fileURLWithPath: path)
        self.playlist = [selectedURL]
        self.playlistIndex = 0
        restartRotationTimer()
        switchMedia(url: selectedURL)
    }

    @objc func clearRecents() {
        try? FileManager.default.removeItem(at: recentsURL)
        setupStatusBarItem()
    }

    func switchMedia(url: URL) {
        guard FileManager.default.fileExists(atPath: url.path) else { return }
        self.mediaURL = url
        self.isGIF = url.pathExtension.lowercased() == "gif"
        saveRecent(path: url.path)
        updateLaunchAgentIfPresent()

        for player in players {
            player.pause()
        }
        players.removeAll()
        loopers.removeAll()
        imageViews.removeAll()

        for window in windows {
            setupWindowContent(window: window)
        }

        evaluatePlaybackState()
        setupStatusBarItem()

        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.syncLockScreen(mediaURL: url)
        }
    }

    func syncLockScreen(mediaURL: URL) {
        let reqFile = "/tmp/.macground_lockscreen_request"
        try? mediaURL.path.write(toFile: reqFile, atomically: true, encoding: .utf8)
        try? FileManager.default.setAttributes([.posixPermissions: 0o666], ofItemAtPath: reqFile)

        let scriptPath: String = {
            if let idx = CommandLine.arguments.firstIndex(of: "--script-path"), idx + 1 < CommandLine.arguments.count {
                return CommandLine.arguments[idx + 1]
            }
            return URL(fileURLWithPath: CommandLine.arguments[0])
                .deletingLastPathComponent()
                .deletingLastPathComponent()
                .appendingPathComponent("Macground.py").path
        }()

        if FileManager.default.fileExists(atPath: scriptPath) {
            let task = Process()
            task.executableURL = URL(fileURLWithPath: "/usr/bin/python3")
            task.arguments = [scriptPath, mediaURL.path, "--mode", "lockscreen"]
            task.standardOutput = FileHandle.nullDevice
            task.standardError = FileHandle.nullDevice
            try? task.run()
            task.waitUntilExit()
        }

        let triggerPath = "/tmp/.macground_unlock_trigger"
        try? "\(Date().timeIntervalSince1970)".write(toFile: triggerPath, atomically: true, encoding: .utf8)

        for procName in ["WallpaperAerialsExtension", "WallpaperVideoExtension", "WallpaperAgent", "NeptuneOneWallpaper"] {
            let task = Process()
            task.executableURL = URL(fileURLWithPath: "/usr/bin/killall")
            task.arguments = ["-9", procName]
            task.standardOutput = FileHandle.nullDevice
            task.standardError = FileHandle.nullDevice
            try? task.run()
        }
    }

    @objc func selectSpeed(_ sender: NSMenuItem) {
        guard let speedNum = sender.representedObject as? NSNumber else { return }
        let newSpeed = speedNum.floatValue
        currentSpeed = newSpeed
        for player in players {
            player.defaultRate = currentSpeed
            if player.rate > 0 {
                player.playImmediately(atRate: currentSpeed)
            }
        }
        updateLaunchAgentIfPresent()
        setupStatusBarItem()
    }

    @objc func togglePlayPause() {
        let isCurrentlyPlaying = isGIF ? (imageViews.first?.animates ?? true) : players.contains { p in p.rate > 0 }
        if isCurrentlyPlaying {
            userManuallyPaused = true
            pauseAllPlayers()
            playPauseMenuItem?.title = "Resume Wallpaper"
            playPauseMenuItem?.image = NSImage(systemSymbolName: "play.fill", accessibilityDescription: nil)
        } else {
            userManuallyPaused = false
            resumeAllPlayers()
            playPauseMenuItem?.title = "Pause Wallpaper"
            playPauseMenuItem?.image = NSImage(systemSymbolName: "pause.fill", accessibilityDescription: nil)
        }
    }

    @objc func quitApp() {
        NSApp.terminate(nil)
    }

    func pauseAllPlayers() {
        for player in players {
            if player.rate != 0 {
                player.pause()
            }
        }
        for iv in imageViews {
            iv.animates = false
        }
    }

    func resumeAllPlayers() {
        for player in players {
            if player.rate == 0 {
                player.defaultRate = currentSpeed
                player.playImmediately(atRate: currentSpeed)
            }
        }
        for iv in imageViews {
            iv.animates = true
        }
    }

    func evaluatePlaybackState() {
        let onBattery = isRunningOnBattery()
        let inLowPower = ProcessInfo.processInfo.isLowPowerModeEnabled
        let batteryBlock = pauseOnBattery && onBattery
        let lowPowerBlock = pauseOnLowPower && inLowPower

        if userManuallyPaused || isScreenLocked || isDisplayAsleep || batteryBlock || lowPowerBlock {
            pauseAllPlayers()
            if batteryBlock {
                playPauseMenuItem?.title = "Paused (On Battery)"
                playPauseMenuItem?.image = NSImage(systemSymbolName: "battery.50", accessibilityDescription: nil)
            } else if lowPowerBlock {
                playPauseMenuItem?.title = "Paused (Low Power Mode)"
                playPauseMenuItem?.image = NSImage(systemSymbolName: "leaf.fill", accessibilityDescription: nil)
            } else {
                playPauseMenuItem?.title = "Resume Wallpaper"
                playPauseMenuItem?.image = NSImage(systemSymbolName: "play.fill", accessibilityDescription: nil)
            }
            return
        }

        let isAnyVisible = windows.isEmpty ? true : windows.contains { w in w.occlusionState.contains(.visible) }
        if isAnyVisible {
            resumeAllPlayers()
            playPauseMenuItem?.title = "Pause Wallpaper"
            playPauseMenuItem?.image = NSImage(systemSymbolName: "pause.fill", accessibilityDescription: nil)
        } else {
            pauseAllPlayers()
            playPauseMenuItem?.title = "Resume Wallpaper"
            playPauseMenuItem?.image = NSImage(systemSymbolName: "play.fill", accessibilityDescription: nil)
        }
    }

    @objc func windowOcclusionDidChange() {
        evaluatePlaybackState()
    }

    @objc func spaceDidChange() {
        evaluatePlaybackState()
    }

    @objc func sessionDidSleep() {
        isDisplayAsleep = true
        pauseAllPlayers()
    }

    @objc func sessionDidWake() {
        isDisplayAsleep = false
        evaluatePlaybackState()
    }

    @objc func sessionDidLock() {
        isScreenLocked = true
        pauseAllPlayers()
    }

    @objc func sessionDidUnlock() {
        isScreenLocked = false
        evaluatePlaybackState()
        for procName in ["WallpaperAerialsExtension", "WallpaperVideoExtension", "WallpaperAgent", "NeptuneOneWallpaper"] {
            let task = Process()
            task.executableURL = URL(fileURLWithPath: "/usr/bin/killall")
            task.arguments = ["-9", procName]
            task.standardOutput = FileHandle.nullDevice
            task.standardError = FileHandle.nullDevice
            try? task.run()
        }
        let triggerPath = "/tmp/.macground_unlock_trigger"
        try? "\(Date().timeIntervalSince1970)".write(toFile: triggerPath, atomically: true, encoding: .utf8)
    }

    @objc func screensDidChange() {
        for player in players {
            player.pause()
        }
        for window in windows {
            window.orderOut(nil)
        }
        windows.removeAll()
        players.removeAll()
        loopers.removeAll()
        imageViews.removeAll()
        setupWindows()
    }

    func setupWindows() {
        for screen in NSScreen.screens {
            let frame = screen.frame
            let window = NSWindow(
                contentRect: frame,
                styleMask: [.borderless],
                backing: .buffered,
                defer: false
            )

            let desktopLevel = Int(CGWindowLevelForKey(.desktopWindow))
            window.level = NSWindow.Level(rawValue: desktopLevel)
            window.collectionBehavior = [
                .canJoinAllSpaces,
                .stationary,
                .ignoresCycle,
                .fullScreenAuxiliary
            ]
            window.ignoresMouseEvents = true
            window.isOpaque = true
            window.hasShadow = false
            window.backgroundColor = .black

            setupWindowContent(window: window)
            windows.append(window)
        }
    }

    func setupWindowContent(window: NSWindow) {
        let frame = window.frame
        if isGIF, let gifImage = NSImage(contentsOf: mediaURL) {
            let containerView = NSView(frame: NSRect(origin: .zero, size: frame.size))
            containerView.wantsLayer = true
            containerView.layer?.masksToBounds = true

            let imageView = NSImageView(frame: containerView.bounds)
            imageView.image = gifImage
            imageView.animates = true
            imageView.canDrawSubviewsIntoLayer = true

            if fillScreen && gifImage.size.width > 0 && gifImage.size.height > 0 {
                let screenRatio = frame.width / frame.height
                let imageRatio = gifImage.size.width / gifImage.size.height
                var drawWidth = frame.width
                var drawHeight = frame.height
                if screenRatio > imageRatio {
                    drawHeight = frame.width / imageRatio
                } else {
                    drawWidth = frame.height * imageRatio
                }
                let offsetX = (frame.width - drawWidth) / 2.0
                let offsetY = (frame.height - drawHeight) / 2.0
                imageView.frame = NSRect(x: offsetX, y: offsetY, width: drawWidth, height: drawHeight)
                imageView.imageScaling = .scaleAxesIndependently
            } else {
                imageView.frame = containerView.bounds
                imageView.autoresizingMask = [.width, .height]
                imageView.imageScaling = .scaleProportionallyUpOrDown
            }

            imageViews.append(imageView)
            containerView.addSubview(imageView)
            window.contentView = containerView
            window.orderFrontRegardless()
        } else {
            let asset = AVURLAsset(url: mediaURL)
            let view = NSView(frame: NSRect(origin: .zero, size: frame.size))
            view.wantsLayer = true

            let playerItem = AVPlayerItem(asset: asset)
            let queuePlayer = AVQueuePlayer(playerItem: playerItem)
            queuePlayer.isMuted = true
            queuePlayer.defaultRate = currentSpeed
            queuePlayer.preventsDisplaySleepDuringVideoPlayback = false

            let looper = AVPlayerLooper(player: queuePlayer, templateItem: playerItem)

            let playerLayer = AVPlayerLayer(player: queuePlayer)
            playerLayer.frame = view.bounds
            playerLayer.autoresizingMask = [.layerWidthSizable, .layerHeightSizable]
            playerLayer.videoGravity = fillScreen ? .resizeAspectFill : .resizeAspect

            view.layer?.addSublayer(playerLayer)
            window.contentView = view
            window.orderFrontRegardless()

            if !userManuallyPaused && !isScreenLocked && !isDisplayAsleep {
                let onBattery = isRunningOnBattery()
                let inLowPower = ProcessInfo.processInfo.isLowPowerModeEnabled
                if !(pauseOnBattery && onBattery) && !(pauseOnLowPower && inLowPower) {
                    queuePlayer.playImmediately(atRate: currentSpeed)
                }
            }

            players.append(queuePlayer)
            loopers.append(looper)
        }
    }
}

let args = CommandLine.arguments
var targetPath: String? = nil
var fitScreen = false
var speedVal: Float = 1.0
var pauseBattery = false
var pauseLowPower = true
var rotateInterval: TimeInterval = 0
var shuffleActive = false
var playlistItems: [URL] = []
var openPickerAtLaunch = false

var idx = 1
while idx < args.count {
    let arg = args[idx]
    if arg == "--fit" {
        fitScreen = true
    } else if arg == "--speed", idx + 1 < args.count {
        if let s = Float(args[idx + 1]), s > 0 { speedVal = s }
        idx += 1
    } else if arg == "--pause-on-battery" {
        pauseBattery = true
    } else if arg == "--no-pause-on-low-power" {
        pauseLowPower = false
    } else if arg == "--interval", idx + 1 < args.count {
        if let sec = Double(args[idx + 1]), sec >= 0 { rotateInterval = sec }
        idx += 1
    } else if arg == "--shuffle" {
        shuffleActive = true
    } else if arg == "--playlist", idx + 1 < args.count {
        let pFile = URL(fileURLWithPath: args[idx + 1])
        if let data = try? Data(contentsOf: pFile),
           let paths = try? JSONDecoder().decode([String].self, from: data) {
            playlistItems = paths.map { URL(fileURLWithPath: $0) }.filter { FileManager.default.fileExists(atPath: $0.path) }
        }
        idx += 1
    } else if arg == "--open-picker" {
        openPickerAtLaunch = true
    } else if !arg.starts(with: "--") && targetPath == nil {
        targetPath = arg
    }
    idx += 1
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)

var resolvedURL: URL? = nil
if let path = targetPath {
    let u = URL(fileURLWithPath: path)
    var isDir: ObjCBool = false
    if FileManager.default.fileExists(atPath: u.path, isDirectory: &isDir) {
        if isDir.boolValue {
            let files = scanMediaFolder(dirURL: u)
            if !files.isEmpty {
                playlistItems = files
                resolvedURL = files[0]
                if rotateInterval == 0 {
                    rotateInterval = 900 // default 15m for folder
                }
            }
        } else {
            resolvedURL = u
        }
    }
}

if resolvedURL == nil && !playlistItems.isEmpty {
    resolvedURL = playlistItems[0]
}

// Fallback to recent wallpaper if none specified
if resolvedURL == nil {
    let home = FileManager.default.homeDirectoryForCurrentUser
    let recURL = home.appendingPathComponent(".macground_recents.json")
    if let data = try? Data(contentsOf: recURL),
       let list = try? JSONDecoder().decode([String].self, from: data) {
        for p in list {
            if FileManager.default.fileExists(atPath: p) {
                resolvedURL = URL(fileURLWithPath: p)
                break
            }
        }
    }
}

guard let activeURL = resolvedURL else {
    // If still no wallpaper found, launch the picker directly
    let dummyURL = URL(fileURLWithPath: "/tmp/dummy.mp4")
    let delegate = WallpaperAppDelegate(
        mediaURL: dummyURL,
        fillScreen: !fitScreen,
        speed: speedVal,
        pauseOnBattery: pauseBattery,
        pauseOnLowPower: pauseLowPower,
        playlist: [],
        interval: rotateInterval,
        shuffle: shuffleActive
    )
    app.delegate = delegate
    DispatchQueue.main.async {
        delegate.openWallpaperFilePicker()
    }
    app.run()
    exit(0)
}

let delegate = WallpaperAppDelegate(
    mediaURL: activeURL,
    fillScreen: !fitScreen,
    speed: speedVal,
    pauseOnBattery: pauseBattery,
    pauseOnLowPower: pauseLowPower,
    playlist: playlistItems,
    interval: rotateInterval,
    shuffle: shuffleActive
)
app.delegate = delegate
if openPickerAtLaunch {
    DispatchQueue.main.async {
        delegate.openWallpaperFilePicker()
    }
}
app.run()
"""

SWIFT_MEDIA_CONVERTER_SOURCE = r"""
import Foundation
import ImageIO
import UniformTypeIdentifiers
import AVFoundation
import CoreVideo
import CoreGraphics

let args = CommandLine.arguments
guard args.count >= 3 else {
    fputs("Usage: media_to_mov <input_file> <output.mov> [--ping-pong] [--crossfade <frames>]\n", stderr)
    exit(1)
}

let inputURL = URL(fileURLWithPath: args[1])
let outputURL = URL(fileURLWithPath: args[2])
let isPingPong = args.contains("--ping-pong")
var crossfadeFrames = 0
if let cfIdx = args.firstIndex(of: "--crossfade"), cfIdx + 1 < args.count {
    if let f = Int(args[cfIdx + 1]), f > 0 { crossfadeFrames = f }
}
let ext = inputURL.pathExtension.lowercased()

try? FileManager.default.removeItem(at: outputURL)

func makePixelBuffer(from image: CGImage, pool: CVPixelBufferPool?, width: Int, height: Int) -> CVPixelBuffer? {
    var pixelBuffer: CVPixelBuffer?
    if let pool = pool {
        CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, pool, &pixelBuffer)
    } else {
        let attrs: [CFString: Any] = [
            kCVPixelBufferCGImageCompatibilityKey: true,
            kCVPixelBufferCGBitmapContextCompatibilityKey: true
        ]
        CVPixelBufferCreate(
            kCFAllocatorDefault,
            width,
            height,
            kCVPixelFormatType_32ARGB,
            attrs as CFDictionary,
            &pixelBuffer
        )
    }
    guard let buffer = pixelBuffer else { return nil }

    CVPixelBufferLockBaseAddress(buffer, [])
    defer { CVPixelBufferUnlockBaseAddress(buffer, []) }

    guard let context = CGContext(
        data: CVPixelBufferGetBaseAddress(buffer),
        width: width,
        height: height,
        bitsPerComponent: 8,
        bytesPerRow: CVPixelBufferGetBytesPerRow(buffer),
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.noneSkipFirst.rawValue
    ) else {
        return nil
    }

    context.clear(CGRect(x: 0, y: 0, width: width, height: height))
    context.draw(image, in: CGRect(x: 0, y: 0, width: width, height: height))
    return buffer
}

func makeMOVWriter(outputURL: URL, width: Int, height: Int) -> (AVAssetWriter, AVAssetWriterInput, AVAssetWriterInputPixelBufferAdaptor)? {
    let candidateCodecs: [AVVideoCodecType] = [.hevc, .h264, .jpeg]
    let sourcePixelBufferAttributes: [String: Any] = [
        kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32ARGB),
        kCVPixelBufferWidthKey as String: width,
        kCVPixelBufferHeightKey as String: height
    ]

    let compressionProps: [String: Any] = [
        AVVideoAverageBitRateKey: 50_000_000,
        AVVideoExpectedSourceFrameRateKey: 60
    ]

    for codec in candidateCodecs {
        try? FileManager.default.removeItem(at: outputURL)
        guard let w = try? AVAssetWriter(outputURL: outputURL, fileType: .mov) else { continue }
        let settings: [String: Any] = [
            AVVideoCodecKey: codec,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height,
            AVVideoCompressionPropertiesKey: compressionProps
        ]
        let inp = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
        inp.expectsMediaDataInRealTime = false
        let adp = AVAssetWriterInputPixelBufferAdaptor(
            assetWriterInput: inp,
            sourcePixelBufferAttributes: sourcePixelBufferAttributes
        )
        if w.canAdd(inp) {
            w.add(inp)
            if w.startWriting() {
                w.startSession(atSourceTime: .zero)
                return (w, inp, adp)
            }
        }
    }

    for codec in candidateCodecs {
        try? FileManager.default.removeItem(at: outputURL)
        guard let w = try? AVAssetWriter(outputURL: outputURL, fileType: .mov) else { continue }
        let settings: [String: Any] = [
            AVVideoCodecKey: codec,
            AVVideoWidthKey: width,
            AVVideoHeightKey: height
        ]
        let inp = AVAssetWriterInput(mediaType: .video, outputSettings: settings)
        inp.expectsMediaDataInRealTime = false
        let adp = AVAssetWriterInputPixelBufferAdaptor(
            assetWriterInput: inp,
            sourcePixelBufferAttributes: sourcePixelBufferAttributes
        )
        if w.canAdd(inp) {
            w.add(inp)
            if w.startWriting() {
                w.startSession(atSourceTime: .zero)
                return (w, inp, adp)
            }
        }
    }
    return nil
}

func convertVideoTo4KMOV(videoURL: URL, outputURL: URL, pingPong: Bool, crossfade: Int) -> Bool {
    let asset = AVURLAsset(url: videoURL)
    guard let videoTrack = asset.tracks(withMediaType: .video).first else {
        fputs("Error: No video track found in \(videoURL.path)\n", stderr)
        return false
    }

    let naturalSize = videoTrack.naturalSize.applying(videoTrack.preferredTransform)
    let srcW = abs(naturalSize.width)
    let srcH = abs(naturalSize.height)
    let maxDim: CGFloat = 3840.0
    var targetW = Int(srcW)
    var targetH = Int(srcH)
    if srcW > maxDim || srcH > maxDim {
        let scale = min(maxDim / max(srcW, 1.0), maxDim / max(srcH, 1.0))
        targetW = Int(srcW * scale)
        targetH = Int(srcH * scale)
    }
    targetW = max(64, (targetW % 2 == 0) ? targetW : targetW + 1)
    targetH = max(64, (targetH % 2 == 0) ? targetH : targetH + 1)

    guard let (writer, writerInput, adaptor) = makeMOVWriter(outputURL: outputURL, width: targetW, height: targetH) else {
        fputs("Error: Could not initialize 4K MOV encoder.\n", stderr)
        return false
    }

    let srcFPS = Double(videoTrack.nominalFrameRate > 0 ? videoTrack.nominalFrameRate : 30.0)
    let targetFPS = 60.0
    let timescale: Int32 = 600
    let step = CMTime(value: 10, timescale: timescale)
    let dupPerFrame = max(1, Int(round(targetFPS / max(1.0, srcFPS))))
    let skipRatio = max(1.0, srcFPS / targetFPS)

    let durationSec = max(0.1, CMTimeGetSeconds(asset.duration))
    let repeatCount = max(1, Int(ceil(6.0 / min(durationSec, 7.2))))
    let maxTotalFrames = pingPong ? 480 : 360

    var currentTime = CMTime.zero
    var totalWritten = 0
    var lastBuffer: CVPixelBuffer?
    let colorSpace = CGColorSpaceCreateDeviceRGB()

    // Store frame buffers if pingPong is active (capped to avoid memory bloat)
    var frameCache: [CVPixelBuffer] = []

    for _ in 0..<repeatCount {
        if totalWritten >= maxTotalFrames { break }
        guard let reader = try? AVAssetReader(asset: asset) else { break }
        let readerSettings: [String: Any] = [
            kCVPixelBufferPixelFormatTypeKey as String: Int(kCVPixelFormatType_32ARGB),
            kCVPixelBufferWidthKey as String: targetW,
            kCVPixelBufferHeightKey as String: targetH
        ]
        let readerOutput = AVAssetReaderTrackOutput(track: videoTrack, outputSettings: readerSettings)
        readerOutput.alwaysCopiesSampleData = false
        guard reader.canAdd(readerOutput) else { break }
        reader.add(readerOutput)
        guard reader.startReading() else { break }

        var frameIdx = 0
        var nextTarget = 0.0

        while let sb = readerOutput.copyNextSampleBuffer() {
            defer { frameIdx += 1 }
            if Double(frameIdx) + 0.01 < nextTarget {
                continue
            }
            nextTarget += skipRatio

            autoreleasepool {
                guard let pb = CMSampleBufferGetImageBuffer(sb) else { return }
                lastBuffer = pb
                CVBufferRemoveAllAttachments(pb)

                if pingPong && frameCache.count < 180 {
                    frameCache.append(pb)
                }

                for _ in 0..<dupPerFrame {
                    if totalWritten >= maxTotalFrames { break }
                    while !writerInput.isReadyForMoreMediaData {
                        Thread.sleep(forTimeInterval: 0.001)
                    }
                    if adaptor.append(pb, withPresentationTime: currentTime) {
                        currentTime = CMTimeAdd(currentTime, step)
                        totalWritten += 1
                    }
                }
            }

            if totalWritten >= maxTotalFrames {
                reader.cancelReading()
                break
            }
        }

        // Append reversed frames for ping-pong bounce loop
        if pingPong && !frameCache.isEmpty {
            for pb in frameCache.reversed() {
                if totalWritten >= maxTotalFrames { break }
                while !writerInput.isReadyForMoreMediaData {
                    Thread.sleep(forTimeInterval: 0.001)
                }
                if adaptor.append(pb, withPresentationTime: currentTime) {
                    currentTime = CMTimeAdd(currentTime, step)
                    totalWritten += 1
                }
            }
        }
    }

    guard totalWritten > 0 else {
        writer.cancelWriting()
        return false
    }

    if let finalBuf = lastBuffer {
        while !writerInput.isReadyForMoreMediaData {
            Thread.sleep(forTimeInterval: 0.002)
        }
        adaptor.append(finalBuf, withPresentationTime: currentTime)
    }

    writerInput.markAsFinished()
    let semaphore = DispatchSemaphore(value: 0)
    writer.finishWriting {
        semaphore.signal()
    }
    semaphore.wait()

    fputs("[+] Encoded \(totalWritten) frames at \(targetW)x\(targetH) (4K Lock Screen pipeline)\n", stderr)
    return writer.status == .completed
}

func convertGIFToMOV(gifURL: URL, outputURL: URL, pingPong: Bool) -> Bool {
    guard let source = CGImageSourceCreateWithURL(gifURL as CFURL, nil) else {
        fputs("Error: Could not read GIF source at \(gifURL.path)\n", stderr)
        return false
    }

    let frameCount = CGImageSourceGetCount(source)
    guard frameCount > 0, let firstImage = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        fputs("Error: GIF contains no frames.\n", stderr)
        return false
    }

    let rawW = max(64, firstImage.width)
    let rawH = max(64, firstImage.height)
    let width = (rawW % 2 == 0) ? rawW : rawW + 1
    let height = (rawH % 2 == 0) ? rawH : rawH + 1

    guard let (writer, writerInput, adaptor) = makeMOVWriter(outputURL: outputURL, width: width, height: height) else {
        fputs("Error: Could not initialize any compatible video encoder for MOV output.\n", stderr)
        return false
    }

    func frameDuration(at index: Int, source: CGImageSource) -> Double {
        var delay = 0.06
        guard let properties = CGImageSourceCopyPropertiesAtIndex(source, index, nil) as? [CFString: Any],
              let gifProps = properties[kCGImagePropertyGIFDictionary] as? [CFString: Any] else {
            return delay
        }
        if let unclamped = gifProps[kCGImagePropertyGIFUnclampedDelayTime] as? Double, unclamped > 0.01 {
            delay = unclamped
        } else if let clamped = gifProps[kCGImagePropertyGIFDelayTime] as? Double, clamped > 0.01 {
            delay = clamped
        }
        return delay
    }

    var singleLoopDuration = 0.0
    for i in 0..<frameCount {
        singleLoopDuration += frameDuration(at: i, source: source)
    }
    let repeatCount = max(1, Int(ceil(6.0 / max(singleLoopDuration, 0.05))))

    let timescale: Int32 = 600
    var currentTime = CMTime.zero

    for _ in 0..<repeatCount {
        for i in 0..<frameCount {
            guard let cgImage = CGImageSourceCreateImageAtIndex(source, i, nil) else { continue }
            while !writerInput.isReadyForMoreMediaData {
                Thread.sleep(forTimeInterval: 0.005)
            }
            if let buffer = makePixelBuffer(from: cgImage, pool: adaptor.pixelBufferPool, width: width, height: height) {
                adaptor.append(buffer, withPresentationTime: currentTime)
            }
            let delay = frameDuration(at: i, source: source)
            let step = CMTime(seconds: delay, preferredTimescale: timescale)
            currentTime = CMTimeAdd(currentTime, step)
        }

        if pingPong && frameCount > 2 {
            for i in stride(from: frameCount - 2, through: 1, by: -1) {
                guard let cgImage = CGImageSourceCreateImageAtIndex(source, i, nil) else { continue }
                while !writerInput.isReadyForMoreMediaData {
                    Thread.sleep(forTimeInterval: 0.005)
                }
                if let buffer = makePixelBuffer(from: cgImage, pool: adaptor.pixelBufferPool, width: width, height: height) {
                    adaptor.append(buffer, withPresentationTime: currentTime)
                }
                let delay = frameDuration(at: i, source: source)
                let step = CMTime(seconds: delay, preferredTimescale: timescale)
                currentTime = CMTimeAdd(currentTime, step)
            }
        }
    }

    if let lastImage = CGImageSourceCreateImageAtIndex(source, frameCount - 1, nil) {
        while !writerInput.isReadyForMoreMediaData {
            Thread.sleep(forTimeInterval: 0.005)
        }
        if let buffer = makePixelBuffer(from: lastImage, pool: adaptor.pixelBufferPool, width: width, height: height) {
            adaptor.append(buffer, withPresentationTime: currentTime)
        }
    }

    writerInput.markAsFinished()
    let semaphore = DispatchSemaphore(value: 0)
    writer.finishWriting {
        semaphore.signal()
    }
    semaphore.wait()

    return writer.status == .completed
}

if ext == "gif" {
    if convertGIFToMOV(gifURL: inputURL, outputURL: outputURL, pingPong: isPingPong) {
        exit(0)
    }
    exit(1)
} else {
    fputs("[*] Encoding '\(inputURL.lastPathComponent)' at native 4K resolution (up to 3840x2160) for Lock Screen...\n", stderr)
    if convertVideoTo4KMOV(videoURL: inputURL, outputURL: outputURL, pingPong: isPingPong, crossfade: crossfadeFrames) {
        exit(0)
    }
    exit(1)
}
"""

AERIAL_ROOT = Path("/Library/Application Support/com.apple.idleassetsd")
AERIAL_SQLITE = AERIAL_ROOT / "Aerial.sqlite"
AERIAL_BASE_DIR = AERIAL_ROOT / "Customer"
AERIAL_VIDEO_DIR = AERIAL_BASE_DIR / "4KSDR240FPS"
AERIAL_ENTRIES_JSON = AERIAL_BASE_DIR / "entries.json"

MODERN_AERIALS_ROOT = Path("/Library/Application Support/com.apple.wallpaper/aerials")
MODERN_AERIALS_VIDEOS_DIR = MODERN_AERIALS_ROOT / "videos"
MODERN_AERIALS_4K_DIR = MODERN_AERIALS_ROOT / "4KSDR240FPS"

LAUNCH_DAEMON_PLIST = Path("/Library/LaunchDaemons/com.macground.lockreset.plist")
OLD_LAUNCH_DAEMON_PLIST = Path("/Library/LaunchDaemons/com.backimate.lockreset.plist")
UNLOCK_TRIGGER_FILE = Path("/tmp/.macground_unlock_trigger")
LOCKSCREEN_REQUEST_FILE = Path("/tmp/.macground_lockscreen_request")
OLD_UNLOCK_TRIGGER_FILE = Path("/tmp/.backimate_unlock_trigger")

HARDCODED_GOLDEN_GATE_ID = "4207734D-74FE-4F92-B5E1-6EC8DEE24A15"

PRIMARY_DEFAULT_ID = "4C108785-A7BA-422E-9C79-B0129F1D5550"  # Tahoe Day
BUILTIN_SLOTS_TO_POPULATE = [
    "4C108785-A7BA-422E-9C79-B0129F1D5550",  # Tahoe Day
    "B2FC91ED-6891-4DEB-85A1-268B2B4160B6",  # Tahoe Morning
    "F88CDF4A-9681-4D1F-88FE-34F1A3C6A62B",  # Sequoia Morning
    "6D6834A4-2F0F-479A-B053-7D4DC5CB8EB7",  # Sequoia Sunrise
    "FE876489-CBD5-479B-A8F0-1B67F0741CEA",  # Sonoma Horizon
    "DAD82DCE-F3AE-4AEC-8A79-1694D412FC0A",  # Yosemite (Aerial.sqlite #1)
    "E5799A24-1949-4E66-A17B-B5EB05F28C5D",  # Yosemite Representative
    "86E89C23-C39B-44C8-A985-E56EEA6456FE",  # Golden Gate Night (non-hardcoded)
]

CACHE_DIR = Path(__file__).resolve().parent / ".wallpaper_cache"


def compile_swift_helper(name: str, source: str, frameworks: list[str]) -> Path:
    """Compiles a Swift helper binary into CACHE_DIR if not already cached."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    binary_path = CACHE_DIR / name
    source_path = CACHE_DIR / f"{name}.swift"

    needs_compile = True
    if binary_path.exists() and source_path.exists():
        if source_path.read_text(encoding="utf-8") == source:
            needs_compile = False

    if needs_compile:
        print(f"[*] Compiling native macOS helper ({name})...")
        source_path.write_text(source, encoding="utf-8")
        cmd = ["xcrun", "swiftc", "-O", str(source_path), "-o", str(binary_path)]
        for fw in frameworks:
            cmd.extend(["-framework", fw])
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"[!] Error compiling {name}:\n{result.stderr}", file=sys.stderr)
            sys.exit(1)
        os.chmod(binary_path, 0o755)
        print(f"[+] Compiled {name} to {binary_path}")

    return binary_path


def ensure_compiled_binary() -> Path:
    """Compiles the lightweight Swift/AVFoundation desktop player if not cached."""
    return compile_swift_helper(
        "desktop_video_player",
        SWIFT_DESKTOP_PLAYER_SOURCE,
        ["Cocoa", "AVFoundation", "UniformTypeIdentifiers", "IOKit"],
    )


def ensure_media_converter_binary() -> Path:
    """Compiles the native Swift ImageIO/AVFoundation media-to-MOV converter if not cached."""
    return compile_swift_helper(
        "media_to_mov",
        SWIFT_MEDIA_CONVERTER_SOURCE,
        ["Foundation", "ImageIO", "UniformTypeIdentifiers", "AVFoundation", "CoreVideo", "CoreGraphics"],
    )


def parse_interval_string(val: str) -> float:
    """Parses a time interval like '30s', '5m', '15m', '1h' or raw seconds into float seconds."""
    v = val.strip().lower()
    if v.endswith("s"):
        return float(v[:-1])
    if v.endswith("m"):
        return float(v[:-1]) * 60.0
    if v.endswith("h"):
        return float(v[:-1]) * 3600.0
    return float(v)


def scan_media_paths(target: Path) -> list[Path]:
    """Scans a file or directory for valid video and GIF media."""
    valid_exts = {".mp4", ".mov", ".m4v", ".gif"}
    if target.is_file():
        if target.suffix.lower() in valid_exts:
            return [target.resolve()]
        return []
    if target.is_dir():
        found = []
        for p in sorted(target.iterdir()):
            if not p.name.startswith(".") and p.suffix.lower() in valid_exts:
                found.append(p.resolve())
        return found
    return []


def build_app_icon_icns(output_path: Path):
    """Generates a native Apple ICNS icon with a modern rounded-rect dark gradient & SF Symbol."""
    swift_icon_gen = """
import Cocoa

let img = NSImage(size: NSSize(width: 512, height: 512))
img.lockFocus()
let rect = NSRect(x: 0, y: 0, width: 512, height: 512)
let path = NSBezierPath(roundedRect: rect, xRadius: 110, yRadius: 110)

let gradient = NSGradient(colors: [
    NSColor(calibratedRed: 0.10, green: 0.12, blue: 0.20, alpha: 1.0),
    NSColor(calibratedRed: 0.16, green: 0.28, blue: 0.52, alpha: 1.0)
])
gradient?.draw(in: path, angle: 45)

if let symbol = NSImage(systemSymbolName: "play.desktopcomputer", accessibilityDescription: nil) {
    let symRect = NSRect(x: 106, y: 106, width: 300, height: 300)
    symbol.draw(in: symRect, from: .zero, operation: .sourceOver, fraction: 1.0)
}
img.unlockFocus()

if let tiff = img.tiffRepresentation,
   let rep = NSBitmapImageRep(data: tiff),
   let png = rep.representation(using: .png, properties: [:]) {
    try? png.write(to: URL(fileURLWithPath: "/tmp/macground_app_icon_512.png"))
}
"""
    subprocess.run(["swift", "-e", swift_icon_gen], check=True, capture_output=True)
    temp_png = Path("/tmp/macground_app_icon_512.png")
    if temp_png.exists():
        png_data = temp_png.read_bytes()
        temp_png.unlink(missing_ok=True)
        block_len = 8 + len(png_data)
        block = b"ic09" + struct.pack(">I", block_len) + png_data
        total_len = 8 + len(block)
        header = b"icns" + struct.pack(">I", total_len)
        output_path.write_bytes(header + block)


def build_macos_app_bundle(output_dir: Path | None = None) -> Path:
    """
    Packages Macground into a standalone, double-clickable macOS application bundle (.app).
    Features LSUIElement=true so it runs cleanly in the macOS menu bar without cluttering the Dock.
    """
    dest_dir = output_dir or Path(__file__).resolve().parent
    app_path = dest_dir / "Macground.app"
    contents_dir = app_path / "Contents"
    macos_dir = contents_dir / "MacOS"
    resources_dir = contents_dir / "Resources"

    if app_path.exists():
        shutil.rmtree(app_path)

    macos_dir.mkdir(parents=True, exist_ok=True)
    resources_dir.mkdir(parents=True, exist_ok=True)

    # 1. Compile native helpers
    desktop_bin = ensure_compiled_binary()
    converter_bin = ensure_media_converter_binary()

    shutil.copy2(desktop_bin, resources_dir / "desktop_video_player")
    shutil.copy2(converter_bin, resources_dir / "media_to_mov")
    shutil.copy2(Path(__file__).resolve(), resources_dir / "Macground.py")

    # 2. Generate native icon
    icon_path = resources_dir / "AppIcon.icns"
    build_app_icon_icns(icon_path)

    # 3. Create Info.plist
    info_plist = {
        "CFBundleName": "Macground",
        "CFBundleDisplayName": "Macground",
        "CFBundleIdentifier": "com.macground.app",
        "CFBundleVersion": "2.0.0",
        "CFBundleShortVersionString": "2.0.0",
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "Macground",
        "CFBundleIconFile": "AppIcon",
        "LSMinimumSystemVersion": "12.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
    }
    with (contents_dir / "Info.plist").open("wb") as f:
        plistlib.dump(info_plist, f)

    (contents_dir / "PkgInfo").write_bytes(b"APPL????")

    # 4. Create executable launcher script
    launcher_path = macos_dir / "Macground"
    launcher_script = f"""#!/bin/sh
DIR="$(cd "$(dirname "$0")/../Resources" && pwd)"
export PATH="/usr/bin:/bin:/usr/sbin:/sbin:$PATH"
cd "$DIR"
exec /usr/bin/python3 "$DIR/Macground.py" --mode desktop "$@"
"""
    launcher_path.write_text(launcher_script, encoding="utf-8")
    os.chmod(launcher_path, 0o755)

    print(f"[+] Created standalone macOS app: {app_path}")
    return app_path


def install_root_watchpaths_daemon():
    """
    Installs a root `WatchPaths` LaunchDaemon (`/Library/LaunchDaemons/com.macground.lockreset.plist`)
    that watches `/tmp/.macground_unlock_trigger` and resets Lock Screen video daemons as root on unlock.
    """
    if os.geteuid() != 0:
        return
    try:
        if OLD_LAUNCH_DAEMON_PLIST.exists():
            subprocess.run(["launchctl", "bootout", "system", str(OLD_LAUNCH_DAEMON_PLIST)], stderr=subprocess.DEVNULL)
            OLD_LAUNCH_DAEMON_PLIST.unlink(missing_ok=True)
        OLD_UNLOCK_TRIGGER_FILE.unlink(missing_ok=True)

        UNLOCK_TRIGGER_FILE.write_text("init", encoding="utf-8")
        os.chmod(UNLOCK_TRIGGER_FILE, 0o666)

        script_path = Path(__file__).resolve()
        shell_cmd = (
            'if [ -s "' + str(LOCKSCREEN_REQUEST_FILE) + '" ]; then '
            'REQ=$(cat "' + str(LOCKSCREEN_REQUEST_FILE) + '" 2>/dev/null); '
            'rm -f "' + str(LOCKSCREEN_REQUEST_FILE) + '"; '
            'if [ -n "$REQ" ] && [ -f "$REQ" ]; then '
            '/usr/bin/python3 "' + str(script_path) + '" "$REQ" --mode lockscreen >> /tmp/macground_lockscreen.log 2>&1; '
            'fi; '
            'fi; '
            '/usr/bin/killall -9 WallpaperAerialsExtension WallpaperVideoExtension idleassetsd WallpaperAgent 2>/dev/null || true'
        )
        plist_content = {
            "Label": "com.macground.lockreset",
            "ProgramArguments": ["/bin/sh", "-c", shell_cmd],
            "WatchPaths": [str(UNLOCK_TRIGGER_FILE), str(LOCKSCREEN_REQUEST_FILE)],
        }
        with LAUNCH_DAEMON_PLIST.open("wb") as f:
            plistlib.dump(plist_content, f)
        os.chmod(LAUNCH_DAEMON_PLIST, 0o644)
        os.chown(LAUNCH_DAEMON_PLIST, 0, 0)

        subprocess.run(["launchctl", "bootout", "system", str(LAUNCH_DAEMON_PLIST)], stderr=subprocess.DEVNULL)
        subprocess.run(["launchctl", "bootstrap", "system", str(LAUNCH_DAEMON_PLIST)], stderr=subprocess.DEVNULL)
        print("[+] Installed WatchPaths Unlock Reset Daemon (resets Lock Screen video daemons as root on every unlock)")
    except Exception as exc:
        print(f"[!] Warning: Could not install Lock Screen Reset Daemon ({exc})")


def prepare_lockscreen_mov(media_path: Path, ping_pong: bool = False, crossfade: int = 0) -> Path:
    """
    Packages `media_path` into a Lock Screen `.mov` at native 4K resolution (up to 3840x2160).
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    sudo_uid = os.environ.get("SUDO_UID")
    sudo_gid = os.environ.get("SUDO_GID")
    if os.geteuid() == 0 and sudo_uid and sudo_gid:
        try:
            os.chown(CACHE_DIR, int(sudo_uid), int(sudo_gid))
        except Exception:
            pass

    stat = media_path.stat()
    tag = f"pp_{ping_pong}_cf_{crossfade}"
    fingerprint = hashlib.sha256(
        f"{media_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}:{tag}:via_4k_60fps_v9".encode("utf-8")
    ).hexdigest()[:12]
    converted_mov = CACHE_DIR / f"{media_path.stem}_{fingerprint}.mov"

    if converted_mov.exists() and converted_mov.stat().st_size > 0:
        print(f"[*] Using cached 4K Lock Screen .mov: {converted_mov.name}")
        return converted_mov

    converter_bin = ensure_media_converter_binary()
    print(f"[*] Encoding '{media_path.name}' at native 4K (up to 3840x2160) for Lock Screen...")

    cmd = []
    sudo_user = os.environ.get("SUDO_USER")
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        cmd.extend(["sudo", "-u", sudo_user])

    cmd.extend([
        str(converter_bin),
        str(media_path.resolve()),
        str(converted_mov),
    ])
    if ping_pong:
        cmd.append("--ping-pong")
    if crossfade > 0:
        cmd.extend(["--crossfade", str(crossfade)])

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stderr:
        for line in result.stderr.strip().splitlines():
            if line.startswith("["):
                print(f"    {line}")
    if result.returncode != 0 or not converted_mov.exists():
        print(f"[!] Failed to convert media to Lock Screen MOV:\n{result.stderr}", file=sys.stderr)
        sys.exit(1)

    print(f"[+] Prepared 4K Lock Screen .mov: {converted_mov.name}")
    return converted_mov


def stop_existing_desktop_player():
    """Stops any running desktop_video_player instances."""
    subprocess.run(["pkill", "-f", "desktop_video_player"], stderr=subprocess.DEVNULL)


def run_desktop_mode(
    video_path: Path,
    fit: bool = False,
    foreground: bool = False,
    speed: float = 1.0,
    pause_on_battery: bool = False,
    pause_on_low_power: bool = True,
    interval: float = 0.0,
    shuffle: bool = False,
    ping_pong: bool = False,
):
    """Launches the desktop video/GIF wallpaper behind desktop icons."""
    binary_path = ensure_compiled_binary()
    stop_existing_desktop_player()

    target_media = video_path
    if ping_pong and video_path.is_file():
        target_media = prepare_lockscreen_mov(video_path, ping_pong=True)

    sudo_user = os.environ.get("SUDO_USER")
    cmd = []
    if os.geteuid() == 0 and sudo_user and sudo_user != "root":
        cmd.extend(["sudo", "-u", sudo_user])

    cmd.extend([str(binary_path), str(target_media.resolve())])
    cmd.extend(["--script-path", str(Path(__file__).resolve())])
    if fit:
        cmd.append("--fit")
    if speed != 1.0:
        cmd.extend(["--speed", str(speed)])
    if pause_on_battery:
        cmd.append("--pause-on-battery")
    if not pause_on_low_power:
        cmd.append("--no-pause-on-low-power")
    if interval > 0:
        cmd.extend(["--interval", str(interval)])
    if shuffle:
        cmd.append("--shuffle")

    if foreground:
        print(f"[*] Playing '{video_path.name}' on Desktop background (Ctrl+C to stop)...")
        try:
            subprocess.run(cmd)
        except KeyboardInterrupt:
            print("\n[*] Stopped desktop wallpaper.")
    else:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print(f"[+] Desktop wallpaper ('{video_path.name}') started in background (PID: {proc.pid}).")
        print(f"    To stop it later, run: python3 {Path(__file__).name} --stop")


def get_target_user_home() -> Path:
    """Returns the real user's home directory even when running under `sudo`."""
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        return Path(f"/Users/{sudo_user}")
    return Path.home()


def configure_startup_launch_agent(
    video_path: Path | None,
    fit: bool = False,
    speed: float = 1.0,
    pause_on_battery: bool = False,
    pause_on_low_power: bool = True,
    interval: float = 0.0,
    shuffle: bool = False,
    enable: bool = True,
):
    """
    Configures or removes ~/Library/LaunchAgents/com.macground.desktop.plist
    to automatically start the Desktop video/GIF wallpaper upon user login.
    """
    user_home = get_target_user_home()
    launch_agents_dir = user_home / "Library" / "LaunchAgents"
    plist_path = launch_agents_dir / "com.macground.desktop.plist"

    sudo_user = os.environ.get("SUDO_USER")
    is_sudo = os.geteuid() == 0 and sudo_user and sudo_user != "root"

    def run_as_user(args_list):
        if is_sudo:
            return subprocess.run(["sudo", "-u", sudo_user] + args_list, capture_output=True, text=True)
        return subprocess.run(args_list, capture_output=True, text=True)

    if plist_path.exists():
        run_as_user(["launchctl", "unload", str(plist_path)])

    if not enable:
        if plist_path.exists():
            plist_path.unlink(missing_ok=True)
            print("[+] Disabled launch-at-login (removed com.macground.desktop.plist).")
        else:
            print("[*] Launch-at-login is already disabled.")
        return

    if not video_path:
        print("[!] Error: A video or GIF path is required to enable launch-at-login.", file=sys.stderr)
        sys.exit(1)

    binary_path = ensure_compiled_binary()
    launch_agents_dir.mkdir(parents=True, exist_ok=True)

    prog_args = [str(binary_path.resolve()), str(video_path.resolve())]
    prog_args.extend(["--script-path", str(Path(__file__).resolve())])
    if fit:
        prog_args.append("--fit")
    if speed != 1.0:
        prog_args.extend(["--speed", str(speed)])
    if pause_on_battery:
        prog_args.append("--pause-on-battery")
    if not pause_on_low_power:
        prog_args.append("--no-pause-on-low-power")
    if interval > 0:
        prog_args.extend(["--interval", str(interval)])
    if shuffle:
        prog_args.append("--shuffle")

    plist_data = {
        "Label": "com.macground.desktop",
        "ProgramArguments": prog_args,
        "RunAtLoad": True,
        "KeepAlive": False,
        "ProcessType": "Interactive",
    }

    with plist_path.open("wb") as f:
        plistlib.dump(plist_data, f)

    if is_sudo:
        sudo_uid = int(os.environ.get("SUDO_UID", 501))
        sudo_gid = int(os.environ.get("SUDO_GID", 20))
        try:
            os.chown(plist_path, sudo_uid, sudo_gid)
        except Exception:
            pass

    run_as_user(["launchctl", "load", "-w", str(plist_path)])
    print(f"[+] Enabled launch-at-login: '{video_path.name}' will start automatically at login.")
    print(f"    Installed LaunchAgent: {plist_path}")


def list_downloaded_aerials() -> list[tuple[str, Path]]:
    """Finds downloaded macOS Aerial wallpaper `.mov` files across both legacy and modern paths."""
    found = []
    seen = set()
    user_home = get_target_user_home()
    user_aerials_dir = user_home / "Library" / "Application Support" / "com.apple.wallpaper" / "aerials"
    search_dirs = [
        AERIAL_VIDEO_DIR,
        MODERN_AERIALS_VIDEOS_DIR,
        MODERN_AERIALS_4K_DIR,
        MODERN_AERIALS_ROOT,
        user_aerials_dir / "videos",
        user_aerials_dir / "4KSDR240FPS",
        user_aerials_dir,
    ]
    for d in search_dirs:
        if d.exists():
            for v in sorted(d.glob("*.mov"), key=lambda p: p.stat().st_mtime, reverse=True):
                if v.name not in seen:
                    seen.add(v.name)
                    found.append(("Installed Aerial Slot", v))
    return found


def build_aerial_wallpaper_choice_node(asset_id: str) -> dict:
    """Constructs the Index.plist dictionary structure for Apple's Aerial wallpaper engine."""
    config_plist = plistlib.dumps({"assetID": asset_id}, fmt=plistlib.FMT_BINARY)
    now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    return {
        "Content": {
            "Choices": [
                {
                    "Configuration": config_plist,
                    "Files": [],
                    "Provider": "com.apple.wallpaper.choice.aerials",
                }
            ],
            "EncodedOptionValues": "$null",
            "Shuffle": "$null",
        },
        "LastSet": now,
        "LastUse": now,
    }


def activate_aerial_in_user_index_plist(asset_id: str) -> list[str]:
    """Updates ~/Library/Application Support/com.apple.wallpaper/Store/Index.plist."""
    user_home = get_target_user_home()
    store_dir = user_home / "Library" / "Application Support" / "com.apple.wallpaper" / "Store"
    index_plist = store_dir / "Index.plist"

    discovered_ids: list[str] = []
    if not index_plist.exists():
        return discovered_ids

    backup_plist = store_dir / "Index.plist.bak"
    if not backup_plist.exists():
        shutil.copy2(index_plist, backup_plist)

    def scan_and_patch_choices(obj):
        if isinstance(obj, dict):
            if "Choices" in obj and isinstance(obj["Choices"], list):
                for choice in obj["Choices"]:
                    if isinstance(choice, dict):
                        cfg_bytes = choice.get("Configuration")
                        if isinstance(cfg_bytes, (bytes, bytearray)) and len(cfg_bytes) > 0:
                            try:
                                cfg = plistlib.loads(cfg_bytes)
                                if isinstance(cfg, dict) and "assetID" in cfg:
                                    aid = str(cfg["assetID"]).strip()
                                    if aid and aid != HARDCODED_GOLDEN_GATE_ID and aid not in discovered_ids:
                                        discovered_ids.append(aid)
                            except Exception:
                                pass
                        choice["Provider"] = "com.apple.wallpaper.choice.aerials"
                        choice["Configuration"] = plistlib.dumps({"assetID": asset_id}, fmt=plistlib.FMT_BINARY)
                        choice["Files"] = []
            for k, v in list(obj.items()):
                if k == " EncodedOptionValues":
                    del obj[k]
                    obj["EncodedOptionValues"] = "$null"
                else:
                    scan_and_patch_choices(v)
        elif isinstance(obj, list):
            for item in obj:
                scan_and_patch_choices(item)

    try:
        with index_plist.open("rb") as f:
            plist_data = plistlib.load(f)

        scan_and_patch_choices(plist_data)
        choice_node = build_aerial_wallpaper_choice_node(asset_id)

        for top_key in ("AllSpacesAndDisplays", "SystemDefault"):
            if top_key in plist_data and isinstance(plist_data[top_key], dict):
                plist_data[top_key]["Desktop"] = choice_node
                plist_data[top_key]["Idle"] = choice_node
                plist_data[top_key]["Type"] = "individual"
            else:
                plist_data[top_key] = {
                    "Desktop": choice_node,
                    "Idle": choice_node,
                    "Type": "individual",
                }

        if "Spaces" in plist_data and isinstance(plist_data["Spaces"], dict):
            for space_uuid, space_dict in plist_data["Spaces"].items():
                if isinstance(space_dict, dict):
                    if "Default" in space_dict and isinstance(space_dict["Default"], dict):
                        space_dict["Default"]["Desktop"] = choice_node
                        space_dict["Default"]["Idle"] = choice_node
                        space_dict["Default"]["Type"] = "individual"
                    if "Displays" in space_dict and isinstance(space_dict["Displays"], dict):
                        for disp_id, disp_dict in space_dict["Displays"].items():
                            if isinstance(disp_dict, dict):
                                disp_dict["Desktop"] = choice_node
                                disp_dict["Idle"] = choice_node
                                disp_dict["Type"] = "individual"

        with index_plist.open("wb") as f:
            plistlib.dump(plist_data, f, fmt=plistlib.FMT_BINARY)

        sudo_uid = os.environ.get("SUDO_UID")
        sudo_gid = os.environ.get("SUDO_GID")
        if sudo_uid and sudo_gid:
            os.chown(index_plist, int(sudo_uid), int(sudo_gid))

        print(f"[+] Updated Index.plist to use non-hardcoded Aerial slot: {asset_id} (Tahoe Day)")
    except Exception as exc:
        print(f"[!] Warning: Could not update Index.plist ({exc}).")

    return discovered_ids


def mark_aerials_downloaded_in_sqlite(asset_ids: list[str]):
    """Marks target asset_ids downloaded in Aerial.sqlite."""
    if not AERIAL_SQLITE.exists():
        return
    try:
        coredata_now = (datetime.datetime.now(datetime.timezone.utc).timestamp()) - 978307200.0
        conn = sqlite3.connect(str(AERIAL_SQLITE))
        with conn:
            for aid in asset_ids:
                conn.execute(
                    "UPDATE ZASSET SET ZLASTDOWNLOADED = ?, ZPOINTSOFINTEREST = NULL WHERE ZIDENTIFIER = ?",
                    (coredata_now, aid),
                )
        conn.close()
        print(f"[+] Updated {len(asset_ids)} Aerial slots in Aerial.sqlite (cleared seek checkpoints)")
    except Exception as exc:
        print(f"[!] Warning: Could not update Aerial.sqlite ({exc})")


def install_mov_to_destination(src_mov: Path, dst_mov: Path, uid: int = 0, gid: int = 80, is_first: bool = False):
    """Installs src_mov to dst_mov via hardlinks after initial placement."""
    dst_mov.parent.mkdir(parents=True, exist_ok=True)
    if dst_mov.exists():
        backup_file = dst_mov.with_suffix(".mov.bak")
        if not backup_file.exists():
            try:
                shutil.copy2(dst_mov, backup_file)
            except Exception:
                pass
        dst_mov.unlink(missing_ok=True)

    if not is_first:
        try:
            os.link(src_mov, dst_mov)
            return
        except Exception:
            pass

    shutil.copy2(src_mov, dst_mov)
    os.chmod(dst_mov, 0o644)
    try:
        os.chown(dst_mov, uid, gid)
    except Exception:
        pass
    subprocess.run(["xattr", "-c", str(dst_mov)], stderr=subprocess.DEVNULL)


def apply_lockscreen_aerial(video_path: Path, target_id: str | None = None, ping_pong: bool = False, crossfade: int = 0):
    """Encodes any video or GIF into a native 4K Lock Screen .mov and installs it."""
    playable_mov = prepare_lockscreen_mov(video_path, ping_pong=ping_pong, crossfade=crossfade)

    if target_id:
        clean_target = target_id[:-4] if target_id.endswith(".mov") else target_id
        if clean_target.upper() == HARDCODED_GOLDEN_GATE_ID:
            primary_id = PRIMARY_DEFAULT_ID
        else:
            primary_id = clean_target
    else:
        primary_id = PRIMARY_DEFAULT_ID

    if os.geteuid() != 0:
        discovered_ids = activate_aerial_in_user_index_plist(primary_id)
        target_ids = []
        for aid in [primary_id] + BUILTIN_SLOTS_TO_POPULATE + discovered_ids:
            if aid and aid not in target_ids:
                target_ids.append(aid)

        user_home = get_target_user_home()
        user_app_support_aerials = user_home / "Library" / "Application Support" / "com.apple.wallpaper" / "aerials"
        user_container_aerials = user_home / "Library" / "Containers" / "com.apple.wallpaper.extension.aerials" / "Data" / "Library" / "Application Support" / "com.apple.wallpaper" / "aerials"
        user_dirs = [user_app_support_aerials, user_app_support_aerials / "videos", user_app_support_aerials / "4KSDR240FPS"]
        if user_container_aerials.parent.exists():
            user_dirs.extend([user_container_aerials, user_container_aerials / "videos", user_container_aerials / "4KSDR240FPS"])

        master_u_mov: Path | None = None
        for d in user_dirs:
            try:
                for aid in target_ids:
                    dst = d / f"{aid}.mov"
                    if master_u_mov is None:
                        install_mov_to_destination(playable_mov, dst, uid=os.getuid(), gid=os.getgid(), is_first=True)
                        master_u_mov = dst
                    else:
                        install_mov_to_destination(master_u_mov, dst, uid=os.getuid(), gid=os.getgid(), is_first=False)
            except Exception:
                pass

        try:
            LOCKSCREEN_REQUEST_FILE.write_text(str(video_path.resolve()), encoding="utf-8")
            os.chmod(LOCKSCREEN_REQUEST_FILE, 0o666)
        except Exception:
            pass

        for daemon in ["WallpaperAerialsExtension", "WallpaperVideoExtension", "WallpaperAgent", "NeptuneOneWallpaper"]:
            subprocess.run(["killall", "-9", daemon], stderr=subprocess.DEVNULL)

        if sys.stdin.isatty():
            print("[*] Elevating privileges with sudo to install Lock Screen wallpaper system-wide...")
            cmd = ["sudo", sys.executable, str(Path(__file__).resolve()), str(playable_mov), "--mode", "lockscreen"]
            if target_id:
                cmd.extend(["--target-id", target_id])
            if ping_pong:
                cmd.append("--ping-pong")
            res = subprocess.run(cmd)
            sys.exit(res.returncode)
        else:
            return

    if target_id:
        clean_target = target_id[:-4] if target_id.endswith(".mov") else target_id
        if clean_target.upper() == HARDCODED_GOLDEN_GATE_ID:
            print(f"[!] Note: {HARDCODED_GOLDEN_GATE_ID} (Golden Gate Sunset) is hardcoded by macOS to play from the read-only system volume.")
            print(f"    Switching automatically to {PRIMARY_DEFAULT_ID} (Tahoe Day) so your custom video loads!")
            primary_id = PRIMARY_DEFAULT_ID
        else:
            primary_id = clean_target
    else:
        primary_id = PRIMARY_DEFAULT_ID

    for daemon in [
        "WallpaperAerialsExtension",
        "WallpaperVideoExtension",
        "WallpaperAgent",
        "idleassetsd",
        "NeptuneOneWallpaper",
    ]:
        subprocess.run(["killall", "-9", daemon], stderr=subprocess.DEVNULL)

    discovered_ids = activate_aerial_in_user_index_plist(primary_id)

    target_ids = []
    for aid in [primary_id] + BUILTIN_SLOTS_TO_POPULATE + discovered_ids:
        if aid and aid not in target_ids:
            target_ids.append(aid)

    for _, existing_path in list_downloaded_aerials():
        stem = existing_path.stem
        if stem not in target_ids and stem != HARDCODED_GOLDEN_GATE_ID:
            target_ids.append(stem)

    user_home = get_target_user_home()
    sudo_uid = int(os.environ.get("SUDO_UID", 0))
    sudo_gid = int(os.environ.get("SUDO_GID", 80))

    user_app_support_aerials = (
        user_home / "Library" / "Application Support" / "com.apple.wallpaper" / "aerials"
    )
    user_container_aerials = (
        user_home
        / "Library"
        / "Containers"
        / "com.apple.wallpaper.extension.aerials"
        / "Data"
        / "Library"
        / "Application Support"
        / "com.apple.wallpaper"
        / "aerials"
    )

    system_dirs = [
        AERIAL_VIDEO_DIR,
        MODERN_AERIALS_ROOT,
        MODERN_AERIALS_VIDEOS_DIR,
        MODERN_AERIALS_4K_DIR,
    ]
    user_dirs = [
        user_app_support_aerials,
        user_app_support_aerials / "videos",
        user_app_support_aerials / "4KSDR240FPS",
    ]
    if user_container_aerials.parent.exists():
        user_dirs.extend([
            user_container_aerials,
            user_container_aerials / "videos",
            user_container_aerials / "4KSDR240FPS",
        ])

    installed_count = 0
    master_system_mov: Path | None = None
    for d in system_dirs:
        try:
            for aid in target_ids:
                dst = d / f"{aid}.mov"
                if master_system_mov is None:
                    install_mov_to_destination(playable_mov, dst, uid=0, gid=80, is_first=True)
                    master_system_mov = dst
                else:
                    install_mov_to_destination(master_system_mov, dst, uid=0, gid=80, is_first=False)
                installed_count += 1
        except Exception:
            pass

    master_user_mov: Path | None = None
    for d in user_dirs:
        try:
            for aid in target_ids:
                dst = d / f"{aid}.mov"
                if master_user_mov is None:
                    install_mov_to_destination(playable_mov, dst, uid=sudo_uid, gid=sudo_gid, is_first=True)
                    master_user_mov = dst
                else:
                    install_mov_to_destination(master_user_mov, dst, uid=sudo_uid, gid=sudo_gid, is_first=False)
                installed_count += 1
        except Exception:
            pass

    print(f"[+] Installed 4K Lock Screen .mov into {len(target_ids)} Aerial slots ({installed_count} hardlinked paths).")

    mark_aerials_downloaded_in_sqlite(target_ids)
    install_root_watchpaths_daemon()

    for daemon in [
        "cfprefsd",
        "WallpaperAerialsExtension",
        "WallpaperVideoExtension",
        "WallpaperAgent",
        "idleassetsd",
        "NeptuneOneWallpaper",
    ]:
        subprocess.run(["killall", "-9", daemon], stderr=subprocess.DEVNULL)

    print("\n[+] Success! Your video is now running at native 4K resolution on both Desktop and Lock Screen!")


def restore_lockscreen_aerials():
    """Restores any backed up .mov.bak Aerial videos and Index.plist.bak."""
    if os.geteuid() != 0:
        subprocess.run(["sudo", sys.executable, str(Path(__file__).resolve()), "--restore-lockscreen"])
        return

    if LAUNCH_DAEMON_PLIST.exists():
        subprocess.run(["launchctl", "bootout", "system", str(LAUNCH_DAEMON_PLIST)], stderr=subprocess.DEVNULL)
        LAUNCH_DAEMON_PLIST.unlink(missing_ok=True)
    if OLD_LAUNCH_DAEMON_PLIST.exists():
        subprocess.run(["launchctl", "bootout", "system", str(OLD_LAUNCH_DAEMON_PLIST)], stderr=subprocess.DEVNULL)
        OLD_LAUNCH_DAEMON_PLIST.unlink(missing_ok=True)
    UNLOCK_TRIGGER_FILE.unlink(missing_ok=True)
    OLD_UNLOCK_TRIGGER_FILE.unlink(missing_ok=True)

    user_home = get_target_user_home()
    user_app_support_aerials = (
        user_home / "Library" / "Application Support" / "com.apple.wallpaper" / "aerials"
    )
    user_container_aerials = (
        user_home
        / "Library"
        / "Containers"
        / "com.apple.wallpaper.extension.aerials"
        / "Data"
        / "Library"
        / "Application Support"
        / "com.apple.wallpaper"
        / "aerials"
    )

    candidate_dirs = [
        AERIAL_VIDEO_DIR,
        MODERN_AERIALS_ROOT,
        MODERN_AERIALS_VIDEOS_DIR,
        MODERN_AERIALS_4K_DIR,
        user_app_support_aerials,
        user_app_support_aerials / "videos",
        user_app_support_aerials / "4KSDR240FPS",
        user_container_aerials,
        user_container_aerials / "videos",
        user_container_aerials / "4KSDR240FPS",
    ]

    for daemon in [
        "WallpaperAerialsExtension",
        "WallpaperVideoExtension",
        "WallpaperAgent",
        "idleassetsd",
        "NeptuneOneWallpaper",
    ]:
        subprocess.run(["killall", "-9", daemon], stderr=subprocess.DEVNULL)

    for d in candidate_dirs:
        if not d.exists():
            continue
        for bak in d.glob("*.mov.bak"):
            orig = bak.with_suffix("")
            print(f"[*] Restoring {orig} from backup...")
            shutil.move(str(bak), str(orig))
            os.chmod(orig, 0o644)
        for slot_id in BUILTIN_SLOTS_TO_POPULATE + [HARDCODED_GOLDEN_GATE_ID]:
            bootstrapped = d / f"{slot_id}.mov"
            if bootstrapped.exists() and not bootstrapped.with_suffix(".mov.bak").exists():
                bootstrapped.unlink(missing_ok=True)

    store_dir = user_home / "Library" / "Application Support" / "com.apple.wallpaper" / "Store"
    backup_plist = store_dir / "Index.plist.bak"
    index_plist = store_dir / "Index.plist"
    if backup_plist.exists():
        print("[*] Restoring original Index.plist wallpaper configuration...")
        shutil.move(str(backup_plist), str(index_plist))

    for daemon in [
        "cfprefsd",
        "WallpaperAerialsExtension",
        "WallpaperVideoExtension",
        "WallpaperAgent",
        "idleassetsd",
        "NeptuneOneWallpaper",
    ]:
        subprocess.run(["killall", "-9", daemon], stderr=subprocess.DEVNULL)

    print("[+] Original Lock Screen settings restored.")


def main():
    parser = argparse.ArgumentParser(
        description="Macground: High-performance 4K video & animated GIF wallpapers for macOS Desktop & Lock Screen."
    )
    parser.add_argument(
        "video",
        nargs="?",
        help="Path to a video file, animated GIF, or a folder of wallpapers.",
    )
    parser.add_argument(
        "--mode",
        choices=["both", "desktop", "lockscreen"],
        default="both",
        help=(
            "'both' sets both the live Desktop background and the Lock Screen wallpaper (default). "
            "'desktop' runs only the zero-sudo live Desktop layer behind icons. "
            "'lockscreen' installs into macOS's native Lock Screen Aerial engine (uses sudo)."
        ),
    )
    parser.add_argument(
        "--target-id",
        help="Optional specific Aerial UUID to replace for Lock Screen mode.",
    )
    parser.add_argument(
        "--fit",
        action="store_true",
        help="Fit video/GIF inside screen bounds instead of filling/cropping (desktop mode).",
    )
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="Keep desktop player attached to the terminal instead of running in the background.",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=1.0,
        help="Playback speed multiplier (e.g. 0.5 for half speed, 0.75 for cinematic slow-mo).",
    )
    parser.add_argument(
        "--pause-on-battery",
        action="store_true",
        help="Automatically pause video playback when running on MacBook battery power.",
    )
    parser.add_argument(
        "--no-pause-on-low-power",
        action="store_true",
        help="Do not pause video playback when macOS Low Power Mode is enabled (paused by default).",
    )
    parser.add_argument(
        "--interval",
        help="Auto-rotation interval when providing a folder or playlist (e.g. '1m', '5m', '15m', '30m', '1h', or seconds).",
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="Randomize wallpaper rotation order.",
    )
    parser.add_argument(
        "--ping-pong",
        action="store_true",
        help="Create a seamless bounce loop (play forward, then reverse back to start).",
    )
    parser.add_argument(
        "--crossfade",
        type=int,
        default=0,
        help="Number of frames to crossfade at the loop boundary for seamless looping.",
    )
    parser.add_argument(
        "--build-app",
        action="store_true",
        help="Package Macground into a standalone, native macOS .app bundle.",
    )
    parser.add_argument(
        "--install-app",
        action="store_true",
        help="Build and install Macground.app into /Applications.",
    )
    parser.add_argument(
        "--startup",
        action="store_true",
        help="Automatically launch this wallpaper on user login via ~/Library/LaunchAgents.",
    )
    parser.add_argument(
        "--no-startup",
        action="store_true",
        help="Disable auto-start at login and remove ~/Library/LaunchAgents/com.macground.desktop.plist.",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Stop the running desktop wallpaper background process.",
    )
    parser.add_argument(
        "--list-lockscreen-slots",
        action="store_true",
        help="List downloaded macOS Aerial wallpapers available for Lock Screen replacement.",
    )
    parser.add_argument(
        "--restore-lockscreen",
        action="store_true",
        help="Restore original Apple Lock Screen Aerial videos and settings from backup (uses sudo).",
    )
    parser.add_argument(
        "--open-picker",
        "--open-picker-if-empty",
        dest="open_picker",
        action="store_true",
        help="Open native file picker to choose wallpaper or folder.",
    )

    args = parser.parse_args()

    if args.build_app or args.install_app:
        dest = Path("/Applications") if args.install_app else None
        app_path = build_macos_app_bundle(output_dir=dest)
        print(f"[+] Successfully built native macOS bundle: {app_path}")
        return

    if args.no_startup:
        configure_startup_launch_agent(None, enable=False)
        if not args.video:
            return

    if args.stop:
        stop_existing_desktop_player()
        print("[+] Stopped desktop video/GIF wallpaper.")
        return

    if args.restore_lockscreen:
        restore_lockscreen_aerials()
        return

    if args.list_lockscreen_slots:
        slots = list_downloaded_aerials()
        if not slots:
            print("No custom/downloaded Aerial wallpapers found yet.")
        else:
            print("Installed Aerial Wallpaper Slots:")
            for label, path in slots:
                print(f"  - {label}: {path.name} ({path})")
        return

    interval_sec = 0.0
    if args.interval:
        try:
            interval_sec = parse_interval_string(args.interval)
        except Exception:
            print(f"[!] Warning: Invalid interval '{args.interval}', using manual rotation.")

    target_path = None
    if args.video:
        target_path = Path(args.video).expanduser().resolve()
        if not target_path.exists():
            print(f"[!] Error: Path not found: {target_path}", file=sys.stderr)
            sys.exit(1)
    else:
        # Check recents first
        recents_file = Path.home() / ".macground_recents.json"
        if recents_file.exists():
            try:
                recents = json.loads(recents_file.read_text(encoding="utf-8"))
                for r in recents:
                    p = Path(r)
                    if p.exists():
                        target_path = p
                        break
            except Exception:
                pass

        # If no recents, check current and script folder for any media
        if not target_path:
            for sdir in [Path.cwd(), Path(__file__).resolve().parent, Path("/Users/mac/Desktop/test")]:
                if sdir.exists():
                    found_media = scan_media_paths(sdir)
                    if found_media:
                        target_path = found_media[0]
                        break

        # If still no media or open_picker requested, open native picker directly
        if not target_path or args.open_picker:
            binary_path = ensure_compiled_binary()
            stop_existing_desktop_player()
            cmd = [str(binary_path)]
            if target_path:
                cmd.append(str(target_path.resolve()))
            cmd.append("--open-picker")
            if args.fit:
                cmd.append("--fit")
            if args.speed != 1.0:
                cmd.extend(["--speed", str(args.speed)])
            if args.pause_on_battery:
                cmd.append("--pause-on-battery")
            if not args.no_pause_on_low_power:
                cmd.append("--no-pause-on-low-power")
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
            print("[+] Opened native wallpaper file picker.")
            return

    items = scan_media_paths(target_path)
    if not items:
        print(f"[!] Error: No supported media files (.mp4, .mov, .m4v, .gif) found in {target_path}", file=sys.stderr)
        sys.exit(1)

    first_video = items[0]
    if args.shuffle:
        random.shuffle(items)
        first_video = items[0]

    if args.startup:
        configure_startup_launch_agent(
            target_path,
            fit=args.fit,
            speed=args.speed,
            pause_on_battery=args.pause_on_battery,
            pause_on_low_power=not args.no_pause_on_low_power,
            interval=interval_sec,
            shuffle=args.shuffle,
            enable=True,
        )

    if args.mode in ("desktop", "both"):
        run_desktop_mode(
            target_path,
            fit=args.fit,
            foreground=args.foreground,
            speed=args.speed,
            pause_on_battery=args.pause_on_battery,
            pause_on_low_power=not args.no_pause_on_low_power,
            interval=interval_sec,
            shuffle=args.shuffle,
            ping_pong=args.ping_pong,
        )

    if args.mode in ("lockscreen", "both"):
        apply_lockscreen_aerial(
            first_video,
            target_id=args.target_id,
            ping_pong=args.ping_pong,
            crossfade=args.crossfade,
        )


if __name__ == "__main__":
    main()
