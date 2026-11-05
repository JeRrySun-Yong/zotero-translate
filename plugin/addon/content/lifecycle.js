/* The task lifecycle of the translation plugin, without Zotero: submitting
 * a source PDF, querying the translation service about each unfinished task
 * on the task's own schedule, handling each answer, attaching the two PDFs,
 * and the fields saved so that after Zotero restarts the plugin keeps
 * querying and still attaches.
 *
 * When the service cannot be reached (no connection, no full answer within
 * the request's time limit, a non-success status other than 404, an answer
 * that cannot be read or has a task state the plugin does not act on), an
 * unfinished task does not fail: it keeps its state, records when the
 * disconnection began, shows how long it has lasted, and tries again every
 * 10 seconds instead of 2. The disconnection ends with a full answer
 * to a query, or when both PDFs are attached. Only the service's `failed`,
 * or a 404 for the task or one of its PDFs, fails the task. An upload that
 * cannot reach the service fails at once, naming the service address.
 *
 * "取消" on a queued, running or waiting task (also while disconnected) first saves
 * the cancel intent and makes the task `cancelling`, which ends whatever
 * operation the task has under way: its request is aborted, and its answer,
 * whenever it comes, is dropped. The cancel request goes out at once; the
 * task is no longer queried. 200, 409 or 404 make it `cancelled`, and
 * nothing is attached; a 503 shows the service's reason, and a 503 or no
 * answer sends it again every 10 seconds, also after Zotero restarts.
 * "重试" (failed or cancelled) submits the PDF again as a new task under a
 * new generation, with nothing of the old task's. An operation's result
 * counts only while the operation is the task's current one, of the same
 * generation and task ID; so an old task's answers never change the new one.
 *
 * Attaching handles the two PDFs one after the other, each in this order:
 * look for the attachment with its identity (service task ID + output,
 * mono or dual), and when there is one record it and go on; otherwise
 * download the PDF, save the import intent (task ID, output, start time),
 * import it, write the identity on the new attachment, record it and
 * clear the intent. Zotero saves an attachment before the plugin can
 * write its identity; so when an intent is saved and the attachment with
 * that identity is not found, the attachment without identity, of the
 * output's title, added no earlier than the intent's start, is claimed
 * (its identity written) instead of importing again. A local error makes
 * the task `attach_failed` with the reason; "继续挂回" handles the PDFs
 * again in the same way. "取消" while downloading or waiting to download,
 * or on `attach_failed`, makes the task `cancelled` at once (the service
 * has already succeeded; no cancel request): the download under way is
 * aborted, and nothing comes after it; attachments already added stay.
 * While a PDF is being imported, the row offers no "取消". An `attaching`
 * task saved by a plugin from before these records (no `attached`) cannot
 * tell how many PDFs it attached: it becomes `attach_failed`, asking the
 * user to check the item's attachments first.
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
 *   attachment at that target; returns the attachment (an ID, saved with the task);
 * - attachments: {find(target, identity), mark(attachment, identity),
 *   unmarked(target, {title, since})}, where identity is {taskId, output}:
 *   the attachment with that identity that the user has not deleted, or
 *   null; write the identity on an attachment; the attachment at the target
 *   with no identity, that title, added at or after the clock time `since`, or null;
 * - store: {load(), save(value)}: the saved tasks, as a JSON string;
 * - announce({outcome, row}): tell the user, outside the task window, that a
 *   task has just ended: outcome "done" (both PDFs attached; the row's
 *   `results` name them) or "failed" (failed or attach_failed; the row's
 *   status has the reason). Once per change into that state, after the
 *   change is saved: a state restored after a restart is no change, so no
 *   notice comes again (done and failed tasks are not even saved). A state
 *   that restoring itself changes (an upload under way at quit fails) is
 *   one, measured from the saved state. Also, at the end of submitBatch(),
 *   {outcome: "submitted", headline, titles, reasons}. Never after stop();
 * - debug(message), logError(error): logging.
 *
 * Zotero loads this file with Services.scriptloader.loadSubScript after
 * core.js, which defines ZoteroTranslateLifecycle in the plugin scope; Node
 * loads it with require() for plugin/test/. Loading it has one side effect
 * in Zotero: the plugin scope has fetch but not AbortController, so the file
 * adds it to that scope with Cu.importGlobalProperties (Node has it already).
 */
if (typeof AbortController === "undefined") {
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
  // More than the up to 15 seconds (10 after the termination signal, 5 after
  // the forced kill) the service takes to reap the task's processes before it answers.
  const CANCEL_LIMIT_MS = 30000;
  const DOWNLOAD_LIMIT_MS = 120000; // each PDF
  // Task states in the service's answers that the plugin acts on; any other
  // answer counts as one that cannot be read (a disconnection).
  const KNOWN_SERVICE_STATES = ["queued", "running", "waiting_rerun", "succeeded", "failed", "cancelled"];
  // Unfinished states (core.js): saved for restarts, and a source PDF with a
  // task in one of them is not submitted again.
  const ACTIVE_STATES = Core.UNFINISHED_STATES;
  // States in which the plugin has a next operation for the task: query the
  // service, (attaching) download and import the PDFs, or (cancelling) send the cancel.
  const SCHEDULED_STATES = ACTIVE_STATES.filter((state) => !["submitting", "attach_failed"].includes(state));
  // States whose rows offer "取消".
  const CANCELLABLE_STATES = ["queued", "running", "waiting_rerun"];
  // Further states whose rows offer "取消", which ends the task here, without a cancel
  // request: the service has already succeeded. Not while a PDF is being imported.
  const LOCALLY_CANCELLABLE_STATES = ["attaching", "attach_failed"];
  // States whose rows offer "重试".
  const RETRYABLE_STATES = ["failed", "cancelled"];
  // Ended states: the plugin has nothing more to do for the task; "清除已结束" removes their rows.
  const ENDED_STATES = ["done", "failed", "cancelled"];
  // States announced when a task changes into them -> the notice's outcome.
  const ANNOUNCED_OUTCOMES = { done: "done", failed: "failed", attach_failed: "failed" };
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
    "generation",
    "error",
    "attached",
    "importIntents",
  ];
  const UPGRADE_ATTACH_ERROR = "升级前的挂回进度不明：请先检查原条目下的附件，再选择继续挂回或取消";

  /** Why a source PDF with an unfinished task was not submitted again. */
  const activeNotice = (title) => `${title}：已有未结束的翻译任务，本次提交已忽略`;

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
    attachments,
    store,
    announce,
    debug,
    logError,
  }) {
    let nextKey = 1;
    let savedValue = null;
    let started = false;
    // Task key -> the task's state when it was last looked at for a notice.
    const announcedStates = new Map();
    // Task key -> the timer of the task's next operation. A task's next
    // operation is set only when its current one has finished, so a task
    // never has two operations at once; a cancel ends the current one first.
    const timers = new Map();
    // Task key -> the operation under way: {generation, taskId, abort}.
    const operations = new Map();

    const lifecycle = {
      // Tasks, oldest first: {key, sourceItemID, itemID (parent item, or null for a
      // standalone PDF), title, sourceFileName, serviceUrl, taskId, state, stage,
      // progress, attempt, rerunsUsed, rerunsMax, nextAttemptInSeconds, queuePosition, error,
      // keptSourceParagraphs, keptSourceAlgorithms, disconnectedSince (clock time when the
      // service stopped answering, or null), generation (0 for a new task, one more for
      // each retry), cleanupError (why the
      // service has not confirmed the cancel yet, or null), attached (output -> the
      // attachment recorded for it, e.g. {mono: 51}), importIntents (output ->
      // {taskId, output, startedAt} while that PDF's import has started and its attachment
      // is not recorded; one per PDF, so handling one never drops the other's), importing (true while a PDF is being imported; not saved)}
      tasks: [],
      // Why items of the latest submission were skipped; shown in the task window.
      notices: [],
      // Called after every change to `tasks` or `notices`; the task window adds its redraw here.
      listeners: new Set(),

      /** Load the tasks saved before Zotero last quit and schedule their next operations. */
      start() {
        started = true;
        restoreTasks();
        for (const task of lifecycle.tasks) {
          schedule(task);
        }
      },

      /** No further operations start; one under way finishes without scheduling another. */
      stop() {
        started = false;
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
       *  unless it already has an unfinished task. Returns the new task once
       *  the upload has ended (its taskId set when the service took it, or
       *  failed), or null when the PDF was skipped. */
      async submit(source) {
        if (skippedAsActive(source.sourceItemID, source.title)) {
          return null;
        }
        const task = {
          key: nextKey++,
          sourceItemID: source.sourceItemID,
          itemID: source.itemID,
          title: source.title,
          sourceFileName: source.sourceFileName,
          generation: 0,
        };
        lifecycle.tasks.push(task);
        await upload(task);
        return task;
      },

      /** "翻译为中文" on several items: submit them one after another
       *  (`sourceOf(item)`: the item's source, or why it has none, a string,
       *  also shown in the task window), then announce the submission notice
       *  (core.js submissionNotice) with this batch's own titles and reasons;
       *  none once stop() has come meanwhile. */
      async submitBatch(items, sourceOf) {
        lifecycle.clearNotices();
        const submitted = [];
        const skipped = [];
        for (const item of items) {
          const source = await sourceOf(item);
          if (typeof source === "string") {
            lifecycle.notice(source);
            skipped.push(source);
            continue;
          }
          const task = await lifecycle.submit(source);
          if (!task) {
            skipped.push(activeNotice(source.title));
          } else if (task.taskId) {
            submitted.push(task.title);
          }
        }
        const notice = Core.submissionNotice({ submitted, skipped });
        if (!started || !notice) {
          return;
        }
        try {
          announce({ outcome: "submitted", ...notice });
        } catch (e) {
          logError(e);
        }
      },

      /** The "清除已结束" button: remove the done, failed and cancelled
       *  tasks (never saved; nothing more to do for them); the unfinished
       *  ones stay as they are. */
      clearEnded() {
        const ended = lifecycle.tasks.filter((task) => ENDED_STATES.includes(task.state));
        if (!ended.length) {
          return;
        }
        for (const task of ended) {
          announcedStates.delete(task.key);
        }
        lifecycle.tasks = lifecycle.tasks.filter((task) => !ENDED_STATES.includes(task.state));
        changed();
      },

      /** The "重试" button of a failed or cancelled row: submit the same
       *  source PDF again, as a new task of a new generation. */
      async retry(key) {
        const task = lifecycle.tasks.find((t) => t.key === key);
        if (!task || !RETRYABLE_STATES.includes(task.state)) {
          return;
        }
        if (skippedAsActive(task.sourceItemID, task.title)) {
          return;
        }
        task.generation = (task.generation || 0) + 1;
        await upload(task);
      },

      /** The "取消" button, no confirmation: end the task's operation under
       *  way; while attaching or attach_failed, the task is cancelled at once,
       *  with no cancel request; otherwise save the cancel intent and send the cancel at once. */
      async cancel(key) {
        const task = lifecycle.tasks.find((t) => t.key === key);
        if (task && cancelsLocally(task)) {
          Object.assign(task, { state: "cancelled", error: null, disconnectedSince: null });
          changed(); // saved first: the task is no longer unfinished, so no restart attaches it
          endOperation(task);
          return;
        }
        if (!task || !CANCELLABLE_STATES.includes(task.state)) {
          return;
        }
        Object.assign(task, { state: "cancelling", cleanupError: null });
        endOperation(task);
        changed(); // the intent is saved before the request goes out
        if (started) {
          await run(task);
        }
      },

      /** The "继续挂回" button of an attach_failed row: attach again,
       *  each PDF looked for with its identity first. */
      async continueAttach(key) {
        const task = lifecycle.tasks.find((t) => t.key === key);
        if (!task || task.state !== "attach_failed") {
          return;
        }
        Object.assign(task, { state: "attaching", error: null });
        changed();
        if (started) {
          await run(task);
        }
      },

      /** Rows for the task window; `key` identifies the task for "重试", "取消" and "继续挂回". */
      rows() {
        const now = clock.now();
        return lifecycle.tasks.map((task) => rowOf(task, now));
      },
    };

    /** A task's row; `results` ({mono, dual}: the two attachments) only once
     *  both PDFs are attached, for "打开译文", "打开双语" and "在库中显示";
     *  `ended` for the rows "清除已结束" removes. */
    function rowOf(task, now) {
      return {
        ...Core.taskRow(task, now),
        key: task.key,
        canRetry: RETRYABLE_STATES.includes(task.state),
        canCancel: CANCELLABLE_STATES.includes(task.state) || cancelsLocally(task),
        canContinue: task.state === "attach_failed",
        ended: ENDED_STATES.includes(task.state),
        results: task.state === "done" ? { mono: task.attached.mono, dual: task.attached.dual } : null,
      };
    }

    /** A source PDF with an unfinished task is not submitted again; the notice says so. */
    function skippedAsActive(sourceItemID, title) {
      const active = lifecycle.tasks.some((t) => t.sourceItemID === sourceItemID && ACTIVE_STATES.includes(t.state));
      if (active) {
        lifecycle.notice(activeNotice(title));
      }
      return active;
    }

    /** Whether "取消" ends the task here: attaching, but not while a PDF is
     *  being imported; or attach_failed. */
    function cancelsLocally(task) {
      return LOCALLY_CANCELLABLE_STATES.includes(task.state) && !task.importing;
    }

    /** Set the task's next operation: 2 seconds from now, or 10 while the
     *  service cannot be reached or has not confirmed a cancel. */
    function schedule(task) {
      if (!started || !SCHEDULED_STATES.includes(task.state) || timers.has(task.key) || operations.has(task.key)) {
        return;
      }
      const waiting = task.disconnectedSince != null || task.cleanupError != null;
      const delay = waiting ? DISCONNECTED_INTERVAL_MS : POLL_INTERVAL_MS;
      timers.set(
        task.key,
        clock.setTimeout(() => run(task), delay),
      );
    }

    /** Run the task's next operation; when it is still the task's current
     *  one at its end, set the one after. */
    async function run(task) {
      timers.delete(task.key);
      const operation = { generation: task.generation, taskId: task.taskId, abort: new AbortController() };
      operations.set(task.key, operation);
      try {
        if (task.state === "attaching") {
          await attachOutputs(task, operation);
        } else if (task.state === "cancelling") {
          await sendCancel(task, operation);
        } else {
          await query(task, operation);
        }
      } finally {
        const current = operations.get(task.key) === operation;
        if (current) {
          operations.delete(task.key);
        }
        changed();
        if (current) {
          schedule(task);
        }
      }
    }

    /** Whether the operation's result still counts for the task. */
    function isCurrent(task, operation) {
      return (
        operations.get(task.key) === operation && task.generation === operation.generation && task.taskId === operation.taskId
      );
    }

    /** End the task's operation under way and its next one: the request is
     *  aborted and the result, whenever it comes, does not count. */
    function endOperation(task) {
      if (timers.has(task.key)) {
        clock.clearTimeout(timers.get(task.key));
        timers.delete(task.key);
      }
      const operation = operations.get(task.key);
      if (operation) {
        operations.delete(task.key);
        operation.abort.abort();
      }
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
        keptSourceParagraphs: null,
        keptSourceAlgorithms: null,
        disconnectedSince: null,
        cleanupError: null,
        attached: {},
        importIntents: {},
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

    /** Ask the service about the task; on success, attach its PDFs. An
     *  answer that comes after the operation ended (a cancel) is dropped. */
    async function query(task, operation) {
      const answer = await operationExchange(task, operation, ["tasks", task.taskId], {}, QUERY_LIMIT_MS, readStatus);
      if (!answer) {
        return;
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
        await attachOutputs(task, operation);
      }
    }

    /** One request of the task's current operation, to `path` under the
     *  task's service address: the answer, or null when it no longer counts
     *  (the operation has ended) or the service cannot be reached (then the
     *  task is disconnected). */
    async function operationExchange(task, operation, path, options, limitMs, read) {
      let answer;
      try {
        answer = await exchange(Core.serviceUrl(task.serviceUrl, ...path), { ...options, signal: operation.abort.signal }, limitMs, read);
      } catch {
        answer = null;
      }
      if (!isCurrent(task, operation)) {
        return null;
      }
      if (!answer) {
        disconnected(task);
      }
      return answer;
    }

    /** Send the cancel: 200, 409 (the task had ended) or 404 (the service no
     *  longer has it) make the task cancelled; a 503 keeps it cancelling,
     *  with the service's reason; no answer keeps it cancelling, disconnected. */
    async function sendCancel(task, operation) {
      const answer = await operationExchange(
        task,
        operation,
        ["tasks", task.taskId, "cancel"],
        { method: "POST", bodyStatuses: [503] },
        CANCEL_LIMIT_MS,
        readCancelAnswer,
      );
      if (!answer) {
        return;
      }
      if ([200, 409, 404].includes(answer.status)) {
        Object.assign(task, { state: "cancelled", disconnectedSince: null, cleanupError: null });
      } else if (answer.status === 503) {
        Object.assign(task, { disconnectedSince: null, cleanupError: answer.body.cleanup_error });
      } else {
        disconnected(task);
      }
    }

    /** Attach the two PDFs, one after the other (see the top of this file);
     *  always remove the temporary directory, when one was made. When the
     *  service cannot be reached while downloading, the task stays attaching
     *  and goes on later; a local error makes it attach_failed. Nothing
     *  happens after the operation has ended (a cancel). */
    async function attachOutputs(task, operation) {
      let dir = null;
      const tempDir = async () => (dir = dir || (await tempFiles.makeDirectory(task)));
      try {
        try {
          await attachEach(task, operation, tempDir);
        } finally {
          if (dir) {
            await tempFiles.remove(dir);
          }
        }
      } catch (e) {
        if (isCurrent(task, operation)) {
          attachFailed(task, e.message);
        }
      }
    }

    /** Each PDF in turn: found with its identity, claimed after an import
     *  intent, or downloaded (into `tempDir()`, made on first use) and imported. */
    async function attachEach(task, operation, tempDir) {
      const target = await attachTarget(task);
      for (const output of Core.OUTPUTS) {
        if (!isCurrent(task, operation)) {
          return;
        }
        const identity = { taskId: task.taskId, output: output.file };
        let attachment = await attachments.find(target, identity);
        if (!isCurrent(task, operation)) {
          return;
        }
        const intent = task.importIntents[output.file];
        if (attachment == null && intent && intent.taskId === task.taskId) {
          attachment = await attachments.unmarked(target, { title: output.title, since: intent.startedAt });
          if (!isCurrent(task, operation)) {
            return;
          }
          if (attachment != null) {
            await attachments.mark(attachment, identity);
            if (!isCurrent(task, operation)) {
              return;
            }
          }
        }
        if (attachment == null) {
          const answer = await operationExchange(
            task,
            operation,
            ["tasks", task.taskId, "files", output.file],
            {},
            DOWNLOAD_LIMIT_MS,
            async (response) => new Uint8Array(await response.arrayBuffer()),
          );
          if (!answer) {
            return; // the operation has ended, or the service cannot be reached
          }
          if (answer.status === 404) {
            return fail(task, "译文已不在翻译服务中，请重试");
          }
          if (answer.body == null) {
            return disconnected(task);
          }
          const path = await tempFiles.write(await tempDir(), Core.outputFileName(task.sourceFileName, output.title), answer.body);
          if (!isCurrent(task, operation)) {
            return; // cancelled: no import
          }
          task.importIntents = {
            ...task.importIntents,
            [output.file]: { taskId: task.taskId, output: output.file, startedAt: clock.now() },
          };
          task.importing = true;
          changed(); // the intent is saved before the import
          try {
            attachment = await importAttachment(target, { path, title: output.title });
            await attachments.mark(attachment, identity);
          } finally {
            task.importing = false;
          }
        }
        task.attached = { ...task.attached, [output.file]: attachment };
        const { [output.file]: _done, ...otherIntents } = task.importIntents;
        task.importIntents = otherIntents; // the other PDF's intent stays until that one is handled
        changed();
      }
      if (isCurrent(task, operation)) {
        task.state = "done";
        task.disconnectedSince = null;
      }
    }

    /** One request to the service, with `limitMs` to send it and read the
     *  whole answer: {status, body}, where body is read(response) for a
     *  success status or one of `options.bodyStatuses`, and null otherwise
     *  (that body is not read; the request is aborted). Throws Unreachable
     *  when there is no connection, the time limit passes, the answer cannot
     *  be read, or `options.signal` (the task's operation) is aborted. */
    async function exchange(url, { signal, bodyStatuses = [], ...options }, limitMs, read) {
      const abort = new AbortController();
      if (signal) {
        signal.addEventListener("abort", () => abort.abort(), { once: true });
      }
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
        if (!response.ok && !bodyStatuses.includes(response.status)) {
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
        // The exchange ends with its request settled: a body read in full is
        // unaffected; a body not read (an error status) or not read in time
        // is aborted, so no request of the task stays open.
        clock.clearTimeout(timer);
        abort.abort();
      }
    }

    async function readTaskId(response) {
      const { task_id: taskId } = await response.json();
      if (typeof taskId !== "string") {
        throw new Error("没有任务号");
      }
      return taskId;
    }

    /** The service's answer about a task; one without a state the plugin
     *  acts on cannot be read. */
    async function readStatus(response) {
      const status = await response.json();
      if (!KNOWN_SERVICE_STATES.includes(status?.state)) {
        throw new Error(`未知的任务状态：${JSON.stringify(status?.state)}`);
      }
      return status;
    }

    /** The answer to a cancel: the cancelled task (200), or the task with
     *  the reason it is not cancelled yet (503); any other answer cannot be read. */
    async function readCancelAnswer(response) {
      const body = await response.json();
      const readable =
        response.status === 503 ? typeof body?.cleanup_error === "string" : body?.state === "cancelled";
      if (!readable) {
        throw new Error(`无法解析的取消回答：${JSON.stringify(body)}`);
      }
      return body;
    }

    /** The service did not answer: keep the task's state, note when this began. */
    function disconnected(task) {
      if (task.disconnectedSince == null) {
        task.disconnectedSince = clock.now();
      }
    }

    /** The task has failed for good: only "重试" goes on. */
    function fail(task, reason) {
      task.state = "failed";
      task.error = reason;
      task.disconnectedSince = null;
    }

    /** Attaching hit a local error: "继续挂回" or "取消" go on. */
    function attachFailed(task, reason) {
      task.state = "attach_failed";
      task.error = reason;
      task.disconnectedSince = null;
    }

    /** Load the tasks saved before Zotero last quit. An upload under way
     *  when Zotero quit is not sent again: its row fails and asks for a retry.
     *  An attaching task saved without attach records (before they existed)
     *  becomes attach_failed. */
    function restoreTasks() {
      let saved = [];
      try {
        saved = JSON.parse(store.load() || "[]");
      } catch (e) {
        logError(e);
      }
      lifecycle.tasks = saved.map((task) => ({
        disconnectedSince: null,
        generation: 0,
        error: null,
        importIntents: {},
        ...task,
        attached: task.attached || {},
        cleanupError: null,
        key: nextKey++,
      }));
      lifecycle.tasks.forEach((task, i) => {
        announcedStates.set(task.key, task.state); // as saved: no notice for it
        if (task.state === "submitting") {
          fail(task, "提交未完成，请重试");
        } else if (task.state === "attaching" && !saved[i].attached) {
          attachFailed(task, UPGRADE_ATTACH_ERROR);
        }
      });
      saveTasks();
      announceEnded();
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

    /** Announce each task whose state has changed into done, failed or
     *  attach_failed since it was last looked at; called after saving.
     *  Nothing after stop(): a request that comes back once the plugin is
     *  shut down brings no notice. */
    function announceEnded() {
      if (!started) {
        return;
      }
      const now = clock.now();
      for (const task of lifecycle.tasks) {
        const before = announcedStates.get(task.key);
        announcedStates.set(task.key, task.state);
        const outcome = ANNOUNCED_OUTCOMES[task.state];
        if (outcome && before !== task.state) {
          try {
            announce({ outcome, row: rowOf(task, now) });
          } catch (e) {
            logError(e);
          }
        }
      }
    }

    function changed() {
      saveTasks();
      announceEnded();
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
