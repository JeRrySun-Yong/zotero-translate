// Tests for the notices of ended tasks still to be seen (notices.js), with
// the window side injected. Run: node --test plugin/test/
const test = require("node:test");
const assert = require("node:assert/strict");
const { createNotices } = require("../addon/content/notices.js");

/** Notices whose windows are records: opened, raised and closed counts. */
function environment({ active = true } = {}) {
  const env = { active, windows: [] };
  env.notices = createNotices({
    isActive: () => env.active,
    open: (payload, dismissed) => {
      const window = { payload, dismissed, raised: 0, closed: 0 };
      env.windows.push(window);
      return { raise: () => window.raised++, close: () => window.closed++ };
    },
  });
  /** [payload, raised, closed] of every window opened so far. */
  env.seen = () => env.windows.map((w) => [w.payload, w.raised, w.closed]);
  return env;
}

test("a notice that comes while Zotero is active opens at once", () => {
  const env = environment();
  env.notices.announce("A");
  assert.deepEqual(env.seen(), [["A", 0, 0]]);
  assert.equal(env.notices.size, 1);
});

test("a notice that comes while Zotero is in the background waits and opens once, on activation", () => {
  const env = environment({ active: false });
  env.notices.announce("A");
  assert.deepEqual(env.seen(), []);
  env.active = true;
  env.notices.activated();
  assert.deepEqual(env.seen(), [["A", 0, 0]]);
});

test("each activation raises the same windows again: nothing is opened again, nothing piles up", () => {
  const env = environment();
  env.notices.announce("A");
  env.notices.announce("B");
  for (let i = 0; i < 50; i++) {
    env.notices.activated();
  }
  assert.deepEqual(env.seen(), [
    ["A", 50, 0],
    ["B", 50, 0],
  ]);
});

test("a dismissed notice (clicked) is no longer raised", () => {
  const env = environment();
  env.notices.announce("A");
  env.notices.announce("B");
  env.windows[0].dismissed();
  env.notices.activated();
  assert.deepEqual(env.seen(), [
    ["A", 0, 0],
    ["B", 1, 0],
  ]);
  assert.equal(env.notices.size, 1);
  env.windows[0].dismissed(); // twice: no harm
  assert.equal(env.notices.size, 1);
});

test("clear (the main window closes, the plugin shuts down) closes every window and forgets waiting notices", () => {
  const env = environment();
  env.notices.announce("A");
  env.active = false;
  env.notices.announce("B"); // waiting
  env.notices.clear();
  assert.deepEqual(env.seen(), [["A", 0, 1]]);
  assert.equal(env.notices.size, 0);
  env.active = true;
  env.notices.activated();
  assert.deepEqual(env.seen(), [["A", 0, 1]]); // nothing opened or raised after clear
});
