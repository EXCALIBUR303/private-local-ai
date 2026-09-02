import AppKit
import Carbon.HIToolbox
import WebKit

final class AppDelegate: NSObject, NSApplicationDelegate, WKUIDelegate, WKNavigationDelegate {
    private let server = ServerController()
    private var window: NSWindow!
    private var webView: WKWebView!
    private var statusItem: NSStatusItem!
    private var hotKey: HotKey?
    private var splash: NSTextField!
    private var loaded = false

    // MARK: - launch

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMenu()
        buildWindow()
        buildStatusItem()

        // ⌥Space from anywhere: the summon gesture. Push-to-talk still
        // requires holding the microphone — nothing listens on its own.
        hotKey = HotKey(keyCode: UInt32(kVK_Space), modifiers: UInt32(optionKey)) { [weak self] in
            self?.toggleWindow()
        }

        server.onState = { [weak self] state in
            DispatchQueue.main.async { self?.render(state) }
        }
        server.start()
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false            // stay resident in the menu bar
    }

    func applicationWillTerminate(_ notification: Notification) {
        server.stop()
    }

    // MARK: - window

    private func buildWindow() {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = .default()
        // The page talks only to 127.0.0.1; the server's own CSP does the
        // rest of the confinement.
        config.preferences.setValue(true, forKey: "developerExtrasEnabled")

        webView = WKWebView(frame: .zero, configuration: config)
        webView.uiDelegate = self
        webView.navigationDelegate = self
        webView.setValue(false, forKey: "drawsBackground")

        window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1180, height: 780),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "Private Local AI"
        window.titlebarAppearsTransparent = true
        window.titleVisibility = .hidden
        window.isMovableByWindowBackground = true
        window.minSize = NSSize(width: 720, height: 520)
        window.backgroundColor = NSColor(calibratedRed: 0.047, green: 0.043, blue: 0.055, alpha: 1)
        window.center()
        window.setFrameAutosaveName("PrivateLocalAIMain")
        window.isReleasedWhenClosed = false

        let container = NSView(frame: window.contentLayoutRect)
        container.autoresizingMask = [.width, .height]

        webView.frame = container.bounds
        webView.autoresizingMask = [.width, .height]
        webView.isHidden = true
        container.addSubview(webView)

        splash = NSTextField(labelWithString: "Starting the local model runtime…")
        splash.font = .systemFont(ofSize: 13, weight: .regular)
        splash.textColor = NSColor(calibratedRed: 0.94, green: 0.63, blue: 0.24, alpha: 1)
        splash.alignment = .center
        splash.maximumNumberOfLines = 8
        splash.frame = NSRect(x: 40, y: container.bounds.midY - 40,
                              width: container.bounds.width - 80, height: 80)
        splash.autoresizingMask = [.width, .minYMargin, .maxYMargin]
        container.addSubview(splash)

        window.contentView = container
    }

    private func render(_ state: ServerController.State) {
        switch state {
        case .idle:
            splash.stringValue = "Idle."
        case .preparing(let message):
            splash.stringValue = message
        case .starting:
            splash.stringValue = "Starting the local server…"
        case .ready:
            guard !loaded else { return }
            loaded = true
            splash.stringValue = "Loading…"
            webView.load(URLRequest(url: server.url))
        case .failed(let message):
            splash.stringValue = "Could not start.\n\n\(message)"
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        webView.isHidden = false
        splash.isHidden = true
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!, withError error: Error) {
        splash.isHidden = false
        splash.stringValue = "Failed to load: \(error.localizedDescription)"
    }

    /// Grant microphone access to the local page. The user has already
    /// approved microphone access for this app at the macOS level; this
    /// avoids a second, confusing in-page prompt for a local origin.
    func webView(_ webView: WKWebView,
                 requestMediaCapturePermissionFor origin: WKSecurityOrigin,
                 initiatedByFrame frame: WKFrameInfo,
                 type: WKMediaCaptureType,
                 decisionHandler: @escaping (WKPermissionDecision) -> Void) {
        let isLocal = origin.host == "127.0.0.1" || origin.host == "localhost"
        decisionHandler(isLocal && type == .microphone ? .grant : .deny)
    }

    // MARK: - menu bar

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        if let button = statusItem.button {
            button.image = Self.statusIcon()
            button.image?.isTemplate = true
            button.toolTip = "Private Local AI — ⌥Space"
        }

        let menu = NSMenu()
        menu.addItem(withTitle: "Show Private Local AI\t⌥Space",
                     action: #selector(showWindow), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "New Thread", action: #selector(newThread), keyEquivalent: "")
            .target = self
        menu.addItem(withTitle: "Open Data Folder", action: #selector(openDataFolder),
                     keyEquivalent: "").target = self
        menu.addItem(withTitle: "Open Server Log", action: #selector(openLog),
                     keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit", action: #selector(quit), keyEquivalent: "q").target = self
        statusItem.menu = menu
    }

    /// Drawn rather than shipped as an asset, so the bundle stays self-contained.
    private static func statusIcon() -> NSImage {
        let size = NSSize(width: 18, height: 18)
        let image = NSImage(size: size, flipped: false) { rect in
            let c = NSPoint(x: rect.midX, y: rect.midY)
            NSColor.black.setStroke()
            for (radius, start, extent) in [(7.0, 20.0, 200.0), (4.6, 200.0, 170.0)] {
                let path = NSBezierPath()
                path.appendArc(withCenter: c, radius: radius,
                               startAngle: start, endAngle: start + extent)
                path.lineWidth = 1.5
                path.lineCapStyle = .round
                path.stroke()
            }
            NSColor.black.setFill()
            NSBezierPath(ovalIn: NSRect(x: c.x - 1.7, y: c.y - 1.7, width: 3.4, height: 3.4)).fill()
            return true
        }
        return image
    }

    private func buildMenu() {
        let main = NSMenu()
        let appItem = NSMenuItem()
        let appMenu = NSMenu()
        appMenu.addItem(withTitle: "About Private Local AI",
                        action: #selector(NSApplication.orderFrontStandardAboutPanel(_:)),
                        keyEquivalent: "")
        appMenu.addItem(.separator())
        appMenu.addItem(withTitle: "Hide", action: #selector(NSApplication.hide(_:)), keyEquivalent: "h")
        appMenu.addItem(withTitle: "Quit", action: #selector(quit), keyEquivalent: "q").target = self
        appItem.submenu = appMenu
        main.addItem(appItem)

        let editItem = NSMenuItem()
        let edit = NSMenu(title: "Edit")
        edit.addItem(withTitle: "Undo", action: Selector(("undo:")), keyEquivalent: "z")
        edit.addItem(withTitle: "Redo", action: Selector(("redo:")), keyEquivalent: "Z")
        edit.addItem(.separator())
        edit.addItem(withTitle: "Cut", action: #selector(NSText.cut(_:)), keyEquivalent: "x")
        edit.addItem(withTitle: "Copy", action: #selector(NSText.copy(_:)), keyEquivalent: "c")
        edit.addItem(withTitle: "Paste", action: #selector(NSText.paste(_:)), keyEquivalent: "v")
        edit.addItem(withTitle: "Select All", action: #selector(NSText.selectAll(_:)), keyEquivalent: "a")
        editItem.submenu = edit
        main.addItem(editItem)

        let viewItem = NSMenuItem()
        let view = NSMenu(title: "View")
        view.addItem(withTitle: "Reload", action: #selector(reload), keyEquivalent: "r").target = self
        view.addItem(withTitle: "Toggle Full Screen",
                     action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        viewItem.submenu = view
        main.addItem(viewItem)

        NSApp.mainMenu = main
    }

    // MARK: - actions

    @objc private func showWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func toggleWindow() {
        if window.isVisible && NSApp.isActive {
            window.orderOut(nil)
        } else {
            showWindow()
            // Land in the composer so the summon gesture is immediately useful.
            webView.evaluateJavaScript("document.getElementById('input')?.focus()")
        }
    }

    @objc private func newThread() {
        showWindow()
        webView.evaluateJavaScript("window.dispatchEvent(new CustomEvent('pai:new-thread'))")
    }

    @objc private func reload() { webView.reload() }

    @objc private func openDataFolder() {
        NSWorkspace.shared.open(ServerController.dataDir)
    }

    @objc private func openLog() {
        NSWorkspace.shared.open(ServerController.logFile)
    }

    @objc private func quit() {
        server.stop()
        NSApp.terminate(nil)
    }
}
