// NiceGUI's CodeMirror element plus the relevance language-server client.
//
// Extends NiceGUI's own component (registered as the "nicegui-codemirror-base"
// ESM by editor.py), so every ui.codemirror feature keeps working, and passes
// its CodeMirror namespace to the shared client: one copy of CodeMirror.
//
// The transport is NiceGUI's event channel: a request is emitted as "lsp"
// {id, method, params}; Python answers with lspReply(id, result, error) and
// pushes server notifications with lspNotify(method, params).
import * as CM from "nicegui-codemirror";
import Base from "nicegui-codemirror-base/codemirror.js";
import { relevanceEditor } from "bigfix-relevance-editor/relevance_editor.js";

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
      return [...Base.methods.setupExtensions.call(this), relevanceEditor(CM, transport)];
    },
    lspReply(id, result, error) {
      const call = this._lsp?.pending.get(id);
      if (!call) return;
      this._lsp.pending.delete(id);
      if (error) call.reject(Object.assign(new Error(error.message), { code: error.code }));
      else call.resolve(result);
    },
    lspNotify(method, params) {
      for (const handler of this._lsp?.handlers || []) handler(method, params);
    },
  },
};
