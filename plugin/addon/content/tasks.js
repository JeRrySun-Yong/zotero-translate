/* The "翻译任务" window: one row per task, redrawn after every poll.
 * Zotero's include.js, loaded before this script, defines `Zotero`. */
var { ZoteroTranslate } = Zotero;

function render() {
  const rows = ZoteroTranslate.rows();
  const tbody = document.getElementById("zotero-translate-rows");
  tbody.replaceChildren(
    ...rows.map((row) => {
      const tr = document.createElementNS("http://www.w3.org/1999/xhtml", "tr");
      for (const text of [row.title, row.stage, row.percent, row.status]) {
        const td = document.createElementNS("http://www.w3.org/1999/xhtml", "td");
        td.textContent = text;
        tr.append(td);
      }
      return tr;
    }),
  );
  document.getElementById("zotero-translate-empty").hidden = rows.length > 0;
}

window.addEventListener("load", () => {
  ZoteroTranslate.listeners.add(render);
  render();
});
window.addEventListener("unload", () => ZoteroTranslate.listeners.delete(render));
