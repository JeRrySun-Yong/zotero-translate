// Tests for the plugin's task lifecycle, with every dependency injected:
// the network request, the clock, reading the source PDF, importing one
// attachment, and the saved state. Run: node --test plugin/test/
const test = require("node:test");
const assert = require("node:assert/strict");
const { createTaskLifecycle } = require("../addon/content/lifecycle.js");

const SERVICE = "http://127.0.0.1:8920";
const SOURCE = { sourceItemID: 11, itemID: 10, title: "AutoDesign", sourceFileName: "AutoDesign.pdf" };

const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const pdfBytes = (text) => new Response(new TextEncoder().encode(text), { status: 200 });

/** A Zotero stand-in: answers requests from `routes` ("METHOD url" -> response
 *  or function), records requests and imports, keeps the saved state. */
function environment({ routes = {}, saved = null } = {}) {
  const env = {
    requests: [],
    imports: [],
    saved,
    timers: [],
    routes,
  };
  env.lifecycle = createTaskLifecycle({
    fetch: async (url, options = {}) => {
      const key = `${options.method || "GET"} ${url}`;
      env.requests.push(key);
      const route = env.routes[key];
      if (route === undefined) {
        throw new TypeError("NetworkError when attempting to fetch resource.");
      }
      return typeof route === "function" ? route() : route;
    },
    clock: {
      now: () => 0,
      setInterval: (fn, ms) => env.timers.push({ fn, ms }) - 1,
      clearInterval: (id) => {
        env.timers[id] = null;
      },
    },
    serviceUrl: () => SERVICE,
    readSource: async () => new Uint8Array([0x25, 0x50, 0x44, 0x46]),
    importAttachment: async (task, output) => {
      env.imports.push({
        itemID: task.itemID,
        title: output.title,
        fileName: output.fileName,
        text: new TextDecoder().decode(output.bytes),
      });
    },
    store: {
      load: () => env.saved,
      save: (value) => {
        env.saved = value;
      },
    },
    debug: () => {},
    logError: (e) => {
      throw e;
    },
  });
  env.status = () => env.lifecycle.rows().map((row) => row.status);
  return env;
}

const status = (state, extra = {}) => json(200, { state, stage: "翻译与审校", progress: 0, ...extra });
const succeededRoutes = (taskId) => ({
  [`GET ${SERVICE}/tasks/${taskId}`]: status("succeeded", { stage: "完成", progress: 100 }),
  [`GET ${SERVICE}/tasks/${taskId}/files/mono`]: pdfBytes("mono pdf"),
  [`GET ${SERVICE}/tasks/${taskId}/files/dual`]: pdfBytes("dual pdf"),
});

test("a submitted PDF is uploaded and its row shows the place in the queue", async () => {
  const env = environment({ routes: { [`POST ${SERVICE}/tasks`]: json(201, { task_id: "t1" }) } });
  env.lifecycle.start();
  await env.lifecycle.submit(SOURCE);
  assert.deepEqual(env.requests, [`POST ${SERVICE}/tasks`]);
  assert.deepEqual(env.status(), ["排队中"]);

  env.routes[`GET ${SERVICE}/tasks/t1`] = status("queued", { stage: "排队中", queue_position: 2 });
  await env.timers[0].fn();
  assert.equal(env.timers[0].ms, 2000);
  assert.deepEqual(env.status(), ["排队中（第 2 位）"]);
});

test("after the service succeeds, both PDFs are attached to the original item", async () => {
  const env = environment({ routes: { [`POST ${SERVICE}/tasks`]: json(201, { task_id: "t1" }), ...succeededRoutes("t1") } });
  env.lifecycle.start();
  await env.lifecycle.submit(SOURCE);
  await env.lifecycle.pollTasks();
  assert.deepEqual(env.imports, [
    { itemID: 10, title: "中文译文", fileName: "AutoDesign-中文译文.pdf", text: "mono pdf" },
    { itemID: 10, title: "双语对照", fileName: "AutoDesign-双语对照.pdf", text: "dual pdf" },
  ]);
  assert.deepEqual(env.status(), ["已挂回"]);
  assert.equal(env.saved, "[]"); // a finished task is no longer saved

  await env.lifecycle.pollTasks(); // a done task is not polled again
  assert.equal(env.requests.filter((r) => r === `GET ${SERVICE}/tasks/t1`).length, 1);
});

test("a failed task shows the service's reason and a retry button that uploads again", async () => {
  const env = environment({
    routes: {
      [`POST ${SERVICE}/tasks`]: json(201, { task_id: "t1" }),
      [`GET ${SERVICE}/tasks/t1`]: status("failed", { error: "管线模型拒绝了请求（HTTP 400）" }),
    },
  });
  env.lifecycle.start();
  await env.lifecycle.submit(SOURCE);
  await env.lifecycle.pollTasks();
  const [row] = env.lifecycle.rows();
  assert.equal(row.status, "失败：管线模型拒绝了请求（HTTP 400）");
  assert.equal(row.canRetry, true);

  env.routes[`POST ${SERVICE}/tasks`] = json(201, { task_id: "t2" });
  await env.lifecycle.retry(row.key);
  assert.deepEqual(env.status(), ["排队中"]);
  assert.equal(env.lifecycle.rows()[0].canRetry, false);
  assert.equal(env.lifecycle.tasks[0].taskId, "t2");
});

test("after Zotero restarts, saved tasks are polled again and still attached", async () => {
  const before = environment({
    routes: {
      [`POST ${SERVICE}/tasks`]: json(201, { task_id: "t1" }),
      [`GET ${SERVICE}/tasks/t1`]: status("running", { progress: 40 }),
    },
  });
  before.lifecycle.start();
  await before.lifecycle.submit(SOURCE);
  await before.lifecycle.pollTasks();
  before.lifecycle.stop();
  assert.equal(before.timers[0], null);
  assert.deepEqual(JSON.parse(before.saved), [
    { ...SOURCE, serviceUrl: SERVICE, taskId: "t1", state: "running" },
  ]);

  const after = environment({ saved: before.saved, routes: succeededRoutes("t1") });
  after.lifecycle.start();
  assert.deepEqual(after.status(), ["翻译中"]);
  await after.timers[0].fn();
  assert.deepEqual(after.requests[0], `GET ${SERVICE}/tasks/t1`);
  assert.deepEqual(after.imports.map((i) => i.title), ["中文译文", "双语对照"]);
  assert.deepEqual(after.status(), ["已挂回"]);
});

test("a task the service no longer has (404) fails and asks for a retry", async () => {
  const env = environment({
    routes: {
      [`POST ${SERVICE}/tasks`]: json(201, { task_id: "t1" }),
      [`GET ${SERVICE}/tasks/t1`]: json(404, { detail: "not found" }),
    },
  });
  env.lifecycle.start();
  await env.lifecycle.submit(SOURCE);
  await env.lifecycle.pollTasks();
  assert.deepEqual(env.status(), ["失败：翻译服务中找不到该任务，请重试"]);
  assert.equal(env.lifecycle.rows()[0].canRetry, true);
  assert.equal(env.saved, "[]");
});

test("a PDF that already has an unfinished task is not submitted again", async () => {
  const env = environment({ routes: { [`POST ${SERVICE}/tasks`]: json(201, { task_id: "t1" }) } });
  env.lifecycle.start();
  await env.lifecycle.submit(SOURCE);
  await env.lifecycle.submit({ ...SOURCE, title: "AutoDesign" });
  assert.equal(env.requests.length, 1);
  assert.equal(env.lifecycle.tasks.length, 1);
  assert.deepEqual(env.lifecycle.notices, ["AutoDesign：已在排队或翻译中，本次提交已忽略"]);

  await env.lifecycle.submit({ ...SOURCE, sourceItemID: 12, title: "Other" }); // another PDF is submitted
  assert.equal(env.requests.length, 2);

  env.lifecycle.clearNotices();
  assert.deepEqual(env.lifecycle.notices, []);
});

test("a connection failure on upload fails at once and names the service address", async () => {
  const env = environment();
  env.lifecycle.start();
  await env.lifecycle.submit(SOURCE);
  assert.deepEqual(env.status(), [`失败：无法连接翻译服务（服务地址：${SERVICE}）`]);
});
