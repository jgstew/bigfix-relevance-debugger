// CodeMirror 6 client for the bigfix-relevance-analyzer language server.
//
// Candidate for bigfix-relevance-analyzer (jgstew/bigfix-relevance-analyzer#142,
// Layer 3): written to move there as is, with its tests
// (tests/unit/upstream/web/). It imports nothing. The page passes its own
// CodeMirror namespace (`CM`), so there is exactly one copy of
// @codemirror/state on the page and any CodeMirror 6 bundle works, including
// NiceGUI's, which exports neither @codemirror/lint nor @codemirror/autocomplete.
//
// What it adds to an editor:
//   - diagnostics: underlines by severity, a gutter marker per line;
//   - completion, through the language data that basicSetup's
//     autocompletion() reads (Ctrl-Space, or while typing);
//   - hover: the diagnostics under the pointer with their quick fixes, and
//     the server's description of what is there (markdown, rendered safely);
//   - Mod-. : the quick fixes at the cursor;
//   - every edit applied as one transaction, so undo reverts it.
//
// Document sync is "host" mode: the page owns the text and keeps the server's
// copy current itself (e.g. Python holding an in-process lsp.Server), so this
// client sends no didOpen/didChange. A "client" mode for pages that talk to a
// server directly (the playground) is planned upstream.
//
// The transport is two functions:
//   request(method, params) -> Promise<result>   rejects on an error response
//   onNotification(handler(method, params))      server -> client messages

/** The CodeMirror names this client uses; all are stable CodeMirror 6 API. */
export const REQUIRED_CM = [
  "Decoration",
  "EditorState",
  "EditorView",
  "GutterMarker",
  "Prec",
  "RangeSetBuilder",
  "StateEffect",
  "StateField",
  "ViewPlugin",
  "closeHoverTooltips",
  "gutter",
  "hoverTooltip",
  "keymap",
  "showTooltip",
];

const SEVERITY = { 1: "error", 2: "warning", 3: "info", 4: "hint" };
const RANK = { error: 0, warning: 1, info: 2, hint: 3 };

// --- positions (pure) --------------------------------------------------------------

/** An LSP position (0-based line, UTF-16 character) as a document offset, clamped. */
export function offsetAt(doc, position) {
  const line = doc.line(Math.min(Math.max(position.line, 0) + 1, doc.lines));
  return Math.min(line.from + Math.max(position.character, 0), line.to);
}

/** A document offset as an LSP position. CodeMirror counts UTF-16 units, as LSP does. */
export function positionAt(doc, offset) {
  const clamped = Math.min(Math.max(offset, 0), doc.length);
  const line = doc.lineAt(clamped);
  return { line: line.number - 1, character: clamped - line.from };
}

// --- diagnostics (pure) ------------------------------------------------------------

/**
 * LSP diagnostics as {from, to, severity, message, code, lsp}, against `doc`.
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
        lsp: d,
      };
    })
    .filter((d) => d.from < d.to);
}

// --- completion (pure) -------------------------------------------------------------

/** A completion item's range: a TextEdit's, or an InsertReplaceEdit's `replace`. */
function editRange(textEdit) {
  return textEdit.range ?? textEdit.replace;
}

/**
 * Whether to ask the server for completions without Ctrl-Space, given the
 * character before the cursor: after a word character, or one of the
 * server's `triggerCharacters`. Not after a space or `(`: there the server
 * lists everything, and Enter would take its first item instead of a newline.
 */
export function shouldAskImplicitly(charBefore, triggerCharacters = []) {
  return /\w/.test(charBefore) || triggerCharacters.includes(charBefore);
}

/**
 * A CodeMirror CompletionResult for an LSP completion result, or null.
 *
 * The server has already matched the partial word and ranked the items, so
 * the result is neither filtered nor re-sorted (`filter: false` keeps their
 * order), and it is asked again on the next keystroke. The result's range is
 * the first item's; an item with another range (or none: it inserts at
 * `pos`) applies its own edit.
 */
export function completionResult(doc, result, pos) {
  const items = (result && result.items) || [];
  if (!items.length) return null;
  const rangeOf = (item) => {
    if (!item.textEdit) return { from: pos, to: pos };
    const range = editRange(item.textEdit);
    return { from: offsetAt(doc, range.start), to: offsetAt(doc, range.end) };
  };
  const { from, to } = rangeOf(items.find((item) => item.textEdit) || items[0]);
  return {
    from,
    to,
    filter: false,
    options: items.map((item) => {
      const insert = item.textEdit ? item.textEdit.newText : (item.insertText ?? item.label);
      const own = rangeOf(item);
      const apply =
        own.from === from && own.to === to
          ? insert
          : (view) =>
              view.dispatch({
                changes: { from: own.from, to: own.to, insert },
                selection: { anchor: own.from + insert.length },
                userEvent: "input.complete",
              });
      return { label: item.label, detail: item.detail, apply, type: "property" };
    }),
  };
}

// --- edits (pure) ------------------------------------------------------------------

/**
 * The CodeMirror changes in an LSP WorkspaceEdit for document `uri`, sorted.
 * Reads both `changes` and `documentChanges`; throws on overlapping edits.
 */
export function workspaceEditChanges(doc, edit, uri) {
  const edits = [];
  if (edit && edit.changes && Array.isArray(edit.changes[uri])) edits.push(...edit.changes[uri]);
  for (const change of (edit && edit.documentChanges) || []) {
    if (change.textDocument && change.textDocument.uri === uri && Array.isArray(change.edits)) {
      edits.push(...change.edits);
    }
  }
  const changes = edits
    .map((e) => ({ from: offsetAt(doc, e.range.start), to: offsetAt(doc, e.range.end), insert: e.newText }))
    .sort((a, b) => a.from - b.from || a.to - b.to);
  for (let i = 1; i < changes.length; i++) {
    if (changes[i].from < changes[i - 1].to) throw new Error("workspace edit: overlapping edits");
  }
  return changes;
}

/**
 * A transaction spec applying `edit` to `state`, or null: when the document
 * is no longer `expectedDoc` (the one the edit was computed for, so its
 * offsets would land in the wrong place), or there is nothing valid to do.
 */
export function editTransaction(state, edit, uri, expectedDoc) {
  if (state.doc !== expectedDoc) return null;
  let changes;
  try {
    changes = workspaceEditChanges(state.doc, edit, uri);
  } catch {
    return null;
  }
  return changes.length ? { changes, userEvent: "input", scrollIntoView: true } : null;
}

/** A code action this client can apply as a quick fix (LSP kinds match by prefix). */
export function isQuickFix(action) {
  const kind = action.kind;
  return Boolean(action.edit) && (!kind || kind === "quickfix" || kind.startsWith("quickfix."));
}

// --- markdown (pure parse, safe render) ---------------------------------------------

const INLINE = /(`[^`]*`)|\*\*(.+?)\*\*|\[([^\]]*)\]\(([^)\s]*)\)/g;
const SAFE_LINK = /^https?:\/\//i;

function inline(text) {
  const nodes = [];
  const push = (node) => {
    const last = nodes[nodes.length - 1];
    if (node.type === "text" && last && last.type === "text") last.text += node.text;
    else if (node.type !== "text" || node.text) nodes.push(node);
  };
  let at = 0;
  for (const m of text.matchAll(INLINE)) {
    push({ type: "text", text: text.slice(at, m.index) });
    if (m[1] !== undefined) push({ type: "code", text: m[1].slice(1, -1) });
    else if (m[2] !== undefined) push({ type: "strong", children: inline(m[2]) });
    else if (SAFE_LINK.test(m[4])) push({ type: "link", href: m[4], children: inline(m[3]) });
    else push({ type: "text", text: m[0] });
    at = m.index + m[0].length;
  }
  push({ type: "text", text: text.slice(at) });
  return nodes;
}

/**
 * The markdown the analyzer's hover produces, as plain data: paragraphs and
 * "- " lists of text, `code`, **strong** and http(s) links. Everything else,
 * HTML included, stays literal text.
 */
export function markdownBlocks(markdown) {
  return markdown
    .split(/\n\s*\n/)
    .map((block) => block.split("\n").filter((line) => line.trim()))
    .filter((lines) => lines.length)
    .map((lines) =>
      lines.every((line) => /^\s*- /.test(line))
        ? { type: "list", items: lines.map((line) => inline(line.replace(/^\s*- /, ""))) }
        : { type: "paragraph", inline: inline(lines.join(" ")) },
    );
}

function renderInline(parent, nodes) {
  for (const node of nodes) {
    if (node.type === "text") {
      parent.appendChild(document.createTextNode(node.text));
    } else if (node.type === "code") {
      const el = parent.appendChild(document.createElement("code"));
      el.textContent = node.text;
    } else {
      const el = parent.appendChild(document.createElement(node.type === "strong" ? "strong" : "a"));
      if (node.type === "link") {
        el.href = node.href;
        el.target = "_blank";
        el.rel = "noopener noreferrer";
      }
      renderInline(el, node.children);
    }
  }
}

/** DOM for `markdown`, built node by node: never innerHTML. */
export function renderMarkdown(markdown) {
  const root = document.createElement("div");
  for (const block of markdownBlocks(markdown)) {
    if (block.type === "list") {
      const ul = root.appendChild(document.createElement("ul"));
      for (const item of block.items) renderInline(ul.appendChild(document.createElement("li")), item);
    } else {
      renderInline(root.appendChild(document.createElement("p")), block.inline);
    }
  }
  return root;
}

// --- the extension -----------------------------------------------------------------

/**
 * The extensions to add to an editor. Options:
 *   uri: the server's name for the document (default "untitled:statement",
 *     what the analyzer's embedded StatementSession uses); edits for other
 *     documents are ignored.
 *   triggerCharacters: the server's completionProvider.triggerCharacters,
 *     which also open completion while typing (word characters always do).
 *   classPrefix: for the CSS classes (default "cm-bfr").
 *
 * Tooltips use CodeMirror's defaults: a host whose page covers part of the
 * window (a fixed header) tells CodeMirror with `tooltips({tooltipSpace})`.
 *
 * `publishDiagnostics` params may carry `text`, the document they were
 * computed for (a host-mode extension to LSP): when it no longer matches the
 * editor, they are dropped, and the host's next publish replaces them.
 */
export function relevanceEditor(CM, transport, options = {}) {
  const missing = REQUIRED_CM.filter((name) => !(name in CM));
  if (missing.length) throw new Error(`relevanceEditor: CM lacks ${missing.join(", ")}`);
  const { classPrefix = "cm-bfr", uri = "untitled:statement", triggerCharacters = [] } = options;
  const css = (name) => `${classPrefix}-${name}`;

  // Diagnostics, mapped through edits until the next publish replaces them.
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
          list.map((d) => CM.Decoration.mark({ class: `${css("diag")} ${css(`diag-${d.severity}`)}` }).range(d.from, d.to)),
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
  const applyDiagnostics = (view, params) => {
    const doc = view.state.doc;
    if (typeof params.text === "string" && params.text !== doc.toString()) return;
    view.dispatch({ effects: setDiagnostics.of(placeDiagnostics(doc, params.diagnostics || [])) });
  };
  transport.onNotification((method, params) => {
    if (method !== "textDocument/publishDiagnostics") return;
    latest = params;
    for (const view of views) applyDiagnostics(view, params);
  });
  const tracker = CM.ViewPlugin.fromClass(
    class {
      constructor(view) {
        this.view = view;
        views.add(view);
        if (latest) queueMicrotask(() => applyDiagnostics(view, latest));
      }
      destroy() {
        views.delete(this.view);
      }
    },
  );

  const request = async (method, params) => {
    try {
      return await transport.request(method, { ...params, textDocument: { uri } });
    } catch {
      return null; // an error reply or a closed editor: nothing to show
    }
  };

  // Quick fixes for [from, to) in the current document, or null when the
  // document changed while the server answered (they'd be for other text).
  const quickFixes = async (view, from, to, diagnostics) => {
    const doc = view.state.doc;
    const actions = await request("textDocument/codeAction", {
      range: { start: positionAt(doc, from), end: positionAt(doc, to) },
      context: { diagnostics: diagnostics.map((d) => d.lsp), only: ["quickfix"] },
    });
    if (view.state.doc !== doc) return null;
    return (actions || []).filter(isQuickFix);
  };

  // Buttons for `actions`, computed for `doc`: a click applies one unless
  // the document has changed since.
  const actionButtons = (view, actions, doc, done) => {
    const box = document.createElement("div");
    box.className = css("actions");
    for (const action of actions) {
      const button = box.appendChild(document.createElement("button"));
      button.type = "button";
      button.className = css("action");
      button.textContent = action.title; // plain text, never HTML
      button.addEventListener("mousedown", (event) => event.preventDefault()); // keep the editor's focus
      button.addEventListener("click", () => {
        done();
        const spec = editTransaction(view.state, action.edit, uri, doc);
        if (spec) view.dispatch(spec);
        view.focus();
      });
    }
    return box;
  };

  // Gutter: one marker per line, the worst severity, every message as its title.
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
      el.className = `${css("marker")} ${css(`marker-${this.severity}`)}`;
      el.textContent = "●";
      el.title = this.messages; // plain text, never HTML
      return el;
    }
  }
  const gutter = CM.gutter({
    class: css("gutter"),
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

  // Hover: diagnostics here, their quick fixes, and the server's description.
  const hover = CM.hoverTooltip(async (view, pos, side) => {
    const doc = view.state.doc;
    const here = view.state
      .field(diagnosticField)
      .list.filter((d) => (side < 0 ? d.from < pos && pos <= d.to : d.from <= pos && pos < d.to));
    const [info, found] = await Promise.all([
      request("textDocument/hover", { position: positionAt(doc, pos) }),
      here.length
        ? quickFixes(view, Math.min(...here.map((d) => d.from)), Math.max(...here.map((d) => d.to)), here)
        : [],
    ]);
    if (view.state.doc !== doc) return null; // edited meanwhile
    const fixes = found || [];
    const contents = info && info.contents && typeof info.contents.value === "string" ? info.contents.value : "";
    if (!here.length && !contents) return null;
    const starts = here.map((d) => d.from);
    const ends = here.map((d) => d.to);
    if (contents && info.range) {
      starts.push(offsetAt(doc, info.range.start));
      ends.push(offsetAt(doc, info.range.end));
    }
    return {
      pos: starts.length ? Math.min(...starts) : pos,
      end: ends.length ? Math.max(...ends) : pos,
      create() {
        const dom = document.createElement("div");
        dom.className = css("tooltip");
        for (const d of here) {
          const row = dom.appendChild(document.createElement("div"));
          row.className = `${css("message")} ${css(`message-${d.severity}`)}`;
          row.textContent = d.message; // plain text, never HTML
        }
        if (fixes.length) {
          dom.appendChild(actionButtons(view, fixes, doc, () => view.dispatch({ effects: CM.closeHoverTooltips })));
        }
        if (contents) {
          const info = dom.appendChild(renderMarkdown(contents));
          info.className = css("info");
        }
        return { dom };
      },
    };
  }, { hideOnChange: true }); // its text and fixes are for the document as it was

  // Mod-. : the quick fixes at the cursor, as a small menu.
  const setMenu = CM.StateEffect.define();
  const menuField = CM.StateField.define({
    create: () => null,
    update(value, tr) {
      for (const effect of tr.effects) if (effect.is(setMenu)) return effect.value;
      return tr.docChanged || tr.selection ? null : value;
    },
    provide: (field) =>
      CM.showTooltip.from(field, (menu) =>
        menu
          ? {
              pos: menu.pos,
              create(view) {
                const close = () => view.dispatch({ effects: setMenu.of(null) });
                const dom = menu.actions.length
                  ? actionButtons(view, menu.actions, menu.doc, close)
                  : Object.assign(document.createElement("div"), {
                      className: css("empty"),
                      textContent: "No quick fixes here",
                    });
                dom.classList.add(css("menu"));
                // Close when focus moves somewhere other than the menu or the editor.
                dom.addEventListener("focusout", (event) => {
                  const next = event.relatedTarget;
                  if (next && (dom.contains(next) || view.dom.contains(next))) return;
                  queueMicrotask(() => view.state.field(menuField) && close());
                });
                dom.addEventListener("keydown", (event) => {
                  const buttons = [...dom.querySelectorAll("button")];
                  const index = buttons.indexOf(document.activeElement);
                  if (event.key === "Escape") {
                    event.preventDefault();
                    close();
                    view.focus();
                  } else if (event.key === "ArrowDown" || event.key === "ArrowUp") {
                    event.preventDefault();
                    const step = event.key === "ArrowDown" ? 1 : -1;
                    buttons[(index + step + buttons.length) % buttons.length]?.focus();
                  }
                });
                return {
                  dom,
                  mount() {
                    dom.querySelector("button")?.focus();
                  },
                };
              },
            }
          : null,
      ),
  });
  const openMenu = (view) => {
    const doc = view.state.doc;
    const { from, to } = view.state.selection.main;
    const here = view.state.field(diagnosticField).list.filter((d) => d.from <= to && from <= d.to);
    const lo = Math.min(from, ...here.map((d) => d.from));
    const hi = Math.max(to, ...here.map((d) => d.to));
    quickFixes(view, lo, hi, here).then((actions) => {
      // Stale (edited meanwhile) or moved: say nothing rather than "no fixes".
      if (actions === null || view.state.doc !== doc || view.state.selection.main.from !== from) return;
      view.dispatch({ effects: setMenu.of({ pos: from, actions, doc }) });
    });
    return true;
  };
  // The empty menu has nothing to focus: close it when the editor loses focus.
  const blurCloses = CM.EditorView.domEventHandlers({
    blur(event, view) {
      if (!view.state.field(menuField) || view.dom.contains(event.relatedTarget)) return false;
      const menu = document.querySelector(`.${css("menu")}`);
      if (menu && menu.contains(event.relatedTarget)) return false;
      queueMicrotask(() => view.state.field(menuField) && view.dispatch({ effects: setMenu.of(null) }));
      return false;
    },
  });
  const keys = CM.Prec.high(
    CM.keymap.of([
      { key: "Mod-.", run: openMenu },
      {
        key: "Escape",
        run: (view) => {
          if (!view.state.field(menuField)) return false;
          view.dispatch({ effects: setMenu.of(null) });
          return true;
        },
      },
    ]),
  );

  // Completion, through language data: no @codemirror/autocomplete import.
  const completionSource = async (context) => {
    if (!context.explicit) {
      const before = context.state.doc.sliceString(Math.max(0, context.pos - 1), context.pos);
      if (!shouldAskImplicitly(before, triggerCharacters)) return null;
    }
    const doc = context.state.doc;
    const result = await request("textDocument/completion", { position: positionAt(doc, context.pos) });
    if (context.aborted) return null;
    return completionResult(doc, result, context.pos);
  };
  const completion = CM.EditorState.languageData.of(() => [{ autocomplete: completionSource }]);

  const p = `.${classPrefix}`;
  const colors = { error: "#d32f2f", warning: "#ed6c02", info: "#0288d1", hint: "#9e9e9e" };
  const themeSpec = {
    [`${p}-gutter`]: { width: "1em" },
    [`${p}-marker`]: { cursor: "default", fontSize: "0.8em" },
    [`${p}-tooltip`]: { padding: "4px 8px", maxWidth: "40em" },
    [`${p}-message`]: { whiteSpace: "pre-wrap", paddingLeft: "6px", margin: "2px 0" },
    [`${p}-info`]: { marginTop: "4px" },
    [`${p}-info p`]: { margin: "2px 0" },
    [`${p}-info ul`]: { margin: "2px 0", paddingLeft: "1.2em" },
    [`${p}-info code`]: { fontFamily: "monospace" },
    [`${p}-actions`]: { display: "flex", flexDirection: "column", gap: "2px", margin: "4px 0" },
    [`${p}-action`]: {
      textAlign: "left",
      cursor: "pointer",
      border: "1px solid rgba(127,127,127,.4)",
      borderRadius: "3px",
      background: "transparent",
      color: "inherit",
      padding: "1px 6px",
      font: "inherit",
    },
    [`${p}-action:hover, ${p}-action:focus`]: { background: "rgba(25,118,210,.15)", outline: "none" },
    [`${p}-menu`]: { padding: "4px" },
    [`${p}-empty`]: { padding: "2px 6px", opacity: "0.7" },
  };
  for (const [severity, color] of Object.entries(colors)) {
    const line = severity === "error" || severity === "warning" ? "wavy" : "dotted";
    themeSpec[`${p}-diag-${severity}`] = { textDecoration: `underline ${line} ${color}`, textUnderlineOffset: "3px" };
    themeSpec[`${p}-marker-${severity}`] = { color };
    themeSpec[`${p}-message-${severity}`] = { borderLeft: `3px solid ${color}` };
  }
  const theme = CM.EditorView.baseTheme(themeSpec);

  return [diagnosticField, tracker, gutter, hover, menuField, keys, blurCloses, completion, theme];
}
