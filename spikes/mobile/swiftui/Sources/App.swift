import SwiftUI

@main
struct BigFixRelevanceApp: App {
    @StateObject private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            Group {
                if let error = model.startError {
                    ContentUnavailableView("Python failed to start", systemImage: "exclamationmark.triangle",
                                           description: Text(error))
                } else if model.ready {
                    RootView().environmentObject(model)
                } else {
                    ProgressView("Starting Python 3.15…")
                }
            }
            .task { await model.start() }
        }
    }
}

@MainActor
final class AppModel: ObservableObject {
    @Published var ready = false
    @Published var startError: String?
    @Published var text = "names of bes computers whose (now - last report time of it < 1 * day)"
    @Published var analysis: Analysis?
    @Published var rows: [ResultRow] = []
    @Published var running = false
    @Published var pendingCert: PendingCert?
    @Published var connections: [SavedConnection] = []
    @Published var info: [String: String] = [:]
    @Published var smokeLines: [String] = []

    private var analyzeTask: Task<Void, Never>?
    private var lastEvaluate: (dialects: [String]?, totp: String?)?

    func start() async {
        do {
            try await Python.start()
            ready = true
            info = (try? await Python.call("info", as: [String: String].self)) ?? [:]
            info["python startup"] = String(format: "%.2f s", Python.startupSeconds)
            await reloadConnections()
            analyzeSoon()
            if ProcessInfo.processInfo.environment["BFRD_SMOKE"] != nil { await smoke() }
        } catch {
            startError = error.localizedDescription
        }
    }

    func analyzeSoon() {
        analyzeTask?.cancel()
        let text = self.text
        analyzeTask = Task {
            try? await Task.sleep(for: .milliseconds(250))
            guard !Task.isCancelled else { return }
            if let result = try? await Python.call("analyze", ["text": text], as: Analysis.self),
               !Task.isCancelled {
                analysis = result
            }
        }
    }

    func evaluate(dialects: [String]?, totp: String?) async {
        running = true
        defer { running = false }
        lastEvaluate = (dialects, totp)
        var args: [String: Any] = ["text": text]
        if let dialects { args["dialects"] = dialects }
        if let totp, !totp.isEmpty { args["totp"] = totp }
        do {
            let response = try await Python.call("evaluate", args, as: EvaluateResponse.self)
            rows = response.rows
            pendingCert = response.pendingCerts.first
        } catch {
            rows = []
            smokeLines = ["evaluate failed: \(error.localizedDescription)"]
        }
    }

    /// TOFU, two-step: the run failed on an unpinned cert; the user said trust.
    func trust(_ cert: PendingCert) async {
        _ = await Python.call("trust_cert", ["host_port": cert.hostPort])
        pendingCert = nil
        await evaluate(dialects: lastEvaluate?.dialects, totp: nil)
    }

    func reloadConnections() async {
        connections = (try? await Python.call("connections", as: ConnectionsResponse.self))?.connections ?? []
    }

    func save(_ connection: SavedConnection, password: String) async {
        var dict = (try? JSONSerialization.jsonObject(with: JSONEncoder.snake.encode(connection))) as? [String: Any] ?? [:]
        dict.removeValue(forKey: "has_secret")
        var args: [String: Any] = ["connection": dict]
        if !password.isEmpty { args["password"] = password }
        _ = await Python.call("save_connection", args)
        await reloadConnections()
    }

    func delete(_ name: String) async {
        _ = await Python.call("delete_connection", ["name": name])
        await reloadConnections()
    }

    func smoke() async {
        smokeLines = ["running…"]
        smokeLines = (try? await Python.call("smoke", as: SmokeResponse.self))?.lines ?? ["smoke failed"]
        for line in smokeLines { NSLog("bfrd smoke: %@", line) }
    }
}

extension JSONEncoder {
    static let snake: JSONEncoder = {
        let e = JSONEncoder()
        e.keyEncodingStrategy = .convertToSnakeCase
        return e
    }()
}
