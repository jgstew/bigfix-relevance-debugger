import SwiftUI

struct RootView: View {
    @EnvironmentObject var model: AppModel
    @State private var tab = ProcessInfo.processInfo.environment["BFRD_TAB"] ?? "relevance"

    var body: some View {
        TabView(selection: $tab) {
            RelevanceView()
                .tabItem { Label("Relevance", systemImage: "chevron.left.forwardslash.chevron.right") }
                .tag("relevance")
            ConnectionsView()
                .tabItem { Label("Connections", systemImage: "server.rack") }
                .tag("connections")
            AboutView()
                .tabItem { Label("About", systemImage: "info.circle") }
                .tag("about")
        }
    }
}

struct RelevanceView: View {
    @EnvironmentObject var model: AppModel
    @State private var dialect = "Auto"
    @State private var totp = ""
    private let dialects: [String: [String]?] = [
        "Auto": nil, "Session": ["session"], "Client (Fast Query)": ["client"], "Both": ["session", "client"],
    ]

    var body: some View {
        NavigationStack {
            List {
                Section {
                    RelevanceTextView(text: $model.text, spans: model.analysis?.spans ?? [])
                        .frame(minHeight: 120)
                        .listRowInsets(EdgeInsets())
                        .onChange(of: model.text) { model.analyzeSoon() }
                    if let a = model.analysis {
                        Label(a.summary, systemImage: a.parseError == nil ? "checkmark.seal" : "xmark.octagon")
                            .font(.footnote)
                            .foregroundStyle(a.parseError == nil ? Color.secondary : Color.red)
                        ForEach(a.findings.prefix(3), id: \.self) { f in
                            Label(f.message, systemImage: f.severity == "error" ? "xmark.circle" : "exclamationmark.triangle")
                                .font(.footnote).foregroundStyle(.orange)
                        }
                    }
                }
                Section {
                    Picker("Run as", selection: $dialect) {
                        ForEach(["Auto", "Session", "Client (Fast Query)", "Both"], id: \.self) { Text($0) }
                    }
                    TextField("TOTP code (only if your SaaS account asks)", text: $totp)
                        .keyboardType(.numberPad)
                        .textContentType(.oneTimeCode)
                    Button {
                        Task {
                            await model.evaluate(dialects: dialects[dialect] ?? nil, totp: totp)
                            totp = ""
                        }
                    } label: {
                        HStack {
                            Text("Evaluate").bold()
                            if model.running { Spacer(); ProgressView() }
                        }
                    }
                    .disabled(model.running)
                }
                if !model.rows.isEmpty {
                    Section("Results") {
                        ForEach(model.rows) { ResultRowView(row: $0) }
                    }
                }
                if !model.smokeLines.isEmpty {
                    Section("Self-check") {
                        ForEach(model.smokeLines, id: \.self) { line in
                            Text(line).font(.caption.monospaced())
                                .foregroundStyle(line.hasPrefix("FAIL") ? .red : .primary)
                        }
                    }
                }
            }
            .navigationTitle("BigFix Relevance")
            .navigationBarTitleDisplayMode(.inline)
            .alert("Trust this certificate?", isPresented: Binding(
                get: { model.pendingCert != nil }, set: { if !$0 { model.pendingCert = nil } }
            ), presenting: model.pendingCert) { cert in
                Button("Trust and pin", role: .destructive) { Task { await model.trust(cert) } }
                Button("Cancel", role: .cancel) {}
            } message: { cert in
                Text("\(cert.hostPort) presented a certificate that isn't trusted yet.\n\nSHA-256:\n\(cert.fingerprint)\n\nTrust it only if this matches the server's real certificate. A different one later is refused.")
            }
        }
    }
}

struct ResultRowView: View {
    let row: ResultRow

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if row.ok {
                Text(row.answers.isEmpty ? "(no answers)" : row.answers.prefix(20).joined(separator: "\n"))
                    .font(.body.monospaced())
                    .textSelection(.enabled)
            } else {
                Label(row.error ?? "error", systemImage: "exclamationmark.triangle.fill")
                    .foregroundStyle(.orange)
            }
            Text([row.dialect, row.connection, row.target, row.type ?? "",
                  row.elapsedMs.map { String(format: "%.0f ms", $0) } ?? ""]
                .filter { !$0.isEmpty }.joined(separator: " · "))
                .font(.caption).foregroundStyle(.secondary)
        }
    }
}

struct ConnectionsView: View {
    @EnvironmentObject var model: AppModel
    @State private var draft = SavedConnection(name: "", backend: "saas", server: "login.bigfix.com",
                                               username: "", sid: nil, computers: "")
    @State private var password = ""
    private let backends = [("saas", "BigFix SaaS"), ("rest", "Root server REST API"),
                            ("explorer", "BigFix Explorer"), ("webreports", "Web Reports")]

    var body: some View {
        NavigationStack {
            Form {
                Section("Connection") {
                    Picker("Type", selection: $draft.backend) {
                        ForEach(backends, id: \.0) { Text($0.1).tag($0.0) }
                    }
                    TextField("Name", text: $draft.name)
                    TextField("Server", text: $draft.server)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .keyboardType(.URL)
                    TextField(draft.backend == "saas" ? "Email" : "Username", text: $draft.username)
                        .textInputAutocapitalization(.never).autocorrectionDisabled()
                        .textContentType(.username)
                    SecureField("Password (saved in the Keychain)", text: $password)
                        .textContentType(.password)
                    if draft.backend == "saas" {
                        TextField("SaaS server id (optional)", text: Binding(
                            get: { draft.sid ?? "" }, set: { draft.sid = $0.isEmpty ? nil : $0 }))
                    }
                }
                if draft.backend == "saas" || draft.backend == "rest" {
                    Section {
                        TextField("Computer ids, or bes computers whose (…)", text: $draft.computers, axis: .vertical)
                            .textInputAutocapitalization(.never).autocorrectionDisabled()
                    } header: { Text("Fast Query targets") } footer: {
                        Text("Client relevance runs on these endpoints through the server.")
                    }
                }
                Button("Save") {
                    var conn = draft
                    if conn.name.isEmpty { conn.name = conn.backend }
                    Task {
                        await model.save(conn, password: password)
                        password = ""
                    }
                }
                Section("Saved") {
                    if model.connections.isEmpty { Text("None yet").foregroundStyle(.secondary) }
                    ForEach(model.connections) { c in
                        Button {
                            draft = c
                        } label: {
                            VStack(alignment: .leading) {
                                HStack {
                                    Text(c.name).bold()
                                    if c.hasSecret == true { Image(systemName: "key.fill").foregroundStyle(.secondary) }
                                }
                                Text("\(c.backend) · \(c.server) · \(c.username)").font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .tint(.primary)
                    }
                    .onDelete { offsets in
                        let names = offsets.map { model.connections[$0].name }
                        Task { for n in names { await model.delete(n) } }
                    }
                }
            }
            .navigationTitle("Connections")
        }
    }
}

struct AboutView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        NavigationStack {
            List {
                Section("Runtime") {
                    ForEach(model.info.keys.sorted(), id: \.self) { key in
                        LabeledContent(key, value: model.info[key] ?? "")
                    }
                }
                Section {
                    Button("Run self-check") { Task { await model.smoke() } }
                    ForEach(model.smokeLines, id: \.self) { Text($0).font(.caption.monospaced()) }
                }
            }
            .navigationTitle("About")
        }
    }
}
