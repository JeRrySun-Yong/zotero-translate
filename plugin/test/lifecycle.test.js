// Tests for the plugin's task lifecycle, with every dependency injected:
// the network request, the clock, reading the source PDF, importing one
// attachment, and the saved state. Run: node --test plugin/test/
const test = require("node:test");
const assert = require("node:assert/strict");
const { createTaskLifecycle } = require("../addon/content/lifecycle.js");

const SERVICE = "http://127.0.0.1:8920";
const SOURCE = { sourceItemID: 11, itemID: 10, title: "AutoDesign", sourceFileName: "AutoDesign.pdf" };
const DISCONNECTED = (minutes) => `翻译服务暂时连不上，恢复后继续（已 ${minutes} 分钟）`;
const MINUTE = 60000;

const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
const pdfBytes = (text) => new Response(new TextEncoder().encode(text), { status: 200 });
// An answer whose headers arrive but whose body never ends.
const stuckBody = () => new Response(new ReadableStream({ start() {} }), { status: 200 });
const never = () => new Promise(() => {});
const refused = () => {
  throw new TypeError("NetworkError when attempting to fetch resource.");
};

/** Let every pending promise and I/O callback run. */
async function settle() {
  for (let i = 0; i < 10; i++) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}

/** A clock whose time moves only in advance(ms), which runs the timers that
 *  fall due, in order, letting each one's work settle. */
function fakeClock(time = 0) {
  const timers = new Map();
  let nextId = 1;
  const clock = {
    timers,
    now: () => time,
    setTimeout: (fn, ms) => {
      timers.set(nextId, { at: time + ms, fn });
      return nextId++;
    },
    clearTimeout: (id) => timers.delete(id),
    async advance(ms) {
      const end = time + ms;
      for (;;) {
        await settle();
        const [due] = [...timers].filter(([, t]) => t.at <= end).sort(([a, x], [b, y]) => x.at - y.at || a - b);
        if (!due) {
          break;
        }
        timers.delete(due[0]);
        time = due[1].at;
        due[1].fn();
      }
      time = end;
      await settle();
    },
  };
  return clock;
}

/** A Zotero stand-in: answers requests from `routes` ("METHOD url" -> response
 *  or function of the request options; a missing route is a refused
 *  connection), records requests (with the time) and imports, keeps temporary
 *  files in memory (`files`, path -> text), logs every file step in `steps`,
 *  keeps the saved state. `failWrite` (a file name) and `failRemove` inject errors. */
function environment({ routes = {}, saved = null, time = 0 } = {}) {
  const env = {
    clock: fakeClock(time),
    requests: [],
    requestTimes: [],
    imports: [],
    saved,
    routes,
    missingSources: [],
    files: new Map(),
    steps: [],
    failWrite: null,
    failRemove: false,
  };
  env.lifecycle = createTaskLifecycle({
    fetch: async (url, options = {}) => {
      const key = `${options.method || "GET"} ${url}`;
      env.requests.push(key);
      env.requestTimes.push([key, env.clock.now()]);
      const route = env.routes[key];
      if (route === undefined) {
        refused();
      }
      return typeof route === "function" ? route(options) : route.clone();
    },
    clock: env.clock,
    serviceUrl: () => SERVICE,
    readSource: async () => new Uint8Array([0x25, 0x50, 0x44, 0x46]),
    attachTarget: async (task) => {
      if (task.itemID) {
        return { parentItemID: task.itemID };
      }
      if (env.missingSources.includes(task.sourceItemID)) {
        throw new Error("找不到原 PDF 附件");
      }
      return { libraryID: 1, collections: [] };
    },
    tempFiles: {
      makeDirectory: async (task) => {
        env.steps.push(`mkdir ${task.taskId}`);
        return `/tmp/zotero-translate/${task.taskId}`;
      },
      write: async (dir, fileName, bytes) => {
        env.steps.push(`write ${fileName}`);
        if (fileName === env.failWrite) {
          throw new Error("磁盘已满");
        }
        const path = `${dir}/${fileName}`;
        env.files.set(path, new TextDecoder().decode(bytes));
        return path;
      },
      remove: async (dir) => {
        env.steps.push(`remove ${dir}`);
        if (env.failRemove) {
          throw new Error("无法删除临时目录");
        }
        for (const path of [...env.files.keys()].filter((p) => p.startsWith(`${dir}/`))) {
          env.files.delete(path);
        }
      },
    },
    importAttachment: async (target, { path, title }) => {
      env.steps.push(`import ${title}`);
      env.imports.push({ target, title, fileName: path.split("/").pop(), text: env.files.get(path) });
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
  /** When (clock time) each request for `key` was sent. */
  env.times = (key) => env.requestTimes.filter(([k]) => k === key).map(([, t]) => t);
  return env;
}

const status = (state, extra = {}) => json(200, { state, stage: "翻译与审校", progress: 0, ...extra });
const QUERY = (taskId) => `GET ${SERVICE}/tasks/${taskId}`;
const MONO = (taskId) => `GET ${SERVICE}/tasks/${taskId}/files/mono`;
const DUAL = (taskId) => `GET ${SERVICE}/tasks/${taskId}/files/dual`;
const UPLOAD = `POST ${SERVICE}/tasks`;
const succeededRoutes = (taskId) => ({
  [QUERY(taskId)]: status("succeeded", { stage: "完成", progress: 100 }),
  [MONO(taskId)]: pdfBytes("mono pdf"),
  [DUAL(taskId)]: pdfBytes("dual pdf"),
});

/** A started lifecycle with SOURCE submitted as task t1. */
async function submitted(routes = {}, options = {}) {
  const env = environment({ routes: { [UPLOAD]: json(201, { task_id: "t1" }), ...routes }, ...options });
  env.lifecycle.start();
  await env.lifecycle.submit(SOURCE);
  return env;
}

// ---- behaviour kept from before (ticket 06) ----

test("a submitted PDF is uploaded and its row shows the place in the queue", async () => {
  const env = await submitted();
  assert.deepEqual(env.requests, [UPLOAD]);
  assert.deepEqual(env.status(), ["排队中"]);

  env.routes[QUERY("t1")] = status("queued", { stage: "排队中", queue_position: 2 });
  await env.clock.advance(1999);
  assert.deepEqual(env.requests, [UPLOAD]);
  await env.clock.advance(1);
  assert.deepEqual(env.requests, [UPLOAD, QUERY("t1")]);
  assert.deepEqual(env.status(), ["排队中（第 2 位）"]);
});

test("after the service succeeds, both PDFs are attached to the original item", async () => {
  const env = await submitted(succeededRoutes("t1"));
  await env.clock.advance(2000);
  assert.deepEqual(env.imports, [
    { target: { parentItemID: 10 }, title: "中文译文", fileName: "AutoDesign-中文译文.pdf", text: "mono pdf" },
    { target: { parentItemID: 10 }, title: "双语对照", fileName: "AutoDesign-双语对照.pdf", text: "dual pdf" },
  ]);
  assert.deepEqual(env.status(), ["已挂回"]);
  assert.equal(env.saved, "[]"); // a finished task is no longer saved

  await env.clock.advance(60000); // a done task is not queried again
  assert.equal(env.times(QUERY("t1")).length, 1);
  assert.equal(env.clock.timers.size, 0);
});

test("a failed task shows the service's reason and a retry button that uploads again", async () => {
  const env = await submitted({ [QUERY("t1")]: status("failed", { error: "管线模型拒绝了请求（HTTP 400）" }) });
  await env.clock.advance(2000);
  const [row] = env.lifecycle.rows();
  assert.equal(row.status, "失败：管线模型拒绝了请求（HTTP 400）");
  assert.equal(row.canRetry, true);

  env.routes[UPLOAD] = json(201, { task_id: "t2" });
  await env.lifecycle.retry(row.key);
  assert.deepEqual(env.status(), ["排队中"]);
  assert.equal(env.lifecycle.rows()[0].canRetry, false);
  assert.equal(env.lifecycle.tasks[0].taskId, "t2");
});

test("after Zotero restarts, saved tasks are queried again and still attached", async () => {
  const before = await submitted({ [QUERY("t1")]: status("running", { progress: 40 }) });
  await before.clock.advance(2000);
  before.lifecycle.stop();
  assert.equal(before.clock.timers.size, 0);
  assert.deepEqual(JSON.parse(before.saved), [
    { ...SOURCE, serviceUrl: SERVICE, taskId: "t1", state: "running", disconnectedSince: null },
  ]);

  const after = environment({ saved: before.saved, routes: succeededRoutes("t1") });
  after.lifecycle.start();
  assert.deepEqual(after.status(), ["翻译中"]);
  await after.clock.advance(2000);
  assert.deepEqual(after.requests[0], QUERY("t1"));
  assert.deepEqual(after.imports.map((i) => i.title), ["中文译文", "双语对照"]);
  assert.deepEqual(after.status(), ["已挂回"]);
});

test("a task the service no longer has (404) fails and asks for a retry", async () => {
  const env = await submitted({ [QUERY("t1")]: json(404, { detail: "not found" }) });
  await env.clock.advance(2000);
  assert.deepEqual(env.status(), ["失败：翻译服务中找不到该任务，请重试"]);
  assert.equal(env.lifecycle.rows()[0].canRetry, true);
  assert.equal(env.saved, "[]");
  await env.clock.advance(60000);
  assert.equal(env.times(QUERY("t1")).length, 1);
});

test("a PDF that already has an unfinished task is not submitted again", async () => {
  const env = await submitted();
  await env.lifecycle.submit({ ...SOURCE, title: "AutoDesign" });
  assert.equal(env.requests.length, 1);
  assert.equal(env.lifecycle.tasks.length, 1);
  assert.deepEqual(env.lifecycle.notices, ["AutoDesign：已在排队或翻译中，本次提交已忽略"]);

  await env.lifecycle.submit({ ...SOURCE, sourceItemID: 12, title: "Other" }); // another PDF is submitted
  assert.equal(env.requests.length, 2);

  env.lifecycle.clearNotices();
  assert.deepEqual(env.lifecycle.notices, []);
});

test("a standalone PDF whose source is gone fails before anything is downloaded", async () => {
  const standalone = { ...SOURCE, itemID: null };
  const env = environment({ routes: { [UPLOAD]: json(201, { task_id: "t1" }), ...succeededRoutes("t1") } });
  env.lifecycle.start();
  await env.lifecycle.submit(standalone);
  env.missingSources.push(standalone.sourceItemID);
  await env.clock.advance(2000);
  assert.deepEqual(env.status(), ["失败：找不到原 PDF 附件"]);
  assert.deepEqual(env.requests, [UPLOAD, QUERY("t1")]);
  assert.deepEqual(env.imports, []);
});

test("a standalone PDF's two outputs go to the same library and collections", async () => {
  const env = environment({ routes: { [UPLOAD]: json(201, { task_id: "t1" }), ...succeededRoutes("t1") } });
  env.lifecycle.start();
  await env.lifecycle.submit({ ...SOURCE, itemID: null });
  await env.clock.advance(2000);
  assert.deepEqual(env.imports.map((i) => i.target), [{ libraryID: 1, collections: [] }, { libraryID: 1, collections: [] }]);
  assert.equal(env.imports[0].target, env.imports[1].target); // resolved once
});

test("a task waiting for an automatic rerun is queried, counts as unfinished and is saved across restarts", async () => {
  const waiting = () => status("waiting_rerun", { reruns_used: 0, reruns_max: 2, next_attempt_in_seconds: 60, error: "HTTP 500" });
  const env = await submitted({ [QUERY("t1")]: waiting });
  await env.clock.advance(2000);
  assert.equal(env.lifecycle.tasks[0].state, "waiting_rerun");
  await env.clock.advance(2000);
  assert.equal(env.times(QUERY("t1")).length, 2);

  await env.lifecycle.submit(SOURCE);
  assert.equal(env.lifecycle.tasks.length, 1);
  assert.deepEqual(env.lifecycle.notices, ["AutoDesign：已在排队或翻译中，本次提交已忽略"]);

  assert.deepEqual(JSON.parse(env.saved).map((t) => t.state), ["waiting_rerun"]);
  const after = environment({ saved: env.saved, routes: { [QUERY("t1")]: waiting } });
  after.lifecycle.start();
  await after.clock.advance(2000);
  assert.deepEqual(after.requests, [QUERY("t1")]);
  assert.match(after.status()[0], /^等待自动重跑/);
});

test("both PDFs are downloaded and written before either is imported; the temporary directory is removed", async () => {
  const env = await submitted(succeededRoutes("t1"));
  await env.clock.advance(2000);
  assert.deepEqual(env.steps, [
    "mkdir t1",
    "write AutoDesign-中文译文.pdf",
    "write AutoDesign-双语对照.pdf",
    "import 中文译文",
    "import 双语对照",
    "remove /tmp/zotero-translate/t1",
  ]);
  assert.deepEqual(env.files, new Map());
});

test("when writing the second PDF fails, nothing is attached and the temporary directory is removed", async () => {
  const env = await submitted(succeededRoutes("t1"));
  env.failWrite = "AutoDesign-双语对照.pdf";
  await env.clock.advance(2000);
  assert.deepEqual(env.imports, []);
  assert.deepEqual(env.status(), ["失败：磁盘已满"]);
  assert.equal(env.steps.at(-1), "remove /tmp/zotero-translate/t1");
  assert.deepEqual(env.files, new Map());
});

test("when removing the temporary directory fails, both PDFs stay attached and the task fails", async () => {
  const env = await submitted(succeededRoutes("t1"));
  env.failRemove = true;
  await env.clock.advance(2000);
  assert.deepEqual(env.imports.map((i) => i.title), ["中文译文", "双语对照"]);
  assert.deepEqual(env.status(), ["失败：无法删除临时目录"]);
});

// ---- the service cannot be reached while querying ----

const UNANSWERED_QUERIES = {
  "no connection": () => refused,
  "no answer within 10 seconds": () => never,
  "an answer whose body does not end within 10 seconds": () => stuckBody,
  "HTTP 500": () => json(500, { detail: "boom" }),
  "HTTP 502 from a proxy": () => new Response("Bad Gateway", { status: 502 }),
  "HTTP 403": () => json(403, { detail: "forbidden" }),
  "an answer that is not JSON": () => new Response("<html>", { status: 200 }),
  "JSON without a task state": () => json(200, { detail: "?" }),
};

for (const [name, route] of Object.entries(UNANSWERED_QUERIES)) {
  test(`a query with ${name} does not fail the task: it shows the disconnection and asks again every 10 seconds`, async () => {
    const env = await submitted({ [QUERY("t1")]: status("running") });
    await env.clock.advance(2000); // t=2s: running
    env.routes[QUERY("t1")] = route();
    await env.clock.advance(2000); // t=4s: no answer (a time limit ends at 14s)
    await env.clock.advance(10000);
    const since = env.lifecycle.tasks[0].disconnectedSince;
    assert.ok(since === 4000 || since === 14000, `disconnectedSince ${since}`);
    assert.equal(env.lifecycle.tasks[0].state, "running");
    assert.deepEqual(env.status(), [DISCONNECTED(0)]);
    assert.equal(env.lifecycle.rows()[0].canRetry, false);
    assert.equal(JSON.parse(env.saved)[0].disconnectedSince, since);

    await env.clock.advance(60000);
    const times = env.times(QUERY("t1"));
    const gaps = times.slice(2).map((t, i) => t - times[i + 1]);
    assert.ok(gaps.length >= 3);
    for (const gap of gaps) {
      assert.ok(gap === 10000 || gap === 20000, `gap ${gap}`); // 10 s after the previous query ended
    }
    assert.equal(env.lifecycle.tasks[0].state, "running");
  });
}

test("the disconnection shows how long it has lasted, keeps its start, and ends when the service answers", async () => {
  const env = await submitted({ [QUERY("t1")]: status("queued", { queue_position: 1 }) });
  await env.clock.advance(2000);
  delete env.routes[QUERY("t1")]; // the service stops
  await env.clock.advance(2000); // t=4s
  assert.equal(env.lifecycle.tasks[0].disconnectedSince, 4000);
  await env.clock.advance(3 * MINUTE);
  assert.equal(env.lifecycle.tasks[0].disconnectedSince, 4000);
  assert.deepEqual(env.status(), [DISCONNECTED(3)]);

  env.routes[QUERY("t1")] = status("running", { progress: 10 }); // the service is back
  await env.clock.advance(10000);
  assert.deepEqual(env.status(), ["翻译中"]);
  assert.equal(env.lifecycle.tasks[0].disconnectedSince, null);
  assert.equal(JSON.parse(env.saved)[0].disconnectedSince, null);
  const last = env.times(QUERY("t1")).at(-1);
  await env.clock.advance(2000);
  assert.equal(env.times(QUERY("t1")).at(-1), last + 2000); // every 2 seconds again
});

test("a disconnection's start survives a Zotero restart", async () => {
  const before = await submitted({ [QUERY("t1")]: status("running") });
  delete before.routes[QUERY("t1")];
  await before.clock.advance(2000);
  before.lifecycle.stop();
  assert.equal(JSON.parse(before.saved)[0].disconnectedSince, 2000);

  const after = environment({ saved: before.saved, time: 2000 + 5 * MINUTE });
  after.lifecycle.start();
  assert.deepEqual(after.status(), [DISCONNECTED(5)]);
  await after.clock.advance(9999);
  assert.deepEqual(after.requests, []); // still every 10 seconds
  await after.clock.advance(1);
  assert.deepEqual(after.requests, [QUERY("t1")]);
  assert.deepEqual(after.status(), [DISCONNECTED(5)]);

  after.routes[QUERY("t1")] = status("running");
  await after.clock.advance(10000);
  assert.deepEqual(after.status(), ["翻译中"]);
});

test("the query's time limit covers reading the body, and the request is aborted", async () => {
  let signal;
  const env = await submitted({
    [QUERY("t1")]: (options) => {
      signal = options.signal;
      return stuckBody();
    },
  });
  await env.clock.advance(2000 + 9999);
  assert.equal(env.lifecycle.tasks[0].disconnectedSince, null);
  assert.equal(signal.aborted, false);
  await env.clock.advance(1);
  assert.equal(env.lifecycle.tasks[0].disconnectedSince, 12000);
  assert.equal(signal.aborted, true);
  assert.deepEqual(env.status(), [DISCONNECTED(0)]);
});

test("a task whose service does not answer does not hold up the queries of other tasks", async () => {
  const env = environment({ routes: { [QUERY("t2")]: status("running") } });
  env.lifecycle.start();
  env.routes[UPLOAD] = json(201, { task_id: "t1" });
  await env.lifecycle.submit(SOURCE);
  env.routes[UPLOAD] = json(201, { task_id: "t2" });
  await env.lifecycle.submit({ ...SOURCE, sourceItemID: 12, title: "Other" });
  env.routes[QUERY("t1")] = never; // t1 hangs until its 10-second limit

  await env.clock.advance(30000);
  assert.deepEqual(env.times(QUERY("t2")), [2000, 4000, 6000, 8000, 10000, 12000, 14000, 16000, 18000, 20000, 22000, 24000, 26000, 28000, 30000]);
  assert.deepEqual(env.times(QUERY("t1")), [2000, 22000]); // one at a time: the next only after the first timed out at 12s
  assert.deepEqual(env.status(), [DISCONNECTED(0), "翻译中"]);
});

// ---- upload ----

test("a connection failure on upload fails at once and names the service address", async () => {
  const env = environment();
  env.lifecycle.start();
  await env.lifecycle.submit(SOURCE);
  assert.deepEqual(env.status(), [`失败：无法连接翻译服务（服务地址：${SERVICE}）`]);
  assert.equal(env.lifecycle.rows()[0].canRetry, true);
  assert.equal(env.saved, "[]");
});

test("an upload with no full answer within 120 seconds fails and names the service address", async () => {
  const env = environment({ routes: { [UPLOAD]: never } });
  env.lifecycle.start();
  const submission = env.lifecycle.submit(SOURCE);
  await env.clock.advance(119999);
  assert.deepEqual(env.status(), ["提交中"]);
  await env.clock.advance(1);
  await submission;
  assert.deepEqual(env.status(), [`失败：翻译服务在 120 秒内没有回答完（服务地址：${SERVICE}）`]);
});

test("an upload the service refuses fails with the status and the service address", async () => {
  const env = environment({ routes: { [UPLOAD]: json(413, { detail: "too large" }) } });
  env.lifecycle.start();
  await env.lifecycle.submit(SOURCE);
  assert.deepEqual(env.status(), [`失败：翻译服务拒绝了提交（HTTP 413，服务地址：${SERVICE}）`]);
});

test("an upload under way is saved; after a Zotero restart its row fails with 提交未完成，请重试 and is not sent again", async () => {
  const before = environment({ routes: { [UPLOAD]: never } });
  before.lifecycle.start();
  before.lifecycle.submit(SOURCE);
  await settle();
  assert.deepEqual(JSON.parse(before.saved), [
    { ...SOURCE, serviceUrl: SERVICE, taskId: null, state: "submitting", disconnectedSince: null },
  ]);
  before.lifecycle.stop();

  const after = environment({ saved: before.saved, routes: { [UPLOAD]: json(201, { task_id: "t9" }) } });
  after.lifecycle.start();
  assert.deepEqual(after.status(), ["失败：提交未完成，请重试"]);
  assert.equal(after.lifecycle.rows()[0].canRetry, true);
  assert.equal(after.saved, "[]");
  await after.clock.advance(60000);
  assert.deepEqual(after.requests, []);

  await after.lifecycle.retry(after.lifecycle.rows()[0].key);
  assert.deepEqual(after.requests, [UPLOAD]);
  assert.deepEqual(after.status(), ["排队中"]);
});

// ---- downloads ----

const UNANSWERED_DOWNLOADS = {
  "no connection": () => refused,
  "HTTP 503": () => json(503, { detail: "restarting" }),
  "no full answer within 120 seconds": () => stuckBody,
};

for (const [name, route] of Object.entries(UNANSWERED_DOWNLOADS)) {
  test(`a download with ${name} keeps the task attaching and downloads again later`, async () => {
    const env = await submitted(succeededRoutes("t1"));
    env.routes[DUAL("t1")] = route();
    await env.clock.advance(2000);
    await env.clock.advance(120000);
    assert.equal(env.lifecycle.tasks[0].state, "attaching");
    assert.ok(env.lifecycle.tasks[0].disconnectedSince != null);
    assert.deepEqual(env.status(), [DISCONNECTED(env.clock.now() - env.lifecycle.tasks[0].disconnectedSince >= MINUTE ? 1 : 0)]);
    assert.deepEqual(env.imports, []); // nothing imported before both are written
    assert.deepEqual(env.files, new Map());
    assert.deepEqual(JSON.parse(env.saved).map((t) => t.state), ["attaching"]);

    env.routes[DUAL("t1")] = pdfBytes("dual pdf");
    await env.clock.advance(10000);
    assert.deepEqual(env.status(), ["已挂回"]);
    assert.deepEqual(env.imports.map((i) => [i.title, i.text]), [["中文译文", "mono pdf"], ["双语对照", "dual pdf"]]);
    assert.equal(env.times(QUERY("t1")).length, 1); // the service is not asked again; the downloads are
  });
}

test("a download the service no longer has (404) fails with 译文已不在翻译服务中，请重试", async () => {
  const env = await submitted(succeededRoutes("t1"));
  env.routes[MONO("t1")] = json(404, { detail: "not found" });
  await env.clock.advance(2000);
  assert.deepEqual(env.status(), ["失败：译文已不在翻译服务中，请重试"]);
  assert.equal(env.lifecycle.rows()[0].canRetry, true);
  assert.deepEqual(env.imports, []);
});

test("each PDF has its own 120-second download limit", async () => {
  const slow = (text) => () => new Promise((resolve) => env.clock.setTimeout(() => resolve(pdfBytes(text)), 100000));
  const env = await submitted(succeededRoutes("t1"));
  env.routes[MONO("t1")] = slow("mono pdf");
  env.routes[DUAL("t1")] = slow("dual pdf");
  await env.clock.advance(2000 + 200000);
  assert.deepEqual(env.status(), ["已挂回"]);
  assert.equal(env.imports.length, 2);
});

test("an attaching task saved before a restart downloads again after it", async () => {
  const before = await submitted(succeededRoutes("t1"));
  delete before.routes[MONO("t1")];
  await before.clock.advance(2000);
  before.lifecycle.stop();
  assert.deepEqual(JSON.parse(before.saved).map((t) => [t.state, t.disconnectedSince]), [["attaching", 2000]]);

  const after = environment({ saved: before.saved, routes: succeededRoutes("t1"), time: 2000 + 2 * MINUTE });
  after.lifecycle.start();
  assert.deepEqual(after.status(), [DISCONNECTED(2)]);
  await after.clock.advance(10000);
  assert.deepEqual(after.status(), ["已挂回"]);
  assert.deepEqual(after.requests, [MONO("t1"), DUAL("t1")]);
});
