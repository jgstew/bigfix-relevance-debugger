// CodeMirror 6 client for the bigfix-relevance-analyzer language server.
//
// Candidate for bigfix-relevance-analyzer (jgstew/bigfix-relevance-analyzer#142,
// Layer 3): written to move there as is. It imports nothing. The page passes
// its own CodeMirror namespace (`CM`), so there is exactly one copy of
// @codemirror/state on the page and any CodeMirror 6 bundle works, including
// NiceGUI's, which exports neither @codemirror/lint nor @codemirror/autocomplete.
//
// Document sync is "host" mode: the page owns the text and keeps the server's
// copy current itself (e.g. Python holding an in-process lsp.Server), so this
// client sends no didOpen/didChange. A "client" mode for pages that talk to a
// server directly (the playground) is planned upstream.
//
// The transport is three functions:
//   request(method, params) -> Promise<result>   rejects on an error response
//   onNotification(handler(method, params))      server -> client messages

/** The CodeMirror names this client uses; all are stable CodeMirror 6 API. */
export const REQUIRED_CM = [
  "Decoration",
  "EditorView",
  "GutterMarker",
  "RangeSetBuilder",
  "StateEffect",
  "StateField",
  "ViewPlugin",
  "gutter",
  "hoverTooltip",
];

const SEVERITY = { 1: "error", 2: "warning", 3: "info", 4: "hint" };

/** An LSP position (0-based line, UTF-16 character) as a document offset, clamped. */
export function offsetAt(doc, position) {
  const line = doc.line(Math.min(Math.max(position.line, 0) + 1, doc.lines));
  return Math.min(line.from + Math.max(position.character, 0), line.to);
}

/**
 * LSP diagnostics as {from, to, severity, message, code}, against `doc`.
 * An empty range widens to one character so it can still be underlined.
 */
export function placeDiagnostics(doc, diagnostics) {
  return diagnostics
    .map((d) => {
      let from = offsetAt(doc, d.range.start);
      let to = offsetAt(doc, d.range.end);
      if (from === to) {
        if (to < doc.length) to += 1;
        else if (from > 0) from -= 1;
      }
      return {
        from,
        to,
        severity: SEVERITY[d.severity] || "error",
        message: d.message,
        code: d.code,
      };
    })
    .filter((d) => d.from < d.to);
}

/**
 * Extensions that show the server's diagnostics: underlines, a gutter marker
 * per line, and the messages in a tooltip on hover.
 *
 * `publishDiagnostics` params may carry `text`, the document they were
 * computed for (a host-mode extension to LSP): when it no longer matches
 * the editor, they are dropped, and the host's next publish replaces them.
 */
export function relevanceEditor(CM, transport, options = {}) {
  const missing = REQUIRED_CM.filter((name) => !(name in CM));
  if (missing.length) throw new Error(`relevanceEditor: CM lacks ${missing.join(", ")}`);
  const { classPrefix = "cm-bfr" } = options;

  const setDiagnostics = CM.StateEffect.define();
  const diagnosticField = CM.StateField.define({
    create: () => ({ list: [], marks: CM.Decoration.none }),
    update(value, tr) {
      let next = value;
      if (tr.docChanged) {
        next = {
          list: value.list.map((d) => ({
            ...d,
            from: tr.changes.mapPos(d.from, 1),
            to: tr.changes.mapPos(d.to, -1),
          })),
          marks: value.marks.map(tr.changes),
        };
      }
      for (const effect of tr.effects) {
        if (!effect.is(setDiagnostics)) continue;
        const list = effect.value;
        const marks = CM.Decoration.set(
          list.map((d) =>
            CM.Decoration.mark({ class: `${classPrefix}-diag ${classPrefix}-diag-${d.severity}` }).range(
              d.from,
              d.to,
            ),
          ),
          true,
        );
        next = { list, marks };
      }
      return next;
    },
    provide: (field) => CM.EditorView.decorations.from(field, (value) => value.marks),
  });

  // A notification can arrive before this client's view exists; keep the
  // latest and apply it to every view as it appears. (A host that rebuilds the
  // client, e.g. on remount, replays or re-sends it: see the host's docs.)
  const views = new Set();
  let latest = null;
  const apply = (view, params) => {
    const doc = view.state.doc;
    if (typeof params.text === "string" && params.text !== doc.toString()) return;
    view.dispatch({ effects: setDiagnostics.of(placeDiagnostics(doc, params.diagnostics || [])) });
  };
  transport.onNotification((method, params) => {
    if (method !== "textDocument/publishDiagnostics") return;
    latest = params;
    for (const view of views) apply(view, params);
  });
  const tracker = CM.ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.view = view;
        views.add(view);
        if (latest) queueMicrotask(() => apply(view, latest));
      }
      destroy() {
        views.delete(this.view);
      }
    },
  );

  class SeverityMarker extends CM.GutterMarker {
    constructor(severity, messages) {
      super();
      this.severity = severity;
      this.messages = messages;
    }
    eq(other) {
      return other.severity === this.severity && other.messages === this.messages;
    }
    toDOM() {
      const el = document.createElement("span");
      el.className = `${classPrefix}-marker ${classPrefix}-marker-${this.severity}`;
      el.textContent = "●";
      el.title = this.messages; // plain text, never HTML
      return el;
    }
  }
  const RANK = { error: 0, warning: 1, info: 2, hint: 3 };
  const gutter = CM.gutter({
    class: `${classPrefix}-gutter`,
    markers(view) {
      const doc = view.state.doc;
      const byLine = new Map();
      for (const d of view.state.field(diagnosticField).list) {
        const from = doc.lineAt(d.from).from;
        const entry = byLine.get(from) || { severity: d.severity, messages: [] };
        if (RANK[d.severity] < RANK[entry.severity]) entry.severity = d.severity;
        entry.messages.push(d.message);
        byLine.set(from, entry);
      }
      const builder = new CM.RangeSetBuilder();
      for (const from of [...byLine.keys()].sort((a, b) => a - b)) {
        const { severity, messages } = byLine.get(from);
        builder.add(from, from, new SeverityMarker(severity, messages.join("\n")));
      }
      return builder.finish();
    },
  });

  const hover = CM.hoverTooltip((view, pos, side) => {
    const here = view.state
      .field(diagnosticField)
      .list.filter((d) => (side < 0 ? d.from < pos && pos <= d.to : d.from <= pos && pos < d.to));
    if (!here.length) return null;
    return {
      pos: Math.min(...here.map((d) => d.from)),
      end: Math.max(...here.map((d) => d.to)),
      above: true,
      create() {
        const dom = document.createElement("div");
        dom.className = `${classPrefix}-tooltip`;
        for (const d of here) {
          const row = document.createElement("div");
          row.className = `${classPrefix}-tooltip-${d.severity}`;
          row.textContent = d.message; // plain text, never HTML
          dom.appendChild(row);
        }
        return { dom };
      },
    };
  });

  const p = `.${classPrefix}`;
  const theme = CM.EditorView.baseTheme({
    [`${p}-diag-error`]: { textDecoration: "underline wavy #d32f2f", textUnderlineOffset: "3px" },
    [`${p}-diag-warning`]: { textDecoration: "underline wavy #ed6c02", textUnderlineOffset: "3px" },
    [`${p}-diag-info`]: { textDecoration: "underline dotted #0288d1", textUnderlineOffset: "3px" },
    [`${p}-diag-hint`]: { textDecoration: "underline dotted #9e9e9e", textUnderlineOffset: "3px" },
    [`${p}-gutter`]: { width: "1em" },
    [`${p}-marker`]: { cursor: "default", fontSize: "0.8em" },
    [`${p}-marker-error`]: { color: "#d32f2f" },
    [`${p}-marker-warning`]: { color: "#ed6c02" },
    [`${p}-marker-info`]: { color: "#0288d1" },
    [`${p}-marker-hint`]: { color: "#9e9e9e" },
    [`${p}-tooltip`]: { padding: "4px 8px", maxWidth: "40em", whiteSpace: "pre-wrap" },
    [`${p}-tooltip-error`]: { borderLeft: "3px solid #d32f2f", paddingLeft: "6px" },
    [`${p}-tooltip-warning`]: { borderLeft: "3px solid #ed6c02", paddingLeft: "6px" },
    [`${p}-tooltip-info`]: { borderLeft: "3px solid #0288d1", paddingLeft: "6px" },
    [`${p}-tooltip-hint`]: { borderLeft: "3px solid #9e9e9e", paddingLeft: "6px" },
  });

  return [diagnosticField, tracker, gutter, hover, theme];
}
