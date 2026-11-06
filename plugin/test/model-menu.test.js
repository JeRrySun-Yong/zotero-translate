// Tests for the item menu's model-profile state, with the profile client
// injected. Run: node --test plugin/test/
const test = require("node:test");
const assert = require("node:assert/strict");
const { createModelMenu } = require("../addon/content/model-menu.js");
const { itemMenuLabel, modelMenuItems } = require("../addon/content/core.js");

const GEMINI = { id: "gemini", name: "Gemini", builtin: true, tested: true };
const DEEPSEEK = { id: "0123456789ab", name: "DeepSeek", tested: true };
const listing = (current) => ({ current, profiles: [GEMINI, DEEPSEEK] });

/** A client whose status() answers the test hands out, in its own time, and
 *  whose apply() answers at once (or fails when the test says so). */
function heldClient() {
  const waiting = [];
  const client = {
    applied: [],
    failing: null,
    status: () => new Promise((resolve) => waiting.push(resolve)),
    apply: async (id) => {
      client.applied.push(id);
      return client.failing ? { error: client.failing } : { ok: true };
    },
    answer: (index, found) => waiting[index](found),
  };
  return client;
}

test("before the service answers, the item menu has no profile name and the submenu one grey item", () => {
  const menu = createModelMenu({ client: heldClient() });
  const { listing, problem } = menu.state();
  assert.equal(listing, null);
  assert.deepEqual(modelMenuItems(listing, problem), [
    { id: null, label: "正在连接翻译服务…", checked: false, disabled: true },
  ]);
  assert.equal(itemMenuLabel(""), "翻译为中文");
});

test("a late answer to an earlier profile request does not overwrite a newer one", async () => {
  const client = heldClient();
  const menu = createModelMenu({ client });
  const first = menu.refresh();
  const second = menu.refresh();
  client.answer(1, { listing: listing(DEEPSEEK.id) }); // the newest request answers first
  assert.equal(await second, true);
  client.answer(0, { listing: listing(GEMINI.id) }); // the older one answers last
  assert.equal(await first, false);
  assert.equal(menu.state().listing.current, DEEPSEEK.id);
});

test("an answer that was already on its way when the user switched does not undo the switch", async () => {
  const client = heldClient();
  const menu = createModelMenu({ client });
  const warm = menu.refresh();
  client.answer(0, { listing: listing(GEMINI.id) });
  await warm;
  assert.equal(menu.state().listing.current, GEMINI.id);

  const late = menu.refresh(); // asked before the click, answered after it
  assert.deepEqual(await menu.switchTo(DEEPSEEK.id), { ok: true });
  client.answer(1, { listing: listing(GEMINI.id) });
  assert.equal(await late, false);

  const { listing: shown } = menu.state();
  assert.equal(shown.current, DEEPSEEK.id);
  const items = modelMenuItems(shown);
  assert.deepEqual(items.map((item) => [item.label, item.checked]), [
    ["Gemini", false],
    ["DeepSeek", true],
  ]);
  assert.equal(itemMenuLabel(items.find((item) => item.checked).label), "翻译为中文（DeepSeek）");
});

test("answers asked while the switch was under way do not undo it either", async () => {
  const client = heldClient();
  const menu = createModelMenu({ client });
  const warm = menu.refresh();
  client.answer(0, { listing: listing(GEMINI.id) });
  await warm;

  let finishApply;
  client.apply = (id) =>
    new Promise((resolve) => {
      finishApply = () => {
        client.applied.push(id);
        resolve({ ok: true });
      };
    });
  const switching = menu.switchTo(DEEPSEEK.id);
  const during = menu.refresh(); // the menu opened again while the switch was on its way
  finishApply();
  await switching;
  client.answer(1, { listing: listing(GEMINI.id) }); // the service had not applied it yet
  assert.equal(await during, false);
  assert.equal(menu.state().listing.current, DEEPSEEK.id);
});

test("two switches whose answers arrive out of order: the last click decides", async () => {
  const client = heldClient();
  const menu = createModelMenu({ client });
  const warm = menu.refresh();
  client.answer(0, { listing: listing(GEMINI.id) });
  await warm;
  assert.equal(menu.state().listing.current, GEMINI.id);

  const waiting = [];
  client.apply = (id) => new Promise((resolve) => waiting.push({ id, resolve }));
  const first = menu.switchTo(DEEPSEEK.id);
  const second = menu.switchTo(GEMINI.id);
  // The service applied DeepSeek and then Gemini; only the answers arrive reversed.
  waiting[1].resolve({ ok: true });
  assert.deepEqual(await second, { ok: true });
  waiting[0].resolve({ ok: true }); // the older click's answer, too late
  assert.deepEqual(await first, { ok: true, superseded: true });
  assert.equal(menu.state().listing.current, GEMINI.id);
});

test("a refused switch reports the reason, draws no change, and keeps the new answers counting", async () => {
  const client = heldClient();
  client.failing = "这份配置还没有通过测试连接";
  const menu = createModelMenu({ client });
  const warm = menu.refresh();
  client.answer(0, { listing: listing(GEMINI.id) });
  await warm;
  assert.deepEqual(await menu.switchTo(DEEPSEEK.id), { error: client.failing });
  assert.equal(menu.state().listing.current, GEMINI.id);

  const again = menu.refresh();
  client.answer(1, { listing: listing(DEEPSEEK.id) });
  assert.equal(await again, true); // a refused switch does not make later answers stale
  assert.equal(menu.state().listing.current, DEEPSEEK.id);
});

test("a service address that is not this computer shows as that, not as an unreachable service (ticket 16)", async () => {
  const client = heldClient();
  const menu = createModelMenu({ client });
  const asking = menu.refresh();
  client.answer(0, { error: "翻译服务地址不是本机（https://example.com），不发送配置和密钥", serviceUrl: "https://example.com" });
  await asking;
  const { listing, problem } = menu.state();
  assert.equal(listing, null);
  assert.equal(problem, "翻译服务地址不是本机");
  assert.deepEqual(modelMenuItems(listing, problem), [{ id: null, label: "翻译服务地址不是本机", checked: false, disabled: true }]);
});
