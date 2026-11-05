// Tests for the plugin's task lifecycle, with every dependency injected:
// the network request, the clock, reading the source PDF, importing one
// attachment, and the saved state. Run: node --test plugin/test/
const test = require("node:test");
const assert = require("node:assert/strict");
const { createTaskLifecycle } = require("../addon/content/lifecycle.js");
const { statusText } = require("../addon/content/core.js");

const SERVICE = "http://127.0.0.1:8920";
const SOURCE = { sourceItemID: 11, itemID: 10, title: "AutoDesign", sourceFileName: "AutoDesign.pdf" };
// Rows' status word and detail line, as statusText joins them ("失败：原因").
const DISCONNECTED = (minutes) => `等待服务：翻译服务暂时连不上，恢复后继续（已 ${minutes} 分钟）`;
const MINUTE = 60000;
const ALREADY_ACTIVE = (title) => `${title}：已有未结束的翻译任务，本次提交已忽略`;

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
 *  keeps the saved state, and keeps the attachments in `library` ({id,
 *  target, title, text, addedAt, identity ("t1/mono") or null, deleted}),
 *  which a restart passes on. `failWrite` (a file name), `failRemove`,
 *  `failImport` and `failMark` (a reason) inject errors; `quitAfterImport`
 *  and `quitAfterMark` make Zotero quit there: the attachment is saved (with
 *  its identity, after mark) and the lifecycle never goes on. The notices
 *  of ended tasks go to `announced` (`announce` replaces that; `logError`
 *  replaces throwing). */
function environment({ routes = {}, saved = null, time = 0, library = [], announce = null, logError = null } = {}) {
  const env = {
    clock: fakeClock(time),
    announced: [],
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
    failImport: null,
    failMark: null,
    quitAfterImport: false,
    quitAfterMark: false,
    findHold: null, // a held() whose route each look-up waits for
    writeHold: null, // the same for writing a temporary file
    unmarkedHold: null, // and for looking for an attachment to claim
    library,
  };
  const identityOf = ({ taskId, output }) => `${taskId}/${output}`;
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
        if (env.writeHold) {
          await env.writeHold.route();
        }
        const path = `${dir}/${fileName}`;
        env.files.set(path, new TextDecoder().decode(bytes));
        return path;
      },
      remove: async (dir) => {
        env.steps.push(`remove ${dir}`);
        if (env.removeHold) {
          await env.removeHold();
        }
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
      if (env.failImport) {
        throw new Error(env.failImport);
      }
      const text = env.files.get(path);
      env.imports.push({ target, title, fileName: path.split("/").pop(), text });
      const id = 100 + env.library.length;
      env.library.push({ id, target, title, text, addedAt: env.clock.now(), identity: null, deleted: false });
      if (env.quitAfterImport) {
        return never();
      }
      return id;
    },
    attachments: {
      find: async (target, identity) => {
        env.steps.push(`find ${identityOf(identity)}`);
        if (env.findHold) {
          await env.findHold.route();
        }
        return env.library.find((a) => a.identity === identityOf(identity) && !a.deleted)?.id ?? null;
      },
      mark: async (id, identity) => {
        env.steps.push(`mark ${id} ${identityOf(identity)}`);
        if (env.failMark) {
          throw new Error(env.failMark);
        }
        env.library.find((a) => a.id === id).identity = identityOf(identity);
        if (env.quitAfterMark) {
          return never();
        }
      },
      unmarked: async (target, { title, since }) => {
        env.steps.push(`unmarked ${title} ${since}`);
        if (env.unmarkedHold) {
          await env.unmarkedHold.route();
        }
        const found = env.library.find(
          (a) => !a.identity && !a.deleted && a.title === title && a.addedAt >= since && JSON.stringify(a.target) === JSON.stringify(target),
        );
        return found ? found.id : null;
      },
    },
    store: {
      load: () => env.saved,
      save: (value) => {
        env.saved = value;
      },
    },
    announce: announce || ((notice) => env.announced.push(notice)),
    debug: () => {},
    logError:
      logError ||
      ((e) => {
        throw e;
      }),
  });
  /** The rows' status words and details, joined by statusText. */
  env.status = () => env.lifecycle.rows().map(statusText);
  /** The notices so far: [outcome, title, status and detail (statusText), results]. */
  env.notices = () => env.announced.map(({ outcome, row }) => [outcome, row.title, statusText(row), row.results]);
  /** The attachments the user sees: [title, text, identity]. */
  env.attached = () => env.library.filter((a) => !a.deleted).map((a) => [a.title, a.text, a.identity]);
  /** When (clock time) each request for `key` was sent. */
  env.times = (key) => env.requestTimes.filter(([k]) => k === key).map(([, t]) => t);
  return env;
}

const status = (state, extra = {}) => json(200, { state, stage: "翻译与审校", progress: 0, ...extra });
const QUERY = (taskId) => `GET ${SERVICE}/tasks/${taskId}`;
const MONO = (taskId) => `GET ${SERVICE}/tasks/${taskId}/files/mono`;
const DUAL = (taskId) => `GET ${SERVICE}/tasks/${taskId}/files/dual`;
const UPLOAD = `POST ${SERVICE}/tasks`;
const CANCEL = (taskId) => `POST ${SERVICE}/tasks/${taskId}/cancel`;
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
  assert.deepEqual(env.status(), ["排队中：第 2 位"]);
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
  assert.deepEqual([row.status, row.detail], ["失败", "管线模型拒绝了请求（HTTP 400）"]);
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
    {
      ...SOURCE,
      serviceUrl: SERVICE,
      taskId: "t1",
      state: "running",
      submittedAt: 0,
      disconnectedSince: null,
      generation: 0,
      attached: {},
      importIntents: {},
    },
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
  assert.deepEqual(env.lifecycle.notices, [ALREADY_ACTIVE("AutoDesign")]);

  await env.lifecycle.submit({ ...SOURCE, sourceItemID: 12, title: "Other" }); // another PDF is submitted
  assert.equal(env.requests.length, 2);

  env.lifecycle.clearNotices();
  assert.deepEqual(env.lifecycle.notices, []);
});

test("a standalone PDF whose source is gone fails to attach before anything is downloaded", async () => {
  const standalone = { ...SOURCE, itemID: null };
  const env = environment({ routes: { [UPLOAD]: json(201, { task_id: "t1" }), ...succeededRoutes("t1") } });
  env.lifecycle.start();
  await env.lifecycle.submit(standalone);
  env.missingSources.push(standalone.sourceItemID);
  await env.clock.advance(2000);
  assert.deepEqual(env.status(), ["挂回失败：找不到原 PDF 附件"]);
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
  assert.deepEqual(env.lifecycle.notices, [ALREADY_ACTIVE("AutoDesign")]);

  assert.deepEqual(JSON.parse(env.saved).map((t) => t.state), ["waiting_rerun"]);
  const after = environment({ saved: env.saved, routes: { [QUERY("t1")]: waiting } });
  after.lifecycle.start();
  await after.clock.advance(2000);
  assert.deepEqual(after.requests, [QUERY("t1")]);
  assert.match(after.status()[0], /^等待自动重跑/);
});

test("each PDF in turn is looked for, downloaded, imported with an intent saved, given its identity and recorded", async () => {
  const env = await submitted(succeededRoutes("t1"));
  const seen = [];
  env.routes[DUAL("t1")] = () => {
    seen.push(["download dual", savedOf(env)[0].importIntents, savedOf(env)[0].attached]);
    return pdfBytes("dual pdf");
  };
  await env.clock.advance(2000);
  assert.deepEqual(env.steps, [
    "find t1/mono",
    "mkdir t1",
    "write AutoDesign-中文译文.pdf",
    "import 中文译文",
    "mark 100 t1/mono",
    "find t1/dual",
    "write AutoDesign-双语对照.pdf",
    "import 双语对照",
    "mark 101 t1/dual",
    "remove /tmp/zotero-translate/t1",
  ]);
  // When dual was downloaded, mono was recorded and its intent cleared.
  assert.deepEqual(seen, [["download dual", {}, { mono: 100 }]]);
  assert.deepEqual(env.attached(), [["中文译文", "mono pdf", "t1/mono"], ["双语对照", "dual pdf", "t1/dual"]]);
  assert.deepEqual(env.status(), ["已挂回"]);
  assert.deepEqual(env.files, new Map());
});

test("the import intent is saved before the import, and the identity is written before the record", async () => {
  const env = await submitted(succeededRoutes("t1"));
  env.quitAfterImport = true;
  await env.clock.advance(2000);
  const [saved] = savedOf(env);
  assert.deepEqual(saved.importIntents, { mono: { taskId: "t1", output: "mono", startedAt: 2000 } });
  assert.deepEqual(saved.attached, {});
  assert.equal(rowOf(env).canCancel, false); // no 取消 while importing

  const marked = await submitted(succeededRoutes("t1"));
  marked.quitAfterMark = true;
  await marked.clock.advance(2000);
  assert.deepEqual(marked.attached(), [["中文译文", "mono pdf", "t1/mono"]]);
  assert.deepEqual(savedOf(marked)[0].attached, {}); // not yet recorded
  assert.deepEqual(Object.keys(savedOf(marked)[0].importIntents), ["mono"]);
});

test("when writing the second PDF fails, the first stays attached, the task fails to attach and the temporary directory is removed", async () => {
  const env = await submitted(succeededRoutes("t1"));
  env.failWrite = "AutoDesign-双语对照.pdf";
  await env.clock.advance(2000);
  assert.deepEqual(env.attached(), [["中文译文", "mono pdf", "t1/mono"]]);
  assert.deepEqual(env.status(), ["挂回失败：磁盘已满"]);
  assert.equal(env.steps.at(-1), "remove /tmp/zotero-translate/t1");
  assert.deepEqual(env.files, new Map());
});

test("when removing the temporary directory fails, both PDFs stay attached and the task fails to attach", async () => {
  const env = await submitted(succeededRoutes("t1"));
  env.failRemove = true;
  await env.clock.advance(2000);
  assert.deepEqual(env.imports.map((i) => i.title), ["中文译文", "双语对照"]);
  assert.deepEqual(env.status(), ["挂回失败：无法删除临时目录"]);
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
  "an empty task state": () => status(""),
  "a task state the plugin does not know": () => status("paused"),
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
    assert.deepEqual(env.status(), [DISCONNECTED(1)]);
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

test("after an answer with a state the plugin does not know, a valid answer brings back polling every 2 seconds", async () => {
  const env = await submitted({ [QUERY("t1")]: status("running", { progress: 10 }) });
  await env.clock.advance(2000); // t=2s: running
  env.routes[QUERY("t1")] = status("paused");
  await env.clock.advance(2000); // t=4s: not understood
  assert.equal(env.lifecycle.tasks[0].state, "running");
  assert.equal(env.lifecycle.tasks[0].disconnectedSince, 4000);
  assert.deepEqual(JSON.parse(env.saved).map((t) => [t.taskId, t.state]), [["t1", "running"]]);

  env.routes[QUERY("t1")] = status("running", { progress: 50 });
  await env.clock.advance(10000); // t=14s
  assert.deepEqual(env.status(), ["翻译中"]);
  assert.equal(env.lifecycle.tasks[0].progress, 50);
  assert.equal(env.lifecycle.tasks[0].disconnectedSince, null);
  await env.clock.advance(4000);
  assert.deepEqual(env.times(QUERY("t1")), [2000, 4000, 14000, 16000, 18000]);
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
  assert.deepEqual(env.status(), [DISCONNECTED(1)]);
});

test("an error status whose body never ends is aborted, and the task's next query starts only after", async () => {
  // A request is settled once its signal is aborted; the fake body never ends on its own.
  const signals = [];
  const unsettledAtStart = [];
  const env = await submitted({
    [QUERY("t1")]: (options) => {
      unsettledAtStart.push(signals.filter((signal) => !signal.aborted).length);
      signals.push(options.signal);
      return new Response(new ReadableStream({ start() {} }), { status: 503 });
    },
  });
  await env.clock.advance(2000); // t=2s: the first query gets the 503 headers
  assert.equal(signals.length, 1);
  assert.equal(signals[0].aborted, true);
  assert.equal(env.lifecycle.tasks[0].disconnectedSince, 2000);

  await env.clock.advance(30000);
  assert.deepEqual(env.times(QUERY("t1")), [2000, 12000, 22000, 32000]);
  assert.deepEqual(unsettledAtStart, [0, 0, 0, 0]); // each query started with no earlier one still open
  assert.ok(signals.every((signal) => signal.aborted));
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
  assert.deepEqual(env.status(), [DISCONNECTED(1), "翻译中"]);
});

test("tasks saved before disconnections were recorded are restored and queried every 2 seconds", async () => {
  const saved = JSON.stringify([
    { ...SOURCE, serviceUrl: SERVICE, taskId: "t1", state: "running" },
    { ...SOURCE, sourceItemID: 12, title: "Other", serviceUrl: SERVICE, taskId: "t2", state: "queued" },
  ]);
  const env = environment({ saved, routes: { [QUERY("t1")]: status("running"), [QUERY("t2")]: status("queued") } });
  env.lifecycle.start();
  assert.deepEqual(env.status(), ["翻译中", "排队中"]);
  assert.deepEqual(JSON.parse(env.saved).map((t) => [t.taskId, t.state, t.disconnectedSince, t.generation]), [
    ["t1", "running", null, 0],
    ["t2", "queued", null, 0],
  ]);
  await env.clock.advance(4000);
  assert.deepEqual(env.times(QUERY("t1")), [2000, 4000]);
  assert.deepEqual(env.times(QUERY("t2")), [2000, 4000]);
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
    {
      ...SOURCE,
      serviceUrl: SERVICE,
      taskId: null,
      state: "submitting",
      submittedAt: 0,
      disconnectedSince: null,
      generation: 0,
      error: null,
      attached: {},
      importIntents: {},
    },
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
  "HTTP 403": () => json(403, { detail: "forbidden" }),
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
    assert.match(env.status()[0], /^等待服务：翻译服务暂时连不上，恢复后继续（已 \d 分钟）$/);
    assert.deepEqual(env.attached(), [["中文译文", "mono pdf", "t1/mono"]]); // the first is attached
    assert.deepEqual(env.files, new Map());
    assert.deepEqual(savedOf(env).map((t) => [t.state, t.attached, t.importIntents]), [["attaching", { mono: 100 }, {}]]);

    env.routes[DUAL("t1")] = pdfBytes("dual pdf");
    await env.clock.advance(10000);
    assert.deepEqual(env.status(), ["已挂回"]);
    assert.deepEqual(env.imports.map((i) => [i.title, i.text]), [["中文译文", "mono pdf"], ["双语对照", "dual pdf"]]);
    assert.equal(env.times(MONO("t1")).length, 1); // found with its identity, not downloaded again
    assert.equal(env.times(QUERY("t1")).length, 1); // the service is not asked again; the downloads are
  });
}

test("a disconnection that began before attaching keeps its start until both PDFs are attached", async () => {
  const env = await submitted(succeededRoutes("t1"));
  delete env.routes[MONO("t1")];
  await env.clock.advance(2000); // t=2s: succeeded; the first download cannot connect
  assert.equal(env.lifecycle.tasks[0].disconnectedSince, 2000);

  env.routes[MONO("t1")] = pdfBytes("mono pdf");
  delete env.routes[DUAL("t1")];
  await env.clock.advance(10000); // t=12s: mono downloads, dual cannot connect
  assert.deepEqual(env.times(MONO("t1")), [2000, 12000]);
  assert.equal(env.lifecycle.tasks[0].disconnectedSince, 2000);
  assert.equal(JSON.parse(env.saved)[0].disconnectedSince, 2000);

  env.routes[DUAL("t1")] = pdfBytes("dual pdf");
  await env.clock.advance(10000); // t=22s: both download and are attached
  assert.deepEqual(env.status(), ["已挂回"]);
  assert.equal(env.lifecycle.tasks[0].disconnectedSince, null);
});

test("a task that is downloading does not hold up the queries of other tasks", async () => {
  const env = environment({ routes: { ...succeededRoutes("t1"), [QUERY("t2")]: status("running") } });
  env.routes[MONO("t1")] = () => new Promise((resolve) => env.clock.setTimeout(() => resolve(pdfBytes("mono pdf")), 100000));
  env.lifecycle.start();
  env.routes[UPLOAD] = json(201, { task_id: "t1" });
  await env.lifecycle.submit(SOURCE);
  env.routes[UPLOAD] = json(201, { task_id: "t2" });
  await env.lifecycle.submit({ ...SOURCE, sourceItemID: 12, title: "Other" });

  await env.clock.advance(60000); // t1 downloads from 2s to 102s
  assert.deepEqual(env.lifecycle.tasks.map((t) => t.state), ["attaching", "running"]);
  assert.deepEqual(env.times(QUERY("t2")), Array.from({ length: 30 }, (_, i) => 2000 * (i + 1)));
  await env.clock.advance(50000);
  assert.deepEqual(env.status(), ["已挂回", "翻译中"]);
  assert.equal(env.times(QUERY("t2")).length, 55);
});

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

// ---- cancel, retry and duplicate submissions (ticket 08) ----

const CANCELLING = "取消中";
const CANCELLING_DISCONNECTED = "取消中：翻译服务暂时连不上";
const cancelledAnswer = (taskId, code = 200) => json(code, { task_id: taskId, state: code === 200 ? "cancelled" : "succeeded", cancel_requested: code === 200 });
const rowOf = (env, index = 0) => env.lifecycle.rows()[index];
const savedOf = (env) => JSON.parse(env.saved);

/** A request the test answers later: `route` for env.routes, `answer(response)` to answer it. */
function held() {
  const pending = [];
  return {
    route: () => new Promise((resolve) => pending.push(resolve)),
    answer: (response) => pending.shift()(response),
    get count() {
      return pending.length;
    },
  };
}

test("queued, running and waiting rows, also while disconnected, have 取消; other rows do not", async () => {
  const env = await submitted({ [QUERY("t1")]: status("queued", { queue_position: 1 }) });
  assert.equal(rowOf(env).canCancel, true); // queued, before the first query
  await env.clock.advance(2000);
  assert.equal(rowOf(env).canCancel, true);
  env.routes[QUERY("t1")] = status("waiting_rerun", { reruns_used: 0, reruns_max: 2, next_attempt_in_seconds: 60, error: "HTTP 500" });
  await env.clock.advance(2000);
  assert.equal(rowOf(env).canCancel, true);
  delete env.routes[QUERY("t1")];
  await env.clock.advance(2000);
  assert.deepEqual(env.status(), [DISCONNECTED(1)]);
  assert.equal(env.lifecycle.tasks[0].state, "waiting_rerun");
  assert.equal(rowOf(env).canCancel, true);
  env.routes[QUERY("t1")] = status("running");
  await env.clock.advance(10000);
  assert.deepEqual(env.status(), ["翻译中"]);
  assert.equal(rowOf(env).canCancel, true);

  const submitting = environment({ routes: { [UPLOAD]: never } });
  submitting.lifecycle.start();
  submitting.lifecycle.submit(SOURCE);
  await settle();
  assert.equal(rowOf(submitting).canCancel, false);

  const done = await submitted(succeededRoutes("t1"));
  await done.clock.advance(2000);
  assert.deepEqual(done.status(), ["已挂回"]);
  assert.equal(rowOf(done).canCancel, false);
  const failed = await submitted({ [QUERY("t1")]: status("failed", { error: "x" }) });
  await failed.clock.advance(2000);
  assert.equal(rowOf(failed).canCancel, false);
});

for (const code of [200, 409, 404]) {
  test(`a cancel saves the intent, sends the cancel at once, stops querying, and on ${code} the row is 已取消 with 重试`, async () => {
    const env = await submitted({ [QUERY("t1")]: status("queued", { queue_position: 1 }) });
    await env.clock.advance(2000);
    const task = env.lifecycle.tasks[0];
    let cancelSeenSaved = null;
    env.routes[CANCEL("t1")] = () => {
      cancelSeenSaved = savedOf(env); // what was saved when the request went out
      return code === 404 ? json(404, { error: "翻译服务中找不到该任务" }) : cancelledAnswer("t1", code);
    };
    await env.lifecycle.cancel(rowOf(env).key);
    assert.deepEqual(cancelSeenSaved.map((t) => [t.taskId, t.state]), [["t1", "cancelling"]]);
    assert.equal(env.requests.at(-1), CANCEL("t1"));
    assert.equal(env.clock.now(), 2000); // at once
    assert.deepEqual(env.status(), ["已取消"]);
    assert.equal(rowOf(env).canRetry, true);
    assert.equal(rowOf(env).canCancel, false);
    assert.equal(env.saved, "[]");
    assert.equal(task.state, "cancelled");

    env.routes[QUERY("t1")] = status("succeeded");
    await env.clock.advance(60000);
    assert.equal(env.times(QUERY("t1")).length, 1); // no query after the click
    assert.deepEqual(env.imports, []);
    assert.equal(env.clock.timers.size, 0);
  });
}

test("a running task's cancel is sent at once; 200 makes the row 已取消, nothing attached", async () => {
  const env = await submitted({ [QUERY("t1")]: status("running") });
  await env.clock.advance(2000);
  assert.deepEqual(env.status(), ["翻译中"]);
  env.routes[CANCEL("t1")] = cancelledAnswer("t1", 200);
  await env.lifecycle.cancel(rowOf(env).key);
  assert.equal(env.requests.at(-1), CANCEL("t1"));
  assert.deepEqual(env.status(), ["已取消"]);
  assert.equal(env.lifecycle.tasks[0].state, "cancelled");
  env.routes[QUERY("t1")] = status("succeeded");
  await env.clock.advance(60000);
  assert.equal(env.times(QUERY("t1")).length, 1);
  assert.deepEqual(env.imports, []);
});

test("a running task's cancel answered 503 shows 取消中 with the service's reason", async () => {
  const env = await submitted({ [QUERY("t1")]: status("running") });
  await env.clock.advance(2000);
  env.routes[CANCEL("t1")] = json(503, { task_id: "t1", state: "running", cancel_requested: true, cleanup_error: "无法扫描进程：OSError: x；暂停调度新的尝试" });
  await env.lifecycle.cancel(rowOf(env).key);
  assert.deepEqual(env.status(), ["取消中：无法扫描进程：OSError: x；暂停调度新的尝试"]);
  assert.equal(rowOf(env).canCancel, false);
  env.routes[CANCEL("t1")] = cancelledAnswer("t1", 200);
  await env.clock.advance(10000);
  assert.deepEqual(env.status(), ["已取消"]);
  assert.deepEqual(env.imports, []);
});

test("a cancel answered 503 shows 取消中 with the service's reason and is sent again every 10 seconds until 200", async () => {
  const env = await submitted({ [QUERY("t1")]: status("queued") });
  await env.clock.advance(2000);
  env.routes[CANCEL("t1")] = json(503, { task_id: "t1", state: "queued", cancel_requested: true, cleanup_error: "进程回收没有完成；暂停调度新的尝试" });
  await env.lifecycle.cancel(rowOf(env).key);
  assert.deepEqual(env.status(), ["取消中：进程回收没有完成；暂停调度新的尝试"]);
  assert.equal(rowOf(env).canCancel, false);
  assert.equal(rowOf(env).canRetry, false);
  await env.clock.advance(30000);
  assert.deepEqual(env.times(CANCEL("t1")), [2000, 12000, 22000, 32000]);
  assert.equal(env.times(QUERY("t1")).length, 1);

  env.routes[CANCEL("t1")] = cancelledAnswer("t1");
  await env.clock.advance(10000);
  assert.deepEqual(env.status(), ["已取消"]);
  await env.clock.advance(60000);
  assert.equal(env.times(CANCEL("t1")).length, 5);
});

const UNANSWERED_CANCELS = {
  "no connection": () => refused,
  "no full answer within 30 seconds": () => stuckBody,
  "HTTP 500": () => json(500, { detail: "boom" }),
  "HTTP 502 from a proxy": () => new Response("Bad Gateway", { status: 502 }),
  "a 200 answer that is not JSON": () => new Response("<html>", { status: 200 }),
  "a 200 answer whose state is not cancelled": () => json(200, { task_id: "t1", state: "queued", cancel_requested: true }),
  "a 503 answer that is not JSON": () => new Response("Service Unavailable", { status: 503 }),
};

for (const [name, route] of Object.entries(UNANSWERED_CANCELS)) {
  test(`a cancel with ${name} stays 取消中 (the service cannot be reached) and is sent again every 10 seconds`, async () => {
    const env = await submitted({ [QUERY("t1")]: status("queued") });
    await env.clock.advance(2000);
    env.routes[CANCEL("t1")] = route();
    const cancelling = env.lifecycle.cancel(rowOf(env).key);
    await env.clock.advance(30000);
    await cancelling;
    assert.equal(env.lifecycle.tasks[0].state, "cancelling");
    assert.ok(env.lifecycle.tasks[0].disconnectedSince != null);
    assert.deepEqual(env.status(), [CANCELLING_DISCONNECTED]);
    assert.equal(savedOf(env)[0].disconnectedSince, env.lifecycle.tasks[0].disconnectedSince);

    await env.clock.advance(60000);
    const times = env.times(CANCEL("t1"));
    assert.ok(times.length >= 3, `${times}`);
    for (let i = 1; i < times.length; i++) {
      const gap = times[i] - times[i - 1];
      assert.ok(gap === 10000 || gap === 40000, `gap ${gap}`); // 10 s after the previous one ended (30 s limit)
    }
    assert.equal(env.times(QUERY("t1")).length, 1);

    env.routes[CANCEL("t1")] = cancelledAnswer("t1");
    await env.clock.advance(40000);
    assert.deepEqual(env.status(), ["已取消"]);
  });
}

test("the cancel request has a 30-second time limit", async () => {
  let signal;
  const env = await submitted({ [QUERY("t1")]: status("queued") });
  await env.clock.advance(2000);
  env.routes[CANCEL("t1")] = (options) => {
    signal = options.signal;
    return never();
  };
  env.lifecycle.cancel(rowOf(env).key);
  await env.clock.advance(29999);
  assert.equal(signal.aborted, false);
  assert.equal(env.lifecycle.tasks[0].disconnectedSince, null);
  assert.deepEqual(env.status(), [CANCELLING]);
  await env.clock.advance(1);
  assert.equal(signal.aborted, true);
  assert.equal(env.lifecycle.tasks[0].disconnectedSince, 32000);
});

test("a cancel while the service cannot be reached shows 取消中, survives a Zotero restart, and once sent nothing is attached", async () => {
  const before = await submitted({ [QUERY("t1")]: status("waiting_rerun", { reruns_used: 0, reruns_max: 2, next_attempt_in_seconds: 60 }) });
  await before.clock.advance(2000);
  delete before.routes[QUERY("t1")]; // the service stops
  await before.clock.advance(2000);
  assert.deepEqual(before.status(), [DISCONNECTED(1)]);
  await before.lifecycle.cancel(rowOf(before).key);
  assert.deepEqual(before.status(), [CANCELLING_DISCONNECTED]);
  assert.equal(before.lifecycle.tasks[0].disconnectedSince, 4000); // kept from before the click
  before.lifecycle.stop();
  assert.deepEqual(savedOf(before).map((t) => [t.taskId, t.state, t.disconnectedSince]), [["t1", "cancelling", 4000]]);

  // The service is back and has run the task to its end meanwhile.
  const after = environment({ saved: before.saved, routes: succeededRoutes("t1"), time: 4000 + 3 * MINUTE });
  after.lifecycle.start();
  assert.deepEqual(after.status(), [CANCELLING_DISCONNECTED]);
  assert.equal(rowOf(after).canCancel, false);
  await after.clock.advance(10000);
  assert.deepEqual(after.requests, [CANCEL("t1")]);
  assert.deepEqual(after.status(), [CANCELLING_DISCONNECTED]);

  after.routes[CANCEL("t1")] = cancelledAnswer("t1", 409); // the service says it succeeded
  await after.clock.advance(10000);
  assert.deepEqual(after.status(), ["已取消"]);
  await after.clock.advance(60000);
  assert.deepEqual(after.requests, [CANCEL("t1"), CANCEL("t1")]); // never queried, never downloaded
  assert.deepEqual(after.imports, []);
});

test("a cancelling task restored without a disconnection sends its cancel 2 seconds after start", async () => {
  const saved = JSON.stringify([{ ...SOURCE, serviceUrl: SERVICE, taskId: "t1", state: "cancelling", disconnectedSince: null, generation: 0 }]);
  const env = environment({ saved, routes: { [CANCEL("t1")]: cancelledAnswer("t1") } });
  env.lifecycle.start();
  assert.deepEqual(env.status(), [CANCELLING]);
  await env.clock.advance(2000);
  assert.deepEqual(env.requests, [CANCEL("t1")]);
  assert.deepEqual(env.status(), ["已取消"]);
});

test("a query sent before the click whose answer comes after it is dropped; cancel and success together: cancel wins", async () => {
  const query = held();
  const env = await submitted({ [QUERY("t1")]: query.route, [MONO("t1")]: pdfBytes("mono pdf"), [DUAL("t1")]: pdfBytes("dual pdf") });
  await env.clock.advance(2000); // the query is out
  assert.equal(query.count, 1);
  const cancel = held();
  env.routes[CANCEL("t1")] = cancel.route;
  const cancelling = env.lifecycle.cancel(rowOf(env).key);
  await settle();
  assert.deepEqual(env.status(), [CANCELLING]);

  query.answer(status("succeeded", { stage: "完成", progress: 100 })); // the service finished just then
  await settle();
  assert.deepEqual(env.status(), [CANCELLING]);
  assert.equal(env.lifecycle.tasks[0].progress, null); // the dropped answer set nothing
  cancel.answer(cancelledAnswer("t1", 409));
  await cancelling;
  assert.deepEqual(env.status(), ["已取消"]);
  await env.clock.advance(60000);
  assert.deepEqual(env.requests, [UPLOAD, QUERY("t1"), CANCEL("t1")]);
  assert.deepEqual(env.imports, []);
});

for (const [name, answer] of Object.entries({
  "failed": () => status("failed", { error: "x" }),
  "404": () => json(404, {}),
  "no connection": () => {
    throw new TypeError("NetworkError");
  },
})) {
  test(`a query answered ${name} after the click changes nothing`, async () => {
    const query = held();
    const env = await submitted({ [QUERY("t1")]: query.route });
    await env.clock.advance(2000);
    const cancel = held();
    env.routes[CANCEL("t1")] = cancel.route;
    const cancelling = env.lifecycle.cancel(rowOf(env).key);
    await settle();
    let response;
    try {
      response = answer();
    } catch (e) {
      response = Promise.reject(e);
    }
    query.answer(response);
    await settle();
    assert.equal(env.lifecycle.tasks[0].state, "cancelling");
    assert.equal(env.lifecycle.tasks[0].disconnectedSince, null);
    assert.equal(env.lifecycle.tasks[0].error, null);
    cancel.answer(cancelledAnswer("t1"));
    await cancelling;
    assert.deepEqual(env.status(), ["已取消"]);
  });
}

test("the query under way when 取消 is clicked is aborted", async () => {
  let signal;
  const env = await submitted({
    [QUERY("t1")]: (options) => {
      signal = options.signal;
      return never();
    },
    [CANCEL("t1")]: cancelledAnswer("t1"),
  });
  await env.clock.advance(2000);
  assert.equal(signal.aborted, false);
  await env.lifecycle.cancel(rowOf(env).key);
  assert.equal(signal.aborted, true);
  assert.deepEqual(env.status(), ["已取消"]);
});

test("the service saying cancelled ends the task as cancelled", async () => {
  const env = await submitted({ [QUERY("t1")]: status("cancelled", { cancel_requested: true }) });
  await env.clock.advance(2000);
  assert.deepEqual(env.status(), ["已取消"]);
  assert.equal(rowOf(env).canRetry, true);
  assert.equal(env.saved, "[]");
  await env.clock.advance(60000);
  assert.equal(env.times(QUERY("t1")).length, 1);
});

test("a retry of a cancelled task starts a new generation with nothing of the old task, whose late answers change nothing", async () => {
  const query = held();
  const env = await submitted({ [QUERY("t1")]: status("waiting_rerun", { attempt: 1, reruns_used: 0, reruns_max: 2, next_attempt_in_seconds: 60, error: "HTTP 500" }) });
  await env.clock.advance(2000);
  delete env.routes[QUERY("t1")];
  await env.clock.advance(2000); // disconnected since 4 s
  env.routes[QUERY("t1")] = query.route;
  await env.clock.advance(10000); // t=14s: a query is out that the old service answers late
  env.routes[CANCEL("t1")] = cancelledAnswer("t1");
  await env.lifecycle.cancel(rowOf(env).key);
  assert.deepEqual(env.status(), ["已取消"]);
  const old = { ...env.lifecycle.tasks[0] };
  assert.equal(old.generation, 0);

  env.routes[UPLOAD] = json(201, { task_id: "t2" });
  env.routes[QUERY("t2")] = status("queued", { queue_position: 3 });
  await env.lifecycle.retry(rowOf(env).key);
  const task = env.lifecycle.tasks[0];
  assert.equal(task.taskId, "t2");
  assert.equal(task.generation, 1);
  assert.equal(task.disconnectedSince, null);
  assert.equal(task.error, null);
  assert.equal(task.cleanupError, null);
  for (const field of ["attempt", "rerunsUsed", "rerunsMax", "nextAttemptInSeconds", "queuePosition", "stage", "progress"]) {
    assert.equal(task[field], null, field);
  }
  assert.deepEqual(savedOf(env).map((t) => [t.taskId, t.state, t.generation]), [["t2", "queued", 1]]);

  query.answer(status("succeeded", { stage: "完成", progress: 100 })); // t1's late answer
  await settle();
  assert.equal(task.state, "queued");
  assert.equal(task.taskId, "t2");
  await env.clock.advance(6000);
  assert.deepEqual(env.times(QUERY("t2")), [16000, 18000, 20000]); // one schedule, every 2 seconds
  assert.deepEqual(env.status(), ["排队中：第 3 位"]);
  assert.ok(!env.requests.some((r) => r.includes("/files/")));
});

test("a source PDF in any unfinished state is not submitted again; once cancelled it can be", async () => {
  const env = await submitted({ [QUERY("t1")]: status("queued") });
  await env.clock.advance(2000);
  delete env.routes[CANCEL("t1")];
  await env.lifecycle.cancel(rowOf(env).key); // cannot be reached: stays cancelling
  assert.equal(env.lifecycle.tasks[0].state, "cancelling");
  await env.lifecycle.submit(SOURCE);
  assert.equal(env.lifecycle.tasks.length, 1);
  assert.deepEqual(env.lifecycle.notices, [ALREADY_ACTIVE("AutoDesign")]);

  env.routes[CANCEL("t1")] = cancelledAnswer("t1");
  await env.clock.advance(10000);
  assert.deepEqual(env.status(), ["已取消"]);
  env.routes[UPLOAD] = json(201, { task_id: "t2" });
  await env.lifecycle.submit(SOURCE);
  assert.deepEqual(env.lifecycle.tasks.map((t) => [t.taskId, t.state]), [["t1", "cancelled"], ["t2", "queued"]]);

  // The cancelled row's retry is refused while the same PDF has an unfinished task.
  env.lifecycle.clearNotices();
  await env.lifecycle.retry(rowOf(env, 0).key);
  assert.equal(env.lifecycle.tasks[0].state, "cancelled");
  assert.deepEqual(env.lifecycle.notices, [ALREADY_ACTIVE("AutoDesign")]);
  assert.equal(env.requests.filter((r) => r === UPLOAD).length, 2);
});

// ---- attaching without duplicates, 继续挂回, cancel while attaching (ticket 10) ----

const ATTACH_FAILED = (reason) => `挂回失败：${reason}`;
const UPGRADE_ATTACH_ERROR = "升级前的挂回进度不明：请先检查原条目下的附件，再选择继续挂回或取消";

/** A task whose service has succeeded, attached as far as `setup(env)` lets it go, then Zotero quits. */
async function quitWhileAttaching(setup) {
  const before = await submitted(succeededRoutes("t1"));
  setup(before);
  await before.clock.advance(2000);
  before.lifecycle.stop();
  return before;
}

test("Zotero quit after an import was identified but before it was recorded: after a restart it is found, not imported again", async () => {
  const before = await quitWhileAttaching((env) => {
    env.quitAfterMark = true;
  });
  assert.deepEqual(savedOf(before).map((t) => [t.state, t.attached, Object.keys(t.importIntents)]), [["attaching", {}, ["mono"]]]);

  const after = environment({ saved: before.saved, routes: succeededRoutes("t1"), library: before.library, time: 60000 });
  after.lifecycle.start();
  await after.clock.advance(2000);
  assert.deepEqual(after.status(), ["已挂回"]);
  assert.deepEqual(after.requests, [DUAL("t1")]); // mono is not downloaded again
  assert.deepEqual(after.imports.map((i) => i.title), ["双语对照"]);
  assert.deepEqual(after.attached(), [["中文译文", "mono pdf", "t1/mono"], ["双语对照", "dual pdf", "t1/dual"]]);
});

test("Zotero quit after an attachment was saved but before its identity was written: after a restart the intent claims it", async () => {
  const before = await quitWhileAttaching((env) => {
    env.quitAfterImport = true;
  });
  assert.deepEqual(before.attached(), [["中文译文", "mono pdf", null]]);
  assert.deepEqual(savedOf(before)[0].importIntents, { mono: { taskId: "t1", output: "mono", startedAt: 2000 } });

  const after = environment({ saved: before.saved, routes: succeededRoutes("t1"), library: before.library, time: 60000 });
  after.lifecycle.start();
  await after.clock.advance(2000);
  assert.deepEqual(after.status(), ["已挂回"]);
  assert.ok(after.steps.includes("unmarked 中文译文 2000"));
  assert.ok(after.steps.includes("mark 100 t1/mono")); // the identity is written on the saved attachment
  assert.deepEqual(after.requests, [DUAL("t1")]);
  assert.deepEqual(after.attached(), [["中文译文", "mono pdf", "t1/mono"], ["双语对照", "dual pdf", "t1/dual"]]);
  assert.equal(savedOf(after).length, 0);
});

test("the intent claims only an attachment of its output's title, without identity, added no earlier than the intent", async () => {
  // Earlier attachments with the same title: one added before the intent, one with another task's identity.
  const library = [
    { id: 1, target: { parentItemID: 10 }, title: "中文译文", text: "old", addedAt: 0, identity: null, deleted: false },
    { id: 2, target: { parentItemID: 10 }, title: "中文译文", text: "other", addedAt: 5000, identity: "t0/mono", deleted: false },
    { id: 3, target: { parentItemID: 10 }, title: "双语对照", text: "dual?", addedAt: 5000, identity: null, deleted: false },
  ];
  const saved = JSON.stringify([
    {
      ...SOURCE,
      serviceUrl: SERVICE,
      taskId: "t1",
      state: "attaching",
      disconnectedSince: null,
      generation: 0,
      error: null,
      attached: {},
      importIntents: { mono: { taskId: "t1", output: "mono", startedAt: 4000 } },
    },
  ]);
  const env = environment({ saved, routes: succeededRoutes("t1"), library, time: 60000 });
  env.lifecycle.start();
  await env.clock.advance(2000);
  assert.deepEqual(env.status(), ["已挂回"]);
  assert.deepEqual(env.imports.map((i) => i.title), ["中文译文", "双语对照"]); // none was claimed
  assert.ok(!env.steps.includes("unmarked 双语对照 4000")); // dual had no intent
  assert.deepEqual(library.slice(0, 3).map((a) => a.identity), [null, "t0/mono", null]);
});

test("Zotero quit in the second PDF's crash window: after a restart, finding the first does not drop the second's intent", async () => {
  const before = await quitWhileAttaching((env) => {
    env.routes[DUAL("t1")] = () => {
      env.quitAfterImport = true; // mono is attached in full; dual is saved without identity
      return pdfBytes("dual pdf");
    };
  });
  assert.deepEqual(before.attached(), [["中文译文", "mono pdf", "t1/mono"], ["双语对照", "dual pdf", null]]);
  assert.deepEqual(savedOf(before).map((t) => [t.attached, Object.keys(t.importIntents)]), [[{ mono: 100 }, ["dual"]]]);

  const after = environment({ saved: before.saved, routes: succeededRoutes("t1"), library: before.library, time: 60000 });
  after.lifecycle.start();
  await after.clock.advance(2000);
  assert.deepEqual(after.status(), ["已挂回"]);
  assert.deepEqual(after.requests, []);
  assert.deepEqual(after.imports, []);
  assert.deepEqual(after.attached(), [["中文译文", "mono pdf", "t1/mono"], ["双语对照", "dual pdf", "t1/dual"]]);
});

test("an intent waiting to claim the second PDF survives importing the first again (deleted by the user)", async () => {
  const before = await quitWhileAttaching((env) => {
    env.routes[DUAL("t1")] = () => {
      env.quitAfterImport = true;
      return pdfBytes("dual pdf");
    };
  });
  before.library[0].deleted = true; // the user deletes mono while Zotero is closed... or before it restarts

  const after = environment({ saved: before.saved, routes: succeededRoutes("t1"), library: before.library, time: 60000 });
  after.lifecycle.start();
  await after.clock.advance(2000);
  assert.deepEqual(after.status(), ["已挂回"]);
  assert.deepEqual(after.imports.map((i) => i.title), ["中文译文"]); // dual is claimed, not imported again
  assert.deepEqual(after.attached(), [["双语对照", "dual pdf", "t1/dual"], ["中文译文", "mono pdf", "t1/mono"]]);
});

test("a recorded attachment the user has deleted is imported again when attaching goes on", async () => {
  const env = await submitted(succeededRoutes("t1"));
  env.failWrite = "AutoDesign-双语对照.pdf";
  await env.clock.advance(2000);
  assert.deepEqual(savedOf(env)[0].attached, { mono: 100 });
  env.library[0].deleted = true; // the user deletes the attached mono
  env.failWrite = null;

  await env.lifecycle.continueAttach(rowOf(env).key);
  assert.deepEqual(env.status(), ["已挂回"]);
  assert.deepEqual(env.attached(), [["中文译文", "mono pdf", "t1/mono"], ["双语对照", "dual pdf", "t1/dual"]]);
  assert.deepEqual(env.library.map((a) => [a.id, a.title, a.deleted]), [[100, "中文译文", true], [101, "中文译文", false], [102, "双语对照", false]]);
  assert.deepEqual(env.times(MONO("t1")).length, 2);
});

test("a local import error makes the row 挂回失败 with the reason; 继续挂回 attaches the rest without duplicates", async () => {
  const env = await submitted(succeededRoutes("t1"));
  env.routes[DUAL("t1")] = () => {
    env.failImport = "无法写入 Zotero 存储目录"; // mono is imported; dual is not
    return pdfBytes("dual pdf");
  };
  await env.clock.advance(2000);
  assert.deepEqual(env.status(), [ATTACH_FAILED("无法写入 Zotero 存储目录")]);
  const row = rowOf(env);
  assert.deepEqual([row.canContinue, row.canCancel, row.canRetry], [true, true, false]);
  assert.deepEqual(env.attached(), [["中文译文", "mono pdf", "t1/mono"]]);
  assert.deepEqual(savedOf(env).map((t) => [t.state, t.error, t.attached]), [["attach_failed", "无法写入 Zotero 存储目录", { mono: 100 }]]);

  const requests = env.requests.length;
  await env.clock.advance(60000); // nothing happens until the user acts
  assert.equal(env.requests.length, requests);
  await env.lifecycle.submit(SOURCE); // unfinished: no second task
  assert.deepEqual(env.lifecycle.notices, [ALREADY_ACTIVE("AutoDesign")]);

  // It survives a Zotero restart.
  env.lifecycle.stop();
  const after = environment({ saved: env.saved, routes: succeededRoutes("t1"), library: env.library, time: env.clock.now() });
  after.lifecycle.start();
  assert.deepEqual(after.status(), [ATTACH_FAILED("无法写入 Zotero 存储目录")]);
  await after.clock.advance(60000);
  assert.deepEqual(after.requests, []);

  assert.equal(after.failImport, null);
  await after.lifecycle.continueAttach(rowOf(after).key);
  assert.deepEqual(after.status(), ["已挂回"]);
  assert.deepEqual(after.requests, [DUAL("t1")]);
  assert.deepEqual(after.attached(), [["中文译文", "mono pdf", "t1/mono"], ["双语对照", "dual pdf", "t1/dual"]]);
  assert.equal(after.saved, "[]");
});

test("an identity that cannot be written makes the row 挂回失败; 继续挂回 claims that attachment instead of importing it again", async () => {
  const env = await submitted(succeededRoutes("t1"));
  env.failMark = "数据库已锁定";
  await env.clock.advance(2000);
  assert.deepEqual(env.status(), [ATTACH_FAILED("数据库已锁定")]);
  assert.deepEqual(env.attached(), [["中文译文", "mono pdf", null]]);
  assert.deepEqual(Object.keys(savedOf(env)[0].importIntents), ["mono"]);

  env.failMark = null;
  await env.lifecycle.continueAttach(rowOf(env).key);
  assert.deepEqual(env.status(), ["已挂回"]);
  assert.deepEqual(env.imports.map((i) => i.title), ["中文译文", "双语对照"]);
  assert.deepEqual(env.attached(), [["中文译文", "mono pdf", "t1/mono"], ["双语对照", "dual pdf", "t1/dual"]]);
});

test("取消 while a download is under way aborts it; the download that comes back afterwards is not imported", async () => {
  const dual = held();
  let signal = null;
  let savedAtAbort = null;
  const env = await submitted({
    ...succeededRoutes("t1"),
    [DUAL("t1")]: (options) => {
      signal = options.signal;
      signal.addEventListener("abort", () => {
        savedAtAbort = env.saved; // what was saved when the download was aborted
      });
      return dual.route();
    },
  });
  await env.clock.advance(2000);
  assert.equal(dual.count, 1);
  assert.equal(rowOf(env).canCancel, true);

  await env.lifecycle.cancel(rowOf(env).key);
  assert.equal(signal.aborted, true);
  assert.equal(savedAtAbort, "[]"); // the cancel was saved (no longer unfinished) before the download was aborted
  assert.deepEqual(env.status(), ["已取消"]);
  assert.equal(rowOf(env).canRetry, true);
  assert.equal(env.saved, "[]");
  dual.answer(pdfBytes("dual pdf")); // the download succeeds after the click
  await env.clock.advance(60000);
  assert.deepEqual(env.attached(), [["中文译文", "mono pdf", "t1/mono"]]); // kept; dual not imported
  assert.deepEqual(env.status(), ["已取消"]);
  assert.ok(!env.requests.includes(CANCEL("t1"))); // the service has succeeded: no cancel request
  assert.equal(env.clock.timers.size, 0);
});

test("取消 while waiting to download again ends the task; nothing is downloaded after it", async () => {
  const env = await submitted(succeededRoutes("t1"));
  delete env.routes[DUAL("t1")];
  await env.clock.advance(2000);
  assert.match(env.status()[0], /^等待服务：翻译服务暂时连不上/);
  assert.equal(rowOf(env).canCancel, true);
  await env.lifecycle.cancel(rowOf(env).key);
  assert.deepEqual(env.status(), ["已取消"]);
  env.routes[DUAL("t1")] = pdfBytes("dual pdf");
  await env.clock.advance(60000);
  assert.equal(env.times(DUAL("t1")).length, 1);
  assert.deepEqual(env.attached(), [["中文译文", "mono pdf", "t1/mono"]]);
});

test("取消 during a look-up: once it returns, nothing is downloaded or imported", async () => {
  const env = await submitted(succeededRoutes("t1"));
  env.findHold = held();
  await env.clock.advance(2000);
  assert.equal(env.findHold.count, 1);
  await env.lifecycle.cancel(rowOf(env).key);
  env.findHold.answer();
  await env.clock.advance(60000);
  assert.deepEqual(env.status(), ["已取消"]);
  assert.ok(!env.requests.includes(MONO("t1")));
  assert.deepEqual(env.imports, []);
});

test("取消 while the downloaded PDF is being written: once written, it is not imported", async () => {
  const env = await submitted(succeededRoutes("t1"));
  env.writeHold = held();
  await env.clock.advance(2000);
  assert.equal(env.writeHold.count, 1);
  await env.lifecycle.cancel(rowOf(env).key);
  env.writeHold.answer();
  await env.clock.advance(60000);
  assert.deepEqual(env.status(), ["已取消"]);
  assert.deepEqual(env.imports, []);
  assert.equal(env.steps.at(-1), "remove /tmp/zotero-translate/t1");
});

test("取消 while looking for the attachment to claim: once the look-up returns, nothing is claimed", async () => {
  const before = await quitWhileAttaching((env) => {
    env.quitAfterImport = true;
  });
  const after = environment({ saved: before.saved, routes: succeededRoutes("t1"), library: before.library, time: 60000 });
  after.unmarkedHold = held();
  after.lifecycle.start();
  await after.clock.advance(2000);
  assert.equal(after.unmarkedHold.count, 1);
  await after.lifecycle.cancel(rowOf(after).key);
  after.unmarkedHold.answer();
  await after.clock.advance(60000);
  assert.deepEqual(after.status(), ["已取消"]);
  assert.ok(!after.steps.some((step) => step.startsWith("mark")));
  assert.deepEqual(after.requests, []);
});

test("取消 on 挂回失败 ends the task; attachments already added stay", async () => {
  const env = await submitted(succeededRoutes("t1"));
  env.failWrite = "AutoDesign-双语对照.pdf";
  await env.clock.advance(2000);
  await env.lifecycle.cancel(rowOf(env).key);
  assert.deepEqual(env.status(), ["已取消"]);
  assert.deepEqual([rowOf(env).canRetry, rowOf(env).canContinue], [true, false]);
  assert.deepEqual(env.attached(), [["中文译文", "mono pdf", "t1/mono"]]);
  assert.ok(!env.requests.includes(CANCEL("t1")));
  await env.lifecycle.continueAttach(rowOf(env).key); // not offered: nothing happens
  assert.deepEqual(env.status(), ["已取消"]);
});

test("an attaching task saved before attach records existed becomes 挂回失败, asking to check the attachments first", async () => {
  const saved = JSON.stringify([
    { ...SOURCE, serviceUrl: SERVICE, taskId: "t1", state: "attaching" },
    { ...SOURCE, sourceItemID: 12, serviceUrl: SERVICE, taskId: "t2", state: "running" },
  ]);
  const env = environment({ saved, routes: { ...succeededRoutes("t1"), [QUERY("t2")]: status("running") } });
  env.lifecycle.start();
  assert.deepEqual(env.status(), [ATTACH_FAILED(UPGRADE_ATTACH_ERROR), "翻译中"]);
  const row = rowOf(env);
  assert.deepEqual([row.canContinue, row.canCancel], [true, true]);
  assert.deepEqual(savedOf(env).map((t) => [t.state, t.error]), [["attach_failed", UPGRADE_ATTACH_ERROR], ["running", null]]);
  await env.clock.advance(2000);
  assert.deepEqual(env.requests, [QUERY("t2")]); // nothing downloaded for t1

  await env.lifecycle.continueAttach(row.key);
  assert.deepEqual(env.status(), ["已挂回", "翻译中"]);
  assert.deepEqual(env.attached(), [["中文译文", "mono pdf", "t1/mono"], ["双语对照", "dual pdf", "t1/dual"]]);
});

test("a retry starts with no attach records or import intent of the old task", async () => {
  const env = await submitted(succeededRoutes("t1"));
  env.failImport = "x";
  await env.clock.advance(2000);
  await env.lifecycle.cancel(rowOf(env).key);
  env.routes[UPLOAD] = json(201, { task_id: "t2" });
  await env.lifecycle.retry(rowOf(env).key);
  assert.deepEqual(savedOf(env).map((t) => [t.taskId, t.attached, t.importIntents]), [["t2", {}, {}]]);
});

// ---- the done notice and opening the results (M4 ticket 07) ----

const RESULTS = { mono: 100, dual: 101 }; // the attachments imported first in an environment

test("only an 已挂回 row has results (打开译文, 打开双语, 在库中显示): its two attachments", async () => {
  const env = await submitted({ [QUERY("t1")]: status("queued", { queue_position: 1 }) });
  const seen = [[env.lifecycle.tasks[0].state, rowOf(env).results]];
  for (const state of ["running", "waiting_rerun"]) {
    env.routes[QUERY("t1")] = status(state, { reruns_used: 0, reruns_max: 2, next_attempt_in_seconds: 60 });
    await env.clock.advance(2000);
    seen.push([env.lifecycle.tasks[0].state, rowOf(env).results]);
  }
  Object.assign(env.routes, succeededRoutes("t1"));
  env.writeHold = held(); // the first PDF is downloaded, not yet written (never)
  await env.clock.advance(2000);
  seen.push([env.lifecycle.tasks[0].state, rowOf(env).results]);
  assert.deepEqual(seen, [
    ["queued", null],
    ["running", null],
    ["waiting_rerun", null],
    ["attaching", null],
  ]);

  const attachFailed = await submitted(succeededRoutes("t1"));
  attachFailed.failImport = "x";
  await attachFailed.clock.advance(2000);
  assert.deepEqual([attachFailed.lifecycle.tasks[0].state, rowOf(attachFailed).results], ["attach_failed", null]);

  const failed = await submitted({ [QUERY("t1")]: status("failed", { error: "x" }) });
  await failed.clock.advance(2000);
  assert.deepEqual([failed.lifecycle.tasks[0].state, rowOf(failed).results], ["failed", null]);

  const cancelled = await submitted({ [CANCEL("t1")]: json(200, { state: "cancelled" }) });
  await cancelled.lifecycle.cancel(rowOf(cancelled).key);
  assert.deepEqual([cancelled.lifecycle.tasks[0].state, rowOf(cancelled).results], ["cancelled", null]);

  const done = await submitted(succeededRoutes("t1"));
  await done.clock.advance(2000);
  assert.deepEqual([done.lifecycle.tasks[0].state, rowOf(done).results], ["done", RESULTS]);
});

test("an 已挂回 row has the results also when attaching found the attachments instead of importing them", async () => {
  const library = [
    { id: 7, target: { parentItemID: 10 }, title: "中文译文", text: "mono pdf", addedAt: 0, identity: "t1/mono", deleted: false },
    { id: 8, target: { parentItemID: 10 }, title: "双语对照", text: "dual pdf", addedAt: 0, identity: "t1/dual", deleted: false },
  ];
  const saved = JSON.stringify([{ ...SOURCE, serviceUrl: SERVICE, taskId: "t1", state: "attaching", attached: {} }]);
  const env = environment({ saved, library, routes: succeededRoutes("t1") });
  env.lifecycle.start();
  await env.clock.advance(2000);
  assert.deepEqual(env.imports, []);
  assert.deepEqual(rowOf(env).results, { mono: 7, dual: 8 });
  assert.deepEqual(env.notices(), [["done", "AutoDesign", "已挂回", { mono: 7, dual: 8 }]]);
});

test("a notice comes once when a task becomes 已挂回, with the row's status and the two attachments", async () => {
  const env = await submitted({ [QUERY("t1")]: status("running", { progress: 40 }) });
  await env.clock.advance(2000);
  assert.deepEqual(env.notices(), []);
  Object.assign(env.routes, succeededRoutes("t1"));
  env.routes[QUERY("t1")] = status("succeeded", { kept_source_paragraphs: 2 });
  await env.clock.advance(2000);
  assert.deepEqual(env.notices(), [["done", "AutoDesign", "已挂回：2 段保留原文", RESULTS]]);
  await env.clock.advance(60000);
  assert.equal(env.announced.length, 1);
});

test("a notice comes once when a task fails, with the reason: the service's failure, a refused upload, a failed attach", async () => {
  const env = await submitted({ [QUERY("t1")]: status("failed", { error: "管线模型拒绝了请求（HTTP 400）" }) });
  await env.clock.advance(2000);
  await env.clock.advance(60000);
  assert.deepEqual(env.notices(), [["failed", "AutoDesign", "失败：管线模型拒绝了请求（HTTP 400）", null]]);

  const refusedUpload = environment({ routes: { [UPLOAD]: json(413, {}) } });
  refusedUpload.lifecycle.start();
  await refusedUpload.lifecycle.submit(SOURCE);
  assert.deepEqual(refusedUpload.notices(), [
    ["failed", "AutoDesign", `失败：翻译服务拒绝了提交（HTTP 413，服务地址：${SERVICE}）`, null],
  ]);

  const attachFailed = await submitted(succeededRoutes("t1"));
  attachFailed.failImport = "无法写入 Zotero 存储目录";
  await attachFailed.clock.advance(2000);
  await attachFailed.clock.advance(60000);
  assert.deepEqual(attachFailed.notices(), [["failed", "AutoDesign", ATTACH_FAILED("无法写入 Zotero 存储目录"), null]]);
});

test("no notice while a task is unfinished, disconnected or cancelled", async () => {
  const env = await submitted({ [QUERY("t1")]: status("waiting_rerun", { reruns_used: 0, reruns_max: 2, next_attempt_in_seconds: 60, error: "HTTP 500" }) });
  await env.clock.advance(2000);
  delete env.routes[QUERY("t1")];
  await env.clock.advance(20000);
  assert.deepEqual(env.status(), [DISCONNECTED(1)]);
  env.routes[CANCEL("t1")] = json(200, { state: "cancelled" });
  await env.lifecycle.cancel(rowOf(env).key);
  assert.deepEqual(env.status(), ["已取消"]);
  assert.deepEqual(env.announced, []);

  const attachCancelled = await submitted(succeededRoutes("t1"));
  attachCancelled.failImport = "x";
  await attachCancelled.clock.advance(2000);
  assert.equal(attachCancelled.announced.length, 1); // 挂回失败
  await attachCancelled.lifecycle.cancel(rowOf(attachCancelled).key);
  assert.deepEqual(attachCancelled.status(), ["已取消"]);
  assert.equal(attachCancelled.announced.length, 1);
});

test("each new failure after 重试 or 继续挂回 has its own notice", async () => {
  const env = await submitted({ [QUERY("t1")]: status("failed", { error: "第一次" }) });
  await env.clock.advance(2000);
  env.routes[UPLOAD] = json(201, { task_id: "t2" });
  env.routes[QUERY("t2")] = status("failed", { error: "第二次" });
  await env.lifecycle.retry(rowOf(env).key);
  await env.clock.advance(2000);
  assert.deepEqual(env.notices().map((n) => n[2]), ["失败：第一次", "失败：第二次"]);

  const attach = await submitted(succeededRoutes("t1"));
  attach.failImport = "第一次";
  await attach.clock.advance(2000);
  attach.failImport = "第二次";
  await attach.lifecycle.continueAttach(rowOf(attach).key);
  attach.failImport = null;
  await attach.lifecycle.continueAttach(rowOf(attach).key);
  assert.deepEqual(attach.notices().map((n) => [n[0], n[2]]), [
    ["failed", ATTACH_FAILED("第一次")],
    ["failed", ATTACH_FAILED("第二次")],
    ["done", "已挂回"],
  ]);
});

test("after a Zotero restart no notice comes again; a task that ends after the restart has its one notice", async () => {
  // Before the restart: one task done (notice), one attach_failed (notice), one running.
  const before = environment({
    routes: {
      [UPLOAD]: json(201, { task_id: "t1" }),
      ...succeededRoutes("t1"),
      [QUERY("t2")]: status("succeeded"),
      [MONO("t2")]: pdfBytes("mono 2"),
      [DUAL("t2")]: pdfBytes("dual 2"),
      [QUERY("t3")]: status("running"),
    },
  });
  before.lifecycle.start();
  await before.lifecycle.submit(SOURCE);
  before.routes[UPLOAD] = json(201, { task_id: "t2" });
  await before.lifecycle.submit({ ...SOURCE, sourceItemID: 12, title: "AutoRef" });
  before.routes[UPLOAD] = json(201, { task_id: "t3" });
  await before.lifecycle.submit({ ...SOURCE, sourceItemID: 13, title: "MaLiang" });
  before.routes[DUAL("t2")] = () => {
    before.failImport = "磁盘已满";
    return pdfBytes("dual 2");
  };
  await before.clock.advance(2000);
  assert.deepEqual(before.notices().map((n) => [n[0], n[1]]), [["done", "AutoDesign"], ["failed", "AutoRef"]]);
  before.lifecycle.stop();

  const after = environment({ saved: before.saved, library: before.library, time: before.clock.now(), routes: { [QUERY("t3")]: status("running") } });
  after.lifecycle.start();
  assert.deepEqual(after.lifecycle.rows().map((r) => [r.title, statusText(r)]), [
    ["AutoRef", ATTACH_FAILED("磁盘已满")],
    ["MaLiang", "翻译中"],
  ]);
  await after.clock.advance(10000);
  assert.deepEqual(after.announced, []);
  Object.assign(after.routes, { [QUERY("t3")]: status("succeeded"), [MONO("t3")]: pdfBytes("mono 3"), [DUAL("t3")]: pdfBytes("dual 3") });
  await after.clock.advance(2000);
  assert.deepEqual(after.notices().map((n) => [n[0], n[1]]), [["done", "MaLiang"]]);

  // A second restart: nothing again.
  after.lifecycle.stop();
  const again = environment({ saved: after.saved, library: after.library, time: after.clock.now() });
  again.lifecycle.start();
  await again.clock.advance(10000);
  assert.deepEqual(again.announced, []);
});

test("a task that fails while being restored (an upload under way at quit) has its notice once, not after the next restart", async () => {
  const before = environment({ routes: { [UPLOAD]: never } });
  before.lifecycle.start();
  before.lifecycle.submit(SOURCE);
  await settle();
  before.lifecycle.stop();

  const after = environment({ saved: before.saved });
  after.lifecycle.start();
  assert.deepEqual(after.notices(), [["failed", "AutoDesign", "失败：提交未完成，请重试", null]]);
  after.lifecycle.stop();

  const again = environment({ saved: after.saved });
  again.lifecycle.start();
  assert.deepEqual(again.announced, []);
});

test("an attaching task from before attach records becomes 挂回失败 at restore with one notice; restored again, none", async () => {
  const saved = JSON.stringify([{ ...SOURCE, serviceUrl: SERVICE, taskId: "t1", state: "attaching" }]);
  const env = environment({ saved });
  env.lifecycle.start();
  assert.deepEqual(env.notices(), [["failed", "AutoDesign", ATTACH_FAILED(UPGRADE_ATTACH_ERROR), null]]);
  env.lifecycle.stop();
  const again = environment({ saved: env.saved });
  again.lifecycle.start();
  assert.deepEqual(again.announced, []);
});

test("after stop (the plugin is shut down), a request under way that comes back brings no notice", async () => {
  for (const [name, answer] of [
    ["failed", () => status("failed", { error: "管线模型拒绝了请求" })],
    ["succeeded", () => status("succeeded")],
  ]) {
    const query = held();
    const env = await submitted({ [QUERY("t1")]: query.route, [MONO("t1")]: pdfBytes("mono pdf"), [DUAL("t1")]: pdfBytes("dual pdf") });
    await env.clock.advance(2000);
    assert.equal(query.count, 1, name);
    env.lifecycle.stop();
    query.answer(answer());
    await env.clock.advance(60000);
    assert.deepEqual(env.announced, [], name);
  }

  // A failed upload after stop: no notice either.
  const upload = held();
  const env = environment({ routes: { [UPLOAD]: upload.route } });
  env.lifecycle.start();
  const submitting = env.lifecycle.submit(SOURCE);
  await settle();
  env.lifecycle.stop();
  upload.answer(json(413, {}));
  await submitting;
  assert.deepEqual(env.announced, []);
});

test("a notice that throws is logged; the task goes on", async () => {
  const logged = [];
  const env = await submitted(succeededRoutes("t1"), {
    announce: () => {
      throw new Error("no window");
    },
    logError: (e) => logged.push(e.message),
  });
  await env.clock.advance(2000);
  assert.deepEqual(env.status(), ["已挂回"]);
  assert.equal(env.saved, "[]");
  assert.deepEqual(logged, ["no window"]);
});

// ---- the submission and "清除已结束" (M4 ticket 08) ----

test("submit returns the new task, or null when the PDF already has an unfinished task", async () => {
  const env = environment({ routes: { [UPLOAD]: json(201, { task_id: "t1" }) } });
  env.lifecycle.start();
  const task = await env.lifecycle.submit(SOURCE);
  assert.deepEqual([task.taskId, task.state], ["t1", "queued"]);
  assert.equal(await env.lifecycle.submit(SOURCE), null);

  const refusedUpload = environment();
  refusedUpload.lifecycle.start();
  const failed = await refusedUpload.lifecycle.submit(SOURCE);
  assert.deepEqual([failed.taskId, failed.state], [null, "failed"]);
});

const NO_PDF = (title) => `${title}：该条目没有 PDF 附件`;
const sourceNamed = (title, id) => ({ sourceItemID: id, itemID: id + 100, title, sourceFileName: `${title}.pdf` });
/** sourceOf for submitBatch: an item is a source, or the reason it has none. */
const asSource = async (item) => item;

test("a batch's submission notice: the titles the service took, and why the others were skipped", async () => {
  const env = await submitted(); // SOURCE has an unfinished task
  env.routes[UPLOAD] = json(201, { task_id: "t2" });
  await env.lifecycle.submitBatch([sourceNamed("B", 21), NO_PDF("C"), SOURCE], asSource);
  assert.deepEqual(env.announced, [
    { outcome: "submitted", headline: "已提交 1 篇", titles: ["B"], reasons: [NO_PDF("C"), ALREADY_ACTIVE("AutoDesign")] },
  ]);
  // The task window shows this submission's reasons too.
  assert.deepEqual(env.lifecycle.notices, [NO_PDF("C"), ALREADY_ACTIVE("AutoDesign")]);
});

test("after stop, an upload under way when it comes back brings no submission notice", async () => {
  const upload = held();
  const env = environment({ routes: { [UPLOAD]: upload.route } });
  env.lifecycle.start();
  const batch = env.lifecycle.submitBatch([SOURCE, NO_PDF("C")], asSource);
  await settle();
  env.lifecycle.stop();
  upload.answer(json(201, { task_id: "t1" }));
  await batch;
  assert.deepEqual(env.announced, []);
});

test("two batches at once each tell only their own skipped items", async () => {
  const upload = held();
  const env = environment({ routes: { [UPLOAD]: upload.route } });
  env.lifecycle.start();
  const first = env.lifecycle.submitBatch([SOURCE, NO_PDF("C")], asSource);
  await settle(); // the first batch's upload is under way
  await env.lifecycle.submitBatch([NO_PDF("D")], asSource);
  upload.answer(json(201, { task_id: "t1" }));
  await first;
  assert.deepEqual(
    env.announced.map(({ headline, reasons }) => [headline, reasons]),
    [
      ["没有提交", [NO_PDF("D")]],
      ["已提交 1 篇", [NO_PDF("C")]],
    ],
  );
});

test("清除已结束 removes only the done, cancelled and failed rows; unfinished ones stay, saved and still handled", async () => {
  const UNFINISHED = ["queued", "running", "waiting_rerun", "attaching", "attach_failed", "cancelling"];
  const saved = UNFINISHED.map((state, i) => ({
    sourceItemID: 50 + i,
    itemID: 40 + i,
    title: `U-${state}`,
    sourceFileName: "u.pdf",
    serviceUrl: SERVICE,
    taskId: `u${i}`,
    state,
    attached: {},
    error: state === "attach_failed" ? "磁盘已满" : null,
  }));
  let uploads = 0;
  const upload = held();
  const env = environment({
    saved: JSON.stringify(saved),
    routes: {
      // t1 succeeds, t2 fails, t3 is cancelled by the service; the fourth upload is still under way.
      [UPLOAD]: (options) => (++uploads <= 3 ? json(201, { task_id: `t${uploads}` }) : upload.route(options)),
      ...succeededRoutes("t1"),
      [QUERY("t2")]: status("failed", { error: "HTTP 400" }),
      [QUERY("t3")]: status("cancelled", { cancel_requested: true }),
    },
  });
  env.lifecycle.start();
  for (const [i, title] of ["Done", "Failed", "Cancelled"].entries()) {
    await env.lifecycle.submit({ sourceItemID: 11 + i, itemID: 10 + i, title, sourceFileName: `${title}.pdf` });
  }
  const submitting = env.lifecycle.submit({ sourceItemID: 20, itemID: 21, title: "Submitting", sourceFileName: "s.pdf" });
  await env.clock.advance(2000);
  const states = () => env.lifecycle.tasks.map((t) => [t.title, t.state]);
  assert.deepEqual(states().slice(6), [["Done", "done"], ["Failed", "failed"], ["Cancelled", "cancelled"], ["Submitting", "submitting"]]);
  assert.deepEqual(
    env.lifecycle.rows().map((row) => row.ended),
    [false, false, false, false, false, false, true, true, true, false],
  );

  const savedBefore = env.saved;
  let redraws = 0;
  env.lifecycle.listeners.add(() => redraws++);
  env.lifecycle.clearEnded();
  assert.deepEqual(
    env.lifecycle.rows().map((row) => row.title),
    [...UNFINISHED.map((state) => `U-${state}`), "Submitting"],
  );
  assert.equal(redraws, 1);
  assert.equal(env.saved, savedBefore); // ended tasks were never saved; the unfinished ones still are
  assert.deepEqual(env.lifecycle.rows().map((row) => row.ended), Array(7).fill(false));

  // The unfinished tasks go on: the queued one is queried again, the upload under way still lands.
  env.routes[QUERY("u0")] = status("running", { progress: 30 });
  upload.answer(json(201, { task_id: "t4" }));
  await submitting;
  await env.clock.advance(10000);
  assert.deepEqual(states(), [...UNFINISHED.map((state, i) => [`U-${state}`, i === 0 ? "running" : state]), ["Submitting", "queued"]]);

  // Nothing ended: nothing to clear, and no redraw.
  const redrawsBefore = redraws;
  env.lifecycle.clearEnded();
  assert.equal(env.lifecycle.rows().length, 7);
  assert.equal(redraws, redrawsBefore);
});

test("a cleared task's late answers change nothing, and a cleared failed PDF can be submitted again", async () => {
  const query = held();
  const env = await submitted({ [QUERY("t1")]: query.route, [CANCEL("t1")]: json(200, { state: "cancelled" }) });
  await env.clock.advance(2000); // the query is under way
  await env.lifecycle.cancel(rowOf(env).key);
  assert.deepEqual(env.status(), ["已取消"]);
  env.lifecycle.clearEnded();
  query.answer(status("succeeded", { stage: "完成", progress: 100 }));
  await env.clock.advance(10000);
  assert.deepEqual(env.lifecycle.rows(), []);
  assert.deepEqual(env.requests.filter((r) => r.includes("/files/")), []);

  env.routes[UPLOAD] = json(201, { task_id: "t2" });
  env.routes[QUERY("t2")] = status("queued");
  assert.equal((await env.lifecycle.submit(SOURCE)).taskId, "t2");
  assert.deepEqual(env.status(), ["排队中"]);
});

// ---- progress and elapsed time (ticket M4-09) ----

const elapsedOf = (env) => env.lifecycle.rows().map((row) => row.elapsed);

test("a row's progress is the service's percentage; elapsed time counts from the submission", async () => {
  const env = await submitted({ [QUERY("t1")]: status("queued", { progress: 0 }) }, { time: 1_000_000 });
  assert.deepEqual(env.lifecycle.rows().map((row) => [row.progress, row.elapsed]), [[null, "0 秒"]]);
  await env.clock.advance(2000);
  env.routes[QUERY("t1")] = status("running", { progress: 37 });
  await env.clock.advance(63000);
  assert.deepEqual(env.lifecycle.rows().map((row) => [row.progress, row.percent, row.elapsed]), [[37, "37%", "1 分 5 秒"]]);
});

test("after Zotero restarts, elapsed time goes on from the submission, never from 0", async () => {
  const before = await submitted({ [QUERY("t1")]: status("running", { progress: 40 }) }, { time: 1_000_000 });
  await before.clock.advance(5 * MINUTE);
  assert.deepEqual(elapsedOf(before), ["5 分 0 秒"]);
  before.lifecycle.stop();
  assert.equal(savedOf(before)[0].submittedAt, 1_000_000);

  // Zotero was closed for 10 minutes.
  const after = environment({ saved: before.saved, time: before.clock.now() + 10 * MINUTE, routes: { [QUERY("t1")]: status("running") } });
  after.lifecycle.start();
  assert.deepEqual(elapsedOf(after), ["15 分 0 秒"]);
  await after.clock.advance(MINUTE);
  assert.deepEqual(elapsedOf(after), ["16 分 0 秒"]);
});

test("an ended task's elapsed time stops at its end; 重试 counts again from the new submission", async () => {
  const env = await submitted({ [QUERY("t1")]: status("running") });
  await env.clock.advance(90000);
  env.routes[QUERY("t1")] = status("failed", { error: "x" });
  await env.clock.advance(2000);
  assert.deepEqual(env.status(), ["失败：x"]);
  assert.deepEqual(elapsedOf(env), ["1 分 32 秒"]);
  await env.clock.advance(10 * MINUTE);
  assert.deepEqual(elapsedOf(env), ["1 分 32 秒"]);

  env.routes[UPLOAD] = json(201, { task_id: "t2" });
  env.routes[QUERY("t2")] = status("queued");
  await env.lifecycle.retry(rowOf(env).key);
  assert.deepEqual(elapsedOf(env), ["0 秒"]);
  await env.clock.advance(2000);
  assert.deepEqual(elapsedOf(env), ["2 秒"]);
});

/** Elapsed time at the end and 10 minutes later: the two are the same once the task has ended. */
async function elapsedStops(env) {
  const atEnd = elapsedOf(env);
  await env.clock.advance(10 * MINUTE);
  return [atEnd, elapsedOf(env)];
}

test("every way into an ended state stops the elapsed time there, the restart's included", async (t) => {
  const SUBMITTED_AT = 1_000_000;
  const savedTask = (fields) => JSON.stringify([{ ...SOURCE, serviceUrl: SERVICE, taskId: "t1", submittedAt: SUBMITTED_AT, ...fields }]);

  await t.test("an upload under way when Zotero quit fails at the restart", async () => {
    const env = environment({ saved: savedTask({ taskId: null, state: "submitting" }), time: SUBMITTED_AT + 3 * MINUTE });
    env.lifecycle.start();
    assert.deepEqual(env.status(), ["失败：提交未完成，请重试"]);
    assert.deepEqual(await elapsedStops(env), [["3 分 0 秒"], ["3 分 0 秒"]]);
  });

  await t.test("a pre-upgrade attaching task becomes attach_failed at the restart, then 取消 ends it here", async () => {
    const env = environment({ saved: savedTask({ state: "attaching" }), time: SUBMITTED_AT + MINUTE });
    env.lifecycle.start();
    await env.lifecycle.cancel(rowOf(env).key);
    assert.deepEqual(env.status(), ["已取消"]);
    assert.deepEqual(await elapsedStops(env), [["1 分 0 秒"], ["1 分 0 秒"]]);
  });

  await t.test("an upload the service refuses", async () => {
    const env = environment({ routes: { [UPLOAD]: json(413, {}) } });
    env.lifecycle.start();
    await env.lifecycle.submit(SOURCE);
    assert.match(env.status()[0], /^失败：/);
    assert.deepEqual(await elapsedStops(env), [["0 秒"], ["0 秒"]]);
  });

  for (const [name, answer, shown] of [
    ["the service says failed", status("failed", { error: "x" }), "失败：x"],
    ["the service says cancelled", status("cancelled"), "已取消"],
    ["the service no longer knows the task", json(404, {}), "失败：翻译服务中找不到该任务，请重试"],
  ]) {
    await t.test(name, async () => {
      const env = await submitted({ [QUERY("t1")]: answer });
      await env.clock.advance(2000);
      assert.deepEqual(env.status(), [shown]);
      assert.deepEqual(await elapsedStops(env), [["2 秒"], ["2 秒"]]);
    });
  }

  await t.test("a cancel the service confirms", async () => {
    const env = await submitted({ [QUERY("t1")]: status("queued"), [CANCEL("t1")]: cancelledAnswer("t1") });
    await env.clock.advance(5000);
    await env.lifecycle.cancel(rowOf(env).key);
    assert.deepEqual(env.status(), ["已取消"]);
    assert.deepEqual(await elapsedStops(env), [["5 秒"], ["5 秒"]]);
  });

  await t.test("both PDFs attached", async () => {
    const env = await submitted(succeededRoutes("t1"));
    await env.clock.advance(2000);
    assert.deepEqual(env.status(), ["已挂回"]);
    assert.deepEqual(await elapsedStops(env), [["2 秒"], ["2 秒"]]);
  });
});

test("a task's end time is taken when it changes into the ended state, before anything else is awaited", async () => {
  // Removing the temporary directory after both PDFs are attached takes 30 seconds.
  const env = await submitted(succeededRoutes("t1"));
  let removing = null;
  env.removeHold = () => new Promise((resolve) => (removing = resolve));
  await env.clock.advance(2000);
  assert.ok(removing, "the removal is under way");
  await env.clock.advance(30000);
  removing();
  await env.clock.advance(0);
  assert.deepEqual(env.status(), ["已挂回"]);
  assert.deepEqual(elapsedOf(env), ["2 秒"]);
});

test("a task saved by a plugin from before the submission time has no elapsed time", () => {
  const saved = JSON.stringify([{ ...SOURCE, serviceUrl: SERVICE, taskId: "t1", state: "running", disconnectedSince: null, generation: 0, attached: {}, importIntents: {} }]);
  const env = environment({ saved, routes: { [QUERY("t1")]: status("running") } });
  env.lifecycle.start();
  assert.deepEqual(elapsedOf(env), [""]);
});

// ---- 取消要确认；已有译文再翻译一次 (ticket M4-10) ----

test("取消 on a queued, running or waiting row asks first: the first press only asks, the second within 5 seconds cancels", async () => {
  const env = await submitted({ [QUERY("t1")]: status("queued"), [CANCEL("t1")]: cancelledAnswer("t1") });
  await env.clock.advance(2000);
  await env.lifecycle.requestCancel(rowOf(env).key);
  assert.deepEqual(env.status(), ["排队中"]);
  assert.equal(rowOf(env).canCancel, true);
  assert.equal(rowOf(env).confirmCancel, true);
  assert.deepEqual(env.requests, [UPLOAD, QUERY("t1")]); // no cancel request yet

  await env.clock.advance(4999);
  await env.lifecycle.requestCancel(rowOf(env).key);
  assert.deepEqual(env.status(), ["已取消"]);
  assert.equal(rowOf(env).confirmCancel, false);
  assert.equal(env.requests.at(-1), CANCEL("t1"));
});

test("the ask goes away after 5 seconds without a second press, and the next press asks again", async () => {
  const env = await submitted({ [QUERY("t1")]: status("running") });
  await env.clock.advance(2000);
  const key = rowOf(env).key;
  await env.lifecycle.requestCancel(key);
  assert.equal(rowOf(env).confirmCancel, true);
  await env.clock.advance(5000);
  assert.equal(rowOf(env).confirmCancel, false);
  assert.deepEqual(env.status(), ["翻译中"]);

  await env.lifecycle.requestCancel(key);
  assert.equal(rowOf(env).confirmCancel, true);
  assert.deepEqual(env.status(), ["翻译中"]);
});

test("a row that stops being cancellable drops the ask; another row's ask is a separate ask", async () => {
  const env = await submitted({ [QUERY("t1")]: status("running") });
  await env.clock.advance(2000);
  await env.lifecycle.requestCancel(rowOf(env).key);
  env.routes[QUERY("t1")] = status("failed", { error: "x" });
  await env.clock.advance(2000);
  assert.deepEqual(env.status(), ["失败：x"]);
  assert.equal(rowOf(env).confirmCancel, false);

  env.routes[UPLOAD] = json(201, { task_id: "t2" });
  await env.lifecycle.submit(sourceNamed("B", 21));
  const second = rowOf(env, 1).key;
  await env.lifecycle.requestCancel(second);
  assert.deepEqual([rowOf(env, 0).confirmCancel, rowOf(env, 1).confirmCancel], [false, true]);
});

test("取消 while 挂回中 or 挂回失败 cancels at once: nothing is lost but the downloads", async () => {
  const saved = JSON.stringify([
    { ...SOURCE, serviceUrl: SERVICE, taskId: "t1", state: "attach_failed", error: "磁盘已满", attached: {}, importIntents: {} },
  ]);
  const env = environment({ saved });
  env.lifecycle.start();
  await env.lifecycle.requestCancel(rowOf(env).key);
  assert.deepEqual(env.status(), ["已取消"]);
  assert.equal(rowOf(env).confirmCancel, false);
  assert.deepEqual(env.requests, []);
});

test("取消 on a waiting-rerun row asks first too; only the second press sends the cancel", async () => {
  const env = await submitted({
    [QUERY("t1")]: status("waiting_rerun", { reruns_used: 1, reruns_max: 2, next_attempt_in_seconds: 300, error: "HTTP 500" }),
    [CANCEL("t1")]: cancelledAnswer("t1"),
  });
  await env.clock.advance(2000);
  assert.equal(rowOf(env).status, "等待自动重跑");
  const key = rowOf(env).key;

  await env.lifecycle.requestCancel(key);
  assert.equal(rowOf(env).confirmCancel, true);
  assert.equal(rowOf(env).status, "等待自动重跑");
  assert.equal(env.requests.at(-1), QUERY("t1")); // no cancel request yet

  await env.lifecycle.requestCancel(key);
  assert.equal(env.requests.at(-1), CANCEL("t1"));
  assert.deepEqual(env.status(), ["已取消"]);
});

const RESUBMIT_ASK = (title) => `${title}：已有译文，等待确认`;

test("a paper that already has translations is asked about, and only 再翻译一次 uploads it", async () => {
  const env = environment({ routes: { [UPLOAD]: json(201, { task_id: "t1" }) } });
  env.lifecycle.start();
  await env.lifecycle.submitBatch([{ ...SOURCE, translated: true }], asSource);
  assert.deepEqual(env.requests, []); // nothing uploaded yet
  assert.deepEqual(env.announced, [
    { outcome: "submitted", headline: "没有提交", titles: [], reasons: [RESUBMIT_ASK("AutoDesign")] },
    { outcome: "resubmit", titles: ["AutoDesign"], keys: [1] },
  ]);
  assert.deepEqual(env.lifecycle.notices, [RESUBMIT_ASK("AutoDesign")]);

  await env.lifecycle.resubmit([1]);
  assert.deepEqual(env.requests, [UPLOAD]);
  assert.deepEqual(env.status(), ["排队中"]);
  assert.deepEqual(env.announced.at(-1), { outcome: "submitted", headline: "已提交 1 篇", titles: ["AutoDesign"], reasons: [] });
});

test("a mixed batch submits the new papers and asks about the translated ones; the notice counts both", async () => {
  const env = environment({ routes: { [UPLOAD]: json(201, { task_id: "t1" }) } });
  env.lifecycle.start();
  await env.lifecycle.submitBatch([{ ...SOURCE, translated: true }, sourceNamed("B", 21)], asSource);
  assert.deepEqual(env.announced, [
    { outcome: "submitted", headline: "已提交 1 篇", titles: ["B"], reasons: [RESUBMIT_ASK("AutoDesign")] },
    { outcome: "resubmit", titles: ["AutoDesign"], keys: [1] },
  ]);
  // The papers asked about are not uploaded until the user says so.
  assert.equal(env.requests.length, 1);
  assert.deepEqual(env.lifecycle.tasks.map((t) => t.title), ["B"]);
});

test("a PDF with an unfinished task is skipped before the 已有译文 ask; no PDF is skipped too", async () => {
  const env = await submitted();
  env.routes[UPLOAD] = json(201, { task_id: "t2" });
  await env.lifecycle.submitBatch([{ ...SOURCE, translated: true }, NO_PDF("C"), { ...sourceNamed("D", 31), translated: true }], asSource);
  assert.deepEqual(env.announced, [
    {
      outcome: "submitted",
      headline: "没有提交",
      titles: [],
      reasons: [ALREADY_ACTIVE("AutoDesign"), NO_PDF("C"), RESUBMIT_ASK("D")],
    },
    { outcome: "resubmit", titles: ["D"], keys: [1] },
  ]);
});

test("再翻译一次 does nothing after the plugin stopped or for a key never announced", async () => {
  const env = environment();
  env.lifecycle.start();
  await env.lifecycle.submitBatch([{ ...SOURCE, translated: true }], asSource);
  await env.lifecycle.resubmit([99]); // never announced
  assert.deepEqual(env.requests, []);
  await env.lifecycle.resubmit([1]);
  assert.deepEqual(env.requests.map((r) => r), [UPLOAD]); // no route: the upload fails, but it was sent
  env.lifecycle.stop();
  const before = env.requests.length;
  await env.lifecycle.resubmit([1]); // already used: no key left
  assert.equal(env.requests.length, before);
});

test("a standalone PDF whose task attached both outputs counts as translated: submitting it again asks first", async () => {
  const env = environment({ routes: { [UPLOAD]: json(201, { task_id: "t1" }), ...succeededRoutes("t1") } });
  env.lifecycle.start();
  const standalone = { sourceItemID: 30, itemID: null, title: "Standalone", sourceFileName: "standalone.pdf" };
  assert.equal(env.lifecycle.wasTranslated(30), false);

  await env.lifecycle.submit(standalone);
  await env.clock.advance(2000);
  assert.deepEqual(env.status(), ["已挂回"]);
  assert.equal(env.lifecycle.wasTranslated(30), true);
  assert.equal(env.lifecycle.wasTranslated(31), false);

  // The next submission carries what the plugin tells about the source: the
  // task record, its outputs having no link back to a standalone PDF.
  const again = { ...standalone, translated: env.lifecycle.wasTranslated(30) };
  await env.lifecycle.submitBatch([again], asSource);
  assert.deepEqual(env.announced.at(-1), { outcome: "resubmit", titles: ["Standalone"], keys: [1] });
  assert.equal(env.requests.filter((request) => request === UPLOAD).length, 1); // no second upload
  assert.equal(env.lifecycle.tasks.length, 1);

  // The record lasts for this Zotero session only: 清除已结束 removes it (and
  // ended tasks are not saved for restarts), so after that a standalone PDF
  // is submitted again without the ask.
  env.lifecycle.clearEnded();
  assert.equal(env.lifecycle.wasTranslated(30), false);
});
