// Tests for the "翻译为中文" item menu entry (itemmenu.js), with the source
// PDF check injected. Run: node --test plugin/test/
const test = require("node:test");
const assert = require("node:assert/strict");
const { createItemMenu } = require("../addon/content/itemmenu.js");

const regular = (id) => ({ id, isRegularItem: () => true, isAttachment: () => false });
const attachment = (id, isPDF, title) => ({
  id,
  isRegularItem: () => false,
  isAttachment: () => true,
  isPDFAttachment: () => isPDF,
  getField: () => title,
});
const note = (id) => ({ id, isRegularItem: () => false, isAttachment: () => false });

const POPUP = {};
const menuOf = () => ({
  hidden: null,
  disabled: null,
  label: null,
  setAttribute(name, value) {
    this[name] = value;
  },
});
const stateOf = (menu) => ({ hidden: menu.hidden, disabled: menu.disabled, label: menu.label });

/** A menu whose source check answers from `pdfs` (item ID -> has one) and
 *  records the items it was asked about. `sourcePdf` replaces that answer
 *  (a promise, for answers that come later). */
function environment({ pdfs = new Map(), sourcePdf = null } = {}) {
  const env = { pdfs, asked: [], menu: menuOf(), popup: POPUP };
  env.controller = createItemMenu({
    sourcePdf:
      sourcePdf ||
      (async (item) => {
        env.asked.push(item.id);
        return env.pdfs.get(item.id) ? { id: 1 } : null;
      }),
    logError: (e) => {
      throw e;
    },
  });
  env.show = (items, selection = null) => env.controller.showing({ popup: env.popup, menu: env.menu, items, selection });
  env.hide = () => env.controller.hidden({ popup: env.popup });
  return env;
}

/** Let every pending promise and I/O callback run. */
async function settle() {
  for (let i = 0; i < 10; i++) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

/** A popup stand-in: addEventListener/removeEventListener as in the DOM, and
 *  dispatch(type, target), which runs the listeners with `currentTarget` the
 *  popup, the way a bubbling event does. */
function fakePopup() {
  const listeners = new Map();
  return {
    addEventListener(type, fn) {
      if (!listeners.has(type)) {
        listeners.set(type, new Set());
      }
      listeners.get(type).add(fn);
    },
    removeEventListener(type, fn) {
      listeners.get(type)?.delete(fn);
    },
    dispatch(type, target) {
      for (const fn of [...(listeners.get(type) || [])]) {
        fn({ type, target, currentTarget: this });
      }
    },
  };
}

test("the first opening greys the entry once its source check answers, with no second opening", async () => {
  const env = environment();
  const opening = env.show([regular(10)]);
  // Before the answer the entry is usable: the check is asynchronous.
  assert.deepEqual(stateOf(env.menu), { hidden: false, disabled: false, label: "翻译为中文" });
  await opening;
  assert.deepEqual(stateOf(env.menu), { hidden: false, disabled: true, label: "翻译为中文（没有 PDF）" });
  assert.deepEqual(env.asked, [10]);
});

test("every opening checks afresh: a PDF added or removed shows at the next opening", async () => {
  const env = environment();

  await env.show([regular(10)]);
  assert.equal(env.menu.disabled, true);

  env.pdfs.set(10, true);
  await env.show([regular(10)]);
  assert.deepEqual(stateOf(env.menu), { hidden: false, disabled: false, label: "翻译为中文" });

  env.pdfs.delete(10);
  await env.show([regular(10)]);
  assert.deepEqual(stateOf(env.menu), { hidden: false, disabled: true, label: "翻译为中文（没有 PDF）" });
  assert.deepEqual(env.asked, [10, 10, 10]);
});

test("an answer that comes after the menu closed leaves it alone", async () => {
  let answer;
  const env = environment({ sourcePdf: () => new Promise((resolve) => (answer = resolve)) });
  const opening = env.show([regular(10)]);
  assert.equal(env.menu.disabled, false);

  env.hide();
  answer(null);
  await opening;
  assert.equal(env.menu.disabled, false);
});

test("an older opening's answer does not overwrite what a newer opening set", async () => {
  const answers = [];
  const env = environment({ sourcePdf: () => new Promise((resolve) => answers.push(resolve)) });
  const first = env.show([regular(10)]);
  const second = env.show([regular(10)]);

  answers[1]({ id: 1 }); // the newer opening: the item has a PDF
  await second;
  assert.equal(env.menu.disabled, false);

  answers[0](null); // the older opening: it did not
  await first;
  assert.equal(env.menu.disabled, false);
});

test("a PDF attachment is usable, a non-PDF attachment is greyed, a note is hidden; none is checked", async () => {
  const env = environment();

  await env.show([attachment(20, true, "Full Text PDF")]);
  assert.deepEqual(stateOf(env.menu), { hidden: false, disabled: false, label: "翻译为中文" });

  await env.show([attachment(21, false, "Snapshot")]);
  assert.deepEqual(stateOf(env.menu), { hidden: false, disabled: true, label: "翻译为中文（没有 PDF）" });

  await env.show([note(22)]);
  assert.deepEqual(stateOf(env.menu), { hidden: true, disabled: true, label: "翻译为中文" });

  assert.deepEqual(env.asked, []);
});

test("one item with a PDF keeps the entry usable while the others are checked", async () => {
  const env = environment();
  env.pdfs.set(11, true);
  await env.show([regular(10), regular(11)]);
  assert.deepEqual(stateOf(env.menu), { hidden: false, disabled: false, label: "翻译为中文" });
  assert.deepEqual(env.asked, [10, 11]);
});

test("a submenu's popupshowing and popuphidden are not the item menu's own; its check still settles", async () => {
  let answer;
  const env = environment({
    sourcePdf: (item) => {
      env.asked.push(item.id);
      return new Promise((resolve) => (answer = resolve));
    },
  });
  const popup = fakePopup();
  const submenu = { id: "a submenu popup" };
  const detach = env.controller.addToPopup(popup, { menu: () => env.menu, selection: () => [regular(10)] });

  popup.dispatch("popupshowing", popup); // the item menu opens
  assert.deepEqual(stateOf(env.menu), { hidden: false, disabled: false, label: "翻译为中文" });
  popup.dispatch("popupshowing", submenu); // a submenu of it opens, while the check is under way
  popup.dispatch("popuphidden", submenu); // and closes again
  assert.deepEqual(env.asked, [10]); // neither is a new opening of the item menu

  answer(null);
  await settle();
  assert.deepEqual(stateOf(env.menu), { hidden: false, disabled: true, label: "翻译为中文（没有 PDF）" });

  detach();
  popup.dispatch("popupshowing", popup);
  assert.deepEqual(env.asked, [10]); // the listeners are gone
});

test("an answer is not used when the selection changed under the open menu: a no-PDF item gives way to one with a PDF", async () => {
  const waiting = [];
  const env = environment({
    sourcePdf: (item) => {
      env.asked.push(item.id);
      return new Promise((resolve) => waiting.push({ id: item.id, resolve }));
    },
  });
  let selection = [regular(10)];
  const opening = env.show(selection, () => selection);

  selection = [regular(11)]; // the menu does not open again
  waiting[0].resolve(null); // A has no PDF; B must not be greyed by that
  await settle();
  assert.equal(env.menu.disabled, false);
  assert.deepEqual(env.asked, [10, 11]); // the current selection is checked instead

  waiting[1].resolve({ id: 1 }); // B has one
  await opening;
  assert.deepEqual(stateOf(env.menu), { hidden: false, disabled: false, label: "翻译为中文" });
});

test("an answer is not used when the selection changed under the open menu: a PDF on the old item does not keep the new one usable", async () => {
  const waiting = [];
  const env = environment({
    sourcePdf: (item) => {
      env.asked.push(item.id);
      return new Promise((resolve) => waiting.push({ id: item.id, resolve }));
    },
  });
  let selection = [regular(10)];
  const opening = env.show(selection, () => selection);

  selection = [regular(11)];
  waiting[0].resolve({ id: 1 }); // A has a PDF; B must still be checked
  await settle();
  assert.deepEqual(env.asked, [10, 11]);

  waiting[1].resolve(null);
  await opening;
  assert.deepEqual(stateOf(env.menu), { hidden: false, disabled: true, label: "翻译为中文（没有 PDF）" });
});
