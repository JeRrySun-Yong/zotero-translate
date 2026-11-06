// Tests for the plugin's related-item wiring (zotero-translate.js, ticket
// M4-13): linking a standalone PDF and one of its outputs must add the
// relation on both items and save both, with a fake Zotero item store.
// Run: node --test plugin/test/
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const Core = require("../addon/content/core.js");

/** The plugin's scope with a Zotero whose Items.getAsync reads `items` (id -> item). */
function loadPlugin(items) {
  const context = vm.createContext({
    ZoteroTranslateCore: Core,
    Zotero: {
      getMainWindows: () => [],
      logError() {},
      Items: { getAsync: async (id) => items.get(id) || null },
    },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "addon", "content", "zotero-translate.js"), "utf8"), context);
  return context.ZoteroTranslate;
}

/** An item that records addRelatedItem and saveTx calls in `log`. */
function fakeItem(log, id) {
  return {
    id,
    addRelatedItem(other) {
      log.push(["addRelatedItem", id, other.id]);
    },
    async saveTx() {
      log.push(["saveTx", id]);
    },
  };
}

test("relating a standalone PDF and an output adds the relation on both and saves both", async () => {
  const log = [];
  const items = new Map([
    [30, fakeItem(log, 30)],
    [100, fakeItem(log, 100)],
  ]);
  const plugin = loadPlugin(items);

  await plugin._relateWithSource(30, 100);

  const added = log.filter(([call]) => call === "addRelatedItem").map(([, from, to]) => [from, to]);
  const saved = log.filter(([call]) => call === "saveTx").map(([, id]) => id);
  assert.deepEqual(added.sort((a, b) => a[0] - b[0]), [
    [30, 100],
    [100, 30],
  ]);
  assert.deepEqual(saved.sort((a, b) => a - b), [30, 100]);
});

test("relating fails before saving anything when either item is gone", async () => {
  const log = [];
  const items = new Map([[30, fakeItem(log, 30)]]);
  const plugin = loadPlugin(items);

  await assert.rejects(plugin._relateWithSource(30, 999), /找不到原 PDF 附件或译文附件/);
  await assert.rejects(plugin._relateWithSource(999, 30), /找不到原 PDF 附件或译文附件/);
  assert.deepEqual(log, []);
});
