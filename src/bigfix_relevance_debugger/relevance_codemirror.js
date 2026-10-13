// NiceGUI's CodeMirror element plus relevance highlighting and the
// language-server client.
//
// Extends NiceGUI's own component (registered as the "nicegui-codemirror-base"
// ESM by editor.py), so every ui.codemirror feature keeps working, and passes
// its CodeMirror namespace to the analyzer's shared modules (its wheel's web/,
// the "bigfix-relevance-editor" ESM): one copy of CodeMirror.
//
// The transport is NiceGUI's event channel: a request is emitted as "lsp"
// {id, method, params}; Python answers with lspReply(id, result, error) and
// pushes server notifications with lspNotify(method, params). On every mount
// the component emits "lspReady", so Python re-sends what a fresh editor lacks.
import * as CM from "nicegui-codemirror";
import Base from "nicegui-codemirror-base/codemirror.js";
import { relevanceEditor } from "bigfix-relevance-editor/relevance_editor.js";
import { relevanceLanguage } from "bigfix-relevance-editor/relevance_language.js";

// The VS Code grammar's rules. The element has no language prop, so this is
// the editor's only language. Colors are optional: if this CodeMirror can't
// build them, the editor keeps diagnostics, completion, hover and quick fixes.
function highlighting() {
  try {
    return [relevanceLanguage(CM)];
  } catch (error) {
    console.warn("relevance highlighting unavailable:", error);
    return [];
  }
}

// Tab completes; it never indents (relevance has no indentation, but NiceGUI's
// ui.codemirror always binds indentWithTab). NiceGUI's bundle doesn't export
// @codemirror/autocomplete, so run basicSetup's own completion keys: Enter
// accepts the selected option, Ctrl-Space opens the list. The content element
// has aria-activedescendant only while an option is selected, which keeps
// Enter from inserting a newline when nothing is.
function runKey(view, key, ctrlKey = false) {
  return CM.runScopeHandlers(view, new KeyboardEvent("keydown", { key, ctrlKey }), "editor");
}
function tabCompletes(view) {
  if (view.contentDOM.hasAttribute("aria-activedescendant")) runKey(view, "Enter");
  else runKey(view, " ", true);
  return true;
}

export default {
  extends: Base,
  methods: {
    setupExtensions() {
      const lsp = { pending: new Map(), next: 1, handlers: [] };
      this._lsp = lsp;
      const transport = {
        request: (method, params) =>
          new Promise((resolve, reject) => {
            const id = lsp.next++;
            lsp.pending.set(id, { resolve, reject });
            this.$emit("lsp", { id, method, params });
          }),
        onNotification: (handler) => lsp.handlers.push(handler),
      };
      const extensions = [
        ...Base.methods.setupExtensions.call(this),
        // The page's fixed header (NiceGUI's ui.header, a q-header) covers the
        // top of the window; CodeMirror can't see it, so tell it where
        // tooltips have room. Applies to every tooltip in this editor.
        CM.tooltips({
          tooltipSpace: () => {
            const header = document.querySelector(".q-header");
            const top = header ? Math.max(0, header.getBoundingClientRect().bottom) : 0;
            return { top, left: 0, bottom: window.innerHeight, right: window.innerWidth };
          },
        }),
        CM.Prec.high(
          CM.keymap.of([
            { key: "Tab", run: tabCompletes },
            { key: "Shift-Tab", run: () => true },
          ]),
        ),
        ...highlighting(),
        relevanceEditor(CM, transport),
      ];
      // Anything that arrived before this client existed, then ask for a refresh.
      for (const [method, params] of this._lastNotify || []) {
        for (const handler of lsp.handlers) handler(method, params);
      }
      this.$emit("lspReady");
      return extensions;
    },
    lspReply(id, result, error) {
      const call = this._lsp?.pending.get(id);
      if (!call) return;
      this._lsp.pending.delete(id);
      if (error) call.reject(Object.assign(new Error(error.message), { code: error.code }));
      else call.resolve(result);
    },
    lspNotify(method, params) {
      // Kept (latest per method) for a client created later in this instance.
      this._lastNotify = new Map(this._lastNotify || []).set(method, params);
      for (const handler of this._lsp?.handlers || []) handler(method, params);
    },
  },
};
