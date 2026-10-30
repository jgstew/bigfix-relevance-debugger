import Foundation

/// The embedded CPython 3.15 (python.org's official iOS framework) running
/// `bfrd_mobile_core.bridge`. Every call is JSON in, JSON out, off the main
/// thread; the C shim takes the GIL per call.
enum Python {
    private static let queue = DispatchQueue(label: "python", qos: .userInitiated)
    private(set) static var startupSeconds: Double = 0

    static func start() async throws {
        let resources = Bundle.main.resourcePath!
        try await withCheckedThrowingContinuation { (done: CheckedContinuation<Void, Error>) in
            queue.async {
                let started = Date()
                if let error = bfrd_python_start(resources, "bfrd_mobile_core.bridge") {
                    let message = String(cString: error)
                    free(error)
                    done.resume(throwing: NSError(domain: "Python", code: 1,
                                                  userInfo: [NSLocalizedDescriptionKey: message]))
                } else {
                    startupSeconds = Date().timeIntervalSince(started)
                    done.resume()
                }
            }
        }
    }

    /// Raw call: returns the bridge's JSON text.
    static func call(_ function: String, _ args: [String: Any] = [:]) async -> Data {
        let json = (try? JSONSerialization.data(withJSONObject: args)) ?? Data("{}".utf8)
        let text = String(decoding: json, as: UTF8.self)
        return await withCheckedContinuation { done in
            queue.async {
                let out = bfrd_python_call(function, text)!
                let data = Data(String(cString: out).utf8)
                free(out)
                done.resume(returning: data)
            }
        }
    }

    static func call<T: Decodable>(_ function: String, _ args: [String: Any] = [:],
                                   as type: T.Type) async throws -> T {
        let data = await call(function, args)
        if let error = try? decoder.decode(BridgeError.self, from: data), let message = error.error {
            throw NSError(domain: "Python", code: 2, userInfo: [NSLocalizedDescriptionKey: message])
        }
        return try decoder.decode(T.self, from: data)
    }

    static let decoder: JSONDecoder = {
        let d = JSONDecoder()
        d.keyDecodingStrategy = .convertFromSnakeCase
        return d
    }()
}

struct BridgeError: Decodable { let error: String? }

struct Span: Decodable { let start: Int; let end: Int; let kind: String }

struct Finding: Decodable, Hashable {
    let severity: String
    let code: String
    let message: String
    let suggestions: [String]
}

struct Analysis: Decodable {
    let dialects: [String]
    let summary: String
    let parseError: String?
    let typeSummary: String?
    let findings: [Finding]
    let spans: [Span]
}

struct ResultRow: Decodable, Identifiable {
    let id = UUID()
    let dialect: String
    let connection: String
    let target: String
    let ok: Bool
    let answers: [String]
    let type: String?
    let error: String?
    let errorKind: String?
    let elapsedMs: Double?

    private enum CodingKeys: String, CodingKey {
        case dialect, connection, target, ok, answers, type, error, errorKind, elapsedMs
    }
}

struct PendingCert: Decodable, Identifiable {
    var id: String { hostPort }
    let hostPort: String
    let fingerprint: String
}

struct EvaluateResponse: Decodable {
    let rows: [ResultRow]
    let pendingCerts: [PendingCert]
}

struct SavedConnection: Codable, Identifiable, Hashable {
    var id: String { name }
    var name: String
    var backend: String
    var server: String
    var username: String
    var sid: String?
    var computers: String
    var useClientContext: Bool?
    var extra: [String: String] = [:]
    var hasSecret: Bool? = nil
}

struct ConnectionsResponse: Decodable { let connections: [SavedConnection] }
struct SmokeResponse: Decodable { let lines: [String] }
