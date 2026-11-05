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
  env.show = (items) => env.controller.showing({ popup: env.popup, menu: env.menu, items });
  env.hide = () => env.controller.hidden({ popup: env.popup });
  return env;
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
