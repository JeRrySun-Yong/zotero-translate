/* The task lifecycle of the translation plugin, without Zotero: submitting
 * a source PDF, querying the translation service about each unfinished task
 * on the task's own schedule, handling each answer, attaching the two PDFs,
 * and the fields saved so that after Zotero restarts the plugin keeps
 * querying and still attaches.
 *
 * When the service cannot be reached (no connection, no full answer within
 * the request's time limit, a non-success status other than 404, an answer
 * that cannot be read), an unfinished task does not fail: it records when
 * the disconnection began, shows how long it has lasted, and tries again
 * every 10 seconds instead of 2. Only the service's `failed`, or a 404 for
 * the task or one of its PDFs, fails the task. An upload that cannot reach
 * the service fails at once, naming the service address.
 *
 * Everything outside the lifecycle comes in as a dependency, given to
 * createTaskLifecycle():
 *
 * - fetch(url, options): the network request (the global fetch in Zotero);
 *   options carry an AbortSignal that is aborted when the time limit passes;
 * - clock: {now(), setTimeout(fn, ms), clearTimeout(id)}: current time and timers;
 * - serviceUrl(): the service address currently set;
 * - readSource(task): the source PDF's bytes; throws with the reason when unreadable;
 * - attachTarget(task): where the task's PDFs go (the original item, or the
 *   standalone source PDF's library and collections), resolved once before
 *   any download; throws with the reason when the source is gone;
 * - tempFiles: {makeDirectory(task), write(dir, fileName, bytes), remove(dir)}:
 *   the task's temporary directory for the downloaded PDFs; write returns the file's path;
 * - importAttachment(target, {path, title}): import one PDF file as an
 *   attachment at that target;
 * - store: {load(), save(value)}: the saved tasks, as a JSON string;
 * - debug(message), logError(error): logging.
 *
 * Zotero loads this file with Services.scriptloader.loadSubScript after
 * core.js, which defines ZoteroTranslateLifecycle in the plugin scope; Node
 * loads it with require() for plugin/test/.
 */
if (typeof AbortController === "undefined") {
  // The Zotero plugin scope has fetch but not AbortController.
  Cu.importGlobalProperties(["AbortController"]);
}

var ZoteroTranslateLifecycle = (() => {
  const Core = typeof ZoteroTranslateCore !== "undefined" ? ZoteroTranslateCore : require("./core.js");

  // How long after one operation on a task the next one starts.
  const POLL_INTERVAL_MS = 2000;
  const DISCONNECTED_INTERVAL_MS = 10000;
  // Time limits of one request, from sending it to reading the whole answer.
  const UPLOAD_LIMIT_MS = 120000;
  const QUERY_LIMIT_MS = 10000;
  const DOWNLOAD_LIMIT_MS = 120000; // each PDF
  // States in which the plugin has a next operation for the task: query the
  // service, or (attaching) download and import the PDFs.
  const SCHEDULED_STATES = ["queued", "running", "waiting_rerun", "attaching"];
  // Unfinished states: saved for restarts, and a source PDF with a task in
  // one of them is not submitted again.
  const ACTIVE_STATES = ["submitting", ...SCHEDULED_STATES];
  // Task fields saved for restarts.
  const SAVED_FIELDS = [
    "sourceItemID",
    "itemID",
    "title",
    "sourceFileName",
    "serviceUrl",
    "taskId",
    "state",
    "disconnectedSince",
  ];

  /** The service could not be reached, did not answer in full within the
   *  time limit, or gave an answer that cannot be read. */
  class Unreachable extends Error {}

  function createTaskLifecycle({
    fetch,
    clock,
    serviceUrl,
    readSource,
    attachTarget,
    tempFiles,
    importAttachment,
    store,
    debug,
    logError,
  }) {
    let nextKey = 1;
    let savedValue = null;
    let running = false;
    // Task key -> the timer of the task's next operation. A task's next
    // operation is set only when its current one has finished, so a task
    // never has two operations at once.
    const timers = new Map();

    const lifecycle = {
      // Tasks, oldest first: {key, sourceItemID, itemID (parent item, or null for a
      // standalone PDF), title, sourceFileName, serviceUrl, taskId, state, stage,
      // progress, attempt, rerunsUsed, rerunsMax, nextAttemptInSeconds, queuePosition, error,
      // keptSourceParagraphs, keptSourceAlgorithms, disconnectedSince (clock time when the
      // service stopped answering, or null)}
      tasks: [],
      // Why items of the latest submission were skipped; shown in the task window.
      notices: [],
      // Called after every change to `tasks` or `notices`; the task window adds its redraw here.
      listeners: new Set(),

      /** Load the tasks saved before Zotero last quit and schedule their next operations. */
      start() {
        running = true;
        restoreTasks();
        for (const task of lifecycle.tasks) {
          schedule(task);
        }
      },

      /** No further operations start; one under way finishes without scheduling another. */
      stop() {
        running = false;
        for (const timer of timers.values()) {
          clock.clearTimeout(timer);
        }
        timers.clear();
      },

      /** A new submission: the notices of the previous one go away. */
      clearNotices() {
        lifecycle.notices = [];
        changed();
      },

      notice(text) {
        lifecycle.notices.push(text);
        changed();
      },

      /** Upload a source PDF, {sourceItemID, itemID, title, sourceFileName},
       *  unless it already has an unfinished task. */
      async submit(source) {
        if (skippedAsActive(source.sourceItemID, source.title)) {
          return;
        }
        const task = {
          key: nextKey++,
          sourceItemID: source.sourceItemID,
          itemID: source.itemID,
          title: source.title,
          sourceFileName: source.sourceFileName,
        };
        lifecycle.tasks.push(task);
        await upload(task);
      },

      /** The "重试" button of a failed row: submit the same source PDF again. */
      async retry(key) {
        const task = lifecycle.tasks.find((t) => t.key === key);
        if (!task || task.state !== "failed") {
          return;
        }
        if (skippedAsActive(task.sourceItemID, task.title)) {
          return;
        }
        await upload(task);
      },

      /** Rows for the task window; `key` identifies the task for "重试". */
      rows() {
        const now = clock.now();
        return lifecycle.tasks.map((task) => ({
          ...Core.taskRow(task, now),
          key: task.key,
          canRetry: task.state === "failed",
        }));
      },
    };

    /** A source PDF with an unfinished task is not submitted again; the notice says so. */
    function skippedAsActive(sourceItemID, title) {
      const active = lifecycle.tasks.some((t) => t.sourceItemID === sourceItemID && ACTIVE_STATES.includes(t.state));
      if (active) {
        lifecycle.notice(`${title}：已在排队或翻译中，本次提交已忽略`);
      }
      return active;
    }

    /** Set the task's next operation: 2 seconds from now, or 10 while the
     *  service cannot be reached. */
    function schedule(task) {
      if (!running || !SCHEDULED_STATES.includes(task.state)) {
        return;
      }
      const delay = task.disconnectedSince == null ? POLL_INTERVAL_MS : DISCONNECTED_INTERVAL_MS;
      timers.set(
        task.key,
        clock.setTimeout(async () => {
          timers.delete(task.key);
          await (task.state === "attaching" ? attachOutputs(task) : query(task));
          changed();
          schedule(task);
        }, delay),
      );
    }

    /** Send the task's source PDF to the service address currently set. */
    async function upload(task) {
      Object.assign(task, {
        serviceUrl: serviceUrl(),
        taskId: null,
        state: "submitting",
        stage: null,
        progress: null,
        attempt: null,
        rerunsUsed: null,
        rerunsMax: null,
        nextAttemptInSeconds: null,
        queuePosition: null,
        error: null,
        disconnectedSince: null,
      });
      changed();
      try {
        const body = Core.uploadRequest(await readSource(task), task.sourceFileName);
        const answer = await exchange(
          Core.serviceUrl(task.serviceUrl, "tasks"),
          { method: "POST", headers: { "Content-Type": body.contentType }, body: body.body },
          UPLOAD_LIMIT_MS,
          readTaskId,
        ).catch((e) => {
          throw e instanceof Unreachable ? new Error(`${e.message}（服务地址：${task.serviceUrl}）`) : e;
        });
        if (answer.status !== 201) {
          throw new Error(`翻译服务拒绝了提交（HTTP ${answer.status}，服务地址：${task.serviceUrl}）`);
        }
        task.taskId = answer.body;
        task.state = "queued";
      } catch (e) {
        task.state = "failed";
        task.error = e.message;
      }
      changed();
      schedule(task);
    }

    /** Ask the service about the task; on success, attach its PDFs. */
    async function query(task) {
      let answer;
      try {
        answer = await exchange(Core.serviceUrl(task.serviceUrl, "tasks", task.taskId), {}, QUERY_LIMIT_MS, readStatus);
      } catch {
        return disconnected(task);
      }
      if (answer.status === 404) {
        return fail(task, "翻译服务中找不到该任务，请重试");
      }
      if (answer.body == null) {
        return disconnected(task);
      }
      const status = answer.body;
      Object.assign(task, {
        disconnectedSince: null,
        stage: status.stage,
        progress: status.progress,
        attempt: status.attempt,
        rerunsUsed: status.reruns_used,
        rerunsMax: status.reruns_max,
        nextAttemptInSeconds: status.next_attempt_in_seconds,
        queuePosition: status.queue_position,
        error: status.error,
        keptSourceParagraphs: status.kept_source_paragraphs,
        keptSourceAlgorithms: status.kept_source_algorithms,
      });
      task.state = status.state === "succeeded" ? "attaching" : status.state;
      changed();
      if (task.state === "attaching") {
        await attachOutputs(task);
      }
    }

    /** Resolve where the PDFs go; download both into the task's temporary
     *  directory; only when both are written, import them; always remove the
     *  directory. A failure before the imports attaches nothing; existing
     *  attachments stay as they are. When the service cannot be reached
     *  while downloading, the task stays attaching and starts over later. */
    async function attachOutputs(task) {
      try {
        const target = await attachTarget(task);
        const dir = await tempFiles.makeDirectory(task);
        try {
          const files = [];
          for (const output of Core.OUTPUTS) {
            const url = Core.serviceUrl(task.serviceUrl, "tasks", task.taskId, "files", output.file);
            const answer = await exchange(url, {}, DOWNLOAD_LIMIT_MS, async (response) => new Uint8Array(await response.arrayBuffer()));
            if (answer.status === 404) {
              throw new Error("译文已不在翻译服务中，请重试");
            }
            if (answer.status >= 500) {
              throw new Unreachable(`HTTP ${answer.status}`);
            }
            if (answer.body == null) {
              throw new Error(`下载${output.title}失败（HTTP ${answer.status}）`);
            }
            task.disconnectedSince = null;
            const fileName = Core.outputFileName(task.sourceFileName, output.title);
            files.push({ path: await tempFiles.write(dir, fileName, answer.body), title: output.title });
          }
          for (const file of files) {
            await importAttachment(target, file);
          }
        } finally {
          await tempFiles.remove(dir);
        }
        task.state = "done";
      } catch (e) {
        if (e instanceof Unreachable) {
          disconnected(task);
        } else {
          fail(task, e.message);
        }
      }
    }

    /** One request to the service, with `limitMs` to send it and read the
     *  whole answer: {status, body}, where body is read(response) for a
     *  success status and null otherwise. Throws Unreachable when there is
     *  no connection, the time limit passes, or the answer cannot be read. */
    async function exchange(url, options, limitMs, read) {
      const abort = new AbortController();
      let timer;
      const timeLimit = new Promise((resolve, reject) => {
        timer = clock.setTimeout(() => {
          abort.abort();
          reject(new Unreachable(`翻译服务在 ${limitMs / 1000} 秒内没有回答完`));
        }, limitMs);
      });
      const answer = (async () => {
        let response;
        try {
          response = await fetch(url, { ...options, signal: abort.signal });
        } catch (e) {
          debug(`zotero-translate: ${url}: ${e}`);
          throw new Unreachable("无法连接翻译服务");
        }
        if (!response.ok) {
          return { status: response.status, body: null };
        }
        try {
          return { status: response.status, body: await read(response) };
        } catch (e) {
          debug(`zotero-translate: ${url}: ${e}`);
          throw new Unreachable("翻译服务的回答无法解析");
        }
      })();
      try {
        return await Promise.race([answer, timeLimit]);
      } finally {
        clock.clearTimeout(timer);
      }
    }

    async function readTaskId(response) {
      const { task_id: taskId } = await response.json();
      if (typeof taskId !== "string") {
        throw new Error("没有任务号");
      }
      return taskId;
    }

    async function readStatus(response) {
      const status = await response.json();
      if (typeof status?.state !== "string") {
        throw new Error("没有任务状态");
      }
      return status;
    }

    /** The service did not answer: keep the task's state, note when this began. */
    function disconnected(task) {
      if (task.disconnectedSince == null) {
        task.disconnectedSince = clock.now();
      }
    }

    function fail(task, reason) {
      task.state = "failed";
      task.error = reason;
      task.disconnectedSince = null;
    }

    /** Load the tasks saved before Zotero last quit. An upload under way
     *  when Zotero quit is not sent again: its row fails and asks for a retry. */
    function restoreTasks() {
      let saved = [];
      try {
        saved = JSON.parse(store.load() || "[]");
      } catch (e) {
        logError(e);
      }
      lifecycle.tasks = saved.map((task) => ({ disconnectedSince: null, ...task, key: nextKey++ }));
      for (const task of lifecycle.tasks.filter((t) => t.state === "submitting")) {
        fail(task, "提交未完成，请重试");
      }
      saveTasks();
    }

    /** Save the unfinished tasks, when they change. */
    function saveTasks() {
      const unfinished = lifecycle.tasks
        .filter((task) => ACTIVE_STATES.includes(task.state))
        .map((task) => Object.fromEntries(SAVED_FIELDS.map((name) => [name, task[name]])));
      const value = JSON.stringify(unfinished);
      if (value !== savedValue) {
        store.save(value);
        savedValue = value;
      }
    }

    function changed() {
      saveTasks();
      for (const listener of lifecycle.listeners) {
        try {
          listener();
        } catch (e) {
          logError(e);
        }
      }
    }

    return lifecycle;
  }

  return { createTaskLifecycle };
})();

if (typeof module !== "undefined") {
  module.exports = ZoteroTranslateLifecycle;
}
