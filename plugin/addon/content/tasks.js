/* The "翻译任务" window: one row per task, updated after every poll, with a
 * "重试" button on failed rows, and the notices of the latest submission.
 * Zotero's include.js, loaded before this script, defines `Zotero`. */
var { lifecycle } = Zotero.ZoteroTranslate;
const XHTML = "http://www.w3.org/1999/xhtml";
// Task key -> <tr>. Tasks are only ever appended, so rows are updated in
// place; a redraw never replaces a "重试" button while it is being clicked.
const shownRows = new Map();

function rowElement(key) {
  let tr = shownRows.get(key);
  if (!tr) {
    tr = document.createElementNS(XHTML, "tr");
    for (let i = 0; i < 5; i++) {
      tr.append(document.createElementNS(XHTML, "td"));
    }
    const button = document.createElementNS(XHTML, "button");
    button.textContent = "重试";
    button.addEventListener("click", () => lifecycle.retry(key));
    tr.lastChild.append(button);
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
    tr.querySelector("button").hidden = !row.canRetry;
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
