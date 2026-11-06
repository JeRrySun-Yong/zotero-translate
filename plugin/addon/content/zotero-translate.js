/* The translation plugin inside Zotero: the item menu entries ("翻译为中文
 * （<current model profile>）", greyed with "（没有 PDF）" for a selection
 * without a source PDF, the "切换模型" submenu that makes a tested profile
 * current in one click, and "翻译任务"), the "翻译任务" window (from
 * the item menu and the Tools menu), the notice
 * "已提交 N 篇" after a submission (a click opens the task window), the
 * notice when a task is done or fails (with
 * "打开译文", "打开双语" and "在库中显示", also on done rows of the task
 * window), the notice asking whether to translate a paper that already has
 * a translation again ("已有译文" with "再翻译一次"), and the Zotero side of
 * the task lifecycle
 * (lifecycle.js): reading the source PDF, temporary files for the downloaded
 * PDFs, importing an output PDF as an attachment, the attachments'
 * identities, saving the tasks in a preference.
 *
 * An attachment's identity (service task ID + output) is kept in the file
 * zotero-translate/attachments.json in the Zotero data directory,
 * {"<task ID>/<output>": {libraryID, key, source}}, written whole by an atomic
 * rename; `source` (M4 ticket 16) is the key of the standalone PDF the output
 * was made from, and is left out for a paper with a parent item and in
 * entries from before it existed. The file is also what says that an
 * attachment is a translation output whatever its title: an attachment
 * recorded in it counts as one after the user renames it (also among a
 * parent item's attachments, among a standalone PDF's related items, and
 * when the source PDF is picked), and a standalone PDF with an undeleted
 * output naming it as `source` counts as translated even when no relation
 * could be saved. A damaged file or entry is skipped, not an error for the
 * item menu or a submission (see attachmentIdentities). Not as an item
 * relation, which would be hidden too: the Zotero
 * sync server accepts only its own relation predicates (dc:relation,
 * owl:sameAs, dc:replaces with Zotero item URIs; Mendeley's), so an
 * attachment with any other relation could never sync (M3 ticket 10).
 * A standalone PDF and each of its outputs are related items
 * (addRelatedItem, dc:relation, both ways), which does sync, and which is
 * what tells that the paper was translated: see _sourceOf.
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
  ITEM_MODEL_MENU_ID: "zotero-translate-itemmenu-model",
  ITEM_MODEL_POPUP_ID: "zotero-translate-itemmenu-model-popup",
  ITEM_TASKS_MENU_ID: "zotero-translate-itemmenu-tasks",
  TOOLS_MENU_ID: "zotero-translate-toolsmenu",
  // The submission notice closes by itself after this long (not while the pointer is over it).
  SUBMISSION_NOTICE_MS: 8000,

  // The task lifecycle; the task window reads its rows, notices and listeners.
  lifecycle: null,
  // The item menu's model profiles (model-menu.js): what the service last
  // said, and the "切换模型" switch. The menu draws from it at once at every
  // opening, then asks the service again, so a change made in the settings
  // page shows up. Answers to asks older than the newest are dropped.
  modelMenu: null,
  profileClient: null,

  startup({ id }) {
    Zotero.ZoteroTranslate = this;
    Zotero.PreferencePanes.register({
      pluginID: id,
      src: "content/preferences.xhtml",
      scripts: ["content/core.js", "content/profiles.js", "content/preferences.js"],
      label: "中文翻译",
    });
    // The item menu's GET /profiles and the "切换模型" click (profiles.js,
    // loaded into the plugin scope by bootstrap.js). AbortSignal.timeout
    // needs a window, which this scope has none of: the main window's timer
    // makes the request's signal.
    this.profileClient = ZoteroTranslateProfiles.createClient({
      fetch: (url, options) => fetch(url, options),
      serviceUrl: () => Zotero.Prefs.get(this.SERVICE_URL_PREF, true),
      timeoutSignal: (ms) => {
        const controller = new AbortController();
        Zotero.getMainWindow()?.setTimeout(() => controller.abort(), ms);
        return controller.signal;
      },
    });
    this.modelMenu = ZoteroTranslateModelMenu.createModelMenu({
      client: this.profileClient,
      onChange: () => {
        for (const window of Zotero.getMainWindows()) {
          this._drawModelMenu(window);
        }
      },
    });
    // Read the profiles now, so the first opening of the item menu already
    // names the current profile (the first opening also asks again).
    this.modelMenu.refresh().catch((e) => Zotero.logError(e));
    this.notices = ZoteroTranslateNotices.createNotices({
      // With no main window open (macOS: Zotero runs on), notices wait for the next one.
      isActive: () => Zotero.getMainWindow() != null && Services.focus.activeWindow != null,
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
      relate: (sourceItemID, outputID) => this._relateWithSource(sourceItemID, outputID),
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
    this.menu = ZoteroTranslateItemMenu.createItemMenu({
      sourcePdf: (item) => this.sourcePdf(item),
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
    const itemPopup = doc.getElementById("zotero-itemmenu");
    const itemMenu = doc.createXULElement("menuitem");
    itemMenu.id = this.ITEM_MENU_ID;
    itemMenu.setAttribute("label", "翻译为中文");
    itemMenu.addEventListener("command", () => this.translateSelected(window));
    // "切换模型 ▸": the tested model profiles, one click to make one current
    // (built from the service's answer on each opening, _drawModelMenu).
    const modelMenu = doc.createXULElement("menu");
    modelMenu.id = this.ITEM_MODEL_MENU_ID;
    modelMenu.setAttribute("label", "切换模型");
    const modelPopup = doc.createXULElement("menupopup");
    modelPopup.id = this.ITEM_MODEL_POPUP_ID;
    modelMenu.append(modelPopup);
    // "翻译任务" in the item menu too, always shown, so the task window is one right-click away.
    const itemTasksMenu = doc.createXULElement("menuitem");
    itemTasksMenu.id = this.ITEM_TASKS_MENU_ID;
    itemTasksMenu.setAttribute("label", "翻译任务");
    itemTasksMenu.addEventListener("command", () => this.openTaskWindow(window));
    itemPopup.append(itemMenu, modelMenu, itemTasksMenu);
    // Only the popup's own popupshowing/popuphidden events: a submenu's
    // bubble up and must not close or replace this menu's source check.
    const detachItemMenu = this.menu.addToPopup(itemPopup, {
      menu: () => doc.getElementById(this.ITEM_MENU_ID),
      selection: () => window.ZoteroPane.getSelectedItems(),
      label: () => this._itemMenuLabel(),
    });
    itemPopup.addEventListener("popupshowing", this._onItemMenuShowing);
    this._menuDetach.set(window, () => {
      detachItemMenu();
      itemPopup.removeEventListener("popupshowing", this._onItemMenuShowing);
    });

    const toolsMenu = doc.createXULElement("menuitem");
    toolsMenu.id = this.TOOLS_MENU_ID;
    toolsMenu.setAttribute("label", "翻译任务");
    toolsMenu.addEventListener("command", () => this.openTaskWindow(window));
    doc.getElementById("menu_ToolsPopup").append(toolsMenu);
    window.addEventListener("activate", this._onMainWindowActivate);
    // A new main window may be active before its listener is added, and a
    // notice opened while the window is still coming up ends behind it: open
    // the waiting notices, and raise the open ones, once it is up.
    window.setTimeout(() => this.notices.activated(), 1000);
  },

  /** A main window closes (or the plugin shuts down): its listeners go, and
   *  the notices in it wait for the next main window activated (notices.js). */
  removeFromWindow(window) {
    window.removeEventListener("activate", this._onMainWindowActivate);
    this.notices.windowClosed(window);
    this._menuDetach.get(window)?.();
    this._menuDetach.delete(window);
    const doc = window.document;
    doc.getElementById(this.ITEM_MENU_ID)?.remove();
    doc.getElementById(this.ITEM_MODEL_MENU_ID)?.remove();
    doc.getElementById(this.ITEM_TASKS_MENU_ID)?.remove();
    doc.getElementById(this.TOOLS_MENU_ID)?.remove();
  },

  // The entry shows for regular items and for attachments (with or without
  // a parent item), and is greyed when no selected item has a source PDF
  // (itemmenu.js). Its label names the current model profile, and "切换模型"
  // lists the tested ones: both from the last answer at once, then from the
  // service again (it may have changed in the settings page since).
  menu: null,

  // Main window -> the function that removes the item menu popup's listeners.
  _menuDetach: new WeakMap(),

  /** A main window's item menu opens: draw the item menu's label and the
   *  "切换模型" submenu from the last answer, then ask the service again. A
   *  submenu's popupshowing bubbles up too; only the item menu's own counts. */
  _onItemMenuShowing(event) {
    if (event.target !== event.currentTarget) {
      return;
    }
    ZoteroTranslate._drawModelMenu(event.target.ownerGlobal);
    ZoteroTranslate._readProfiles().catch((e) => Zotero.logError(e));
  },

  /** Ask the service for its model profiles; its answer redraws the menu
   *  (modelMenu's onChange), unless a newer ask or a switch has replaced it. */
  async _readProfiles() {
    await this.modelMenu.refresh();
  },

  /** The item menu's label with the current profile's name, without the
   *  item state's suffix: itemmenu.js adds "（没有 PDF）" when the selection
   *  has none. */
  _itemMenuLabel() {
    const { listing } = this.modelMenu.state();
    const current = listing && listing.profiles.find((profile) => profile.id === listing.current);
    return ZoteroTranslateCore.itemMenuLabel(current ? current.name : "");
  },

  /** Draw the item menu's label and the "切换模型" submenu (core.modelMenuItems)
   *  from the last answer. The submenu is rebuilt only when its items change,
   *  so an answer that arrives while the menu is open does not close an open
   *  submenu; `force` rebuilds it anyway, which also undoes the radio check
   *  XUL put on a refused switch's item. */
  _drawModelMenu(window, { force = false } = {}) {
    const doc = window.document;
    const itemMenu = doc.getElementById(this.ITEM_MENU_ID);
    const popup = doc.getElementById(this.ITEM_MODEL_POPUP_ID);
    if (!itemMenu || !popup) {
      return;
    }
    const Core = ZoteroTranslateCore;
    const suffix = itemMenu.disabled && !itemMenu.hidden ? Core.NO_PDF_SUFFIX : "";
    itemMenu.setAttribute("label", `${this._itemMenuLabel()}${suffix}`);
    const { listing, problem } = this.modelMenu.state();
    const items = Core.modelMenuItems(listing, problem || undefined);
    const signature = JSON.stringify(items);
    if (!force && popup.dataset.items === signature) {
      return;
    }
    popup.dataset.items = signature;
    popup.replaceChildren(
      ...items.map((item) => {
        const element = doc.createXULElement("menuitem");
        element.setAttribute("type", "radio");
        element.setAttribute("label", item.label);
        element.checked = item.checked; // the property: "checked" as an attribute would be true even for false
        element.disabled = item.disabled;
        if (item.id != null) {
          element.addEventListener("command", () => this.switchToProfile(item.id));
        }
        return element;
      }),
    );
  },

  /** A "切换模型" click: make the profile current. One request; only the
   *  papers submitted afterwards use it. The service refuses a profile whose
   *  connection test has not passed. XUL checked the clicked radio itself,
   *  so the menu is drawn again from the known current profile after every
   *  answer, even one that changes nothing (`force`: the items may be the
   *  same); a refused switch says why, a superseded one says nothing
   *  (model-menu.js). Answers already on their way cannot undo the switch. */
  async switchToProfile(id) {
    const outcome = await this.modelMenu.switchTo(id);
    for (const window of Zotero.getMainWindows()) {
      this._drawModelMenu(window, { force: true });
    }
    if (!outcome.ok && !outcome.superseded) {
      this._tell(`没有切换模型：${outcome.error}`);
    }
  },

  /** Submit every selected item, one after another; the service runs them in
   *  turn. The task window does not open (the main window keeps the focus):
   *  once all are uploaded, the submission notice says "已提交 N 篇" (and why
   *  any were skipped; lifecycle.js submitBatch, which announces it, never
   *  after the plugin is shut down); a click on it opens the task window. */
  async translateSelected(window) {
    await this.lifecycle.submitBatch(window.ZoteroPane.getSelectedItems(), (item) => this._sourceOf(item));
  },

  /** Submit the item's source PDF, unless the item has no PDF; the lifecycle
   *  skips a PDF that already has an unfinished task. Returns the new task, or null. */
  async submit(item) {
    const source = await this._sourceOf(item);
    if (typeof source === "string") {
      this.lifecycle.notice(source);
      return null;
    }
    return this.lifecycle.submit(source);
  },

  /** The item's source for the lifecycle, or why it has none (a string).
   *  `translated` tells the lifecycle that a translation output is already
   *  there, so it asks before translating the paper again: for a paper with
   *  a parent item, one among the parent's attachments; for a standalone PDF
   *  (whose outputs have no link back to it), a task of this Zotero session
   *  that attached both outputs, or one of its related items being an
   *  undeleted translation output — the plugin links the outputs to the
   *  source both ways when it attaches them, and the relations stay after a
   *  restart or "清除已结束" remove the task records, or an undeleted output
   *  that the identity file records with the PDF as its `source`, which
   *  holds even when no relation could be saved (core.lifecycleSource).
   *  An output counts under any title once the plugin has attached it. */
  async _sourceOf(item) {
    const source = await this.sourcePdf(item);
    if (!source) {
      return `${item.getDisplayTitle()}：该条目没有 PDF 附件`;
    }
    const parent = source.parentItem; // null for a standalone PDF
    return ZoteroTranslateCore.lifecycleSource({
      sourceItemID: source.id,
      itemID: parent ? parent.id : null,
      title: (parent || source).getDisplayTitle(),
      sourceFileName: source.attachmentFilename,
      ...(parent
        ? { parentTranslated: await this._hasTranslation(parent) }
        : {
            taskRecord: this.lifecycle.wasTranslated(source.id),
            related: await this._relatedItems(source),
            recorded: await this.attachmentIdentities.hasOutputOf(source),
          }),
    });
  },

  /** A standalone PDF's related items, as {title, deleted, identified}, for
   *  the "already translated" check (core.lifecycleSource): `relatedItems`
   *  are item keys in the same library; `identified` says the plugin
   *  attached the item (the identity file records it), whatever its title. */
  async _relatedItems(source) {
    const attached = await this.attachmentIdentities.attachedKeys();
    const items = await Promise.all(
      source.relatedItems.map((key) => Zotero.Items.getByLibraryAndKeyAsync(source.libraryID, key)),
    );
    return items.filter(Boolean).map((item) => ({
      title: item.getField("title"),
      deleted: item.deleted,
      identified: attached.has(this.attachmentIdentities._itemKey(item)),
    }));
  },

  /** Link a standalone PDF and one of its outputs as related items, both
   *  ways, and save both; a separate save is what Zotero needs for a
   *  relation (item.js addRelatedItem). A failure makes the lifecycle log it
   *  and go on with the attaching: the outputs are attached either way. */
  async _relateWithSource(sourceItemID, outputID) {
    const source = await Zotero.Items.getAsync(sourceItemID);
    const output = await Zotero.Items.getAsync(outputID);
    if (!source || !output) {
      throw new Error("找不到原 PDF 附件或译文附件，未能建立关联");
    }
    source.addRelatedItem(output);
    await source.saveTx();
    output.addRelatedItem(source);
    await output.saveTx();
  },

  /** Whether the item already has one of the plugin's translation outputs
   *  among its attachments: the plugin's way of telling that the paper was
   *  translated, also by an earlier Zotero session. A standalone PDF has no
   *  such parent item: `_sourceOf` asks for its related items and the
   *  lifecycle's task records instead. */
  async _hasTranslation(item) {
    const attachments = await Zotero.Items.getAsync(item.getAttachments());
    const attached = await this.attachmentIdentities.attachedKeys();
    return attachments.some(
      (attachment) =>
        !attachment.deleted &&
        ZoteroTranslateCore.isTranslationOutput(
          attachment.getField("title"),
          attached.has(this.attachmentIdentities._itemKey(attachment)),
        ),
    );
  },

  /** The PDF to translate: the selected attachment itself, or the item's best
   *  PDF attachment; never an earlier translation output (by its title, or
   *  because the identity file records it: also after a rename). */
  async sourcePdf(item) {
    if (!item.isRegularItem()) {
      return this._asSource(item);
    }
    const attachments = await item.getBestAttachments();
    return this._pick(attachments);
  },

  async _asSource(attachment) {
    return attachment.isAttachment() ? this._pick([attachment]) : null;
  },

  async _pick(attachments) {
    const attached = await this.attachmentIdentities.attachedKeys();
    const candidates = attachments.map((attachment) => ({
      isPDF: attachment.isPDFAttachment(),
      title: attachment.getField("title"),
      identified: attached.has(this.attachmentIdentities._itemKey(attachment)),
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

  /** The attachments' identities ({taskId, output}), for the lifecycle.
   *
   *  The file can be damaged (edited by hand, cut short, written by another
   *  version). A top level that is not an object counts as an empty file; an
   *  entry that is not {libraryID (an integer), key (a non-empty string),
   *  source (a string, or left out)} is skipped when the file is read, with
   *  one log line saying how many. The skipped entries stay in the file
   *  (`mark` writes the file whole, with them), and what they could have
   *  told is lost only until the file is repaired: the item menu and a
   *  submission must not fail on a file. */
  attachmentIdentities: {
    _path: null,
    _byIdentity: null, // "<task ID>/<output>" -> {libraryID, key, source?}
    _loading: null,
    _writing: Promise.resolve(),

    async _load() {
      if (!this._loading) {
        this._path = PathUtils.join(Zotero.DataDirectory.dir, "zotero-translate", "attachments.json");
        this._loading = (async () => {
          let data = {};
          let damaged = 0;
          if (await IOUtils.exists(this._path)) {
            data = await IOUtils.readJSON(this._path);
            if (data === null || typeof data !== "object" || Array.isArray(data)) {
              data = {};
              damaged = 1;
            } else {
              damaged = Object.values(data).filter((entry) => !this._valid(entry)).length;
            }
          }
          this._byIdentity = data;
          if (damaged) {
            Zotero.logError(new Error(`附件身份文件 ${this._path} 有 ${damaged} 处损坏，已忽略（文件没有改）`));
          }
        })();
        this._loading.catch(() => {
          this._loading = null; // read it again next time
        });
      }
      await this._loading;
      return this._byIdentity;
    },

    /** Whether `entry` is a usable record (see above). */
    _valid(entry) {
      return (
        entry !== null &&
        typeof entry === "object" &&
        !Array.isArray(entry) &&
        Number.isInteger(entry.libraryID) &&
        typeof entry.key === "string" &&
        entry.key !== "" &&
        (entry.source === undefined || typeof entry.source === "string")
      );
    },

    /** The usable entries, as the records themselves. */
    async _entries() {
      return Object.values(await this._load()).filter((entry) => this._valid(entry));
    },

    /** The attachment with this identity, unless it is gone or in the trash. */
    _key: ({ taskId, output }) => `${taskId}/${output}`,
    _itemKey: ({ libraryID, key }) => `${libraryID}/${key}`,

    /** "<libraryID>/<key>" of every attachment the file records: the
     *  plugin's outputs, whatever their titles now are. The file only
     *  helps to recognise outputs, so one that cannot be read is logged and
     *  counts as empty: the item menu and a submission must not fail on it. */
    async attachedKeys() {
      try {
        return new Set((await this._entries()).map(this._itemKey));
      } catch (e) {
        Zotero.logError(e);
        return new Set();
      }
    },

    /** Whether the file records an output of the standalone PDF `source` (a
     *  Zotero item) that still exists and is not in the trash: an entry whose
     *  `source` is its key. Entries from before `source` have none. */
    async hasOutputOf(source) {
      let entries;
      try {
        entries = await this._entries();
      } catch (e) {
        Zotero.logError(e);
        return false;
      }
      for (const entry of entries) {
        if (entry.source !== source.key || entry.libraryID !== source.libraryID) {
          continue;
        }
        const output = await Zotero.Items.getByLibraryAndKeyAsync(entry.libraryID, entry.key);
        if (output && !output.deleted) {
          return true;
        }
      }
      return false;
    },

    async find(target, identity) {
      const entry = (await this._load())[this._key(identity)];
      const item = this._valid(entry) && (await Zotero.Items.getByLibraryAndKeyAsync(entry.libraryID, entry.key));
      return item && !item.deleted ? item.id : null;
    },

    /** Write the identity; the file is replaced only after the new one is
     *  written in full, and the identity counts only once it is. For the
     *  output of a standalone PDF (`identity.sourceItemID`) the entry also
     *  records the PDF's key as its `source`. */
    async mark(id, identity) {
      const written = this._writing.then(async () => {
        const byIdentity = await this._load();
        const item = await Zotero.Items.getAsync(id);
        const source = identity.sourceItemID != null ? await Zotero.Items.getAsync(identity.sourceItemID) : null;
        const entry = { libraryID: item.libraryID, key: item.key, ...(source ? { source: source.key } : {}) };
        const next = { ...byIdentity, [this._key(identity)]: entry };
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
      const marked = new Set((await this._entries()).map(this._itemKey));
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

  /** Open a notice's window in the main window (for notices.js): {parent,
   *  raise(), close()}. A click on it calls `dismissed` and closes it. */
  _openNotice(payload, dismissed) {
    if (payload.outcome === "submitted") {
      return this._openSubmissionNotice(payload, dismissed);
    }
    if (payload.outcome === "resubmit") {
      return this._openResubmitNotice(payload, dismissed);
    }
    const { outcome, row } = payload;
    const done = outcome === "done";
    const parent = Zotero.getMainWindow();
    const notice = new Zotero.ProgressWindow({ window: parent, closeOnClick: false });
    let window = null; // once loaded
    notice.changeHeadline(done ? "翻译完成" : "翻译失败");
    const line = new notice.ItemProgress("attachmentPDF", row.title);
    if (done) {
      line.setProgress(100);
    } else {
      line.setError();
    }
    notice.addDescription(ZoteroTranslateCore.statusText(row));
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
      parent,
      raise: () => window?.focus(),
      close: () => {
        closed = true;
        notice.close();
      },
    };
  },

  /** The 已有译文 notice (lifecycle.js submitBatch): "已有译文" with the
   *  papers that already have a translation, "再翻译一次？" and a
   *  "再翻译一次" link that submits them (lifecycle.resubmit); a click
   *  anywhere else only closes it, so nothing is translated again without
   *  that click. It stays until it is clicked, like the notices of ended
   *  tasks. */
  _openResubmitNotice({ titles, keys }, dismissed) {
    const parent = Zotero.getMainWindow();
    const notice = new Zotero.ProgressWindow({ window: parent, closeOnClick: false });
    let window = null; // once loaded
    let closed = false;
    notice.changeHeadline("已有译文");
    const lines = titles.map((title) => new notice.ItemProgress("attachmentPDF", title));
    for (const line of lines) {
      line.setProgress(100);
    }
    notice.addDescription("再翻译一次？");
    notice.show();
    this._noticeDocument(lines[0]).then((doc) => {
      if (!doc || closed) {
        return;
      }
      window = doc.defaultView;
      const link = doc.createXULElement("label");
      link.setAttribute("value", "再翻译一次");
      link.setAttribute("class", "zotero-text-link");
      link.setAttribute("role", "link");
      link.dataset.action = "resubmit";
      Object.assign(link.style, { color: "LinkText", textDecoration: "underline", cursor: "pointer" });
      link.addEventListener("click", () => ZoteroTranslate.lifecycle.resubmit(keys).catch((e) => Zotero.logError(e)));
      const box = doc.createXULElement("hbox");
      box.setAttribute("class", "zotero-progress-item-hbox");
      box.append(link);
      doc.getElementById("zotero-progress-text-box").append(box);
      window.sizeToContent();
      Zotero.ProgressWindowSet.tile(window);
      doc.addEventListener("click", () => {
        dismissed();
        notice.close();
      });
    });
    return {
      parent,
      raise: () => window?.focus(),
      close: () => {
        closed = true;
        notice.close();
      },
    };
  },

  /** The submission notice (core.js submissionNotice): the headline
   *  ("已提交 N 篇"), the titles taken, why others were skipped, and
   *  "查看翻译任务". A click anywhere on it opens the task window. Unlike
   *  the notices of ended tasks it closes by itself after
   *  SUBMISSION_NOTICE_MS (Zotero keeps it open while the pointer is over
   *  it), and is then forgotten (`dismissed`). */
  _openSubmissionNotice({ headline, titles, reasons }, dismissed) {
    const parent = Zotero.getMainWindow();
    const notice = new Zotero.ProgressWindow({ window: parent, closeOnClick: false });
    let window = null; // once loaded
    let closed = false;
    notice.changeHeadline(headline);
    const lines = [
      ...titles.map((title) => new notice.ItemProgress("attachmentPDF", title)),
      ...reasons.map((reason) => new notice.ItemProgress(null, reason)),
    ];
    for (const line of lines) {
      line.setProgress(100);
    }
    notice.show();
    notice.startCloseTimer(this.SUBMISSION_NOTICE_MS);
    this._noticeDocument(lines[0]).then((doc) => {
      if (!doc || closed) {
        return;
      }
      window = doc.defaultView;
      const link = doc.createXULElement("label");
      link.setAttribute("value", "查看翻译任务");
      link.setAttribute("class", "zotero-text-link");
      link.setAttribute("role", "link");
      link.dataset.action = "openTaskWindow";
      Object.assign(link.style, { color: "LinkText", textDecoration: "underline", cursor: "pointer" });
      const box = doc.createXULElement("hbox");
      box.setAttribute("class", "zotero-progress-item-hbox");
      box.append(link);
      doc.getElementById("zotero-progress-text-box").append(box);
      window.sizeToContent();
      Zotero.ProgressWindowSet.tile(window);
      window.addEventListener("unload", () => {
        if (!closed) {
          dismissed(); // closed by its timer, or by Zotero with the main window
        }
      });
      doc.addEventListener("click", () => {
        dismissed();
        closed = true;
        notice.close();
        const main = parent && !parent.closed ? parent : Zotero.getMainWindow();
        if (main) {
          this.openTaskWindow(main);
        }
      });
    });
    return {
      parent,
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
    const item = await this._liveItem(id);
    if (!item) {
      this._tell("找不到这份译文附件，可能已被删除");
      return;
    }
    const window = await this._readyMainWindow();
    if (window) {
      window.focus();
      await window.ZoteroPane.viewAttachment(item.id);
    }
  },

  /** Select the attachments in the library (switching to the library tab, expanding their item). */
  async showInLibrary(ids) {
    const items = (await Promise.all(ids.map((id) => this._liveItem(id)))).filter(Boolean);
    if (!items.length) {
      this._tell("找不到这两份译文附件，可能已被删除");
      return;
    }
    const window = await this._readyMainWindow();
    if (window) {
      window.focus();
      await window.ZoteroPane.selectItems(items.map((item) => item.id));
    }
  },

  /** The main window to act in; with none open (macOS), a new one once its
   *  panes are ready (up to 10 seconds; otherwise a short notice and null). */
  async _readyMainWindow() {
    const window = await ZoteroTranslateNotices.readyMainWindow({
      mainWindow: () => Zotero.getMainWindow(),
      openMainWindow: () => Zotero.openMainWindow(),
      isReady: (w) => !!(w.ZoteroPane && w.ZoteroPane.collectionsView && w.ZoteroPane.itemsView),
      delay: () => Zotero.Promise.delay(100),
    });
    if (!window) {
      this._tell("Zotero 主窗口没能打开，请先打开主窗口再试");
    }
    return window;
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
