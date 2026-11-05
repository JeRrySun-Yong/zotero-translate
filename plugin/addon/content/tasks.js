/* The "翻译任务" window: one row per task, updated after every poll and
 * every second (for the elapsed time): title, the model profile the task was
 * submitted with, stage, a progress bar with the
 * percentage, the elapsed time (core.js), a short status word with its
 * detail line in small type under it, and a "取消" button on rows that can be cancelled (no confirmation), a "重试"
 * button on failed and cancelled rows, a "继续挂回" button on rows whose
 * attaching failed, "打开译文", "打开双语" and "在库中显示" on rows whose
 * PDFs are attached, "清除已结束" (removes the done, failed and cancelled
 * rows; enabled while there are any), and the notices of the latest
 * submission. Zotero's include.js, loaded before this script, defines `Zotero`. */
var { lifecycle } = Zotero.ZoteroTranslate;
const XHTML = "http://www.w3.org/1999/xhtml";
// Task key -> <tr>. Tasks are only ever appended or removed ("清除已结束"),
// so rows are updated in place; a redraw never replaces a button while it is being clicked.
const shownRows = new Map();
// Task key -> the row's results ({mono, dual} attachment IDs) at the last redraw.
const shownResults = new Map();
// The buttons that open a done task's results: action, label, what they do with the results.
const RESULT_BUTTONS = [
  ["openMono", "打开译文", (results) => Zotero.ZoteroTranslate.openAttachment(results.mono)],
  ["openDual", "打开双语", (results) => Zotero.ZoteroTranslate.openAttachment(results.dual)],
  ["showInLibrary", "在库中显示", (results) => Zotero.ZoteroTranslate.showInLibrary([results.mono, results.dual])],
];

// How often the elapsed times are redrawn between polls.
const TICK_MS = 1000;

/** A new element of the window, with a class name. */
function element(name, className) {
  const el = document.createElementNS(XHTML, name);
  if (className) {
    el.className = className;
  }
  return el;
}

/** Set an element's text only when it changed. */
function setText(el, text) {
  if (el.textContent !== text) {
    el.textContent = text;
  }
}

function rowElement(key) {
  let tr = shownRows.get(key);
  if (!tr) {
    tr = document.createElementNS(XHTML, "tr");
    const progress = element("td", "progress");
    const bar = element("progress");
    bar.max = 100;
    progress.append(bar, element("span", "percent"));
    const status = element("td", "status");
    status.append(element("div", "status-word"), element("div", "status-detail"));
    tr.append(
      element("td", "title"),
      element("td", "profile"),
      element("td", "stage"),
      progress,
      element("td", "elapsed"),
      status,
      element("td", "buttons"),
    );
    for (const [action, label] of [["cancel", "取消"], ["retry", "重试"], ["continueAttach", "继续挂回"]]) {
      const button = document.createElementNS(XHTML, "button");
      button.textContent = label;
      button.dataset.action = action;
      button.addEventListener("click", () => lifecycle[action](key));
      tr.lastChild.append(button);
    }
    for (const [action, label, open] of RESULT_BUTTONS) {
      const button = document.createElementNS(XHTML, "button");
      button.textContent = label;
      button.dataset.action = action;
      button.addEventListener("click", () => {
        const results = shownResults.get(key);
        if (results) {
          open(results).catch((e) => Zotero.logError(e));
        }
      });
      tr.lastChild.append(button);
    }
    document.getElementById("zotero-translate-rows").append(tr);
    shownRows.set(key, tr);
  }
  return tr;
}

function render() {
  const rows = lifecycle.rows();
  const keys = new Set(rows.map((row) => row.key));
  for (const [key, tr] of shownRows) {
    if (!keys.has(key)) {
      tr.remove();
      shownRows.delete(key);
      shownResults.delete(key);
    }
  }
  document.getElementById("zotero-translate-clear-ended").disabled = !rows.some((row) => row.ended);
  for (const row of rows) {
    const tr = rowElement(row.key);
    setText(tr.querySelector(".title"), row.title);
    setText(tr.querySelector(".profile"), row.profile);
    setText(tr.querySelector(".stage"), row.stage);
    const bar = tr.querySelector("progress");
    bar.hidden = row.progress == null;
    if (row.progress != null && bar.value !== row.progress) {
      bar.value = row.progress;
    }
    setText(tr.querySelector(".percent"), row.percent);
    setText(tr.querySelector(".elapsed"), row.elapsed);
    setText(tr.querySelector(".status-word"), row.status);
    const detail = tr.querySelector(".status-detail");
    setText(detail, row.detail);
    detail.hidden = !row.detail;
    tr.querySelector('button[data-action="cancel"]').hidden = !row.canCancel;
    tr.querySelector('button[data-action="retry"]').hidden = !row.canRetry;
    tr.querySelector('button[data-action="continueAttach"]').hidden = !row.canContinue;
    shownResults.set(row.key, row.results);
    for (const [action] of RESULT_BUTTONS) {
      tr.querySelector(`button[data-action="${action}"]`).hidden = !row.results;
    }
  }
  document.getElementById("zotero-translate-empty").hidden = rows.length > 0;
  const notices = document.getElementById("zotero-translate-notices");
  if ([...notices.children].map((li) => li.textContent).join("\n") === lifecycle.notices.join("\n")) {
    return; // unchanged: the redraw every second leaves the list (and any selection in it) alone
  }
  notices.replaceChildren(
    ...lifecycle.notices.map((text) => {
      const li = document.createElementNS(XHTML, "li");
      li.textContent = text;
      return li;
    }),
  );
  notices.hidden = lifecycle.notices.length === 0;
}

let tick = null;

window.addEventListener("load", () => {
  document.getElementById("zotero-translate-clear-ended").addEventListener("click", () => lifecycle.clearEnded());
  lifecycle.listeners.add(render);
  render();
  tick = setInterval(render, TICK_MS);
});
window.addEventListener("unload", () => {
  clearInterval(tick);
  lifecycle.listeners.delete(render);
});
