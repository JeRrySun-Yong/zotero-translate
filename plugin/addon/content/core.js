/* Translation plugin logic that does not need Zotero.
 *
 * Zotero loads this file with Services.scriptloader.loadSubScript, which
 * defines ZoteroTranslateCore in the plugin scope; Node loads it with
 * require() for plugin/test/.
 */
var ZoteroTranslateCore = (() => {
  // The two translation outputs: service file name, attachment title.
  const OUTPUTS = [
    { file: "mono", title: "中文译文" },
    { file: "dual", title: "双语对照" },
  ];
  const OUTPUT_TITLES = new Set(OUTPUTS.map((output) => output.title));

  /** The source PDF among an item's attachments, given in Zotero's best-first
   *  order as {isPDF, title}: the first PDF that is not a translation output. */
  function pickSourcePdf(candidates) {
    return candidates.find((c) => c.isPDF && !OUTPUT_TITLES.has(c.title)) || null;
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
   *  file name, nothing else. The plugin scope has Blob but no FormData. */
  function uploadRequest(pdfBytes, fileName) {
    const boundary = `zotero-translate-${crypto.randomUUID()}`;
    const part = (headers) => `--${boundary}\r\n${headers}\r\n\r\n`;
    const body = new Blob([
      part('Content-Disposition: form-data; name="file"; filename="source.pdf"\r\nContent-Type: application/pdf'),
      pdfBytes,
      "\r\n",
      part('Content-Disposition: form-data; name="filename"'),
      fileName,
      `\r\n--${boundary}--\r\n`,
    ]);
    return { contentType: `multipart/form-data; boundary=${boundary}`, body };
  }

  // Task states: the service's queued/running/failed, plus the plugin's own
  // submitting (upload under way), attaching (service succeeded, PDFs being
  // added to the item) and done (both PDFs attached).
  const STATUS_LABELS = {
    submitting: "提交中",
    queued: "排队中",
    running: "翻译中",
    attaching: "挂回中",
    done: "已挂回",
    failed: "失败",
  };

  /** What the task window shows for one task. */
  function taskRow(task) {
    const status = STATUS_LABELS[task.state] || task.state;
    return {
      title: task.title,
      stage: task.stage || "",
      percent: task.progress == null ? "" : `${task.progress}%`,
      status: task.state === "failed" && task.error ? `${status}：${task.error}` : status,
    };
  }

  return { OUTPUTS, pickSourcePdf, outputFileName, serviceUrl, uploadRequest, taskRow };
})();

if (typeof module !== "undefined") {
  module.exports = ZoteroTranslateCore;
}
