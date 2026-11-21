import Cocoa
import WebKit

final class LoginWindowDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var pollTimer: Timer?
    private var captured = false
    private let resetSession: Bool

    init(resetSession: Bool) {
        self.resetSession = resetSession
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        let config = WKWebViewConfiguration()
        config.websiteDataStore = resetSession ? .nonPersistent() : .default()

        let frame = NSRect(x: 0, y: 0, width: 520, height: 700)
        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Вход в Anycubic Cloud — OrcaCubic"
        window.center()
        window.delegate = self

        webView = WKWebView(frame: frame, configuration: config)
        webView.navigationDelegate = self
        webView.autoresizingMask = [.width, .height]
        window.contentView = webView

        if let url = URL(string: "https://cloud-universe.anycubic.com/") {
            webView.load(URLRequest(url: url))
        }

        NSApp.setActivationPolicy(.regular)
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.7, repeats: true) { [weak self] _ in
            self?.checkForToken()
        }
    }

    private func checkForToken() {
        guard !captured else { return }
        guard let host = webView.url?.host, host.hasSuffix("anycubic.com") else { return }

        let js = "(function(){ try { return window.localStorage.getItem('XX-Token') || ''; } catch(e) { return ''; } })()"
        webView.evaluateJavaScript(js) { [weak self] result, _ in
            guard let self = self, !self.captured else { return }
            if let token = result as? String {
                let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
                if trimmed.count >= 20 {
                    self.captured = true
                    self.pollTimer?.invalidate()
                    FileHandle.standardOutput.write(Data("TOKEN:\(trimmed)\n".utf8))
                    NSApp.terminate(nil)
                }
            }
        }
    }

    func windowWillClose(_ notification: Notification) {
        if !captured {
            FileHandle.standardError.write(Data("CANCELLED\n".utf8))
            exit(2)
        }
    }
}

if CommandLine.arguments.contains("--choose-folder") {
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    app.activate(ignoringOtherApps: true)
    let panel = NSOpenPanel()
    panel.title = "Выберите папку для сохранения таймлапсов"
    panel.canChooseFiles = false
    panel.canChooseDirectories = true
    panel.canCreateDirectories = true
    panel.allowsMultipleSelection = false
    if panel.runModal() == .OK, let url = panel.url {
        FileHandle.standardOutput.write(Data("DIR:\(url.path)\n".utf8))
        exit(0)
    } else {
        exit(2)
    }
}

let reset = CommandLine.arguments.contains("--reset-session")
let app = NSApplication.shared
let delegate = LoginWindowDelegate(resetSession: reset)
app.delegate = delegate
app.run()
