/* The "翻译任务" window: one row per task, updated after every poll, with a
 * "取消" button on rows that can be cancelled (no confirmation), a "重试"
 * button on failed and cancelled rows, and the notices of the latest
 * submission. Zotero's include.js, loaded before this script, defines `Zotero`. */
var { lifecycle } = Zotero.ZoteroTranslate;
const XHTML = "http://www.w3.org/1999/xhtml";
// Task key -> <tr>. Tasks are only ever appended, so rows are updated in
// place; a redraw never replaces a button while it is being clicked.
const shownRows = new Map();

function rowElement(key) {
  let tr = shownRows.get(key);
  if (!tr) {
    tr = document.createElementNS(XHTML, "tr");
    for (let i = 0; i < 5; i++) {
      tr.append(document.createElementNS(XHTML, "td"));
    }
    for (const [action, label] of [["cancel", "取消"], ["retry", "重试"]]) {
      const button = document.createElementNS(XHTML, "button");
      button.textContent = label;
      button.dataset.action = action;
      button.addEventListener("click", () => lifecycle[action](key));
      tr.lastChild.append(button);
    }
    document.getElementById("zotero-translate-rows").append(tr);
    shownRows.set(key, tr);
  }
  return tr;
}

function render() {
  const rows = lifecycle.rows();
  for (const row of rows) {
    const tr = rowElement(row.key);
    [row.title, row.stage, row.percent, row.status].forEach((text, i) => {
      if (tr.children[i].textContent !== text) {
        tr.children[i].textContent = text;
      }
    });
    tr.querySelector('button[data-action="cancel"]').hidden = !row.canCancel;
    tr.querySelector('button[data-action="retry"]').hidden = !row.canRetry;
  }
  document.getElementById("zotero-translate-empty").hidden = rows.length > 0;
  const notices = document.getElementById("zotero-translate-notices");
  notices.replaceChildren(
    ...lifecycle.notices.map((text) => {
      const li = document.createElementNS(XHTML, "li");
      li.textContent = text;
      return li;
    }),
  );
  notices.hidden = lifecycle.notices.length === 0;
}

window.addEventListener("load", () => {
  lifecycle.listeners.add(render);
  render();
});
window.addEventListener("unload", () => lifecycle.listeners.delete(render));
