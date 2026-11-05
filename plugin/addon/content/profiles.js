/* The settings page's model profiles, without Zotero (M4 ticket 04, ADR 0005):
 * the form's fields as a request to the translation service, what the page
 * shows for the service, the profile list, a connection test and the
 * buttons, and the requests themselves (fetch is injected).
 *
 * The API key goes from the form to the service once, in the request that
 * saves the profile; it is never kept in a Zotero setting. The service
 * gives back only whether a key is set and its last 4 characters. The
 * service's interface is in src/zotero_translate/service.py (its file
 * header): every write carries the header X-Zotero-Translate: 1 and a JSON
 * body, so a web page cannot send it across origins.
 *
 * The preference pane (preferences.js) loads this file with
 * Services.scriptloader.loadSubScript, which defines ZoteroTranslateProfiles
 * in the pane's scope; Node loads it with require() for plugin/test/.
 */
var ZoteroTranslateProfiles = (() => {
  // The service's limits (zotero_translate.profiles).
  const MAX_CONCURRENCY = 256;
  const MAX_TIMEOUT_SECONDS = 3600;

  /** The form's fields (strings) for a saved profile, or for a new one (null).
   *  The key field stays empty: the key is never sent back. */
  function formFromProfile(profile) {
    if (!profile) {
      return { name: "", baseUrl: "", apiKey: "", model: "", concurrency: "", timeout: "" };
    }
    return {
      name: profile.name || "",
      baseUrl: profile.base_url || "",
      apiKey: "",
      model: profile.model || "",
      concurrency: profile.concurrency == null ? "" : String(profile.concurrency),
      timeout: profile.request_timeout_seconds == null ? "" : String(profile.request_timeout_seconds),
    };
  }

  /** The form as the service's profile fields, or {error} naming the field to fix. */
  function formFields(form, isNew) {
    const text = (value) => String(value ?? "").trim();
    const fields = { name: text(form.name), base_url: text(form.baseUrl), model: text(form.model) };
    for (const [name, label] of [["name", "名称"], ["base_url", "地址"]]) {
      if (!fields[name]) {
        return { error: `请填写${label}` };
      }
    }
    const apiKey = text(form.apiKey);
    if (apiKey) {
      fields.api_key = apiKey;
    } else if (isNew) {
      return { error: "请填写密钥" };
    }
    if (!fields.model) {
      return { error: "请填写模型 ID" };
    }
    const concurrency = text(form.concurrency);
    if (concurrency) {
      const value = Number(concurrency);
      if (!/^\d+$/.test(concurrency) || value < 1 || value > MAX_CONCURRENCY) {
        return { error: `并发上限要是 1 到 ${MAX_CONCURRENCY} 的整数` };
      }
      fields.concurrency = value;
    } // empty: the service's default (16), or for an edit the saved value
    const timeout = text(form.timeout);
    if (timeout) {
      const value = Number(timeout);
      if (!Number.isFinite(value) || value < 1 || value > MAX_TIMEOUT_SECONDS) {
        return { error: `请求超时要是 1 到 ${MAX_TIMEOUT_SECONDS} 的秒数，或留空` };
      }
      fields.request_timeout_seconds = value;
    } else if (!isNew) {
      fields.request_timeout_seconds = null; // empty: the service's default
    }
    return { fields };
  }

  /** The request that saves the form: for a new profile (`saved` null) a POST
   *  with every field given, for a saved one a PUT with only the fields that
   *  changed (an empty key field keeps the key); null when nothing changed;
   *  {error} when the form cannot be saved. `path` is for core's serviceUrl. */
  function profileRequest(form, saved) {
    const { fields, error } = formFields(form, !saved);
    if (error) {
      return { error };
    }
    if (!saved) {
      return { method: "POST", path: ["profiles"], body: fields };
    }
    const before = {
      name: saved.name,
      base_url: saved.base_url,
      model: saved.model,
      concurrency: saved.concurrency,
      request_timeout_seconds: saved.request_timeout_seconds ?? null,
    };
    const changed = Object.fromEntries(Object.entries(fields).filter(([name, value]) => before[name] !== value));
    if (Object.keys(changed).length === 0) {
      return null;
    }
    return { method: "PUT", path: ["profiles", saved.id], body: changed };
  }

  // What to check after each connection test outcome (model_client.CONNECTION_TEST_RESULTS).
  const TEST_HINTS = {
    succeeded: "可以应用",
    invalid_address: "请检查地址",
    unreachable: "请检查地址和网络",
    unauthorized: "请检查密钥",
    model_not_found: "请检查模型 ID",
    timeout: "端点没有按时回答，可以稍后再试或加大请求超时",
    unparseable: "请确认地址是 OpenAI 兼容接口、模型 ID 正确",
    http_error: "可以稍后再试",
  };

  /** A connection test's answer as {ok, text, detail}: the outcome and what to
   *  check, and the service's detail (the endpoint's reason, no API key). */
  function testResultView({ ok, result, message, seconds }) {
    const text = String(message || result || "");
    const colon = text.indexOf("：");
    let [label, detail] = colon < 0 ? [text, ""] : [text.slice(0, colon), text.slice(colon + 1)];
    if (ok) {
      label = `${label}（${Number(seconds || 0).toFixed(1)} 秒）`;
    }
    const hint = TEST_HINTS[result];
    return { ok: Boolean(ok), text: hint ? `${label}，${hint}` : label, detail };
  }

  /** Which buttons are clickable for the form showing `saved` (null: a new
   *  profile), and, while "应用" is not, why. "测试连接" saves the form first,
   *  so it is clickable for any user profile; "应用" only for a saved one
   *  whose last test succeeded, that is not current, with no unsaved change. */
  function buttons(form, saved) {
    const current = saved && saved.current;
    if (saved && saved.builtin) {
      return { test: false, apply: !current, remove: false, applyHint: current ? CURRENT_HINT : "" };
    }
    let applyHint = "";
    if (!saved || !saved.tested) {
      applyHint = "测试连接成功后才能应用";
    } else if (profileRequest(form, saved) !== null) {
      applyHint = "有改动还没测试：先点“测试连接”";
    } else if (current) {
      applyHint = CURRENT_HINT;
    }
    return { test: true, apply: !applyHint, remove: Boolean(saved), applyHint };
  }
  const CURRENT_HINT = "正在使用这份配置";

  return { formFromProfile, profileRequest, testResultView, buttons };
})();

if (typeof module !== "undefined") {
  module.exports = ZoteroTranslateProfiles;
}
