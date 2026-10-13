// Tests for upstream/web/relevance_editor.js, the shared CodeMirror client.
//
// Written to move into bigfix-relevance-analyzer with the client. They need
// a CodeMirror 6 bundle, given as the CM_MODULE environment variable (a file
// path or URL): here, NiceGUI's own bundle, which the page uses too.
//
//   CM_MODULE=/path/to/codemirror/dist/index.js node --test tests/unit/upstream/web/
import assert from "node:assert/strict";
import { test } from "node:test";
import { pathToFileURL } from "node:url";

import {
  REQUIRED_CM,
  completionResult,
  editTransaction,
  isQuickFix,
  markdownBlocks,
  offsetAt,
  placeDiagnostics,
  positionAt,
  relevanceEditor,
  shouldAskImplicitly,
  workspaceEditChanges,
} from "../../../../src/bigfix_relevance_debugger/upstream/web/relevance_editor.js";

const cmPath = process.env.CM_MODULE;
assert.ok(cmPath, "set CM_MODULE to a CodeMirror 6 bundle");
const CM = await import(cmPath.startsWith("file:") ? cmPath : pathToFileURL(cmPath).href);

const URI = "untitled:statement";
const doc = (text) => CM.Text.of(text.split("\n"));
const at = (line, character) => ({ line, character });
const span = (a, b) => ({ start: at(...a), end: at(...b) });

// --- positions --------------------------------------------------------------------

test("offsetAt and positionAt are inverses, across lines", () => {
  const d = doc("name of\noperating system");
  for (const offset of [0, 4, 7, 8, 12, d.length]) {
    assert.equal(offsetAt(d, positionAt(d, offset)), offset);
  }
  assert.deepEqual(positionAt(d, 12), at(1, 4));
});

test("offsetAt clamps past the end of a line or the document", () => {
  const d = doc("ab\ncd");
  assert.equal(offsetAt(d, at(0, 99)), 2);
  assert.equal(offsetAt(d, at(9, 0)), 3);
  assert.equal(offsetAt(d, at(-1, -1)), 0);
});

test("positions count UTF-16 units, like LSP and CodeMirror", () => {
  const d = doc('"😀" & nme');
  // The emoji is two UTF-16 units: `nme` starts at character 7.
  assert.equal(d.sliceString(offsetAt(d, at(0, 7)), offsetAt(d, at(0, 10))), "nme");
});

// --- diagnostics ------------------------------------------------------------------

test("placeDiagnostics widens an empty range so it can be underlined", () => {
  const d = doc("name of (x");
  const [end] = placeDiagnostics(d, [{ range: span([0, 10], [0, 10]), severity: 1, message: "m" }]);
  assert.deepEqual([end.from, end.to, end.severity], [9, 10, "error"]);
});

// --- completion -------------------------------------------------------------------

const ITEMS = {
  isIncomplete: true,
  items: [
    {
      label: "operating system",
      detail: "operating system: operating system",
      sortText: "0000",
      textEdit: { range: span([0, 8], [0, 10]), newText: "operating system" },
    },
    {
      label: "bes filters",
      detail: "bes filters: bes filter",
      sortText: "0001",
      textEdit: { range: span([0, 8], [0, 10]), newText: "bes filters" },
    },
  ],
};

test("completionResult maps the server's items, keeping its order", () => {
  const result = completionResult(doc("name of op"), ITEMS);
  assert.deepEqual([result.from, result.to], [8, 10]);
  assert.deepEqual(
    result.options.map((o) => [o.label, o.detail, o.apply]),
    [
      ["operating system", "operating system: operating system", "operating system"],
      ["bes filters", "bes filters: bes filter", "bes filters"],
    ],
  );
  assert.equal(result.filter, false); // the server already matched and ranked them
});

test("completionResult without a textEdit inserts the label at the cursor", () => {
  const result = completionResult(doc("name of "), { items: [{ label: "drive" }] }, 8);
  assert.deepEqual([result.from, result.to, result.options[0].apply], [8, 8, "drive"]);
});

// A stand-in for an EditorView: records what an option's apply() dispatches.
const recorder = () => {
  const dispatched = [];
  return { dispatched, view: { dispatch: (spec) => dispatched.push(spec) } };
};

test("completionResult gives an item with its own range its own edit", () => {
  const d = doc("names of opera");
  const result = completionResult(d, {
    items: [
      { label: "operating system", textEdit: { range: span([0, 9], [0, 14]), newText: "operating system" } },
      { label: "names of x", textEdit: { range: span([0, 0], [0, 14]), newText: "names of x" } },
      { label: "drive" },
    ],
  }, 14);
  assert.deepEqual([result.from, result.to], [9, 14]);
  assert.equal(result.options[0].apply, "operating system"); // same range: plain text
  for (const [option, from, insert] of [[result.options[1], 0, "names of x"], [result.options[2], 14, "drive"]]) {
    const { dispatched, view } = recorder();
    option.apply(view, option, result.from, result.to);
    assert.deepEqual(dispatched[0].changes, { from, to: 14, insert }); // both end at the cursor
    assert.deepEqual(dispatched[0].selection, { anchor: from + insert.length });
  }
});

test("completionResult reads an InsertReplaceEdit's replace range", () => {
  const result = completionResult(doc("name of op"), {
    items: [
      {
        label: "operating system",
        textEdit: { newText: "operating system", insert: span([0, 8], [0, 10]), replace: span([0, 8], [0, 10]) },
      },
    ],
  }, 10);
  assert.deepEqual([result.from, result.to, result.options[0].apply], [8, 10, "operating system"]);
});

test("completion asks implicitly only after a word character", () => {
  // After a space or `(` the server lists everything, and Enter would take
  // the first item instead of starting a new line.
  for (const ch of ["a", "Z", "7", "_"]) assert.equal(shouldAskImplicitly(ch), true, ch);
  for (const ch of [" ", "(", '"', ",", ""]) assert.equal(shouldAskImplicitly(ch), false, JSON.stringify(ch));
});

test("a host can add the server's trigger characters", () => {
  assert.equal(shouldAskImplicitly('"', ['"']), true);
  assert.equal(shouldAskImplicitly(" ", ['"']), false);
});

test("completionResult is null for nothing", () => {
  assert.equal(completionResult(doc("x"), { items: [] }, 1), null);
  assert.equal(completionResult(doc("x"), null, 1), null);
});

// --- edits ------------------------------------------------------------------------

test("workspaceEditChanges reads `changes` for our document", () => {
  const d = doc('exists values of setting "x" of client');
  const edit = { changes: { [URI]: [{ range: span([0, 24], [0, 24]), newText: "s" }] } };
  assert.deepEqual(workspaceEditChanges(d, edit, URI), [{ from: 24, to: 24, insert: "s" }]);
});

test("workspaceEditChanges reads `documentChanges` too, and sorts", () => {
  const d = doc("nme of nme");
  const edit = {
    documentChanges: [
      {
        textDocument: { uri: URI, version: 3 },
        edits: [
          { range: span([0, 7], [0, 10]), newText: "name" },
          { range: span([0, 0], [0, 3]), newText: "name" },
        ],
      },
    ],
  };
  assert.deepEqual(workspaceEditChanges(d, edit, URI), [
    { from: 0, to: 3, insert: "name" },
    { from: 7, to: 10, insert: "name" },
  ]);
});

test("workspaceEditChanges ignores other documents", () => {
  const edit = { changes: { "file:///other.rel": [{ range: span([0, 0], [0, 1]), newText: "x" }] } };
  assert.deepEqual(workspaceEditChanges(doc("abc"), edit, URI), []);
});

test("workspaceEditChanges refuses overlapping edits", () => {
  const edit = {
    changes: {
      [URI]: [
        { range: span([0, 0], [0, 3]), newText: "a" },
        { range: span([0, 2], [0, 4]), newText: "b" },
      ],
    },
  };
  assert.throws(() => workspaceEditChanges(doc("abcdef"), edit, URI), /overlap/);
});

test("editTransaction applies to the document it was computed for", () => {
  const state = CM.EditorState.create({ doc: 'exists values of setting "x" of client' });
  const edit = { changes: { [URI]: [{ range: span([0, 24], [0, 24]), newText: "s" }] } };
  const spec = editTransaction(state, edit, URI, state.doc);
  assert.equal(state.update(spec).state.doc.toString(), 'exists values of settings "x" of client');
});

test("editTransaction refuses a document that changed since", () => {
  // Offsets computed for the old text would land in the wrong place.
  const before = CM.EditorState.create({ doc: 'exists values of setting "x" of client' });
  const after = before.update({ changes: { from: 0, insert: "not " } }).state;
  const edit = { changes: { [URI]: [{ range: span([0, 24], [0, 24]), newText: "s" }] } };
  assert.equal(editTransaction(after, edit, URI, before.doc), null);
});

test("editTransaction is null for nothing to do or overlapping edits", () => {
  const state = CM.EditorState.create({ doc: "abcdef" });
  assert.equal(editTransaction(state, { changes: {} }, URI, state.doc), null);
  const overlapping = {
    changes: { [URI]: [{ range: span([0, 0], [0, 3]), newText: "a" }, { range: span([0, 2], [0, 4]), newText: "b" }] },
  };
  assert.equal(editTransaction(state, overlapping, URI, state.doc), null);
});

test("isQuickFix takes plain and hierarchical quickfix kinds that carry an edit", () => {
  const edit = { changes: {} };
  assert.equal(isQuickFix({ kind: "quickfix", edit }), true);
  assert.equal(isQuickFix({ kind: "quickfix.spelling", edit }), true);
  assert.equal(isQuickFix({ edit }), true); // no kind: the server already filtered by `only`
  assert.equal(isQuickFix({ kind: "quickfixer", edit }), false);
  assert.equal(isQuickFix({ kind: "source.fixAll.bigfix-relevance", edit }), false);
  assert.equal(isQuickFix({ kind: "quickfix" }), false); // a command, not an edit
});

test("an applied edit gives the fixed text", () => {
  const state = CM.EditorState.create({ doc: 'exists values of setting "x" of client' });
  const edit = { changes: { [URI]: [{ range: span([0, 24], [0, 24]), newText: "s" }] } };
  const changes = workspaceEditChanges(state.doc, edit, URI);
  assert.equal(state.update({ changes }).state.doc.toString(), 'exists values of settings "x" of client');
});

// --- markdown ---------------------------------------------------------------------

const HOVER =
  "**`operating system`** - inspector\n\n" +
  "- `operating system` -> `operating system`, singular - client: debian, macos\n" +
  "- `x` -> `y`\n\n" +
  "[Client relevance](https://github.com/jgstew/bigfix-relevance-analyzer/blob/main/docs/x.md)";

test("markdownBlocks parses the analyzer's hover text", () => {
  const blocks = markdownBlocks(HOVER);
  assert.deepEqual(
    blocks.map((b) => b.type),
    ["paragraph", "list", "paragraph"],
  );
  const [title] = blocks;
  assert.deepEqual(title.inline[0], { type: "strong", children: [{ type: "code", text: "operating system" }] });
  assert.deepEqual(title.inline[1], { type: "text", text: " - inspector" });
  assert.equal(blocks[1].items.length, 2);
  assert.deepEqual(blocks[2].inline, [
    {
      type: "link",
      href: "https://github.com/jgstew/bigfix-relevance-analyzer/blob/main/docs/x.md",
      children: [{ type: "text", text: "Client relevance" }],
    },
  ]);
});

test("markdownBlocks never makes a link from anything but http(s)", () => {
  const [p] = markdownBlocks("[x](javascript:alert(1)) [y](data:text/html,hi)");
  assert.ok(p.inline.every((node) => node.type === "text"));
});

test("markdownBlocks keeps markup-looking text as text", () => {
  const [p] = markdownBlocks("<img src=x onerror=alert(1)> & `<b>`");
  assert.deepEqual(p.inline, [
    { type: "text", text: "<img src=x onerror=alert(1)> & " },
    { type: "code", text: "<b>" },
  ]);
});

// --- the extension ----------------------------------------------------------------

test("relevanceEditor names what CM lacks", () => {
  assert.throws(() => relevanceEditor({}, { request() {}, onNotification() {} }), /CM lacks/);
});

test("every name the client needs is in this bundle", () => {
  assert.deepEqual(
    REQUIRED_CM.filter((name) => !(name in CM)),
    [],
  );
});

test("the extensions load into a real editor state", () => {
  const transport = { request: async () => null, onNotification() {} };
  const state = CM.EditorState.create({ doc: "now", extensions: relevanceEditor(CM, transport) });
  // The completion source is offered through language data at any position.
  const sources = state.languageDataAt("autocomplete", 0);
  assert.equal(sources.length, 1);
  assert.equal(typeof sources[0], "function");
});
