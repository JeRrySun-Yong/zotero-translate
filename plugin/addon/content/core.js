/* Translation plugin logic that does not need Zotero.
 *
 * Zotero loads this file with Services.scriptloader.loadSubScript, which
 * defines ZoteroTranslateCore in the plugin scope; Node loads it with
 * require() for plugin/test/.
 *
 * The task window's "已用时间" (ticket M4-09) counts from the moment the
 * plugin starts sending the paper to the service (`submittedAt`, set by
 * the lifecycle for each submission and each "重试"), and stops when the
 * task ends (`endedAt`: done, failed or cancelled). Why that moment:
 * - It is what the user waits through: from "翻译为中文" to the PDFs, the
 *   time in the queue and any automatic reruns included.
 * - The plugin knows it without asking the service: the service's answer
 *   has no times, and a time the service gives would start again with
 *   every new attempt (a service restart, an automatic rerun).
 * - It survives a Zotero restart: `submittedAt` is wall clock time
 *   (Date.now()) saved with the task, so after a restart the count goes
 *   on, the time Zotero was closed included, and never starts again at 0.
 * A task saved by a plugin from before this field has no elapsed time ("").
 */
var ZoteroTranslateCore = (() => {
  // The two translation outputs: service file name, attachment title.
  const OUTPUTS = [
    { file: "mono", title: "中文译文" },
    { file: "dual", title: "双语对照" },
  ];
  const OUTPUT_TITLES = new Set(OUTPUTS.map((output) => output.title));
  const TRANSLATE_MENU_LABEL = "翻译为中文";
  const NO_PDF_SUFFIX = "（没有 PDF）";

  /** The source PDF among an item's attachments, given in Zotero's best-first
   *  order as {isPDF, title}: the first PDF that is not a translation output. */
  function pickSourcePdf(candidates) {
    return candidates.find((c) => c.isPDF && !OUTPUT_TITLES.has(c.title)) || null;
  }

  /** Whether an attachment title is one of the two translation outputs: the
   *  plugin's way of telling that a paper already has a translation. */
  function isTranslationOutput(title) {
    return OUTPUT_TITLES.has(title);
  }

  /** The item menu's "翻译为中文" for the selection, {hidden, disabled, label}:
   *  hidden when no selected item could be a source (a regular item or an
   *  attachment); greyed, saying why, when none of them has a source PDF;
   *  else shown as usual (items without a PDF are skipped on submission).
   *  `selected` are {candidate, hasPdf}: a regular item or an attachment, and
   *  whether it has a source PDF. `baseLabel` is the label before the
   *  no-PDF suffix (the menu passes the entry's label with the current model
   *  profile's name). */
  function translateMenuItem(selected, baseLabel = TRANSLATE_MENU_LABEL) {
    const candidates = selected.filter((item) => item.candidate);
    if (!candidates.length) {
      return { hidden: true, disabled: true, label: baseLabel };
    }
    if (candidates.some((item) => item.hasPdf)) {
      return { hidden: false, disabled: false, label: baseLabel };
    }
    return { hidden: false, disabled: true, label: `${baseLabel}${NO_PDF_SUFFIX}` };
  }

  /** "paper.pdf" + "中文译文" -> "paper-中文译文.pdf". */
  function outputFileName(sourceFileName, title) {
    return `${sourceFileName.replace(/\.pdf$/i, "")}-${title}.pdf`;
  }

  /** serviceUrl("http://127.0.0.1:8920/", "tasks", id) -> "http://127.0.0.1:8920/tasks/<id>". */
  function serviceUrl(base, ...path) {
    return [base.trim().replace(/\/+$/, ""), ...path.map(encodeURIComponent)].join("/");
  }

  /** The POST /tasks body: a multipart form with the PDF and its original
   *  file name, and for a retry the profile ID its task recorded (`profile`),
   *  which the service uses instead of the current profile. A new submission
   *  names none. The plugin scope has Blob but no FormData. */
  function uploadRequest(pdfBytes, fileName, profileId) {
    const boundary = `zotero-translate-${crypto.randomUUID()}`;
    const part = (headers) => `--${boundary}\r\n${headers}\r\n\r\n`;
    const profile = profileId
      ? ["\r\n", part('Content-Disposition: form-data; name="profile"'), profileId]
      : [];
    const body = new Blob([
      part('Content-Disposition: form-data; name="file"; filename="source.pdf"\r\nContent-Type: application/pdf'),
      pdfBytes,
      "\r\n",
      part('Content-Disposition: form-data; name="filename"'),
      fileName,
      ...profile,
      `\r\n--${boundary}--\r\n`,
    ]);
    return { contentType: `multipart/form-data; boundary=${boundary}`, body };
  }

  /** The item menu's label: "翻译为中文（DeepSeek）" once the service has said
   *  which profile is current, else plain "翻译为中文"; `suffix` is for the
   *  menu's other states (an appendage after the profile's name). */
  function itemMenuLabel(profileName, suffix = "") {
    return `翻译为中文${profileName ? `（${profileName}）` : ""}${suffix}`;
  }

  /** The "切换模型" submenu: one radio item per profile whose connection test
   *  passed ({id, label, checked, disabled}), the current one checked. With no
   *  listing (the service cannot be reached, or has no profiles) one disabled
   *  item `unavailable`, which says why. */
  function modelMenuItems(listing, unavailable = "翻译服务连不上") {
    if (!listing) {
      return [{ id: null, label: unavailable, checked: false, disabled: true }];
    }
    return listing.profiles
      .filter((profile) => profile.tested)
      .map((profile) => ({
        id: profile.id,
        label: profile.name,
        checked: profile.id === listing.current,
        disabled: false,
      }));
  }

  // Task states: the service's queued/running/waiting_rerun/failed/cancelled,
  // plus the plugin's own submitting (upload under way), attaching (service
  // succeeded, PDFs being added to the item), attach_failed (a local error
  // while attaching; "继续挂回" goes on), done (both PDFs attached) and
  // cancelling (the user cancelled; the cancel is not yet confirmed by the service).
  const STATUS_LABELS = {
    submitting: "提交中",
    queued: "排队中",
    running: "翻译中",
    waiting_rerun: "等待自动重跑",
    attaching: "挂回中",
    attach_failed: "挂回失败",
    done: "已挂回",
    failed: "失败",
    cancelling: "取消中",
    cancelled: "已取消",
  };
  // Unfinished states: the plugin still has work to do for the task (the
  // lifecycle saves these across restarts and submits no second task for the
  // same source PDF); only these show a disconnection.
  const UNFINISHED_STATES = ["submitting", "queued", "running", "waiting_rerun", "attaching", "attach_failed", "cancelling"];

  /** What the task window shows for one task (at `now`): title, `profile`
   *  (the model profile's name the task was submitted with, "" until the
   *  service answers), stage, `progress` (the service's percentage, the
   *  progress bar's value; null before the service answers) and `percent`
   *  (the same as text), `elapsed` (see the file header), a short `status`
   *  word and a `detail` line ("" when there is nothing more to say), shown
   *  in small type under the word.
   *  The detail: a waiting task's place in the service's queue, a running
   *  one's attempt when the service has started more than one (after a
   *  restart or a rerun), for one waiting for an automatic rerun about how
   *  many minutes are left, which rerun comes next and why the last attempt
   *  failed, a finished one how many paragraphs and algorithms kept their
   *  source text, a failed one the reason. A cancelling task says why the
   *  service has not confirmed the cancel yet (`cleanupError`), or that the
   *  service cannot be reached. Any other unfinished task whose service has
   *  not answered since `disconnectedSince` is 等待服务, and says for how
   *  many whole minutes so far (at least 1). */
  function taskRow(task, now) {
    const progress = task.progress == null ? null : task.progress;
    const row = {
      title: task.title,
      profile: (task.profile && task.profile.name) || "",
      stage: task.stage || "",
      progress,
      percent: progress == null ? "" : `${progress}%`,
      elapsed: task.submittedAt == null ? "" : elapsedText((task.endedAt ?? now) - task.submittedAt),
    };
    return { ...row, ...statusAndDetail(task, now) };
  }

  /** A task's {status, detail} for taskRow. */
  function statusAndDetail(task, now) {
    const status = STATUS_LABELS[task.state] || task.state;
    if (task.state === "cancelling") {
      if (task.disconnectedSince != null) {
        return { status, detail: "翻译服务暂时连不上" };
      }
      return { status, detail: task.cleanupError || "" };
    }
    if (task.disconnectedSince != null && UNFINISHED_STATES.includes(task.state)) {
      const minutes = Math.max(1, Math.floor((now - task.disconnectedSince) / 60000));
      return { status: "等待服务", detail: `翻译服务暂时连不上，恢复后继续（已 ${minutes} 分钟）` };
    }
    const details = [];
    if (task.state === "queued" && task.queuePosition) {
      details.push(`第 ${task.queuePosition} 位`);
    }
    if (task.state === "running" && task.attempt > 1) {
      details.push(`第 ${task.attempt} 次尝试`);
    }
    if (task.state === "waiting_rerun") {
      const minutes = Math.max(1, Math.ceil((task.nextAttemptInSeconds || 0) / 60));
      details.push(`约 ${minutes} 分钟后，第 ${(task.rerunsUsed || 0) + 1} 次自动重跑，共 ${task.rerunsMax || 2} 次`);
      if (task.error) {
        details.push(`；上次失败：${task.error}`);
      }
    }
    if (task.state === "done") {
      const kept = [];
      if (task.keptSourceParagraphs > 0) {
        kept.push(`${task.keptSourceParagraphs} 段保留原文`);
      }
      if (task.keptSourceAlgorithms > 0) {
        kept.push(`${task.keptSourceAlgorithms} 个算法保留原文`);
      }
      details.push(kept.join("，"));
    }
    if (["failed", "attach_failed"].includes(task.state) && task.error) {
      details.push(task.error);
    }
    return { status, detail: details.join("") };
  }

  /** A row's status word and detail on one line, for the notices: "失败：原因". */
  function statusText({ status, detail }) {
    return detail ? `${status}：${detail}` : status;
  }

  /** Milliseconds as the task window's elapsed time: "59 秒", "1 分 5 秒",
   *  "2 小时 7 分"; never negative (a clock set back reads 0 秒). */
  function elapsedText(ms) {
    const seconds = Math.max(0, Math.floor(ms / 1000));
    if (seconds < 60) {
      return `${seconds} 秒`;
    }
    if (seconds < 3600) {
      return `${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
    }
    return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分`;
  }

  /** The notice after "翻译为中文", {headline, titles, reasons}: the titles
   *  of the PDFs the service took (`submitted`) and how many, and why the
   *  others were skipped (`skipped`: the lifecycle's notices of this
   *  submission). Null when there is nothing to say: the uploads that failed
   *  have their own failure notices. */
  function submissionNotice({ submitted, skipped }) {
    if (!submitted.length && !skipped.length) {
      return null;
    }
    return {
      headline: submitted.length ? `已提交 ${submitted.length} 篇` : "没有提交",
      titles: [...submitted],
      reasons: [...skipped],
    };
  }

  return {
    OUTPUTS,
    UNFINISHED_STATES,
    pickSourcePdf,
    isTranslationOutput,
    translateMenuItem,
    NO_PDF_SUFFIX,
    outputFileName,
    serviceUrl,
    uploadRequest,
    itemMenuLabel,
    modelMenuItems,
    taskRow,
    statusText,
    submissionNotice,
  };
})();

if (typeof module !== "undefined") {
  module.exports = ZoteroTranslateCore;
}
