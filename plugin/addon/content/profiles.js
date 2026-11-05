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
      const time = Number(seconds || 0);
      label = `${label}（${time < 0.1 ? "不到 0.1" : time.toFixed(1)} 秒）`;
    }
    const hint = TEST_HINTS[result];
    return { ok: Boolean(ok), text: hint ? `${label}，${hint}` : label, detail };
  }

  /** Which buttons are clickable for the form showing `saved` (null: a new
   *  profile), and, while "应用" is not, why. "测试连接" saves the form first,
   *  so it is clickable for any user profile; "应用" only for a saved one
   *  whose last test succeeded, that is not current, with no unsaved change;
   *  none while the service cannot be reached (`connected` false). */
  function buttons(form, saved, { connected = true } = {}) {
    if (!connected) {
      return { test: false, apply: false, remove: false, applyHint: "连上翻译服务后才能测试和应用" };
    }
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

  /** "已设置 ••••wxyz" (or "已设置" for a key too short to show any of it). */
  function keyText(profile) {
    const key = profile.api_key;
    if (!key || !key.set) {
      return profile.builtin ? "" : "未设置";
    }
    return key.last4 ? `已设置 ••••${key.last4}` : "已设置";
  }

  /** The list's rows, in the service's order (the built-in profile first). */
  function profileRows(listing) {
    return listing.profiles.map((profile) => ({
      id: profile.id,
      name: profile.name,
      model: profile.model,
      current: profile.id === listing.current,
      state: profile.builtin
        ? "内置，只读"
        : profile.tested
          ? "测试通过"
          : profile.last_test
            ? "测试未通过"
            : "未测试",
      key: keyText(profile),
    }));
  }

  /** The built-in profile's two roles, as the form shows them instead of fields. */
  function builtinDetail(profile) {
    const role = (label, { model, base_url, concurrency }) => `${label} ${model}（${base_url}，并发 ${concurrency}）`;
    const { flash, argon } = profile.roles;
    return `${role("主模型", flash)}；${role("副模型", argon)}。由翻译服务的启动参数决定，这里不能修改。`;
  }

  // The service runs under a LaunchAgent (docs/deployment.md, section 4);
  // the plugin neither starts nor restarts it (spec, out of scope).
  const UNREACHABLE_HELP =
    "翻译服务由 LaunchAgent 在登录时启动。没在运行时，可以在终端运行 " +
    "launchctl kickstart -k gui/$(id -u)/local.zotero-translate.service 重启它，" +
    "启动出错的原因在 ~/Library/Logs/zotero-translate/service.log。" +
    "服务地址不对时，在下面的“高级”里改。";

  /** The status line: {running, text, help}, from what status() found. */
  function statusView(found) {
    if (found.listing) {
      const current = found.listing.profiles.find((profile) => profile.id === found.listing.current);
      const name = current ? `${current.name}（${current.model}）` : found.listing.current;
      return { running: true, text: `翻译服务在运行，当前配置：${name}`, help: "" };
    }
    if (found.noProfiles) {
      return { running: false, text: "翻译服务在运行，但版本太旧，没有模型配置：请更新翻译服务", help: "" };
    }
    return { running: false, text: `连不上翻译服务（${found.serviceUrl}）`, help: UNREACHABLE_HELP };
  }

  // Time limits of one request: a connection test takes up to 60 seconds in the service.
  const REQUEST_LIMIT_MS = 10000;
  const TEST_LIMIT_MS = 90000;

  /** The service's profile interface, through `fetch` at `serviceUrl()`.
   *  Every method resolves (never rejects); a failure is {error: text}. */
  function createClient({ fetch, serviceUrl }) {
    const Core = typeof ZoteroTranslateCore !== "undefined" ? ZoteroTranslateCore : require("./core.js");
    const unreachable = () => `连不上翻译服务（${serviceUrl().trim()}）`;

    /** {status, body} of one request, or null when the service cannot be reached. */
    async function send(method, path, body, limitMs = REQUEST_LIMIT_MS) {
      const options = { method, signal: AbortSignal.timeout(limitMs) };
      if (method !== "GET") {
        options.headers = { "X-Zotero-Translate": "1", "Content-Type": "application/json" };
        options.body = JSON.stringify(body ?? {});
      }
      try {
        const response = await fetch(Core.serviceUrl(serviceUrl(), ...path), options);
        let answer = null;
        try {
          answer = await response.json();
        } catch {
          // not JSON: only the status counts
        }
        return { status: response.status, body: answer };
      } catch {
        return null;
      }
    }

    const refusal = (verb, answer) => `${verb}：${(answer.body && answer.body.error) || `HTTP ${answer.status}`}`;

    return {
      /** {listing} (GET /profiles), {noProfiles, serviceUrl} for a service
       *  without profiles, or {unreachable, serviceUrl}. */
      async status() {
        const answer = await send("GET", ["profiles"]);
        if (answer && answer.status === 200 && answer.body && Array.isArray(answer.body.profiles)) {
          return { listing: answer.body };
        }
        const where = serviceUrl().trim();
        return answer && answer.status === 404 ? { noProfiles: true, serviceUrl: where } : { unreachable: true, serviceUrl: where };
      },

      /** Save the form when it differs from `saved` (or is new), then test the
       *  saved profile: {profile, view} with the profile as saved, or {error}.
       *  After a save, `onSaved(profile)` is awaited before the test starts:
       *  a save can already have changed the service's current profile (a new
       *  model makes the built-in one current again), and the test takes up
       *  to a minute. */
      async saveAndTest(form, saved, { onSaved = () => {} } = {}) {
        const request = profileRequest(form, saved);
        if (request && request.error) {
          return { error: request.error };
        }
        let profile = saved;
        if (request) {
          const answer = await send(request.method, request.path, request.body);
          if (!answer) {
            return { error: unreachable() };
          }
          if (answer.status !== 200 && answer.status !== 201) {
            return { error: refusal("没有保存", answer) };
          }
          profile = answer.body;
          await onSaved(profile);
        }
        const answer = await send("POST", ["profiles", profile.id, "test"], {}, TEST_LIMIT_MS);
        if (!answer) {
          return { profile, error: unreachable() };
        }
        if (answer.status !== 200) {
          return { profile, error: refusal("没有测试", answer) };
        }
        return { profile, view: testResultView(answer.body) };
      },

      /** Make the profile current: {ok} or {error}. */
      async apply(id) {
        const answer = await send("POST", ["profiles", id, "apply"], {});
        if (!answer) {
          return { error: unreachable() };
        }
        return answer.status === 200 ? { ok: true } : { error: refusal("没有应用", answer) };
      },

      /** Delete the profile: {ok} or {error}. */
      async remove(id) {
        const answer = await send("DELETE", ["profiles", id], {});
        if (!answer) {
          return { error: unreachable() };
        }
        return answer.status === 200 ? { ok: true } : { error: refusal("没有删除", answer) };
      },
    };
  }

  return { formFromProfile, profileRequest, testResultView, buttons, keyText, profileRows, builtinDetail, statusView, createClient };
})();

if (typeof module !== "undefined") {
  module.exports = ZoteroTranslateProfiles;
}
