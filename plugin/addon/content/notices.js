/* The notices of ended tasks still to be seen (the bottom-right notices of
 * zotero-translate.js), without Zotero.
 *
 * On macOS a notice window goes behind the main window when it opens while
 * Zotero is in the background, and again whenever Zotero comes back to the
 * front. So a notice that comes while Zotero is in the background waits and
 * opens on the next activation of the main window; and each activation
 * raises the windows already open, the same windows: a notice's window is
 * opened once, never again. A notice is forgotten when the user dismisses it
 * (clicks it), and all are closed and forgotten by clear() (the main window
 * closes, or the plugin shuts down).
 *
 * The window side comes in as dependencies, given to createNotices():
 *
 * - isActive(): whether Zotero is the active application;
 * - open(payload, dismissed): open the notice's window; returns {raise(),
 *   close()}; the window calls dismissed() when the user clicks it.
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

      /** The main window was activated: open the waiting notices, raise the open ones. */
      activated() {
        for (const entry of [...entries]) {
          if (entry.window) {
            entry.window.raise();
          } else {
            show(entry);
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

  return { createNotices };
})();

if (typeof module !== "undefined") {
  module.exports = ZoteroTranslateNotices;
}
