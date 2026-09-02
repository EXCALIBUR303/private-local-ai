import Foundation

/// Owns the local Python server process.
///
/// The server is launched from the copy of the application bundled inside
/// Resources, with its data directory pointed at Application Support, so the
/// installed app does not depend on the folder it was built from.
final class ServerController {
    enum State: Equatable {
        case idle
        case preparing(String)
        case starting
        case ready
        case failed(String)
    }

    private(set) var state: State = .idle {
        didSet { if oldValue != state { onState?(state) } }
    }
    var onState: ((State) -> Void)?

    let port: Int
    private var process: Process?

    /// ~/Library/Application Support/Private Local AI
    static let supportDir: URL = {
        let base = FileManager.default.urls(for: .applicationSupportDirectory,
                                            in: .userDomainMask)[0]
        return base.appendingPathComponent("Private Local AI", isDirectory: true)
    }()

    static var venvDir: URL { supportDir.appendingPathComponent("venv") }
    static var dataDir: URL { supportDir.appendingPathComponent("local_data") }
    static var logFile: URL { supportDir.appendingPathComponent("server.log") }

    /// The bundled server source (Contents/Resources/server).
    static var serverDir: URL? {
        Bundle.main.resourceURL?.appendingPathComponent("server", isDirectory: true)
    }

    var url: URL { URL(string: "http://127.0.0.1:\(port)/")! }

    init(port: Int = 8765) { self.port = port }

    // MARK: - lifecycle

    func start() {
        guard case .idle = state else { return }
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.prepareAndLaunch()
        }
    }

    private func prepareAndLaunch() {
        let fm = FileManager.default
        try? fm.createDirectory(at: Self.supportDir, withIntermediateDirectories: true)
        try? fm.createDirectory(at: Self.dataDir, withIntermediateDirectories: true)

        guard let serverDir = Self.serverDir,
              fm.fileExists(atPath: serverDir.path) else {
            state = .failed("The bundled server files are missing from the app.")
            return
        }

        let python = Self.venvDir.appendingPathComponent("bin/python")
        if !fm.fileExists(atPath: python.path) {
            state = .preparing("Setting up the Python environment (first run only)…")
            if let err = createEnvironment(serverDir: serverDir) {
                state = .failed(err)
                return
            }
        }

        // Another copy of the app (or a dev server) may already own the port.
        // Adopting it is friendlier than failing to bind — but only if it is
        // running the code we just installed. Adopting a stale server means a
        // rebuild silently changes nothing, with no error to explain why.
        let wanted = bundledBuildID(serverDir: serverDir)
        if let running = runningBuildID() {
            if running == wanted || wanted == nil || running == "dev" {
                state = .ready
                return
            }
            state = .preparing("Replacing an older server on this port…")
            stopWhateverOwnsThePort()
        }

        state = .starting
        launch(python: python, serverDir: serverDir)
    }

    /// The build stamp written into the bundle by `mac/build.sh`.
    private func bundledBuildID(serverDir: URL) -> String? {
        try? String(contentsOf: serverDir.appendingPathComponent("BUILD_ID"),
                    encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// The build stamp reported by whatever server currently holds the port,
    /// or nil if nothing is answering there.
    private func runningBuildID() -> String? {
        let endpoint = url.appendingPathComponent("api/status")
        var request = URLRequest(url: endpoint, timeoutInterval: 2.0)
        request.httpMethod = "GET"
        var result: String?
        let done = DispatchSemaphore(value: 0)
        URLSession.shared.dataTask(with: request) { data, response, _ in
            defer { done.signal() }
            guard let http = response as? HTTPURLResponse, http.statusCode == 200,
                  let data,
                  let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else { return }
            result = (json["build"] as? String) ?? "unknown"
        }.resume()
        _ = done.wait(timeout: .now() + 3)
        return result
    }

    /// Terminate the process listening on our port, then wait for it to let go.
    private func stopWhateverOwnsThePort() {
        let lsof = Process()
        lsof.executableURL = URL(fileURLWithPath: "/usr/sbin/lsof")
        lsof.arguments = ["-ti", "tcp:\(port)", "-sTCP:LISTEN"]
        let pipe = Pipe()
        lsof.standardOutput = pipe
        lsof.standardError = FileHandle.nullDevice
        try? lsof.run()
        let out = pipe.fileHandleForReading.readDataToEndOfFile()
        lsof.waitUntilExit()

        let pids = String(decoding: out, as: UTF8.self)
            .split(whereSeparator: \.isNewline)
            .compactMap { Int32($0.trimmingCharacters(in: .whitespaces)) }
        for pid in pids { kill(pid, SIGTERM) }

        // Give it a moment to release the socket before we try to bind.
        for _ in 0..<20 {
            if runningBuildID() == nil { return }
            Thread.sleep(forTimeInterval: 0.25)
        }
        for pid in pids { kill(pid, SIGKILL) }
        Thread.sleep(forTimeInterval: 0.5)
    }

    /// First-run only: build a virtual environment beside the app's data.
    private func createEnvironment(serverDir: URL) -> String? {
        guard let systemPython = Self.findSystemPython() else {
            return "No Python 3.10+ found. Install it with:  brew install python@3.14"
        }
        if let err = run(systemPython, ["-m", "venv", Self.venvDir.path]) {
            return "Could not create the virtual environment: \(err)"
        }
        let pip = Self.venvDir.appendingPathComponent("bin/pip").path
        state = .preparing("Installing dependencies (first run only, needs internet)…")
        let reqs = serverDir.appendingPathComponent("requirements.txt").path
        if let err = run(pip, ["install", "--quiet", "--only-binary=:all:", "-r", reqs]) {
            return "Could not install dependencies: \(err)"
        }
        return nil
    }

    private static func findSystemPython() -> String? {
        let candidates = [
            "/opt/homebrew/bin/python3.14", "/opt/homebrew/bin/python3.13",
            "/opt/homebrew/bin/python3.12", "/opt/homebrew/bin/python3.11",
            "/opt/homebrew/bin/python3", "/usr/local/bin/python3", "/usr/bin/python3",
        ]
        for path in candidates where FileManager.default.isExecutableFile(atPath: path) {
            // Require 3.10+; the bundled wheels assume a modern interpreter.
            let p = Process()
            p.executableURL = URL(fileURLWithPath: path)
            p.arguments = ["-c", "import sys; raise SystemExit(0 if sys.version_info >= (3,10) else 1)"]
            try? p.run(); p.waitUntilExit()
            if p.terminationStatus == 0 { return path }
        }
        return nil
    }

    @discardableResult
    private func run(_ launchPath: String, _ args: [String]) -> String? {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: launchPath)
        p.arguments = args
        let pipe = Pipe()
        p.standardError = pipe
        p.standardOutput = Pipe()
        do { try p.run() } catch { return error.localizedDescription }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        if p.terminationStatus != 0 {
            let text = String(data: data, encoding: .utf8) ?? ""
            return text.isEmpty ? "exit code \(p.terminationStatus)"
                                : String(text.suffix(400))
        }
        return nil
    }

    private func launch(python: URL, serverDir: URL) {
        ensureOllama()

        let p = Process()
        p.executableURL = python
        p.arguments = ["-m", "uvicorn", "app.main:app",
                       "--host", "127.0.0.1", "--port", "\(port)",
                       "--log-level", "warning", "--no-access-log"]
        p.currentDirectoryURL = serverDir

        var env = ProcessInfo.processInfo.environment
        env["PRIVATE_AI_DATA_DIR"] = Self.dataDir.path
        env["PRIVATE_AI_PORT"] = "\(port)"
        env["OLLAMA_HOST"] = env["OLLAMA_HOST"] ?? "127.0.0.1:11434"
        env["OLLAMA_NOHISTORY"] = "1"
        env["PYTHONUNBUFFERED"] = "1"
        p.environment = env

        FileManager.default.createFile(atPath: Self.logFile.path, contents: nil)
        if let handle = try? FileHandle(forWritingTo: Self.logFile) {
            handle.seekToEndOfFile()
            p.standardOutput = handle
            p.standardError = handle
        }

        do { try p.run() } catch {
            state = .failed("Could not start the server: \(error.localizedDescription)")
            return
        }
        process = p
        waitUntilReady()
    }

    /// Start `ollama serve` if nothing is answering on the runtime port.
    private func ensureOllama() {
        if probe(URL(string: "http://127.0.0.1:11434/api/version")!, timeout: 1.5) { return }
        let candidates = ["/opt/homebrew/bin/ollama", "/usr/local/bin/ollama"]
        guard let bin = candidates.first(where: {
            FileManager.default.isExecutableFile(atPath: $0)
        }) else { return }
        let p = Process()
        p.executableURL = URL(fileURLWithPath: bin)
        p.arguments = ["serve"]
        p.standardOutput = Pipe()
        p.standardError = Pipe()
        try? p.run()
        for _ in 0..<20 {
            if probe(URL(string: "http://127.0.0.1:11434/api/version")!, timeout: 1) { return }
            Thread.sleep(forTimeInterval: 0.5)
        }
    }

    private func probe(_ url: URL, timeout: TimeInterval) -> Bool {
        var request = URLRequest(url: url)
        request.timeoutInterval = timeout
        let sem = DispatchSemaphore(value: 0)
        var ok = false
        URLSession.shared.dataTask(with: request) { _, response, _ in
            if let http = response as? HTTPURLResponse, http.statusCode < 500 { ok = true }
            sem.signal()
        }.resume()
        _ = sem.wait(timeout: .now() + timeout + 0.5)
        return ok
    }

    private func waitUntilReady() {
        let status = url.appendingPathComponent("api/status")
        for _ in 0..<80 {
            if probe(status, timeout: 1.0) { state = .ready; return }
            Thread.sleep(forTimeInterval: 0.25)
        }
        let log = (try? String(contentsOf: Self.logFile, encoding: .utf8)) ?? ""
        state = .failed("The server did not come up.\n\n" + String(log.suffix(500)))
    }

    func stop() {
        process?.terminate()
        process = nil
        state = .idle
    }
}
