/* The "翻译为中文" item menu entry, without Zotero: whether the entry is
 * shown, greyed ("（没有 PDF）") or usable for the current selection, and the
 * source-PDF check that settles it while the menu is open.
 *
 * Whether a regular item has a source PDF takes Zotero's attachments
 * (sourcePdf is asynchronous), which the menu cannot wait for: the entry is
 * first shown as usable, then, if the answers say that none of the selected
 * items has a PDF, greyed before the menu closes. Every opening asks afresh;
 * nothing is remembered from an earlier opening, so a PDF added or removed
 * since then shows at the next opening. An answer counts only while its
 * opening is still showing (the popup's popupshowing/popuphidden events, not
 * the menu item's own state) and no newer opening has replaced it.
 */
var ZoteroTranslateItemMenu = (() => {
  const Core = typeof module !== "undefined" ? require("./core.js") : ZoteroTranslateCore;

  /** `sourcePdf(item)`: the item's source PDF, or null (a promise).
   *  `logError(e)`: where a failed check goes (the item then keeps the
   *  optimistic "has one": a failed check must not grey the entry).
   *  Returns {showing({popup, menu, items}), hidden({popup})}. */
  function createItemMenu({ sourcePdf, logError }) {
    let openings = 0;
    // The popups an opening is still showing for (a WeakSet: a popup that
    // was never hidden stays until its window goes).
    const showing = new WeakSet();

    /** One selected item for core.translateMenuItem: {candidate, hasPdf,
     *  known}. `known`: this opening's answers, item ID -> has a PDF. */
    function stateOf(item, known) {
      if (!item.isRegularItem() && !item.isAttachment()) {
        return { candidate: false, hasPdf: false, known: true };
      }
      if (item.isAttachment()) {
        const pdf = Core.pickSourcePdf([{ isPDF: item.isPDFAttachment(), title: item.getField("title") }]);
        return { candidate: true, hasPdf: !!pdf, known: true };
      }
      return known.has(item.id)
        ? { candidate: true, hasPdf: known.get(item.id), known: true }
        : { candidate: true, hasPdf: true, known: false };
    }

    return {
      async showing({ popup, menu, items }) {
        const opening = ++openings;
        showing.add(popup);
        // This opening's answers: item ID -> has a source PDF.
        const known = new Map();
        const apply = () => {
          const { hidden, disabled, label } = Core.translateMenuItem(items.map((item) => stateOf(item, known)));
          menu.hidden = hidden;
          menu.disabled = disabled;
          menu.setAttribute("label", label);
        };
        apply();
        await Promise.all(
          items.map(async (item) => {
            if (stateOf(item, known).known) {
              return;
            }
            try {
              known.set(item.id, !!(await sourcePdf(item)));
            } catch (e) {
              logError(e);
            }
          }),
        );
        // Closed meanwhile, or a newer opening: the answer belongs to neither.
        if (opening !== openings || !showing.has(popup)) {
          return;
        }
        apply();
      },

      hidden({ popup }) {
        showing.delete(popup);
      },
    };
  }

  return { createItemMenu };
})();

if (typeof module !== "undefined") {
  module.exports = ZoteroTranslateItemMenu;
}
