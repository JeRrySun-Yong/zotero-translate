/* The translation plugin inside Zotero: the "翻译为中文" item menu entry,
 * the "翻译任务" window, polling the translation service every 2 seconds,
 * and attaching the two PDFs to the original item.
 *
 * The plugin sends the service only the PDF and its file name; model
 * credentials and translation parameters live in the service.
 *
 * Unfinished tasks are saved in a preference, so that after Zotero restarts
 * the plugin keeps polling them and still attaches their PDFs.
 */
var ZoteroTranslate = {
  SERVICE_URL_PREF: "extensions.zotero-translate.serviceUrl",
  // Not a setting: the unfinished tasks, as JSON, kept across restarts.
  TASKS_PREF: "extensions.zotero-translate.tasks",
  POLL_INTERVAL_MS: 2000,
  TASK_WINDOW_URL: "chrome://zotero-translate/content/tasks.xhtml",
  TASK_WINDOW_TYPE: "zotero-translate:tasks",
  ITEM_MENU_ID: "zotero-translate-itemmenu",
  TOOLS_MENU_ID: "zotero-translate-toolsmenu",
  // States in which the service still has the task: polled, and saved for restarts.
  POLLED_STATES: ["queued", "running", "attaching"],
  // A source PDF with a task in one of these states is not submitted again.
  ACTIVE_STATES: ["submitting", "queued", "running", "attaching"],
  // Task fields saved for restarts.
  SAVED_FIELDS: ["sourceItemID", "itemID", "title", "sourceFileName", "serviceUrl", "taskId", "state"],

  // Tasks, oldest first: {key, sourceItemID, itemID (parent item, or null for a
  // standalone PDF), title, sourceFileName, serviceUrl, taskId, state, stage,
  // progress, queuePosition, error}
  tasks: [],
  // Why items of the latest submission were skipped; shown in the task window.
  notices: [],
  // Called after every change to `tasks` or `notices`; the task window adds its redraw here.
  listeners: new Set(),
  _nextKey: 1,
  _savedTasks: null,
  _pollTimer: null,
  _polling: false,

  startup({ id }) {
    Zotero.ZoteroTranslate = this;
    Zotero.PreferencePanes.register({
      pluginID: id,
      src: "content/preferences.xhtml",
      label: "中文翻译",
    });
    this._restoreTasks();
    this._pollTimer = setInterval(() => this.pollTasks(), this.POLL_INTERVAL_MS);
    for (const window of Zotero.getMainWindows()) {
      this.addToWindow(window);
    }
  },

  shutdown() {
    clearInterval(this._pollTimer);
    Services.wm.getMostRecentWindow(this.TASK_WINDOW_TYPE)?.close();
    for (const window of Zotero.getMainWindows()) {
      this.removeFromWindow(window);
    }
    delete Zotero.ZoteroTranslate;
  },

  addToWindow(window) {
    const doc = window.document;
    const itemMenu = doc.createXULElement("menuitem");
    itemMenu.id = this.ITEM_MENU_ID;
    itemMenu.setAttribute("label", "翻译为中文");
    itemMenu.addEventListener("command", () => this.translateSelected(window));
    const itemPopup = doc.getElementById("zotero-itemmenu");
    itemPopup.append(itemMenu);
    itemPopup.addEventListener("popupshowing", this._onItemMenuShowing);

    const toolsMenu = doc.createXULElement("menuitem");
    toolsMenu.id = this.TOOLS_MENU_ID;
    toolsMenu.setAttribute("label", "翻译任务");
    toolsMenu.addEventListener("command", () => this.openTaskWindow(window));
    doc.getElementById("menu_ToolsPopup").append(toolsMenu);
  },

  removeFromWindow(window) {
    const doc = window.document;
    doc.getElementById("zotero-itemmenu")?.removeEventListener("popupshowing", this._onItemMenuShowing);
    doc.getElementById(this.ITEM_MENU_ID)?.remove();
    doc.getElementById(this.TOOLS_MENU_ID)?.remove();
  },

  // The entry shows for regular items (with or without a PDF) and for PDF
  // attachments, with or without a parent item.
  _onItemMenuShowing(event) {
    const window = event.target.ownerGlobal;
    const items = window.ZoteroPane.getSelectedItems();
    const menu = window.document.getElementById(ZoteroTranslate.ITEM_MENU_ID);
    menu.hidden = !items.some((item) => item.isRegularItem() || ZoteroTranslate._asSource(item));
  },

  /** Submit every selected item, one after another; the service runs them in turn. */
  async translateSelected(window) {
    this.notices = [];
    this.openTaskWindow(window);
    this._changed();
    for (const item of window.ZoteroPane.getSelectedItems()) {
      await this.submit(item);
    }
  },

  /** Upload the item's source PDF to the translation service, unless the item
   *  has no PDF or its PDF already has an unfinished task. */
  async submit(item) {
    const source = await this.sourcePdf(item);
    if (!source) {
      this._notice(`${item.getDisplayTitle()}：该条目没有 PDF 附件`);
      return;
    }
    const parent = source.parentItem; // null for a standalone PDF
    const title = (parent || source).getDisplayTitle();
    if (this._activeTask(source.id)) {
      this._notice(`${title}：已在排队或翻译中，本次提交已忽略`);
      return;
    }
    const task = {
      key: this._nextKey++,
      sourceItemID: source.id,
      itemID: parent ? parent.id : null,
      title,
      sourceFileName: source.attachmentFilename,
    };
    this.tasks.push(task);
    await this._upload(task);
  },

  /** The "重试" button of a failed row: submit the same source PDF again. */
  async retry(key) {
    const task = this.tasks.find((t) => t.key === key);
    if (!task || task.state !== "failed") {
      return;
    }
    if (this._activeTask(task.sourceItemID)) {
      this._notice(`${task.title}：已在排队或翻译中，本次提交已忽略`);
      return;
    }
    await this._upload(task);
  },

  _activeTask(sourceItemID) {
    return this.tasks.find((t) => t.sourceItemID === sourceItemID && this.ACTIVE_STATES.includes(t.state));
  },

  /** Send the task's source PDF to the service address currently set. */
  async _upload(task) {
    Object.assign(task, {
      serviceUrl: Zotero.Prefs.get(this.SERVICE_URL_PREF, true),
      taskId: null,
      state: "submitting",
      stage: null,
      progress: null,
      queuePosition: null,
      error: null,
    });
    this._changed();
    try {
      const source = await Zotero.Items.getAsync(task.sourceItemID);
      const path = source && (await source.getFilePathAsync());
      if (!path) {
        throw new Error("找不到 PDF 文件");
      }
      const upload = ZoteroTranslateCore.uploadRequest(await IOUtils.read(path), task.sourceFileName);
      const response = await this._fetch(task, ZoteroTranslateCore.serviceUrl(task.serviceUrl, "tasks"), {
        method: "POST",
        headers: { "Content-Type": upload.contentType },
        body: upload.body,
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
    this._changed();
  },

  /** fetch(), with a connection failure reported as "无法连接翻译服务" plus the address. */
  async _fetch(task, url, options) {
    try {
      return await fetch(url, options);
    } catch (e) {
      Zotero.debug(`zotero-translate: ${url}: ${e}`);
      throw new Error(`无法连接翻译服务（服务地址：${task.serviceUrl}）`);
    }
  },

  /** The PDF to translate: the selected attachment itself, or the item's best
   *  PDF attachment; never an earlier translation output. */
  async sourcePdf(item) {
    if (!item.isRegularItem()) {
      return this._asSource(item);
    }
    const attachments = await item.getBestAttachments();
    return this._pick(attachments);
  },

  _asSource(attachment) {
    return attachment.isAttachment() ? this._pick([attachment]) : null;
  },

  _pick(attachments) {
    const candidates = attachments.map((attachment) => ({
      isPDF: attachment.isPDFAttachment(),
      title: attachment.getField("title"),
      attachment,
    }));
    return ZoteroTranslateCore.pickSourcePdf(candidates)?.attachment || null;
  },

  /** Query every unfinished task once; attach the PDFs of finished ones. */
  async pollTasks() {
    if (this._polling) {
      return;
    }
    this._polling = true;
    try {
      for (const task of this.tasks.filter((t) => this.POLLED_STATES.includes(t.state))) {
        await this._poll(task);
      }
    } finally {
      this._polling = false;
    }
  },

  async _poll(task) {
    try {
      const url = ZoteroTranslateCore.serviceUrl(task.serviceUrl, "tasks", task.taskId);
      const response = await this._fetch(task, url);
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
        queuePosition: status.queue_position,
        error: status.error,
        keptSourceParagraphs: status.kept_source_paragraphs,
        keptSourceAlgorithms: status.kept_source_algorithms,
      });
      task.state = status.state === "succeeded" ? "attaching" : status.state;
      this._changed();
      if (task.state === "attaching") {
        await this._attachOutputs(task);
        task.state = "done";
      }
    } catch (e) {
      task.state = "failed";
      task.error = e.message;
    }
    this._changed();
  },

  /** Download both PDFs, then import them: as child attachments of the
   *  original item, or, for a standalone PDF, as standalone attachments in
   *  the same library and collections. Existing attachments stay as they are. */
  async _attachOutputs(task) {
    const target = task.itemID ? { parentItemID: task.itemID } : await this._standaloneTarget(task);
    const dir = PathUtils.join(PathUtils.tempDir, "zotero-translate", task.taskId);
    await IOUtils.makeDirectory(dir);
    try {
      const files = [];
      for (const output of ZoteroTranslateCore.OUTPUTS) {
        const url = ZoteroTranslateCore.serviceUrl(task.serviceUrl, "tasks", task.taskId, "files", output.file);
        const response = await this._fetch(task, url);
        if (!response.ok) {
          throw new Error(`下载${output.title}失败（HTTP ${response.status}）`);
        }
        const path = PathUtils.join(dir, ZoteroTranslateCore.outputFileName(task.sourceFileName, output.title));
        await IOUtils.write(path, new Uint8Array(await response.arrayBuffer()));
        files.push({ path, title: output.title });
      }
      for (const { path, title } of files) {
        await Zotero.Attachments.importFromFile({ file: path, title, ...target });
      }
    } finally {
      await IOUtils.remove(dir, { recursive: true });
    }
  },

  async _standaloneTarget(task) {
    const source = await Zotero.Items.getAsync(task.sourceItemID);
    if (!source) {
      throw new Error("找不到原 PDF 附件");
    }
    return { libraryID: source.libraryID, collections: source.getCollections() };
  },

  /** Rows for the task window; `key` identifies the task for "重试". */
  rows() {
    return this.tasks.map((task) => ({
      ...ZoteroTranslateCore.taskRow(task),
      key: task.key,
      canRetry: task.state === "failed",
    }));
  },

  openTaskWindow(window) {
    const open = Services.wm.getMostRecentWindow(this.TASK_WINDOW_TYPE);
    if (open) {
      open.focus();
      return open;
    }
    return window.openDialog(this.TASK_WINDOW_URL, "", "chrome,resizable,centerscreen,dialog=no");
  },

  _notice(text) {
    this.notices.push(text);
    this._changed();
  },

  /** Load the tasks saved before Zotero last quit; polling picks them up. */
  _restoreTasks() {
    let saved = [];
    try {
      saved = JSON.parse(Zotero.Prefs.get(this.TASKS_PREF, true) || "[]");
    } catch (e) {
      Zotero.logError(e);
    }
    this.tasks = saved.map((task) => ({ ...task, key: this._nextKey++ }));
    this._saveTasks();
  },

  /** Save the tasks the service still has, when they change. */
  _saveTasks() {
    const unfinished = this.tasks
      .filter((task) => task.taskId && this.POLLED_STATES.includes(task.state))
      .map((task) => Object.fromEntries(this.SAVED_FIELDS.map((name) => [name, task[name]])));
    const value = JSON.stringify(unfinished);
    if (value !== this._savedTasks) {
      Zotero.Prefs.set(this.TASKS_PREF, value, true);
      this._savedTasks = value;
    }
  },

  _changed() {
    this._saveTasks();
    for (const listener of this.listeners) {
      try {
        listener();
      } catch (e) {
        Zotero.logError(e);
      }
    }
  },
};
