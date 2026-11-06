// Tests for the notices of ended tasks still to be seen (notices.js), with
// the window side injected. Run: node --test plugin/test/
const test = require("node:test");
const assert = require("node:assert/strict");
const { createNotices, readyMainWindow } = require("../addon/content/notices.js");

/** Notices whose windows are records: opened, raised and closed counts, and
 *  the main window each opened in (`main`, the active one; null: none open). */
function environment({ active = true } = {}) {
  const env = { active, main: "W1", windows: [] };
  env.notices = createNotices({
    isActive: () => env.active && env.main != null,
    open: (payload, dismissed) => {
      const window = { payload, dismissed, parent: env.main, raised: 0, closed: 0 };
      env.windows.push(window);
      return { parent: env.main, raise: () => window.raised++, close: () => window.closed++ };
    },
  });
  /** [payload, raised, closed] of every window opened so far. */
  env.seen = () => env.windows.map((w) => [w.payload, w.raised, w.closed]);
  /** [payload, parent, closed] of every window opened so far. */
  env.opened = () => env.windows.map((w) => [w.payload, w.parent, w.closed]);
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

test("with two main windows, closing one keeps every notice: its own wait and open again in the other on activation", () => {
  const env = environment();
  env.notices.announce("A"); // in W1
  env.main = "W2";
  env.notices.announce("B"); // in W2
  env.notices.windowClosed("W1");
  assert.deepEqual(env.opened(), [
    ["A", "W1", 1],
    ["B", "W2", 0],
  ]);
  assert.equal(env.notices.size, 2);
  env.notices.activated(); // W2 is activated
  assert.deepEqual(env.opened(), [
    ["A", "W1", 1],
    ["B", "W2", 0],
    ["A", "W2", 0],
  ]);
  assert.deepEqual(env.seen()[1], ["B", 1, 0]); // raised, not opened again
});

test("when the last main window closes, the notices wait and open in the next main window", () => {
  const env = environment();
  env.notices.announce("A");
  env.notices.windowClosed("W1");
  env.main = null;
  env.notices.activated(); // (no main window: nothing can open)
  assert.deepEqual(env.opened(), [["A", "W1", 1]]);
  env.main = "W3";
  env.notices.activated();
  assert.deepEqual(env.opened(), [
    ["A", "W1", 1],
    ["A", "W3", 0],
  ]);
  assert.equal(env.notices.size, 1);
});

test("a task that ends while no main window is open: its notice waits for the next main window", () => {
  const env = environment();
  env.main = null; // Zotero runs on (macOS) with every main window closed
  env.notices.announce("A");
  assert.deepEqual(env.opened(), []);
  env.main = "W2";
  env.notices.activated();
  assert.deepEqual(env.opened(), [["A", "W2", 0]]);
});

test("a dismissed notice is not opened again when its window's main window closes", () => {
  const env = environment();
  env.notices.announce("A");
  env.windows[0].dismissed();
  env.notices.windowClosed("W1");
  env.main = "W2";
  env.notices.activated();
  assert.deepEqual(env.opened(), [["A", "W1", 0]]);
  assert.equal(env.notices.size, 0);
});

test("the main window to act in: the open one; else a new one once it is ready; else null", async () => {
  const opened = [];
  const delay = async () => {};
  const open1 = await readyMainWindow({ mainWindow: () => "W1", openMainWindow: () => opened.push(1), isReady: () => true, delay });
  assert.deepEqual([open1, opened], ["W1", []]);

  let main = null;
  let checks = 0;
  const ready = await readyMainWindow({
    mainWindow: () => main,
    openMainWindow: () => {
      opened.push(2);
      main = "W2";
    },
    isReady: () => ++checks >= 3, // its panes load a little later
    delay,
  });
  assert.deepEqual([ready, opened], ["W2", [2]]);

  const never = await readyMainWindow({ mainWindow: () => null, openMainWindow: () => opened.push(3), isReady: () => false, delay });
  assert.deepEqual([never, opened], [null, [2, 3]]);
});

test("clear (the plugin shuts down) closes every window and forgets waiting notices", () => {
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
