/* The item menu's model-profile state (M4 ticket 05), without Zotero: what
 * the service last said about its model profiles, and the "切换模型"
 * switch that makes one current.
 *
 * The menu draws from this state at every opening, then asks the service
 * again (refresh); an answer only counts when it belongs to the newest ask.
 * A successful switch changes the cached current profile at once, and makes
 * every ask still on its way stale: one asked before the switch describes
 * the service before it, and one asked while the switch was under way may
 * have been answered before it took effect. So a late answer can never put
 * the menu back to the profile the user has just left.
 *
 * Switches are numbered too: two windows can click one almost at once, and
 * the answers can arrive in either order. Only the newest switch's answer
 * changes the state; an older one comes back {superseded: true}.
 *
 * A profile changed in the settings page shows up at the next look: that
 * page has its own client (profiles.js) and writes nothing here.
 */
var ZoteroTranslateModelMenu = (() => {
  /** What the submenu's grey item says when the status has no listing. */
  function unavailable(found) {
    if (found.error) {
      return "翻译服务地址不是本机"; // the client sent nothing (profiles.js, addressProblem)
    }
    return found.noProfiles ? "翻译服务版本太旧" : "翻译服务连不上";
  }

  /** The item menu's state, {listing, problem}: `listing` is the service's
   *  answer ({current, profiles}), null while the service cannot be reached,
   *  has no profiles, or has not answered yet; `problem` says which, for the
   *  "切换模型" submenu's single grey item ("" while there is a listing).
   *
   *  `client` is profiles.createClient's client; `onChange(state)` runs after
   *  every change, for the menu's redraw. */
  function createModelMenu({ client, onChange = () => {} }) {
    let state = { listing: null, problem: "正在连接翻译服务…" };
    let asked = 0;
    let switched = 0;

    return {
      state: () => state,

      /** Ask the service for its profiles again. Answers false when a newer
       *  ask or a switch has made this answer stale (it is dropped then). */
      async refresh() {
        const ask = ++asked;
        const found = await client.status();
        if (ask !== asked) {
          return false;
        }
        state = found.listing
          ? { listing: found.listing, problem: "" }
          : { listing: null, problem: unavailable(found) };
        onChange(state);
        return true;
      },

      /** The "切换模型" click: make the profile current. {ok} or {error}, as
       *  the client answers; nothing changes when the service refuses. A
       *  switch answered after a newer one changes nothing: {superseded}. */
      async switchTo(id) {
        const attempt = ++switched;
        asked += 1; // answers asked before the switch describe the service before it
        const outcome = await client.apply(id);
        if (attempt !== switched) {
          return { ...outcome, superseded: true }; // a newer click owns the menu
        }
        if (!outcome.ok) {
          return outcome;
        }
        if (state.listing) {
          state = { ...state, listing: { ...state.listing, current: id } };
        }
        asked += 1; // answers asked while it was under way may be from before it
        onChange(state);
        return outcome;
      },
    };
  }

  return { createModelMenu };
})();

if (typeof module !== "undefined") {
  module.exports = ZoteroTranslateModelMenu;
}
