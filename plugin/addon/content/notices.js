/* The notices of ended tasks still to be seen (the bottom-right notices of
 * zotero-translate.js), without Zotero.
 *
 * On macOS a notice window goes behind the main window when it opens while
 * Zotero is in the background, and again whenever Zotero comes back to the
 * front. So a notice that comes while Zotero is in the background waits and
 * opens on the next activation of the main window; and each activation
 * raises the windows already open, the same windows: a notice's window is
 * opened once while its main window stays open. When a main window closes,
 * its notices wait and open in the main window activated next; with no main
 * window open (macOS: Zotero runs on), notices wait for the next one. A
 * notice is forgotten when the user dismisses it (clicks it), and all are
 * closed and forgotten by clear() (the plugin shuts down).
 *
 * The window side comes in as dependencies, given to createNotices():
 *
 * - isActive(): whether a main window is open and Zotero is the active application;
 * - open(payload, dismissed): open the notice's window in the main window;
 *   returns {parent (that main window), raise(), close()}; the window calls
 *   dismissed() when the user clicks it.
 *
 * Zotero loads this file with Services.scriptloader.loadSubScript, which
 * defines ZoteroTranslateNotices in the plugin scope; Node loads it with
 * require() for plugin/test/.
 */
var ZoteroTranslateNotices = (() => {
  function createNotices({ isActive, open }) {
    // payload -> its window ({raise, close}), or null while it waits.
    const entries = new Set();

    function show(entry) {
      entry.window = open(entry.payload, () => entries.delete(entry));
    }

    return {
      get size() {
        return entries.size;
      },

      /** A task has just ended: open its notice now, or on the next activation. */
      announce(payload) {
        const entry = { payload, window: null };
        entries.add(entry);
        if (isActive()) {
          show(entry);
        }
      },

      /** A main window was activated: open the waiting notices, raise the open ones. */
      activated() {
        if (!isActive()) {
          return;
        }
        for (const entry of [...entries]) {
          if (entry.window) {
            entry.window.raise();
          } else {
            show(entry);
          }
        }
      },

      /** A main window closes: the notices whose windows are in it are
       *  closed there and wait, to open again in the main window activated
       *  next (another one still open, or the next one opened). */
      windowClosed(parent) {
        for (const entry of entries) {
          if (entry.window && entry.window.parent === parent) {
            entry.window.close();
            entry.window = null;
          }
        }
      },

      /** Close every notice's window and forget them all. */
      clear() {
        for (const entry of entries) {
          entry.window?.close();
        }
        entries.clear();
      },
    };
  }

  /** The main window to act in (opening a result): the one open; else open
   *  one and return it once its panes are ready (`isReady(window)`), checked
   *  every `delay()` up to `attempts` times; null when it never is. */
  async function readyMainWindow({ mainWindow, openMainWindow, isReady, delay, attempts = 100 }) {
    let window = mainWindow();
    if (window) {
      return window;
    }
    openMainWindow();
    for (let i = 0; i < attempts; i++) {
      window = mainWindow();
      if (window && isReady(window)) {
        return window;
      }
      await delay();
    }
    return null;
  }

  return { createNotices, readyMainWindow };
})();

if (typeof module !== "undefined") {
  module.exports = ZoteroTranslateNotices;
}
