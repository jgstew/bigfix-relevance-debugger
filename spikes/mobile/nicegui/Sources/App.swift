import SwiftUI
import WebKit

/// The whole native side: start CPython, start the debugger's NiceGUI app on
/// 127.0.0.1, show it in a WKWebView. Every bit of UI is NiceGUI's.
@main
struct BigFixRelevanceNiceGUIApp: App {
    @StateObject private var server = NiceGUIServer()

    var body: some Scene {
        WindowGroup {
            Group {
                if let url = server.url {
                    WebView(url: url).ignoresSafeArea(.container, edges: .bottom)
                } else if let error = server.error {
                    ScrollView { Text(error).font(.caption.monospaced()).padding() }
                } else {
                    ProgressView(server.status)
                }
            }
            .task { await server.start() }
        }
    }
}

@MainActor
final class NiceGUIServer: ObservableObject {
    @Published var url: URL?
    @Published var error: String?
    @Published var status = "Starting Python 3.15…"

    private let queue = DispatchQueue(label: "python")

    private func call(_ function: String) async -> [String: Any] {
        await withCheckedContinuation { done in
            queue.async {
                let out = bfrd_python_call(function, "{}")!
                let text = String(cString: out)
                free(out)
                let json = (try? JSONSerialization.jsonObject(with: Data(text.utf8))) as? [String: Any]
                done.resume(returning: json ?? ["error": text])
            }
        }
    }

    func start() async {
        let resources = Bundle.main.resourcePath!
        let started = Date()
        let startError: String? = await withCheckedContinuation { done in
            queue.async {
                if let err = bfrd_python_start(resources, "nicegui_ios_host") {
                    let message = String(cString: err)
                    free(err)
                    done.resume(returning: message)
                } else {
                    done.resume(returning: nil)
                }
            }
        }
        if let startError { error = "Python failed to start: \(startError)"; return }

        let reply = await call("start")
        guard let port = reply["port"] as? Int else { error = "start failed: \(reply)"; return }
        status = "Starting NiceGUI on port \(port)…"
        let target = URL(string: "http://127.0.0.1:\(port)/")!

        // Poll until the server answers (or the Python side reports a crash).
        for _ in 0..<240 {
            if let (_, response) = try? await URLSession.shared.data(from: target),
               (response as? HTTPURLResponse)?.statusCode == 200 {
                NSLog("bfrd nicegui: ready in %.2f s on %@", Date().timeIntervalSince(started), target.absoluteString)
                url = target
                return
            }
            let state = await call("status")
            if let crash = state["error"] as? String { error = crash; return }
            try? await Task.sleep(for: .milliseconds(250))
        }
        error = "NiceGUI did not answer on \(target) within 60 s"
    }
}

struct WebView: UIViewRepresentable {
    let url: URL

    func makeUIView(context: Context) -> WKWebView {
        let config = WKWebViewConfiguration()
        config.defaultWebpagePreferences.allowsContentJavaScript = true
        let view = WKWebView(frame: .zero, configuration: config)
        view.isInspectable = true  // Safari > Develop > Simulator, for debugging the page
        view.load(URLRequest(url: url))
        return view
    }

    func updateUIView(_ view: WKWebView, context: Context) {}
}
