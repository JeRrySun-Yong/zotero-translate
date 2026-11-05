/* The "中文翻译" settings page (preferences.xhtml): the service's status, the
 * model profiles with the current one marked, and the form to make or edit
 * one, with "测试连接" (saves the form first, then tests), "应用" and
 * "删除" (a second click confirms; no dialog). The logic without Zotero is
 * in profiles.js; this file only draws it and wires the page.
 *
 * Zotero loads core.js, profiles.js and this file into the pane's scope
 * (Zotero.PreferencePanes.register's scripts, zotero-translate.js) before
 * it inserts the pane, then dispatches "load" at the pane's root element;
 * "unload" when the settings window closes. The page asks the service for
 * its profiles every 5 seconds while it is open, and after every action;
 * a refresh never touches what is being typed in the form.
 */
{
  const P = ZoteroTranslateProfiles;
  const SERVICE_URL_PREF = "extensions.zotero-translate.serviceUrl";
  const ROOT_ID = "zotero-translate-prefs";
  const REFRESH_MS = 5000;
  const CONFIRM_REMOVE_MS = 5000;
  const XHTML = "http://www.w3.org/1999/xhtml";
  const FIELDS = {
    name: "zotero-translate-name",
    baseUrl: "zotero-translate-base-url",
    apiKey: "zotero-translate-key",
    model: "zotero-translate-model",
    concurrency: "zotero-translate-concurrency",
    timeout: "zotero-translate-timeout",
  };
  // The 流式请求 switch (a checkbox: .checked, not .value), ticket 12.
  const STREAM_ID = "zotero-translate-stream";

  const client = P.createClient({
    fetch: (url, options) => fetch(url, options),
    serviceUrl: () => Zotero.Prefs.get(SERVICE_URL_PREF, true),
  });
  const $ = (id) => document.getElementById(id);

  const page = {
    listing: null,
    selectedId: null, // null: the form is a new profile
    picked: false, // a profile (or a new one) is in the form; until then the first answer picks one
    busy: false,
    confirmRemove: null, // timer while "删除" waits for its second click
    timer: null,

    saved() {
      return (this.listing && this.listing.profiles.find((profile) => profile.id === this.selectedId)) || null;
    },

    form() {
      return {
        ...Object.fromEntries(Object.entries(FIELDS).map(([name, id]) => [name, $(id).value])),
        stream: $(STREAM_ID).checked,
      };
    },

    async refresh() {
      const found = await client.status();
      if (!$(ROOT_ID)) {
        return; // the window closed meanwhile
      }
      const view = P.statusView(found);
      const status = $("zotero-translate-status");
      status.textContent = view.text;
      status.dataset.running = String(view.running);
      $("zotero-translate-status-help").textContent = view.help;
      $("zotero-translate-status-help").hidden = !view.help;
      if (this.listing && !found.listing) {
        this.showResult(null); // what the last action said no longer holds
      }
      this.listing = found.listing || null;
      if (this.listing && !this.picked) {
        // First answer: a new profile's form when there is none yet, else the current one.
        const users = this.listing.profiles.filter((profile) => !profile.builtin);
        this.select(users.length ? this.listing.current : null);
      } else if (this.selectedId && this.listing && !this.saved()) {
        this.select(null); // deleted elsewhere
      }
      this.drawList();
      this.drawButtons();
    },

    /** Show a profile (or a new one) in the form, discarding what was typed. */
    select(id) {
      this.selectedId = id;
      this.picked = true;
      const saved = this.saved();
      const form = P.formFromProfile(saved);
      for (const [name, fieldId] of Object.entries(FIELDS)) {
        $(fieldId).value = form[name];
        $(fieldId).disabled = Boolean(saved && saved.builtin);
      }
      $(STREAM_ID).checked = form.stream;
      $(STREAM_ID).disabled = Boolean(saved && saved.builtin);
      $(FIELDS.apiKey).placeholder = saved && !saved.builtin ? `${P.keyText(saved)}（不改就留空）` : "密钥只保存在本机翻译服务";
      $("zotero-translate-form-title").textContent = !saved
        ? "新建配置"
        : saved.builtin
          ? `内置配置：${saved.name}`
          : `编辑配置：${saved.name}`;
      $("zotero-translate-fields").hidden = Boolean(saved && saved.builtin);
      $("zotero-translate-key-note").hidden = Boolean(saved && saved.builtin);
      $("zotero-translate-builtin").hidden = !(saved && saved.builtin);
      $("zotero-translate-builtin").textContent = saved && saved.builtin ? P.builtinDetail(saved) : "";
      this.showResult(null);
      this.resetRemove();
      this.drawList();
      this.drawButtons();
    },

    drawList() {
      const rows = this.listing ? P.profileRows(this.listing) : [];
      $("zotero-translate-profiles").replaceChildren(
        ...rows.map((row) => {
          const tr = document.createElementNS(XHTML, "tr");
          tr.dataset.selected = String(row.id === this.selectedId);
          for (const text of [row.current ? "●" : "", row.name, row.model, row.state, row.key]) {
            const td = document.createElementNS(XHTML, "td");
            td.textContent = text;
            tr.append(td);
          }
          tr.addEventListener("click", () => this.select(row.id));
          return tr;
        }),
      );
    },

    drawButtons() {
      const connected = Boolean(this.listing);
      const state = P.buttons(this.form(), this.saved(), { connected });
      $("zotero-translate-test").disabled = this.busy || !state.test;
      $("zotero-translate-apply").disabled = this.busy || !state.apply;
      $("zotero-translate-remove").disabled = this.busy || !state.remove;
      if (connected) {
        // Hidden where they never apply: no test for the built-in profile, no delete for a new one.
        const saved = this.saved();
        $("zotero-translate-test").hidden = Boolean(saved && saved.builtin);
        $("zotero-translate-remove").hidden = !saved || saved.builtin;
      }
      $("zotero-translate-apply-hint").textContent = state.applyHint;
      $("zotero-translate-new").disabled = !connected;
    },

    /** {ok, text, detail} under the buttons; null hides it. */
    showResult(view) {
      const result = $("zotero-translate-result");
      result.hidden = !view;
      result.textContent = view ? view.text : "";
      result.dataset.ok = view ? String(view.ok) : "";
      $("zotero-translate-result-detail").hidden = !(view && view.detail);
      $("zotero-translate-result-detail").textContent = (view && view.detail) || "";
    },

    async run(action) {
      this.busy = true;
      this.resetRemove();
      this.drawButtons();
      try {
        await action();
      } catch (e) {
        Zotero.logError(e);
        this.showResult({ ok: false, text: String(e), detail: "" });
      } finally {
        this.busy = false;
        await this.refresh();
      }
    },

    test() {
      return this.run(async () => {
        const testing = { ok: true, text: "正在测试连接…（最长约 1 分钟）", detail: "" };
        this.showResult(testing);
        const outcome = await client.saveAndTest(this.form(), this.saved(), {
          // Saved: show the service's state now (the current mark, "未测试"),
          // not after the test; a save of the current profile's model makes
          // the built-in profile current again.
          onSaved: async (profile) => {
            await this.refresh();
            this.select(profile.id); // the form as saved (the key field empty again)
            this.showResult(testing);
          },
        });
        this.showResult(outcome.error ? { ok: false, text: outcome.error, detail: "" } : outcome.view);
      }); // run() asks the service again once the test is over
    },

    apply() {
      const saved = this.saved();
      return this.run(async () => {
        const outcome = await client.apply(saved.id);
        this.showResult(
          outcome.error
            ? { ok: false, text: outcome.error, detail: "" }
            : { ok: true, text: `已应用：之后提交的论文用“${saved.name}”`, detail: "" },
        );
      });
    },

    remove() {
      const button = $("zotero-translate-remove");
      if (!this.confirmRemove) {
        button.textContent = "再点一次确认删除";
        this.confirmRemove = setTimeout(() => this.resetRemove(), CONFIRM_REMOVE_MS);
        return;
      }
      const saved = this.saved();
      this.run(async () => {
        const outcome = await client.remove(saved.id);
        if (outcome.error) {
          this.showResult({ ok: false, text: outcome.error, detail: "" });
          return;
        }
        await this.refresh();
        this.select(null);
        this.showResult({ ok: true, text: `已删除“${saved.name}”`, detail: "" });
      });
    },

    resetRemove() {
      clearTimeout(this.confirmRemove);
      this.confirmRemove = null;
      $("zotero-translate-remove").textContent = "删除";
    },

    init() {
      for (const id of [...Object.values(FIELDS), STREAM_ID]) {
        $(id).addEventListener("input", () => {
          this.resetRemove();
          this.drawButtons();
        });
      }
      $("zotero-translate-new").addEventListener("click", () => this.select(null));
      $("zotero-translate-test").addEventListener("click", () => this.test());
      $("zotero-translate-apply").addEventListener("click", () => this.apply());
      $("zotero-translate-remove").addEventListener("click", () => this.remove());
      $("zotero-translate-service-url").addEventListener("change", () => this.refresh());
      $(ROOT_ID).addEventListener("unload", () => clearInterval(this.timer));
      this.timer = setInterval(() => {
        if (!this.busy) {
          this.refresh();
        }
      }, REFRESH_MS);
      this.refresh();
    },
  };

  document.addEventListener(
    "load",
    (event) => {
      if (event.target.id === ROOT_ID) {
        page.init();
      }
    },
    true,
  );
}
