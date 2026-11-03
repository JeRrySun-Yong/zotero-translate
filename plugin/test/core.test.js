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

test("a task window row shows title, stage, percentage and status", () => {
  assert.deepEqual(
    core.taskRow({ title: "AutoDesign", state: "running", stage: "翻译与审校", progress: 42 }),
    { title: "AutoDesign", stage: "翻译与审校", percent: "42%", status: "翻译中" },
  );
  assert.deepEqual(
    core.taskRow({ title: "AutoDesign", state: "queued", stage: "排队中", progress: 0 }).status,
    "排队中",
  );
  assert.equal(core.taskRow({ title: "A", state: "attaching", stage: "完成", progress: 100 }).status, "挂回中");
  assert.equal(core.taskRow({ title: "A", state: "done", stage: "完成", progress: 100 }).status, "已挂回");
});

test("a finished row says how many paragraphs and algorithms kept their source text", () => {
  const done = { title: "A", state: "done", stage: "完成", progress: 100 };
  assert.equal(core.taskRow({ ...done, keptSourceParagraphs: 0, keptSourceAlgorithms: 0 }).status, "已挂回");
  assert.equal(core.taskRow({ ...done, keptSourceAlgorithms: 3 }).status, "已挂回（3 个算法保留原文）");
  assert.equal(
    core.taskRow({ ...done, keptSourceParagraphs: 2, keptSourceAlgorithms: 1 }).status,
    "已挂回（2 段保留原文，1 个算法保留原文）",
  );
});

test("a running row names the attempt when the service has started more than one", () => {
  const running = { title: "A", state: "running", stage: "翻译与审校", progress: 10 };
  assert.equal(core.taskRow({ ...running, attempt: 1 }).status, "翻译中");
  assert.equal(core.taskRow(running).status, "翻译中");
  assert.equal(core.taskRow({ ...running, attempt: 2 }).status, "翻译中（第 2 次尝试）");
  assert.equal(core.taskRow({ title: "A", state: "queued", attempt: 2, queuePosition: 1 }).status, "排队中（第 1 位）");
});

test("a failed row shows the reason", () => {
  const row = core.taskRow({ title: "A", state: "failed", stage: "翻译与审校", progress: 30, error: "管线模型请求连续失败（第 2 页附近）" });
  assert.equal(row.status, "失败：管线模型请求连续失败（第 2 页附近）");
});

test("a row before the service answers has no stage or percentage yet", () => {
  assert.deepEqual(core.taskRow({ title: "A", state: "submitting" }), { title: "A", stage: "", percent: "", status: "提交中" });
});

test("the upload carries only the PDF bytes and the original file name", async () => {
  const pdfBytes = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x2d, 0x00, 0xff, 0x0d, 0x0a]); // "%PDF-" + binary
  const upload = core.uploadRequest(pdfBytes, "Smith 2024 智能体.pdf");
  const form = await new Response(upload.body, { headers: { "Content-Type": upload.contentType } }).formData();
  assert.deepEqual([...form.keys()], ["file", "filename"]);
  assert.deepEqual(new Uint8Array(await form.get("file").arrayBuffer()), pdfBytes);
  assert.equal(form.get("filename"), "Smith 2024 智能体.pdf");
});
