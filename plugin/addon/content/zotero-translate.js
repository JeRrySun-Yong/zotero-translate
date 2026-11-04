/* The translation plugin inside Zotero: the "翻译为中文" item menu entry,
 * the "翻译任务" window, and the Zotero side of the task lifecycle
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
        save: (value) => Zotero.Prefs.set(this.TASKS_PREF, value, true),
      },
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

  openTaskWindow(window) {
    const open = Services.wm.getMostRecentWindow(this.TASK_WINDOW_TYPE);
    if (open) {
      open.focus();
      return open;
    }
    return window.openDialog(this.TASK_WINDOW_URL, "", "chrome,resizable,centerscreen,dialog=no");
  },
};
