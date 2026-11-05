// Tests for the plugin's item menu wiring of the model menu (zotero-translate.js),
// with a fake DOM: a switch XUL checked itself, and a refused or superseded
// switch must leave the check on the profile the plugin knows is current.
// Run: node --test plugin/test/
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const Core = require("../addon/content/core.js");
const { createModelMenu } = require("../addon/content/model-menu.js");

const GEMINI = { id: "gemini", name: "Gemini", builtin: true, tested: true };
const DEEPSEEK = { id: "0123456789ab", name: "DeepSeek", tested: true };
const listing = (current) => ({ current, profiles: [GEMINI, DEEPSEEK] });

class Element {
  constructor() {
    this.attributes = {};
    this.dataset = {};
    this.children = [];
    this.checked = false;
    this.disabled = false;
    this.hidden = false;
    this.listeners = new Map();
  }

  setAttribute(name, value) {
    this.attributes[name] = value;
    this[name] = value;
  }

  addEventListener(type, fn) {
    this.listeners.set(type, fn);
  }

  replaceChildren(...children) {
    this.children = children;
  }
}

/** The plugin's scope: its own globals plus Zotero's main windows. */
function loadPlugin(windows) {
  const context = vm.createContext({
    ZoteroTranslateCore: Core,
    ZoteroTranslateModelMenu: { createModelMenu },
    Zotero: { getMainWindows: () => windows, logError() {} },
  });
  vm.runInContext(fs.readFileSync(path.join(__dirname, "..", "addon", "content", "zotero-translate.js"), "utf8"), context);
  return context.ZoteroTranslate;
}

/** A main window with the item menu's entry and the "切换模型" popup. */
function fakeWindow(plugin) {
  const itemMenu = new Element();
  const popup = new Element();
  const elements = new Map([
    [plugin.ITEM_MENU_ID, itemMenu],
    [plugin.ITEM_MODEL_POPUP_ID, popup],
  ]);
  return {
    itemMenu,
    popup,
    window: {
      document: {
        getElementById: (id) => elements.get(id) || null,
        createXULElement: () => new Element(),
      },
    },
  };
}

const checked = (popup) => popup.children.filter((element) => element.checked).map((element) => element.attributes.label);

test("a refused switch puts the check back on the current profile even though the list did not change", async () => {
  const windows = [];
  const plugin = loadPlugin(windows);
  const { window, itemMenu, popup } = fakeWindow(plugin);
  windows.push(window);
  const notices = [];
  plugin._tell = (text) => notices.push(text);
  plugin.modelMenu = createModelMenu({
    client: { status: async () => ({ listing: listing(GEMINI.id) }), apply: async () => ({ error: "这份配置还没有通过测试连接" }) },
    onChange: () => plugin._drawModelMenu(window),
  });
  await plugin.modelMenu.refresh();
  assert.deepEqual(checked(popup), ["Gemini"]);
  assert.equal(itemMenu.attributes.label, "翻译为中文（Gemini）");

  // XUL checks the clicked radio itself, before the command handler runs.
  popup.children[0].checked = false;
  popup.children[1].checked = true;
  await plugin.switchToProfile(DEEPSEEK.id);
  assert.deepEqual(checked(popup), ["Gemini"]);
  assert.equal(itemMenu.attributes.label, "翻译为中文（Gemini）");
  assert.deepEqual(notices, ["没有切换模型：这份配置还没有通过测试连接"]);

  // The next query answers with the same list (the redraw's items are unchanged).
  await plugin.modelMenu.refresh();
  assert.deepEqual(checked(popup), ["Gemini"]);
});

test("a switch superseded by a newer one says nothing, even when it fails late", async () => {
  const windows = [];
  const plugin = loadPlugin(windows);
  const { window, popup } = fakeWindow(plugin);
  windows.push(window);
  const notices = [];
  plugin._tell = (text) => notices.push(text);
  const waiting = [];
  plugin.modelMenu = createModelMenu({
    client: {
      status: async () => ({ listing: listing(GEMINI.id) }),
      apply: (id) => new Promise((resolve) => waiting.push({ id, resolve })),
    },
    onChange: () => plugin._drawModelMenu(window),
  });
  await plugin.modelMenu.refresh();

  popup.children[1].checked = true; // the click on DeepSeek, checked by XUL
  const first = plugin.switchToProfile(DEEPSEEK.id);
  const second = plugin.switchToProfile(GEMINI.id);
  popup.children[0].checked = true; // the click on Gemini in another window
  popup.children[1].checked = false;
  waiting[1].resolve({ ok: true }); // the newer switch answers first
  await second;
  waiting[0].resolve({ error: "这份配置还没有通过测试连接" }); // the older one, too late
  await first;
  assert.deepEqual(notices, []);
  assert.deepEqual(checked(popup), ["Gemini"]);
});

test("the item menu's label names the current profile; （没有 PDF） follows it", async () => {
  const windows = [];
  const plugin = loadPlugin(windows);
  const { window, itemMenu } = fakeWindow(plugin);
  windows.push(window);
  plugin.modelMenu = createModelMenu({
    client: { status: async () => ({ listing: listing(DEEPSEEK.id) }), apply: async () => ({ ok: true }) },
    onChange: () => plugin._drawModelMenu(window),
  });
  await plugin.modelMenu.refresh();
  assert.equal(itemMenu.attributes.label, "翻译为中文（DeepSeek）");

  itemMenu.disabled = true; // itemmenu.js found no source PDF in the selection
  plugin._drawModelMenu(window);
  assert.equal(itemMenu.attributes.label, `翻译为中文（DeepSeek）${Core.NO_PDF_SUFFIX}`);
});
