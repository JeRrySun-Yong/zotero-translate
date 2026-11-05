// Tests for the plugin logic that does not need Zotero. Run: node --test plugin/test/
const test = require("node:test");
const assert = require("node:assert/strict");
const core = require("../addon/content/core.js");

const pdf = (title) => ({ isPDF: true, title });

test("the best PDF attachment is the first PDF in Zotero's order", () => {
  const candidates = [{ isPDF: false, title: "Snapshot" }, pdf("Full Text PDF"), pdf("Preprint PDF")];
  assert.equal(core.pickSourcePdf(candidates).title, "Full Text PDF");
});

test("earlier translation outputs are never chosen as the source", () => {
  const candidates = [pdf("中文译文"), pdf("双语对照"), pdf("Full Text PDF")];
  assert.equal(core.pickSourcePdf(candidates).title, "Full Text PDF");
});

test("an item without a usable PDF has no source", () => {
  assert.equal(core.pickSourcePdf([pdf("中文译文"), { isPDF: false, title: "Snapshot" }]), null);
  assert.equal(core.pickSourcePdf([]), null);
});

test("output file names are the source file name plus the output's suffix", () => {
  assert.equal(core.outputFileName("AutoDesign.pdf", "中文译文"), "AutoDesign-中文译文.pdf");
  assert.equal(core.outputFileName("Smith 2024 - Agents.PDF", "双语对照"), "Smith 2024 - Agents-双语对照.pdf");
  assert.equal(core.outputFileName("paper", "中文译文"), "paper-中文译文.pdf");
});

test("service URLs follow the service's HTTP interface, with or without a trailing slash", () => {
  for (const base of ["http://127.0.0.1:8920", "http://127.0.0.1:8920/", " http://127.0.0.1:8920// "]) {
    assert.equal(core.serviceUrl(base, "tasks"), "http://127.0.0.1:8920/tasks");
    assert.equal(core.serviceUrl(base, "tasks", "ab12"), "http://127.0.0.1:8920/tasks/ab12");
    assert.equal(core.serviceUrl(base, "tasks", "ab12", "files", "mono"), "http://127.0.0.1:8920/tasks/ab12/files/mono");
  }
});

/** A row's status word and its detail line, [status, detail]. */
const statusOf = (task, now) => {
  const row = core.taskRow(task, now);
  return [row.status, row.detail];
};

test("a task window row shows title, stage, progress, elapsed time, a status word and a detail line", () => {
  assert.deepEqual(
    core.taskRow({ title: "AutoDesign", state: "running", stage: "翻译与审校", progress: 42, submittedAt: 1000 }, 1000 + 65000),
    {
      title: "AutoDesign",
      profile: "",
      stage: "翻译与审校",
      progress: 42,
      percent: "42%",
      elapsed: "1 分 5 秒",
      status: "翻译中",
      detail: "",
    },
  );
  assert.deepEqual(statusOf({ title: "AutoDesign", state: "queued", stage: "排队中", progress: 0 }), ["排队中", ""]);
  assert.deepEqual(statusOf({ title: "A", state: "attaching", stage: "完成", progress: 100 }), ["挂回中", ""]);
  assert.deepEqual(statusOf({ title: "A", state: "done", stage: "完成", progress: 100 }), ["已挂回", ""]);
});

test("every status word is short; the longer part is in the detail line", () => {
  const tasks = [
    { state: "queued", queuePosition: 12 },
    { state: "running", attempt: 3 },
    { state: "waiting_rerun", rerunsUsed: 1, nextAttemptInSeconds: 300, error: "管线模型请求超时（480 秒）" },
    { state: "done", keptSourceParagraphs: 2, keptSourceAlgorithms: 1 },
    { state: "failed", error: "管线模型拒绝了请求（HTTP 400）" },
    { state: "attach_failed", error: "找不到原 PDF 附件" },
    { state: "cancelling", cleanupError: "进程回收没有完成" },
    { state: "cancelling", disconnectedSince: 0 },
    { state: "running", disconnectedSince: 0 },
  ];
  for (const task of tasks) {
    const [status, detail] = statusOf({ title: "A", ...task }, 60000);
    assert.ok(status.length <= 6, `${task.state}: ${status}`);
    assert.ok(detail.length > 0, `${task.state} has a detail line`);
  }
});

test("statusText joins the status word and the detail line for one-line uses such as the notices", () => {
  assert.equal(core.statusText({ status: "失败", detail: "管线模型拒绝了请求（HTTP 400）" }), "失败：管线模型拒绝了请求（HTTP 400）");
  assert.equal(core.statusText({ status: "已挂回", detail: "" }), "已挂回");
});

test("the progress bar's value is the service's percentage, none before the service answers", () => {
  assert.equal(core.taskRow({ title: "A", state: "running", progress: 0 }).progress, 0);
  assert.equal(core.taskRow({ title: "A", state: "done", progress: 100 }).progress, 100);
  assert.equal(core.taskRow({ title: "A", state: "submitting", progress: null }).progress, null);
  assert.equal(core.taskRow({ title: "A", state: "submitting" }).progress, null);
});

test("elapsed time counts from the submission; an ended task's stops at its end", () => {
  const task = { title: "A", state: "running", submittedAt: 5000 };
  assert.equal(core.taskRow(task, 5000).elapsed, "0 秒");
  assert.equal(core.taskRow(task, 5000 + 59999).elapsed, "59 秒");
  assert.equal(core.taskRow(task, 5000 + 60000).elapsed, "1 分 0 秒");
  assert.equal(core.taskRow(task, 5000 + 3599000).elapsed, "59 分 59 秒");
  assert.equal(core.taskRow(task, 5000 + 3600000).elapsed, "1 小时 0 分");
  assert.equal(core.taskRow(task, 5000 + 26 * 3600000 + 7 * 60000 + 30000).elapsed, "26 小时 7 分");
  const ended = { ...task, state: "done", endedAt: 5000 + 90000 };
  assert.equal(core.taskRow(ended, 5000 + 90000).elapsed, "1 分 30 秒");
  assert.equal(core.taskRow(ended, 5000 + 3600000).elapsed, "1 分 30 秒");
  // A task saved by a plugin from before the submission time was recorded.
  assert.equal(core.taskRow({ title: "A", state: "running" }, 5000).elapsed, "");
  // A clock set back never shows a negative time.
  assert.equal(core.taskRow(task, 1000).elapsed, "0 秒");
});

test("a finished row says how many paragraphs and algorithms kept their source text", () => {
  const done = { title: "A", state: "done", stage: "完成", progress: 100 };
  assert.deepEqual(statusOf({ ...done, keptSourceParagraphs: 0, keptSourceAlgorithms: 0 }), ["已挂回", ""]);
  assert.deepEqual(statusOf({ ...done, keptSourceAlgorithms: 3 }), ["已挂回", "3 个算法保留原文"]);
  assert.deepEqual(statusOf({ ...done, keptSourceParagraphs: 2, keptSourceAlgorithms: 1 }), ["已挂回", "2 段保留原文，1 个算法保留原文"]);
});

test("a running row names the attempt when the service has started more than one", () => {
  const running = { title: "A", state: "running", stage: "翻译与审校", progress: 10 };
  assert.deepEqual(statusOf({ ...running, attempt: 1 }), ["翻译中", ""]);
  assert.deepEqual(statusOf(running), ["翻译中", ""]);
  assert.deepEqual(statusOf({ ...running, attempt: 2 }), ["翻译中", "第 2 次尝试"]);
  assert.deepEqual(statusOf({ title: "A", state: "queued", attempt: 2, queuePosition: 1 }), ["排队中", "第 1 位"]);
});

test("a row waiting for an automatic rerun says so, about when, which rerun and the last reason", () => {
  const waiting = {
    title: "A",
    state: "waiting_rerun",
    stage: "生成论文档案",
    progress: 2,
    rerunsUsed: 0,
    rerunsMax: 2,
    nextAttemptInSeconds: 55,
    error: "生成论文档案失败：管线模型服务端出错（HTTP 500）",
  };
  assert.deepEqual(statusOf(waiting), [
    "等待自动重跑",
    "约 1 分钟后，第 1 次自动重跑，共 2 次；上次失败：生成论文档案失败：管线模型服务端出错（HTTP 500）",
  ]);
  assert.deepEqual(statusOf({ ...waiting, rerunsUsed: 1, nextAttemptInSeconds: 241, error: "管线模型请求超时（480 秒）" }), [
    "等待自动重跑",
    "约 5 分钟后，第 2 次自动重跑，共 2 次；上次失败：管线模型请求超时（480 秒）",
  ]);
  assert.deepEqual(statusOf({ ...waiting, error: null }), ["等待自动重跑", "约 1 分钟后，第 1 次自动重跑，共 2 次"]);
  // Due now (0 seconds left) still reads "about 1 minute", never "0 minutes".
  assert.match(core.taskRow({ ...waiting, nextAttemptInSeconds: 0 }).detail, /^约 1 分钟后/);
  assert.equal(core.taskRow({ ...waiting, rerunsUsed: 1 }).stage, "生成论文档案");
});

test("after the reruns, a failed row shows the last attempt's reason", () => {
  const task = { title: "A", state: "failed", attempt: 3, rerunsUsed: 2, error: "管线模型拒绝了请求（HTTP 400）" };
  assert.deepEqual(statusOf(task), ["失败", "管线模型拒绝了请求（HTTP 400）"]);
});

test("a failed row shows the reason; so does a row whose attaching failed", () => {
  const task = { title: "A", state: "failed", stage: "翻译与审校", progress: 30, error: "管线模型请求连续失败（第 2 页附近）" };
  assert.deepEqual(statusOf(task), ["失败", "管线模型请求连续失败（第 2 页附近）"]);
  assert.deepEqual(statusOf({ title: "A", state: "attach_failed", error: "找不到原 PDF 附件" }), ["挂回失败", "找不到原 PDF 附件"]);
  assert.deepEqual(statusOf({ title: "A", state: "failed" }), ["失败", ""]);
});

test("a row before the service answers has no stage or percentage yet", () => {
  assert.deepEqual(core.taskRow({ title: "A", state: "submitting", submittedAt: 0 }, 2000), {
    title: "A",
    profile: "",
    stage: "",
    progress: null,
    percent: "",
    elapsed: "2 秒",
    status: "提交中",
    detail: "",
  });
});

test("a row names the profile the task was submitted with", () => {
  const task = { title: "A", state: "running", profile: { id: "0123456789ab", name: "DeepSeek" } };
  assert.equal(core.taskRow(task, 0).profile, "DeepSeek");
  assert.equal(core.taskRow({ title: "A", state: "running" }, 0).profile, "");
  assert.equal(core.taskRow({ title: "A", state: "running", profile: { id: "gemini" } }, 0).profile, "");
});

test("the upload carries only the PDF bytes and the original file name", async () => {
  const pdfBytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0x00, 0xff, 0x0d, 0x0a]); // "%PDF-" + binary
  const upload = core.uploadRequest(pdfBytes, "Smith 2024 智能体.pdf");
  const form = await new Response(upload.body, { headers: { "Content-Type": upload.contentType } }).formData();
  assert.deepEqual([...form.keys()], ["file", "filename"]);
  assert.deepEqual(new Uint8Array(await form.get("file").arrayBuffer()), pdfBytes);
  assert.equal(form.get("filename"), "Smith 2024 智能体.pdf");
});

test("a retry's upload names the profile its task recorded; a new submission names none", async () => {
  const pdfBytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x01, 0x02]);
  const upload = core.uploadRequest(pdfBytes, "paper.pdf", "0123456789ab");
  const form = await new Response(upload.body, { headers: { "Content-Type": upload.contentType } }).formData();
  assert.deepEqual([...form.keys()], ["file", "filename", "profile"]);
  assert.equal(form.get("profile"), "0123456789ab");
  assert.deepEqual(new Uint8Array(await form.get("file").arrayBuffer()), pdfBytes);
  assert.equal(form.get("filename"), "paper.pdf");
});

test("the item menu names the current profile; without one it is plain 翻译为中文", () => {
  assert.equal(core.itemMenuLabel("DeepSeek"), "翻译为中文（DeepSeek）");
  assert.equal(core.itemMenuLabel(""), "翻译为中文");
  assert.equal(core.itemMenuLabel(null), "翻译为中文");
  // The suffix is for the item menu's other states (no PDF selected): it comes after the profile.
  assert.equal(core.itemMenuLabel("Gemini", "（没有 PDF）"), "翻译为中文（Gemini）（没有 PDF）");
  assert.equal(core.itemMenuLabel(null, "（没有 PDF）"), "翻译为中文（没有 PDF）");
});

test("the switch-model submenu lists the tested profiles as radio items, the current one checked", () => {
  const listing = {
    current: "0123456789ab",
    profiles: [
      { id: "gemini", name: "Gemini", builtin: true, tested: true },
      { id: "0123456789ab", name: "DeepSeek", builtin: false, tested: true },
      { id: "ba9876543210", name: "Untested", builtin: false, tested: false },
    ],
  };
  assert.deepEqual(core.modelMenuItems(listing), [
    { id: "gemini", label: "Gemini", checked: false, disabled: false },
    { id: "0123456789ab", label: "DeepSeek", checked: true, disabled: false },
  ]);
  assert.deepEqual(core.modelMenuItems({ ...listing, current: "gemini" }).map((item) => item.checked), [true, false]);
});

test("with the service unreachable the switch-model submenu has only a grey item saying so", () => {
  assert.deepEqual(core.modelMenuItems(null), [
    { id: null, label: "翻译服务连不上", checked: false, disabled: true },
  ]);
  assert.deepEqual(core.modelMenuItems(null, "翻译服务版本太旧"), [
    { id: null, label: "翻译服务版本太旧", checked: false, disabled: true },
  ]);
});

test("a disconnected unfinished row says how many whole minutes it has waited, at least 1", () => {
  const task = { title: "A", state: "queued", queuePosition: 2, disconnectedSince: 1000 };
  assert.deepEqual(statusOf(task, 1000), ["等待服务", "翻译服务暂时连不上，恢复后继续（已 1 分钟）"]);
  assert.deepEqual(statusOf(task, 1000 + 119999), ["等待服务", "翻译服务暂时连不上，恢复后继续（已 1 分钟）"]);
  assert.deepEqual(statusOf(task, 1000 + 180000), ["等待服务", "翻译服务暂时连不上，恢复后继续（已 3 分钟）"]);
  assert.deepEqual(statusOf({ ...task, state: "failed", error: "x" }, 1000), ["失败", "x"]);
});

test("a cancelling row says so, with the service's reason or the disconnection; a cancelled row says 已取消", () => {
  const cancelling = { title: "A", state: "cancelling", stage: "排队中", progress: 0 };
  assert.deepEqual(statusOf(cancelling), ["取消中", ""]);
  assert.deepEqual(statusOf({ ...cancelling, cleanupError: "进程回收没有完成" }), ["取消中", "进程回收没有完成"]);
  assert.deepEqual(statusOf({ ...cancelling, disconnectedSince: 0 }, 5 * 60000), ["取消中", "翻译服务暂时连不上"]);
  assert.deepEqual(statusOf({ title: "A", state: "cancelled" }), ["已取消", ""]);
  assert.deepEqual(statusOf({ title: "A", state: "cancelled", disconnectedSince: 0 }, 60000), ["已取消", ""]);
});

test("the submission notice says how many PDFs the service took, and why the others were skipped", () => {
  const skipped = ["B：该条目没有 PDF 附件", "C：已有未结束的翻译任务，本次提交已忽略"];
  assert.deepEqual(core.submissionNotice({ submitted: ["A", "D", "E"], skipped: [] }), {
    headline: "已提交 3 篇",
    titles: ["A", "D", "E"],
    reasons: [],
  });
  assert.deepEqual(core.submissionNotice({ submitted: ["A"], skipped }), { headline: "已提交 1 篇", titles: ["A"], reasons: skipped });
  assert.deepEqual(core.submissionNotice({ submitted: [], skipped }), { headline: "没有提交", titles: [], reasons: skipped });
  // Uploads that failed have their own failure notices: nothing more to say.
  assert.equal(core.submissionNotice({ submitted: [], skipped: [] }), null);
});

test("the two translation outputs are recognized by their attachment titles", () => {
  assert.equal(core.isTranslationOutput("中文译文"), true);
  assert.equal(core.isTranslationOutput("双语对照"), true);
  assert.equal(core.isTranslationOutput("Full Text PDF"), false);
  assert.equal(core.isTranslationOutput("中文译文（副本）"), false);
});

test("the 翻译为中文 menu item: greyed with （没有 PDF） when no selected item has a PDF", () => {
  const item = (candidate, hasPdf) => ({ candidate, hasPdf });
  // Nothing that could be a source at all (a note, a collection): hidden.
  assert.deepEqual(core.translateMenuItem([]), { hidden: true, disabled: true, label: "翻译为中文" });
  assert.deepEqual(core.translateMenuItem([item(false, false)]), { hidden: true, disabled: true, label: "翻译为中文" });
  // Items that could be sources, none with a PDF: shown, greyed, and saying why.
  assert.deepEqual(core.translateMenuItem([item(true, false)]), { hidden: false, disabled: true, label: "翻译为中文（没有 PDF）" });
  assert.deepEqual(core.translateMenuItem([item(true, false), item(false, false)]), {
    hidden: false,
    disabled: true,
    label: "翻译为中文（没有 PDF）",
  });
  // One item with a PDF: usable as usual; the others without one are skipped on submission.
  assert.deepEqual(core.translateMenuItem([item(true, false), item(true, true)]), { hidden: false, disabled: false, label: "翻译为中文" });
  assert.deepEqual(core.translateMenuItem([item(true, true)]), { hidden: false, disabled: false, label: "翻译为中文" });
});

test("the menu's label keeps the current profile's name, and adds （没有 PDF） after it", () => {
  const item = (candidate, hasPdf) => ({ candidate, hasPdf });
  assert.deepEqual(core.translateMenuItem([item(true, true)], "翻译为中文（Gemini）"), {
    hidden: false,
    disabled: false,
    label: "翻译为中文（Gemini）",
  });
  assert.deepEqual(core.translateMenuItem([item(true, false)], "翻译为中文（Gemini）"), {
    hidden: false,
    disabled: true,
    label: `翻译为中文（Gemini）${core.NO_PDF_SUFFIX}`,
  });
});
