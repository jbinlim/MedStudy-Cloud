import AppKit
import Foundation
import UniformTypeIdentifiers
import WebKit

private let projectPath = "/Users/jaebin/Projects/MedStudy"
private let serverURL = URL(string: "http://127.0.0.1:8504")!
private let healthURL = URL(string: "http://127.0.0.1:8504/_stcore/health")!

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, WKNavigationDelegate, WKUIDelegate, WKDownloadDelegate {
    private var window: NSWindow!
    private var webView: WKWebView!
    private var loadingView: NSView!
    private var statusLabel: NSTextField!
    private var serverProcess: Process?
    private var ownsServer = false
    private var isTerminating = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        if let iconURL = Bundle.main.url(
            forResource: "MedStudyIcon",
            withExtension: "icns"
        ), let iconImage = NSImage(contentsOf: iconURL) {
            NSApp.applicationIconImage = iconImage
        }
        createMainMenu()
        createWindow()
        NSApp.activate(ignoringOtherApps: true)
        prepareServer()
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }

    func applicationWillTerminate(_ notification: Notification) {
        isTerminating = true
        stopOwnedServer()
    }

    func windowWillClose(_ notification: Notification) {
        NSApp.terminate(nil)
    }

    private func createMainMenu() {
        let mainMenu = NSMenu()

        let appMenuItem = NSMenuItem()
        let appMenu = NSMenu(title: "MedStudy")
        appMenu.addItem(
            withTitle: "MedStudy 종료",
            action: #selector(NSApplication.terminate(_:)),
            keyEquivalent: "q"
        )
        appMenuItem.submenu = appMenu
        mainMenu.addItem(appMenuItem)

        // WKWebView의 텍스트 편집 명령은 macOS responder chain을 통해
        // 전달된다. 표준 편집 메뉴가 있어야 Cmd+C/V/A 등이 정상 작동한다.
        let editMenuItem = NSMenuItem()
        let editMenu = NSMenu(title: "편집")
        editMenu.addItem(
            withTitle: "실행 취소",
            action: Selector(("undo:")),
            keyEquivalent: "z"
        )
        let redoItem = NSMenuItem(
            title: "다시 실행",
            action: Selector(("redo:")),
            keyEquivalent: "Z"
        )
        redoItem.keyEquivalentModifierMask = [.command, .shift]
        editMenu.addItem(redoItem)
        editMenu.addItem(.separator())
        editMenu.addItem(
            withTitle: "잘라내기",
            action: #selector(NSText.cut(_:)),
            keyEquivalent: "x"
        )
        editMenu.addItem(
            withTitle: "복사",
            action: #selector(NSText.copy(_:)),
            keyEquivalent: "c"
        )
        editMenu.addItem(
            withTitle: "붙여넣기",
            action: #selector(NSText.paste(_:)),
            keyEquivalent: "v"
        )
        editMenu.addItem(.separator())
        editMenu.addItem(
            withTitle: "전체 선택",
            action: #selector(NSText.selectAll(_:)),
            keyEquivalent: "a"
        )
        editMenuItem.submenu = editMenu
        mainMenu.addItem(editMenuItem)

        let viewMenuItem = NSMenuItem()
        let viewMenu = NSMenu(title: "보기")
        let reloadItem = NSMenuItem(
            title: "새로고침",
            action: #selector(reloadWebView(_:)),
            keyEquivalent: "r"
        )
        reloadItem.keyEquivalentModifierMask = [.command]
        reloadItem.target = self
        viewMenu.addItem(reloadItem)
        viewMenu.addItem(.separator())
        let fullScreenItem = NSMenuItem(
            title: "전체 화면 시작/종료",
            action: #selector(NSWindow.toggleFullScreen(_:)),
            keyEquivalent: "f"
        )
        fullScreenItem.keyEquivalentModifierMask = [.control, .command]
        viewMenu.addItem(fullScreenItem)
        viewMenuItem.submenu = viewMenu
        mainMenu.addItem(viewMenuItem)

        let windowMenuItem = NSMenuItem()
        let windowMenu = NSMenu(title: "윈도우")
        windowMenu.addItem(
            withTitle: "최소화",
            action: #selector(NSWindow.performMiniaturize(_:)),
            keyEquivalent: "m"
        )
        windowMenu.addItem(
            withTitle: "윈도우 닫기",
            action: #selector(NSWindow.performClose(_:)),
            keyEquivalent: "w"
        )
        windowMenuItem.submenu = windowMenu
        mainMenu.addItem(windowMenuItem)
        NSApp.windowsMenu = windowMenu

        NSApp.mainMenu = mainMenu
    }

    @objc private func reloadWebView(_ sender: Any?) {
        webView?.reload()
    }

    private func createWindow() {
        let frame = NSRect(x: 0, y: 0, width: 1360, height: 880)
        window = NSWindow(
            contentRect: frame,
            styleMask: [.titled, .closable, .miniaturizable, .resizable],
            backing: .buffered,
            defer: false
        )
        window.title = "MedStudy"
        window.minSize = NSSize(width: 980, height: 680)
        window.collectionBehavior.insert(.fullScreenPrimary)
        window.center()
        window.delegate = self
        window.isReleasedWhenClosed = false

        let configuration = WKWebViewConfiguration()
        configuration.websiteDataStore = .default()
        webView = WKWebView(frame: frame, configuration: configuration)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        webView.autoresizingMask = [.width, .height]

        loadingView = NSView(frame: frame)
        loadingView.wantsLayer = true
        loadingView.layer?.backgroundColor = NSColor.windowBackgroundColor.cgColor
        loadingView.autoresizingMask = [.width, .height]

        let titleLabel = NSTextField(labelWithString: "MedStudy")
        titleLabel.font = NSFont.systemFont(ofSize: 34, weight: .semibold)
        titleLabel.alignment = .center
        titleLabel.translatesAutoresizingMaskIntoConstraints = false

        statusLabel = NSTextField(labelWithString: "학습 자료를 준비하고 있습니다…")
        statusLabel.font = NSFont.systemFont(ofSize: 15)
        statusLabel.textColor = .secondaryLabelColor
        statusLabel.alignment = .center
        statusLabel.translatesAutoresizingMaskIntoConstraints = false

        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .regular
        spinner.translatesAutoresizingMaskIntoConstraints = false
        spinner.startAnimation(nil)

        loadingView.addSubview(titleLabel)
        loadingView.addSubview(statusLabel)
        loadingView.addSubview(spinner)

        NSLayoutConstraint.activate([
            titleLabel.centerXAnchor.constraint(equalTo: loadingView.centerXAnchor),
            titleLabel.centerYAnchor.constraint(equalTo: loadingView.centerYAnchor, constant: -44),
            spinner.centerXAnchor.constraint(equalTo: loadingView.centerXAnchor),
            spinner.topAnchor.constraint(equalTo: titleLabel.bottomAnchor, constant: 24),
            statusLabel.centerXAnchor.constraint(equalTo: loadingView.centerXAnchor),
            statusLabel.topAnchor.constraint(equalTo: spinner.bottomAnchor, constant: 18),
        ])

        let container = NSView(frame: frame)
        container.addSubview(webView)
        container.addSubview(loadingView)
        window.contentView = container
        window.makeKeyAndOrderFront(nil)
    }

    private func prepareServer() {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }

            if self.isServerHealthy() {
                self.loadApp()
                return
            }

            do {
                try self.startServer()
            } catch {
                self.showError("MedStudy 서버를 시작하지 못했습니다.\n\n\(error.localizedDescription)")
                return
            }

            for _ in 0..<120 {
                if self.isTerminating { return }
                if self.isServerHealthy() {
                    self.loadApp()
                    return
                }
                Thread.sleep(forTimeInterval: 0.25)
            }

            self.showError(
                "MedStudy 준비 시간이 너무 오래 걸립니다.\n"
                + "~/Library/Logs/MedStudy/streamlit.log를 확인해주세요."
            )
        }
    }

    private func startServer() throws {
        let fileManager = FileManager.default
        let projectURL = URL(fileURLWithPath: projectPath, isDirectory: true)
        let executableURL = projectURL.appendingPathComponent(".venv/bin/streamlit")
        let appURL = projectURL.appendingPathComponent("app.py")

        guard fileManager.isExecutableFile(atPath: executableURL.path) else {
            throw NSError(
                domain: "MedStudy",
                code: 1,
                userInfo: [NSLocalizedDescriptionKey: "Python 가상환경의 Streamlit을 찾지 못했습니다."]
            )
        }

        let logDirectory = fileManager.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Logs/MedStudy", isDirectory: true)
        try fileManager.createDirectory(
            at: logDirectory,
            withIntermediateDirectories: true
        )
        let logURL = logDirectory.appendingPathComponent("streamlit.log")
        if !fileManager.fileExists(atPath: logURL.path) {
            fileManager.createFile(atPath: logURL.path, contents: nil)
        }
        let logHandle = try FileHandle(forWritingTo: logURL)
        try logHandle.seekToEnd()

        let process = Process()
        process.executableURL = executableURL
        process.currentDirectoryURL = projectURL
        process.arguments = [
            "run", appURL.path,
            "--server.address", "0.0.0.0",
            "--server.port", "8504",
            "--server.headless", "true",
            "--browser.gatherUsageStats", "false",
        ]

        var environment = ProcessInfo.processInfo.environment
        let commonPaths = [
            projectURL.appendingPathComponent(".venv/bin").path,
            "/opt/homebrew/bin",
            "/usr/local/bin",
            "/usr/bin",
            "/bin",
            "/usr/sbin",
            "/sbin",
        ]
        environment["PATH"] = commonPaths.joined(separator: ":")
        environment["PYTHONUNBUFFERED"] = "1"
        // 이 앱이 강제 종료(Force Quit)되면 `applicationWillTerminate`가
        // 전혀 불리지 않아 `stopOwnedServer()`도 실행되지 않는다. 그러면
        // streamlit 프로세스가 고아로 남아 포트 8504를 계속 붙잡는다.
        // app.py가 이 PID를 직접 감시하다가 우리가 사라지면 스스로
        // 종료하도록, 우리 PID를 넘겨준다.
        environment["MEDSTUDY_PARENT_PID"] = String(ProcessInfo.processInfo.processIdentifier)
        process.environment = environment
        process.standardOutput = logHandle
        process.standardError = logHandle
        process.terminationHandler = { [weak self] _ in
            guard let self, !self.isTerminating else { return }
            if !self.isServerHealthy() {
                self.showError(
                    "MedStudy 서버가 예기치 않게 종료되었습니다.\n"
                    + "~/Library/Logs/MedStudy/streamlit.log를 확인해주세요."
                )
            }
        }

        try process.run()
        serverProcess = process
        ownsServer = true
    }

    private func isServerHealthy() -> Bool {
        let semaphore = DispatchSemaphore(value: 0)
        var healthy = false
        var request = URLRequest(url: healthURL)
        request.timeoutInterval = 1.0

        URLSession.shared.dataTask(with: request) { data, response, _ in
            if let response = response as? HTTPURLResponse,
               response.statusCode == 200,
               let data,
               String(data: data, encoding: .utf8)?.trimmingCharacters(in: .whitespacesAndNewlines) == "ok" {
                healthy = true
            }
            semaphore.signal()
        }.resume()

        _ = semaphore.wait(timeout: .now() + 1.2)
        return healthy
    }

    private func loadApp() {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.webView.load(URLRequest(url: serverURL))
        }
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        loadingView.isHidden = true
    }

    func webView(
        _ webView: WKWebView,
        runOpenPanelWith parameters: WKOpenPanelParameters,
        initiatedByFrame frame: WKFrameInfo,
        completionHandler: @escaping ([URL]?) -> Void
    ) {
        let openPanel = NSOpenPanel()
        openPanel.title = "학습 자료 선택"
        openPanel.prompt = "선택"
        openPanel.canChooseFiles = true
        openPanel.canChooseDirectories = parameters.allowsDirectories
        openPanel.allowsMultipleSelection = parameters.allowsMultipleSelection
        openPanel.beginSheetModal(for: window) { result in
            completionHandler(result == .OK ? openPanel.urls : nil)
        }
    }

    func webView(
        _ webView: WKWebView,
        decidePolicyFor navigationAction: WKNavigationAction,
        decisionHandler: @escaping (WKNavigationActionPolicy) -> Void
    ) {
        if navigationAction.shouldPerformDownload {
            decisionHandler(.download)
        } else {
            decisionHandler(.allow)
        }
    }

    func webView(
        _ webView: WKWebView,
        navigationAction: WKNavigationAction,
        didBecome download: WKDownload
    ) {
        download.delegate = self
    }

    func webView(
        _ webView: WKWebView,
        navigationResponse: WKNavigationResponse,
        didBecome download: WKDownload
    ) {
        download.delegate = self
    }

    func download(
        _ download: WKDownload,
        decideDestinationUsing response: URLResponse,
        suggestedFilename: String,
        completionHandler: @escaping (URL?) -> Void
    ) {
        DispatchQueue.main.async { [weak self] in
            guard let self else {
                completionHandler(nil)
                return
            }

            let savePanel = NSSavePanel()
            savePanel.title = "PDF 저장"
            savePanel.prompt = "저장"
            savePanel.allowedContentTypes = [.pdf]
            savePanel.isExtensionHidden = false
            savePanel.nameFieldStringValue = suggestedFilename.lowercased().hasSuffix(".pdf")
                ? suggestedFilename
                : suggestedFilename + ".pdf"
            savePanel.canCreateDirectories = true
            savePanel.beginSheetModal(for: self.window) { result in
                completionHandler(result == .OK ? savePanel.url : nil)
            }
        }
    }

    func download(
        _ download: WKDownload,
        didFailWithError error: Error,
        resumeData: Data?
    ) {
        showError("파일을 다운로드하지 못했습니다.\n\n\(error.localizedDescription)")
    }

    func webView(
        _ webView: WKWebView,
        didFail navigation: WKNavigation!,
        withError error: Error
    ) {
        showError("MedStudy 화면을 불러오지 못했습니다.\n\n\(error.localizedDescription)")
    }

    private func showError(_ message: String) {
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            self.statusLabel.stringValue = message
            self.statusLabel.textColor = .systemRed
        }
    }

    private func stopOwnedServer() {
        guard ownsServer, let process = serverProcess, process.isRunning else { return }
        process.terminate()
        process.waitUntilExit()
    }
}

let application = NSApplication.shared
let delegate = AppDelegate()
application.delegate = delegate
application.setActivationPolicy(.regular)
application.run()
