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
 * opening is still showing (the popup's own popupshowing/popuphidden events,
 * not the menu item's own state) and no newer opening has replaced it; an
 * answer that belongs to items the selection has left is not used: the
 * current selection is checked in its place.
 *
 * A popup's submenus' popupshowing/popuphidden events bubble up to it, with
 * event.target their own popup: they must not open or close the item menu's
 * opening (addToPopup), or a submenu opened and closed while the check is
 * under way would throw the check's answer away.
 */
var ZoteroTranslateItemMenu = (() => {
  const Core = typeof module !== "undefined" ? require("./core.js") : ZoteroTranslateCore;

  // How many times one opening re-checks when the selection changes under the
  // open menu; after that no answer of it is used (the menu keeps its
  // optimistic state).
  const MAX_SELECTION_CHANGES = 3;

  /** `sourcePdf(item)`: the item's source PDF, or null (a promise).
   *  `logError(e)`: where a failed check goes (the item then keeps the
   *  optimistic "has one": a failed check must not grey the entry).
   *  Returns {showing({popup, menu, items, selection}), hidden({popup}),
   *  addToPopup(popup, {menu, selection})}. */
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

    /** Whether two selections are the same items, in the same order. */
    function sameSelection(a, b) {
      return a.length === b.length && a.every((item, i) => item.id === b[i].id);
    }

    /** Whether the event is the popup's own: a submenu's popupshowing and
     *  popuphidden bubble up to the outer popup with event.target their own
     *  popup, and are not the outer menu opening or closing. */
    function ownEvent(event) {
      return event.target === event.currentTarget;
    }

    const controller = {
      /** One opening: show the entry for `items` at once, check the source
       *  PDFs, and settle it with the answers while this opening is still
       *  showing. `selection()`, when given, is asked again after the checks:
       *  an answer about a selection that has changed in the meantime is not
       *  used; the current selection is checked in its place. `baseLabel()`,
       *  when given, is the entry's label without the state suffix (the
       *  current model profile's name). */
      async showing({ popup, menu, items, selection = null, baseLabel = null }) {
        const opening = ++openings;
        showing.add(popup);
        let selected = items;
        for (let round = 0; ; round++) {
          // This round's answers: item ID -> has a source PDF.
          const known = new Map();
          const apply = () => {
            const { hidden, disabled, label } = Core.translateMenuItem(
              selected.map((item) => stateOf(item, known)),
              baseLabel ? baseLabel() : undefined,
            );
            menu.hidden = hidden;
            menu.disabled = disabled;
            menu.setAttribute("label", label);
          };
          apply();
          await Promise.all(
            selected.map(async (item) => {
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
          const current = selection ? selection() : selected;
          if (sameSelection(selected, current)) {
            apply();
            return;
          }
          if (round >= MAX_SELECTION_CHANGES) {
            return; // the selection keeps changing: no answer belongs to it
          }
          selected = current; // it changed under the open menu: check the current one
        }
      },

      hidden({ popup }) {
        showing.delete(popup);
      },

      /** Wire a popup's own popupshowing/popuphidden events: `menu()` is the
       *  entry to show for the selection, `selection()` the selection, and
       *  `label()` the entry's label without the state suffix. A submenu's
       *  events (bubbling up from inside the popup) are ignored. Returns a
       *  function that removes the listeners. */
      addToPopup(popup, { menu, selection, label = null }) {
        const onShowing = (event) => {
          if (!ownEvent(event)) {
            return;
          }
          controller.showing({ popup, menu: menu(), items: selection(), selection, baseLabel: label }).catch((e) => logError(e));
        };
        const onHidden = (event) => {
          if (!ownEvent(event)) {
            return;
          }
          controller.hidden({ popup });
        };
        popup.addEventListener("popupshowing", onShowing);
        popup.addEventListener("popuphidden", onHidden);
        return () => {
          popup.removeEventListener("popupshowing", onShowing);
          popup.removeEventListener("popuphidden", onHidden);
        };
      },
    };
    return controller;
  }

  return { createItemMenu };
})();

if (typeof module !== "undefined") {
  module.exports = ZoteroTranslateItemMenu;
}
