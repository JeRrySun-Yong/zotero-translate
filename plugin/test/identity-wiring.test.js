// Tests for the plugin's use of its identity file (attachments.json) in
// zotero-translate.js (ticket M4-16 items 4 and 5): a renamed output still
// counts as a translation, and a standalone PDF's outputs record the PDF as
// their source. The Zotero side is a fake item store and a fake file system,
// as in related-items-wiring.test.js. Run: node --test plugin/test/
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const Core = require("../addon/content/core.js");

const DATA_DIR = "/zotero-data";
const IDENTITY_FILE = `${DATA_DIR}/zotero-translate/attachments.json`;

/** An attachment or regular item for the fake store: `fields` are extras. */
function item(id, libraryID, key, fields = {}) {
  const { title = "", attachments = [], parent = null, pdf = true, regular = false, deleted = false, related = [], filename = `${key}.pdf` } = fields;
  return {
    id,
    libraryID,
    key,
    deleted,
    parentItem: parent,
    relatedItems: related,
    attachmentFilename: filename,
    getField: (name) => (name === "title" ? title : ""),
    getDisplayTitle: () => title,
    getAttachments: () => attachments,
    isRegularItem: () => regular,
    isAttachment: () => !regular,
    isPDFAttachment: () => pdf,
    getBestAttachments: async () => attachments.map((attachmentID) => items.get(attachmentID)),
  };
}

let items; // id -> item; reset by `world`

/** The plugin's scope over `entries` (the identity file's content; null: no file;
 *  a string: an unreadable file) and `list` (items); returns {plugin, written, errors}. */
function world(list, entries = null) {
  items = new Map(list.map((i) => [i.id, i]));
  const files = new Map();
  const written = [];
  const errors = [];
  if (entries !== null) {
    files.set(IDENTITY_FILE, entries);
  }
  const context = vm.createContext({
    ZoteroTranslateCore: Core,
    PathUtils: { join: (...parts) => parts.join("/"), parent: (p) => p.slice(0, p.lastIndexOf("/")) },
    IOUtils: {
      exists: async (p) => files.has(p),
      readJSON: async (p) => {
        if (typeof files.get(p) === "string") {
          throw new SyntaxError("JSON.parse: unexpected character");
        }
        return JSON.parse(JSON.stringify(files.get(p)));
      },
      makeDirectory: async () => {},
      writeJSON: async (p, value) => {
        written.push([p, JSON.parse(JSON.stringify(value))]);
        files.set(p, value);
      },
    },
    Zotero: {
      DataDirectory: { dir: DATA_DIR },
      getMainWindows: () => [],
      logError: (e) => errors.push(e.message || String(e)),
      Items: {
        getAsync: async (idOrIds) => (Array.isArray(idOrIds) ? idOrIds.map((id) => items.get(id)).filter(Boolean) : items.get(idOrIds) || null),
        getByLibraryAndKeyAsync: async (libraryID, key) =>
          [...items.values()].find((i) => i.libraryID === libraryID && i.key === key) || null,
      },
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "addon", "content", "zotero-translate.js"), "utf8"), context);
  const plugin = context.ZoteroTranslate;
  plugin.lifecycle = { wasTranslated: () => false };
  return { plugin, written, errors };
}

// An identity file: the plugin attached items 100 and 101 as the outputs of task t1.
const ENTRIES = {
  "t1/mono": { libraryID: 1, key: "MONO0001" },
  "t1/dual": { libraryID: 1, key: "DUAL0001" },
};

test("a renamed output among a parent item's attachments still makes the paper translated", async () => {
  const { plugin } = world(
    [
      item(10, 1, "PARENT01", { regular: true, attachments: [11, 100] }),
      item(11, 1, "SRC00001", { title: "Full Text PDF", parent: 10 }),
      item(100, 1, "MONO0001", { title: "AutoDesign 中文版", parent: 10 }),
    ],
    ENTRIES,
  );
  assert.equal(await plugin._hasTranslation(items.get(10)), true);
});

test("a parent item's attachment that is neither titled as an output nor recorded does not make it translated", async () => {
  const { plugin } = world(
    [
      item(10, 1, "PARENT01", { regular: true, attachments: [11, 12] }),
      item(11, 1, "SRC00001", { title: "Full Text PDF", parent: 10 }),
      item(12, 1, "OTHER001", { title: "我自己翻的", parent: 10 }),
    ],
    ENTRIES,
  );
  assert.equal(await plugin._hasTranslation(items.get(10)), false);
});

test("a renamed output in the trash does not make the paper translated; a fixed title still does (as before)", async () => {
  const { plugin } = world(
    [
      item(10, 1, "PARENT01", { regular: true, attachments: [100] }),
      item(100, 1, "MONO0001", { title: "AutoDesign 中文版", parent: 10, deleted: true }),
    ],
    ENTRIES,
  );
  assert.equal(await plugin._hasTranslation(items.get(10)), false);
  const titled = world([
    item(10, 1, "PARENT01", { regular: true, attachments: [100] }),
    item(100, 1, "WHATEVER", { title: "中文译文", parent: 10 }),
  ]);
  assert.equal(await titled.plugin._hasTranslation(items.get(10)), true); // no identity file at all
});

test("a standalone PDF's related renamed output is reported as identified; others are not", async () => {
  const { plugin } = world(
    [
      item(30, 1, "STANDALO", { title: "Standalone", related: ["MONO0001", "OTHER001", "GONE0001"] }),
      item(100, 1, "MONO0001", { title: "改过名的译文" }),
      item(101, 1, "OTHER001", { title: "随手关联的论文", deleted: true }),
    ],
    ENTRIES,
  );
  assert.deepEqual(JSON.parse(JSON.stringify(await plugin._relatedItems(items.get(30)))), [
    { title: "改过名的译文", deleted: false, identified: true },
    { title: "随手关联的论文", deleted: true, identified: false },
  ]);
});

test("a renamed output is not picked as the source PDF; the real source is", async () => {
  const { plugin } = world(
    [
      item(10, 1, "PARENT01", { regular: true, attachments: [100, 11] }),
      item(11, 1, "SRC00001", { title: "Full Text PDF", parent: 10 }),
      item(100, 1, "MONO0001", { title: "AutoDesign 中文版", parent: 10 }),
    ],
    ENTRIES,
  );
  assert.equal((await plugin.sourcePdf(items.get(10))).id, 11);
  // Selected on its own, the renamed output is no source either.
  assert.equal(await plugin.sourcePdf(items.get(100)), null);
  assert.equal((await plugin.sourcePdf(items.get(11))).id, 11);
});

test("a missing or unreadable identity file breaks neither the menu nor a submission: it is logged and treated as empty", async () => {
  for (const entries of [null, "not json"]) {
    const { plugin, errors } = world(
      [
        item(10, 1, "PARENT01", { regular: true, attachments: [100, 11] }),
        item(11, 1, "SRC00001", { title: "Full Text PDF", parent: 10 }),
        item(100, 1, "MONO0001", { title: "AutoDesign 中文版", parent: 10 }),
      ],
      entries,
    );
    assert.equal(await plugin._hasTranslation(items.get(10)), false);
    assert.equal((await plugin.sourcePdf(items.get(10))).id, 100); // nothing says it is an output
    assert.equal(errors.length, entries === null ? 0 : 2);
  }
});

// ---- item 5: the identity file records the source of a standalone PDF's outputs ----

test("marking an output of a standalone PDF records the PDF's key as its source", async () => {
  const { plugin, written } = world([item(30, 1, "STANDALO", { title: "Standalone" }), item(100, 1, "MONO0001", { title: "中文译文" })], {});
  await plugin.attachmentIdentities.mark(100, { taskId: "t1", output: "mono", sourceItemID: 30 });
  assert.deepEqual(written, [[IDENTITY_FILE, { "t1/mono": { libraryID: 1, key: "MONO0001", source: "STANDALO" } }]]);
});

test("marking an output of a paper with a parent item records no source", async () => {
  const { plugin, written } = world([item(100, 1, "MONO0001", { title: "中文译文" })], {});
  await plugin.attachmentIdentities.mark(100, { taskId: "t1", output: "mono" });
  assert.deepEqual(written, [[IDENTITY_FILE, { "t1/mono": { libraryID: 1, key: "MONO0001" } }]]);
});

test("marking keeps the other entries, and a source whose item is gone is left out", async () => {
  const { plugin, written } = world([item(100, 1, "MONO0001", { title: "中文译文" })], { "t0/mono": { libraryID: 1, key: "OLD00001", source: "STANDALO" } });
  await plugin.attachmentIdentities.mark(100, { taskId: "t1", output: "mono", sourceItemID: 999 });
  assert.deepEqual(written[0][1], {
    "t0/mono": { libraryID: 1, key: "OLD00001", source: "STANDALO" },
    "t1/mono": { libraryID: 1, key: "MONO0001" },
  });
});

const RECORDED = {
  "t1/mono": { libraryID: 1, key: "MONO0001", source: "STANDALO" },
  "t1/dual": { libraryID: 1, key: "DUAL0001", source: "STANDALO" },
};

test("a standalone PDF with a recorded, undeleted output is translated, with no relation and no task record", async () => {
  const { plugin } = world(
    [
      item(30, 1, "STANDALO", { title: "Standalone" }),
      item(100, 1, "MONO0001", { title: "Standalone 译" }),
      item(101, 1, "DUAL0001", { title: "Standalone 对照", deleted: true }),
    ],
    RECORDED,
  );
  const source = await plugin._sourceOf(items.get(30));
  assert.equal(source.translated, true); // one undeleted output is enough
  assert.equal(source.sourceItemID, 30);
});

test("outputs in the trash, or recorded for another PDF, leave the standalone PDF untranslated", async () => {
  const trashed = world(
    [item(30, 1, "STANDALO", { title: "Standalone" }), item(100, 1, "MONO0001", { deleted: true }), item(101, 1, "DUAL0001", { deleted: true })],
    RECORDED,
  );
  assert.equal((await trashed.plugin._sourceOf(items.get(30))).translated, false);

  const gone = world([item(30, 1, "STANDALO", { title: "Standalone" })], RECORDED); // the outputs were deleted for good
  assert.equal((await gone.plugin._sourceOf(items.get(30))).translated, false);

  const another = world(
    [item(30, 1, "STANDALO", { title: "Standalone" }), item(31, 1, "ANOTHER1", { title: "Another" }), item(100, 1, "MONO0001", {})],
    RECORDED,
  );
  assert.equal((await another.plugin._sourceOf(items.get(31))).translated, false);

  const otherLibrary = world([item(30, 2, "STANDALO", { title: "Standalone" }), item(100, 1, "MONO0001", {})], RECORDED);
  assert.equal((await otherLibrary.plugin._sourceOf(items.get(30))).translated, false);
});

test("an entry from before the source was recorded still works by the relation", async () => {
  const old = world(
    [item(30, 1, "STANDALO", { title: "Standalone", related: ["MONO0001"] }), item(100, 1, "MONO0001", { title: "改过名的译文" })],
    ENTRIES, // no `source` anywhere
  );
  assert.equal((await old.plugin._sourceOf(items.get(30))).translated, true);
  const unrelated = world([item(30, 1, "STANDALO", { title: "Standalone" }), item(100, 1, "MONO0001", { title: "改过名的译文" })], ENTRIES);
  assert.equal((await unrelated.plugin._sourceOf(items.get(30))).translated, false);
});

test("a paper with a parent item ignores the recorded sources", async () => {
  const { plugin } = world(
    [
      item(10, 1, "PARENT01", { regular: true, attachments: [11] }),
      item(11, 1, "SRC00001", { title: "Full Text PDF", parent: 10 }),
      item(100, 1, "MONO0001", { title: "x" }),
    ],
    { "t1/mono": { libraryID: 1, key: "MONO0001", source: "SRC00001" } },
  );
  items.get(11).parentItem = items.get(10);
  assert.equal((await plugin._sourceOf(items.get(10))).translated, false);
});
