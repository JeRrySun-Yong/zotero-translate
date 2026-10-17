/* The translation plugin inside Zotero: the "翻译为中文" item menu entry,
 * the "翻译任务" window, polling the translation service every 2 seconds,
 * and attaching the two PDFs to the original item.
 *
 * The plugin sends the service only the PDF and its file name; model
 * credentials and translation parameters live in the service.
 */
var ZoteroTranslate = {
  SERVICE_URL_PREF: "extensions.zotero-translate.serviceUrl",
  POLL_INTERVAL_MS: 2000,
  TASK_WINDOW_URL: "chrome://zotero-translate/content/tasks.xhtml",
  TASK_WINDOW_TYPE: "zotero-translate:tasks",
  ITEM_MENU_ID: "zotero-translate-itemmenu",
  TOOLS_MENU_ID: "zotero-translate-toolsmenu",

  // Tasks of this Zotero session, oldest first:
  // {itemID, title, sourceFileName, serviceUrl, taskId, state, stage, progress, error}
  tasks: [],
  // Called after every change to `tasks`; the task window adds its redraw here.
  listeners: new Set(),
  _pollTimer: null,
  _polling: false,

  startup({ id }) {
    Zotero.ZoteroTranslate = this;
    Zotero.PreferencePanes.register({
      pluginID: id,
      src: "content/preferences.xhtml",
      label: "中文翻译",
    });
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

  // The entry shows for regular items and for PDF attachments with a parent item.
  _onItemMenuShowing(event) {
    const window = event.target.ownerGlobal;
    const items = window.ZoteroPane.getSelectedItems();
    const menu = window.document.getElementById(ZoteroTranslate.ITEM_MENU_ID);
    menu.hidden = !items.some(
      (item) => item.isRegularItem() || (!item.isTopLevelItem() && ZoteroTranslate._asSource(item)),
    );
  },

  async translateSelected(window) {
    this.openTaskWindow(window);
    for (const item of window.ZoteroPane.getSelectedItems()) {
      await this.submit(item);
    }
  },

  /** Upload the item's source PDF to the translation service. */
  async submit(item) {
    const parent = item.isRegularItem() ? item : item.parentItem;
    const source = await this.sourcePdf(item);
    if (!parent || !source) {
      return;
    }
    const task = {
      itemID: parent.id,
      title: parent.getDisplayTitle(),
      sourceFileName: source.attachmentFilename,
      serviceUrl: Zotero.Prefs.get(this.SERVICE_URL_PREF, true),
      state: "submitting",
    };
    this.tasks.push(task);
    this._changed();
    try {
      const path = await source.getFilePathAsync();
      if (!path) {
        throw new Error("找不到 PDF 文件");
      }
      const upload = ZoteroTranslateCore.uploadRequest(await IOUtils.read(path), task.sourceFileName);
      const response = await fetch(ZoteroTranslateCore.serviceUrl(task.serviceUrl, "tasks"), {
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
      for (const task of this.tasks.filter((t) => t.state === "queued" || t.state === "running")) {
        await this._poll(task);
      }
    } finally {
      this._polling = false;
    }
  },

  async _poll(task) {
    try {
      const response = await fetch(ZoteroTranslateCore.serviceUrl(task.serviceUrl, "tasks", task.taskId));
      if (!response.ok) {
        throw new Error(`查询任务失败（HTTP ${response.status}）`);
      }
      const status = await response.json();
      Object.assign(task, { stage: status.stage, progress: status.progress, error: status.error });
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

  /** Download both PDFs, then add them to the original item as imported
   *  attachments. Existing attachments stay as they are. */
  async _attachOutputs(task) {
    const dir = PathUtils.join(PathUtils.tempDir, "zotero-translate", task.taskId);
    await IOUtils.makeDirectory(dir);
    try {
      const files = [];
      for (const output of ZoteroTranslateCore.OUTPUTS) {
        const url = ZoteroTranslateCore.serviceUrl(task.serviceUrl, "tasks", task.taskId, "files", output.file);
        const response = await fetch(url);
        if (!response.ok) {
          throw new Error(`下载${output.title}失败（HTTP ${response.status}）`);
        }
        const path = PathUtils.join(dir, ZoteroTranslateCore.outputFileName(task.sourceFileName, output.title));
        await IOUtils.write(path, new Uint8Array(await response.arrayBuffer()));
        files.push({ path, title: output.title });
      }
      for (const { path, title } of files) {
        await Zotero.Attachments.importFromFile({ file: path, parentItemID: task.itemID, title });
      }
    } finally {
      await IOUtils.remove(dir, { recursive: true });
    }
  },

  /** Rows for the task window. */
  rows() {
    return this.tasks.map(ZoteroTranslateCore.taskRow);
  },

  openTaskWindow(window) {
    const open = Services.wm.getMostRecentWindow(this.TASK_WINDOW_TYPE);
    if (open) {
      open.focus();
      return open;
    }
    return window.openDialog(this.TASK_WINDOW_URL, "", "chrome,resizable,centerscreen,dialog=no");
  },

  _changed() {
    for (const listener of this.listeners) {
      try {
        listener();
      } catch (e) {
        Zotero.logError(e);
      }
    }
  },
};
