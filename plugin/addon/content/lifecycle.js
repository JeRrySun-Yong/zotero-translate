/* The task lifecycle of the translation plugin, without Zotero: submitting
 * a source PDF, polling the translation service every 2 seconds, handling
 * each poll result, attaching the two PDFs, and the fields saved so that
 * after Zotero restarts the plugin keeps polling and still attaches.
 *
 * Everything outside the lifecycle comes in as a dependency, given to
 * createTaskLifecycle():
 *
 * - fetch(url, options): the network request (the global fetch in Zotero);
 * - clock: {now(), setInterval(fn, ms), clearInterval(id)}: current time and timers;
 * - serviceUrl(): the service address currently set;
 * - readSource(task): the source PDF's bytes; throws with the reason when unreadable;
 * - importAttachment(task, output): import one PDF, output = {file, title,
 *   fileName, bytes}, as an attachment next to the task's source PDF;
 * - store: {load(), save(value)}: the saved tasks, as a JSON string;
 * - debug(message), logError(error): logging.
 *
 * Zotero loads this file with Services.scriptloader.loadSubScript after
 * core.js, which defines ZoteroTranslateLifecycle in the plugin scope; Node
 * loads it with require() for plugin/test/.
 */
var ZoteroTranslateLifecycle = (() => {
  const Core = typeof ZoteroTranslateCore !== "undefined" ? ZoteroTranslateCore : require("./core.js");

  const POLL_INTERVAL_MS = 2000;
  // States in which the service still has the task: polled, and saved for restarts.
  const POLLED_STATES = ["queued", "running", "waiting_rerun", "attaching"];
  // A source PDF with a task in one of these states is not submitted again.
  const ACTIVE_STATES = ["submitting", "queued", "running", "waiting_rerun", "attaching"];
  // Task fields saved for restarts.
  const SAVED_FIELDS = ["sourceItemID", "itemID", "title", "sourceFileName", "serviceUrl", "taskId", "state"];

  function createTaskLifecycle({ fetch, clock, serviceUrl, readSource, importAttachment, store, debug, logError }) {
    let nextKey = 1;
    let savedValue = null;
    let pollTimer = null;
    let polling = false;

    const lifecycle = {
      // Tasks, oldest first: {key, sourceItemID, itemID (parent item, or null for a
      // standalone PDF), title, sourceFileName, serviceUrl, taskId, state, stage,
      // progress, attempt, rerunsUsed, rerunsMax, nextAttemptInSeconds, queuePosition, error}
      tasks: [],
      // Why items of the latest submission were skipped; shown in the task window.
      notices: [],
      // Called after every change to `tasks` or `notices`; the task window adds its redraw here.
      listeners: new Set(),

      /** Load the tasks saved before Zotero last quit, and poll every 2 seconds. */
      start() {
        restoreTasks();
        pollTimer = clock.setInterval(() => lifecycle.pollTasks(), POLL_INTERVAL_MS);
      },

      stop() {
        clock.clearInterval(pollTimer);
        pollTimer = null;
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
        if (activeTask(source.sourceItemID)) {
          lifecycle.notice(`${source.title}：已在排队或翻译中，本次提交已忽略`);
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
        if (activeTask(task.sourceItemID)) {
          lifecycle.notice(`${task.title}：已在排队或翻译中，本次提交已忽略`);
          return;
        }
        await upload(task);
      },

      /** Query every unfinished task once; attach the PDFs of finished ones. */
      async pollTasks() {
        if (polling) {
          return;
        }
        polling = true;
        try {
          for (const task of lifecycle.tasks.filter((t) => POLLED_STATES.includes(t.state))) {
            await poll(task);
          }
        } finally {
          polling = false;
        }
      },

      /** Rows for the task window; `key` identifies the task for "重试". */
      rows() {
        return lifecycle.tasks.map((task) => ({
          ...Core.taskRow(task),
          key: task.key,
          canRetry: task.state === "failed",
        }));
      },
    };

    function activeTask(sourceItemID) {
      return lifecycle.tasks.find((t) => t.sourceItemID === sourceItemID && ACTIVE_STATES.includes(t.state));
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
      });
      changed();
      try {
        const body = Core.uploadRequest(await readSource(task), task.sourceFileName);
        const response = await request(task, Core.serviceUrl(task.serviceUrl, "tasks"), {
          method: "POST",
          headers: { "Content-Type": body.contentType },
          body: body.body,
        });
        if (response.status !== 201) {
          throw new Error(`翻译服务拒绝了提交（HTTP ${response.status}）`);
        }
        task.taskId = (await response.json()).task_id;
        task.state = "queued";
      } catch (e) {
        task.state = "failed";
        task.error = e.message;
      }
      changed();
    }

    /** fetch(), with a connection failure reported as "无法连接翻译服务" plus the address. */
    async function request(task, url, options) {
      try {
        return await fetch(url, options);
      } catch (e) {
        debug(`zotero-translate: ${url}: ${e}`);
        throw new Error(`无法连接翻译服务（服务地址：${task.serviceUrl}）`);
      }
    }

    async function poll(task) {
      try {
        const url = Core.serviceUrl(task.serviceUrl, "tasks", task.taskId);
        const response = await request(task, url);
        if (response.status === 404) {
          throw new Error("翻译服务中找不到该任务，请重试");
        }
        if (!response.ok) {
          throw new Error(`查询任务失败（HTTP ${response.status}）`);
        }
        const status = await response.json();
        Object.assign(task, {
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
          task.state = "done";
        }
      } catch (e) {
        task.state = "failed";
        task.error = e.message;
      }
      changed();
    }

    /** Download both PDFs, then import them one by one; existing attachments stay as they are. */
    async function attachOutputs(task) {
      const outputs = [];
      for (const output of Core.OUTPUTS) {
        const url = Core.serviceUrl(task.serviceUrl, "tasks", task.taskId, "files", output.file);
        const response = await request(task, url);
        if (!response.ok) {
          throw new Error(`下载${output.title}失败（HTTP ${response.status}）`);
        }
        outputs.push({
          file: output.file,
          title: output.title,
          fileName: Core.outputFileName(task.sourceFileName, output.title),
          bytes: new Uint8Array(await response.arrayBuffer()),
        });
      }
      for (const output of outputs) {
        await importAttachment(task, output);
      }
    }

    /** Load the tasks saved before Zotero last quit; polling picks them up. */
    function restoreTasks() {
      let saved = [];
      try {
        saved = JSON.parse(store.load() || "[]");
      } catch (e) {
        logError(e);
      }
      lifecycle.tasks = saved.map((task) => ({ ...task, key: nextKey++ }));
      saveTasks();
    }

    /** Save the tasks the service still has, when they change. */
    function saveTasks() {
      const unfinished = lifecycle.tasks
        .filter((task) => task.taskId && POLLED_STATES.includes(task.state))
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

  return { POLL_INTERVAL_MS, SAVED_FIELDS, createTaskLifecycle };
})();

if (typeof module !== "undefined") {
  module.exports = ZoteroTranslateLifecycle;
}
