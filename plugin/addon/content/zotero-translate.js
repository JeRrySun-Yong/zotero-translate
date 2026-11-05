/* The translation plugin inside Zotero: the "翻译为中文" item menu entry,
 * the "翻译任务" window, the notice when a task is done or fails (with
 * "打开译文", "打开双语" and "在库中显示", also on done rows of the task
 * window), and the Zotero side of the task lifecycle
 * (lifecycle.js): reading the source PDF, temporary files for the downloaded
 * PDFs, importing an output PDF as an attachment, the attachments'
 * identities, saving the tasks in a preference.
 *
 * An attachment's identity (service task ID + output) is kept in the file
 * zotero-translate/attachments.json in the Zotero data directory,
 * {"<task ID>/<output>": {libraryID, key}}, written whole by an atomic
 * rename. Not as an item relation, which would be hidden too: the Zotero
 * sync server accepts only its own relation predicates (dc:relation,
 * owl:sameAs, dc:replaces with Zotero item URIs; Mendeley's), so an
 * attachment with any other relation could never sync (M3 ticket 10).
 *
 * The plugin sends the service only the PDF and its file name; model
 * credentials and translation parameters live in the service.
 */
var ZoteroTranslate = {
  SERVICE_URL_PREF: "extensions.zotero-translate.serviceUrl",
  // Not a setting: the unfinished tasks, as JSON, kept across restarts.
  TASKS_PREF: "extensions.zotero-translate.tasks",
  TASK_WINDOW_URL: "chrome://zotero-translate/content/tasks.xhtml",
  TASK_WINDOW_TYPE: "zotero-translate:tasks",
  ITEM_MENU_ID: "zotero-translate-itemmenu",
  TOOLS_MENU_ID: "zotero-translate-toolsmenu",

  // The task lifecycle; the task window reads its rows, notices and listeners.
  lifecycle: null,

  startup({ id }) {
    Zotero.ZoteroTranslate = this;
    Zotero.PreferencePanes.register({
      pluginID: id,
      src: "content/preferences.xhtml",
      label: "中文翻译",
    });
    this.notices = ZoteroTranslateNotices.createNotices({
      // No main window: the notice opens without one (Zotero shows it on top).
      isActive: () => !Zotero.getMainWindow() || Services.focus.activeWindow != null,
      open: (payload, dismissed) => this._openNotice(payload, dismissed),
    });
    this.lifecycle = ZoteroTranslateLifecycle.createTaskLifecycle({
      fetch: (url, options) => fetch(url, options),
      clock: {
        now: () => Date.now(),
        setTimeout: (fn, ms) => setTimeout(fn, ms),
        clearTimeout: (id) => clearTimeout(id),
      },
      serviceUrl: () => Zotero.Prefs.get(this.SERVICE_URL_PREF, true),
      readSource: (task) => this._readSource(task),
      attachTarget: (task) => this._attachTarget(task),
      tempFiles: {
        makeDirectory: async (task) => {
          const dir = PathUtils.join(PathUtils.tempDir, "zotero-translate", task.taskId);
          await IOUtils.makeDirectory(dir);
          return dir;
        },
        write: async (dir, fileName, bytes) => {
          const path = PathUtils.join(dir, fileName);
          await IOUtils.write(path, bytes);
          return path;
        },
        remove: (dir) => IOUtils.remove(dir, { recursive: true }),
      },
      importAttachment: async (target, { path, title }) =>
        (await Zotero.Attachments.importFromFile({ file: path, title, ...target })).id,
      attachments: this.attachmentIdentities,
      store: {
        load: () => Zotero.Prefs.get(this.TASKS_PREF, true),
        // Ask for prefs.js to be written now (Firefox writes it off the main
        // thread within about 0.1 s; otherwise about 0.6 s later), so that an
        // import intent is on disk before the import commits, also if Zotero is killed.
        save: (value) => {
          Zotero.Prefs.set(this.TASKS_PREF, value, true);
          Services.prefs.savePrefFile(null);
        },
      },
      announce: (notice) => this.announce(notice),
      debug: (message) => Zotero.debug(message),
      logError: (e) => Zotero.logError(e),
    });
    this.lifecycle.start();
    for (const window of Zotero.getMainWindows()) {
      this.addToWindow(window);
    }
  },

  shutdown() {
    this.lifecycle.stop();
    this.notices.clear();
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
    window.addEventListener("activate", this._onMainWindowActivate);
  },

  /** The main window closes (or the plugin shuts down): its listeners go,
   *  and so do all notices (their windows close with the main window). */
  removeFromWindow(window) {
    window.removeEventListener("activate", this._onMainWindowActivate);
    this.notices.clear();
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
    this.lifecycle.clearNotices();
    this.openTaskWindow(window);
    for (const item of window.ZoteroPane.getSelectedItems()) {
      await this.submit(item);
    }
  },

  /** Submit the item's source PDF, unless the item has no PDF; the lifecycle
   *  skips a PDF that already has an unfinished task. */
  async submit(item) {
    const source = await this.sourcePdf(item);
    if (!source) {
      this.lifecycle.notice(`${item.getDisplayTitle()}：该条目没有 PDF 附件`);
      return;
    }
    const parent = source.parentItem; // null for a standalone PDF
    await this.lifecycle.submit({
      sourceItemID: source.id,
      itemID: parent ? parent.id : null,
      title: (parent || source).getDisplayTitle(),
      sourceFileName: source.attachmentFilename,
    });
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

  async _readSource(task) {
    const source = await Zotero.Items.getAsync(task.sourceItemID);
    const path = source && (await source.getFilePathAsync());
    if (!path) {
      throw new Error("找不到 PDF 文件");
    }
    return IOUtils.read(path);
  },

  /** Where the PDFs go: the original item (child attachments), or, for a
   *  standalone PDF, its library and collections (standalone attachments). */
  async _attachTarget(task) {
    if (task.itemID) {
      return { parentItemID: task.itemID };
    }
    const source = await Zotero.Items.getAsync(task.sourceItemID);
    if (!source) {
      throw new Error("找不到原 PDF 附件");
    }
    return { libraryID: source.libraryID, collections: source.getCollections() };
  },

  /** The attachments' identities ({taskId, output}), for the lifecycle. */
  attachmentIdentities: {
    _path: null,
    _byIdentity: null, // "<task ID>/<output>" -> {libraryID, key}
    _loading: null,
    _writing: Promise.resolve(),

    async _load() {
      if (!this._loading) {
        this._path = PathUtils.join(Zotero.DataDirectory.dir, "zotero-translate", "attachments.json");
        this._loading = (async () => {
          this._byIdentity = (await IOUtils.exists(this._path)) ? await IOUtils.readJSON(this._path) : {};
        })();
        this._loading.catch(() => {
          this._loading = null; // read it again next time
        });
      }
      await this._loading;
      return this._byIdentity;
    },

    /** The attachment with this identity, unless it is gone or in the trash. */
    _key: ({ taskId, output }) => `${taskId}/${output}`,
    _itemKey: ({ libraryID, key }) => `${libraryID}/${key}`,

    async find(target, identity) {
      const entry = (await this._load())[this._key(identity)];
      const item = entry && (await Zotero.Items.getByLibraryAndKeyAsync(entry.libraryID, entry.key));
      return item && !item.deleted ? item.id : null;
    },

    /** Write the identity; the file is replaced only after the new one is
     *  written in full, and the identity counts only once it is. */
    async mark(id, identity) {
      const written = this._writing.then(async () => {
        const byIdentity = await this._load();
        const item = await Zotero.Items.getAsync(id);
        const next = { ...byIdentity, [this._key(identity)]: { libraryID: item.libraryID, key: item.key } };
        await IOUtils.makeDirectory(PathUtils.parent(this._path));
        await IOUtils.writeJSON(this._path, next, { tmpPath: `${this._path}.tmp` });
        this._byIdentity = next;
      });
      this._writing = written.catch(() => {});
      await written;
    },

    /** An attachment at the target (under the original item, or a standalone
     *  one in its library) with this title, added at or after `since`, that
     *  has no identity; null when there is none. */
    async unmarked(target, { title, since }) {
      const byIdentity = await this._load();
      const marked = new Set(Object.values(byIdentity).map(this._itemKey));
      let items;
      if (target.parentItemID) {
        const parent = await Zotero.Items.getAsync(target.parentItemID);
        items = parent ? await Zotero.Items.getAsync(parent.getAttachments()) : [];
      } else {
        const search = new Zotero.Search({ libraryID: target.libraryID });
        search.addCondition("itemType", "is", "attachment");
        search.addCondition("title", "is", title);
        items = (await Zotero.Items.getAsync(await search.search())).filter((item) => !item.parentItemID);
      }
      const sinceSecond = Math.floor(since / 1000) * 1000; // dateAdded has whole seconds
      const found = items.find(
        (item) =>
          !item.deleted &&
          item.getField("title") === title &&
          Zotero.Date.sqlToDate(item.dateAdded, true).getTime() >= sinceSecond &&
          !marked.has(this._itemKey(item)),
      );
      return found ? found.id : null;
    },
  },

  /** The notice at the bottom right of the main window (Zotero's own
   *  progress window) for a task that has just ended (lifecycle.js,
   *  `announce`): "翻译完成" with "打开译文", "打开双语" and "在库中显示",
   *  or "翻译失败" with the reason. It stays until it is clicked: a click on
   *  an action runs it, and any click closes the notice.
   *
   *  On macOS a progress window goes behind the main window when it opens
   *  while Zotero is in the background, and again whenever Zotero comes back
   *  to the front; Zotero's own ones close within seconds, these do not. So
   *  `notices` (notices.js) keeps the notices still to be seen: one that
   *  comes while Zotero is in the background waits for the main window to be
   *  activated, and each activation raises the open ones (focus() on the
   *  notice's window brings it to the front and leaves the main window
   *  active). Each notice's window is opened once. */
  announce(payload) {
    this.notices.announce(payload);
  },

  // The notices still to be seen (notices.js); made in startup().
  notices: null,

  /** The main window's "activate" listener. */
  _onMainWindowActivate() {
    ZoteroTranslate.notices.activated();
  },

  /** Open a notice's window (for notices.js): {raise(), close()}. A click
   *  on it calls `dismissed` and closes it. */
  _openNotice({ outcome, row }, dismissed) {
    const done = outcome === "done";
    const notice = new Zotero.ProgressWindow({ closeOnClick: false });
    let window = null; // once loaded
    notice.changeHeadline(done ? "翻译完成" : "翻译失败");
    const line = new notice.ItemProgress("attachmentPDF", row.title);
    if (done) {
      line.setProgress(100);
    } else {
      line.setError();
    }
    notice.addDescription(row.status);
    notice.show();
    const actions = done ? this._resultActions(row.results) : [];
    let closed = false;
    this._noticeDocument(line).then((doc) => {
      if (!doc || closed) {
        return;
      }
      window = doc.defaultView;
      if (actions.length) {
        const box = doc.createXULElement("hbox");
        box.setAttribute("class", "zotero-progress-item-hbox");
        for (const [label, action] of actions) {
          const link = doc.createXULElement("label");
          link.setAttribute("value", label);
          link.setAttribute("class", "zotero-text-link");
          link.setAttribute("role", "link");
          link.dataset.action = label;
          Object.assign(link.style, {
            marginInlineEnd: "12px",
            color: "LinkText",
            textDecoration: "underline",
            cursor: "pointer",
          });
          link.addEventListener("click", () => action().catch((e) => Zotero.logError(e)));
          box.append(link);
        }
        doc.getElementById("zotero-progress-text-box").append(box);
        doc.defaultView.sizeToContent();
        Zotero.ProgressWindowSet.tile(doc.defaultView);
      }
      doc.addEventListener("click", () => {
        dismissed();
        notice.close();
      });
    });
    return {
      raise: () => window?.focus(),
      close: () => {
        closed = true;
        notice.close();
      },
    };
  },

  /** "打开译文", "打开双语" and "在库中显示" for a done task's two attachments, {mono, dual}. */
  _resultActions(results) {
    return [
      ["打开译文", () => this.openAttachment(results.mono)],
      ["打开双语", () => this.openAttachment(results.dual)],
      ["在库中显示", () => this.showInLibrary([results.mono, results.dual])],
    ];
  },

  /** The notice's document once Zotero has loaded it and added the line
   *  (ProgressWindow gives no load callback); null after 5 seconds. */
  async _noticeDocument(line) {
    for (let i = 0; i < 100 && !line._hbox; i++) {
      await Zotero.Promise.delay(50);
    }
    return line._hbox ? line._hbox.ownerDocument : null;
  },

  /** Open the attachment the way Zotero opens it on a double click (a reader tab, by default). */
  async openAttachment(id) {
    const window = Zotero.getMainWindow();
    const item = await this._liveItem(id);
    if (!window || !item) {
      this._tell("找不到这份译文附件，可能已被删除");
      return;
    }
    window.focus();
    await window.ZoteroPane.viewAttachment(item.id);
  },

  /** Select the attachments in the library (switching to the library tab, expanding their item). */
  async showInLibrary(ids) {
    const window = Zotero.getMainWindow();
    const items = (await Promise.all(ids.map((id) => this._liveItem(id)))).filter(Boolean);
    if (!window || !items.length) {
      this._tell("找不到这两份译文附件，可能已被删除");
      return;
    }
    window.focus();
    await window.ZoteroPane.selectItems(items.map((item) => item.id));
  },

  /** The item with this ID, unless it is gone or in the trash; else null. */
  async _liveItem(id) {
    const item = id == null ? null : await Zotero.Items.getAsync(id);
    return item && !item.deleted ? item : null;
  },

  /** A short notice that closes by itself. */
  _tell(text) {
    const notice = new Zotero.ProgressWindow();
    notice.changeHeadline("中文翻译");
    notice.addDescription(text);
    notice.show();
    notice.startCloseTimer(5000);
  },

  openTaskWindow(window) {
    const open = Services.wm.getMostRecentWindow(this.TASK_WINDOW_TYPE);
    if (open) {
      open.focus();
      return open;
    }
    return window.openDialog(this.TASK_WINDOW_URL, "", "chrome,resizable,centerscreen,dialog=no");
  },
};
