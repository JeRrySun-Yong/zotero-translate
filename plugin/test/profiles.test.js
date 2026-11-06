// Tests for the settings page's model profile logic (profiles.js), with the
// network request injected. Run: node --test plugin/test/
const test = require("node:test");
const assert = require("node:assert/strict");
const profiles = require("../addon/content/profiles.js");

const SERVICE = "http://127.0.0.1:8920";
const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });

// A user profile as GET /profiles gives it (service.py, M4 ticket 02).
const SAVED = {
  id: "0123456789ab",
  name: "DeepSeek",
  builtin: false,
  current: false,
  tested: true,
  base_url: "https://api.example.com/v1",
  model: "vendor/model-a",
  concurrency: 32,
  request_timeout_seconds: 900,
  stream: false,
  api_key: { set: true, last4: "wxyz" },
  last_test: { result: "succeeded", message: "连接成功", seconds: 1.5, http_status: 200, tested_at: "2026-11-05T01:00:00Z" },
};
const BUILTIN = {
  id: "builtin-id",
  name: "内置",
  builtin: true,
  current: true,
  tested: true,
  base_url: "https://a.example/v1",
  model: "model-main",
  roles: {
    flash: { base_url: "https://a.example/v1", model: "model-main", concurrency: 40 },
    argon: { base_url: "https://b.example/v1", model: "model-second", concurrency: 16 },
  },
};
const NEW_FORM = {
  name: " DeepSeek ",
  baseUrl: "https://api.example.com/v1",
  apiKey: "sk-test-1234wxyz",
  model: "vendor/model-a",
  concurrency: "32",
  timeout: "900",
  stream: false,
};

/** A fake fetch: answers in turn, recording each request. */
function fakeFetch(...answers) {
  const calls = [];
  const fetch = async (url, options = {}) => {
    calls.push({
      url,
      method: options.method || "GET",
      headers: options.headers || {},
      body: options.body && JSON.parse(options.body),
      signal: "signal" in options ? options.signal : undefined,
    });
    const answer = answers.shift();
    if (!answer) {
      throw new Error(`unexpected request ${url}`);
    }
    return answer();
  };
  return { fetch, calls };
}

test("a new profile's form becomes one POST with every field the service takes", () => {
  assert.deepEqual(profiles.profileRequest(NEW_FORM, null), {
    method: "POST",
    path: ["profiles"],
    body: {
      name: "DeepSeek",
      base_url: "https://api.example.com/v1",
      api_key: "sk-test-1234wxyz",
      model: "vendor/model-a",
      concurrency: 32,
      request_timeout_seconds: 900,
      stream: false,
    },
  });
});

test("an empty concurrency or timeout leaves the service's default", () => {
  const { body } = profiles.profileRequest({ ...NEW_FORM, concurrency: "", timeout: " " }, null);
  assert.deepEqual(Object.keys(body).sort(), ["api_key", "base_url", "model", "name", "stream"]);
});

test("a form that cannot be a profile names the field instead of a request", () => {
  const cases = [
    [{ name: "" }, "请填写名称"],
    [{ baseUrl: "" }, "请填写地址"],
    [{ apiKey: "" }, "请填写密钥"],
    [{ model: " " }, "请填写模型 ID"],
    [{ concurrency: "3.5" }, "并发上限要是 1 到 256 的整数"],
    [{ concurrency: "0" }, "并发上限要是 1 到 256 的整数"],
    [{ concurrency: "300" }, "并发上限要是 1 到 256 的整数"],
    [{ timeout: "abc" }, "请求超时要是 1 到 3600 的秒数，或留空"],
    [{ timeout: "0" }, "请求超时要是 1 到 3600 的秒数，或留空"],
  ];
  for (const [change, error] of cases) {
    assert.deepEqual(profiles.profileRequest({ ...NEW_FORM, ...change }, null), { error }, JSON.stringify(change));
  }
});

test("an edit sends only the fields that changed; an empty key field keeps the key", () => {
  const form = { ...profiles.formFromProfile(SAVED), model: "vendor/model-b" };
  assert.equal(form.apiKey, "");
  assert.deepEqual(profiles.profileRequest(form, SAVED), {
    method: "PUT",
    path: ["profiles", SAVED.id],
    body: { model: "vendor/model-b" },
  });
  const newKey = { ...profiles.formFromProfile(SAVED), apiKey: "sk-new-key-0000", timeout: "" };
  assert.deepEqual(profiles.profileRequest(newKey, SAVED).body, { api_key: "sk-new-key-0000", request_timeout_seconds: null });
});

test("an unchanged saved profile needs no request", () => {
  assert.equal(profiles.profileRequest(profiles.formFromProfile(SAVED), SAVED), null);
  assert.equal(profiles.profileRequest({ ...profiles.formFromProfile(SAVED), name: "DeepSeek " }, SAVED), null);
});

test("the form's 流式请求 setting is the profile's stream field (ticket 12)", () => {
  const form = profiles.formFromProfile(SAVED);
  assert.equal(form.stream, false);
  assert.equal(profiles.formFromProfile({ ...SAVED, stream: true }).stream, true);
  assert.equal(profiles.formFromProfile(null).stream, false);
  // A new profile states it even when off (the service's default is off).
  assert.equal(profiles.profileRequest({ ...NEW_FORM, stream: true }, null).body.stream, true);
  // Editing only this setting sends only it; an unchanged one sends nothing.
  assert.deepEqual(profiles.profileRequest({ ...form, stream: true }, SAVED), {
    method: "PUT",
    path: ["profiles", SAVED.id],
    body: { stream: true },
  });
  const on = { ...SAVED, stream: true, tested: false };
  assert.deepEqual(profiles.profileRequest({ ...profiles.formFromProfile(on), stream: false }, on).body, { stream: false });
  assert.equal(profiles.profileRequest(profiles.formFromProfile(on), on), null);
});

// POST /profiles/{id}/test answers (service.py): the service's message, then what to check.
const answer = (result, message, seconds = 2.3) => ({ ok: result === "succeeded", result, message, seconds, http_status: null });

test("each connection test outcome reads as what happened and what to check", () => {
  const cases = [
    [answer("succeeded", "连接成功", 1.25), "连接成功（1.3 秒），可以应用", ""],
    [answer("unauthorized", "密钥无效：HTTP 401 invalid api key"), "密钥无效，请检查密钥", "HTTP 401 invalid api key"],
    [answer("model_not_found", "模型不存在：HTTP 404 no such model"), "模型不存在，请检查模型 ID", "HTTP 404 no such model"],
    [answer("unreachable", "地址连不上：Connection refused（ConnectError）"), "地址连不上，请检查地址和网络", "Connection refused（ConnectError）"],
    [answer("invalid_address", "地址无效：没有主机名"), "地址无效，请检查地址", "没有主机名"],
    [answer("timeout", "请求超时：60 秒内没有回答"), "请求超时，端点没有按时回答，可以稍后再试或加大请求超时", "60 秒内没有回答"],
    [answer("unparseable", "回答不能解析：回答不是 {\"ok\": true}：hello"), "回答不能解析，请确认地址是 OpenAI 兼容接口、模型 ID 正确", "回答不是 {\"ok\": true}：hello"],
    [answer("http_error", "端点出错：HTTP 503 busy"), "端点出错，可以稍后再试", "HTTP 503 busy"],
  ];
  for (const [given, text, detail] of cases) {
    assert.deepEqual(profiles.testResultView(given), { ok: given.ok, text, detail }, given.result);
  }
});

test("a test faster than a tenth of a second does not read as 0.0 seconds", () => {
  assert.equal(profiles.testResultView(answer("succeeded", "连接成功", 0.003)).text, "连接成功（不到 0.1 秒），可以应用");
});

test("while the service cannot be reached nothing can be tested, applied or deleted", () => {
  assert.deepEqual(profiles.buttons(profiles.formFromProfile(SAVED), SAVED, { connected: false }), {
    test: false,
    apply: false,
    remove: false,
    applyHint: "连上翻译服务后才能测试和应用",
  });
});

test("a result the plugin does not know still shows the service's message", () => {
  assert.deepEqual(profiles.testResultView(answer("new_kind", "新的情况：详情")), { ok: false, text: "新的情况", detail: "详情" });
});

test("apply is only clickable for a saved, tested profile whose form has no unsaved change", () => {
  const form = profiles.formFromProfile(SAVED);
  assert.deepEqual(profiles.buttons(form, SAVED), { test: true, apply: true, remove: true, applyHint: "" });
  const untested = { ...SAVED, tested: false, last_test: null };
  assert.deepEqual(profiles.buttons(form, untested), { test: true, apply: false, remove: true, applyHint: "测试连接成功后才能应用" });
  const failed = { ...SAVED, tested: false, last_test: { ...SAVED.last_test, result: "unauthorized" } };
  assert.equal(profiles.buttons(form, failed).apply, false);
  const edited = { ...form, model: "vendor/model-b" };
  assert.deepEqual(profiles.buttons(edited, SAVED), {
    test: true,
    apply: false,
    remove: true,
    applyHint: "有改动还没测试：先点“测试连接”",
  });
  assert.deepEqual(profiles.buttons(profiles.formFromProfile(null), null), {
    test: true,
    apply: false,
    remove: false,
    applyHint: "测试连接成功后才能应用",
  });
  assert.deepEqual(profiles.buttons(form, { ...SAVED, current: true }), { test: true, apply: false, remove: true, applyHint: "正在使用这份配置" });
});

test("the built-in profile can only be applied", () => {
  const form = profiles.formFromProfile(BUILTIN);
  assert.deepEqual(profiles.buttons(form, { ...BUILTIN, current: false }), { test: false, apply: true, remove: false, applyHint: "" });
  assert.deepEqual(profiles.buttons(form, BUILTIN), { test: false, apply: false, remove: false, applyHint: "正在使用这份配置" });
});

const LISTING = { current: BUILTIN.id, profiles: [BUILTIN, SAVED] };

test("the list marks the current profile and says which ones are tested; a key shows only as set", () => {
  const untested = { ...SAVED, id: "ba9876543210", name: "Other", tested: false, last_test: null, api_key: { set: true, last4: "" } };
  const failed = { ...SAVED, id: "cccccccccccc", name: "Failed", tested: false, last_test: { ...SAVED.last_test, result: "timeout" } };
  const rows = profiles.profileRows({ current: BUILTIN.id, profiles: [BUILTIN, SAVED, untested, failed] });
  assert.deepEqual(rows, [
    { id: BUILTIN.id, name: "内置", model: "model-main", current: true, state: "内置，只读", key: "" },
    { id: SAVED.id, name: "DeepSeek", model: "vendor/model-a", current: false, state: "测试通过", key: "已设置 ••••wxyz" },
    { id: untested.id, name: "Other", model: "vendor/model-a", current: false, state: "未测试", key: "已设置" },
    { id: failed.id, name: "Failed", model: "vendor/model-a", current: false, state: "测试未通过", key: "已设置 ••••wxyz" },
  ]);
  assert.equal(profiles.keyText(SAVED), "已设置 ••••wxyz");
});

test("the built-in profile shows its two roles", () => {
  assert.equal(
    profiles.builtinDetail(BUILTIN),
    "主模型 model-main（https://a.example/v1，并发 40）；副模型 model-second（https://b.example/v1，并发 16）。由翻译服务的启动参数决定，这里不能修改。",
  );
});

test("the service status says it runs and which profile is current", () => {
  assert.deepEqual(profiles.statusView({ listing: { ...LISTING, current: SAVED.id, profiles: [{ ...BUILTIN, current: false }, { ...SAVED, current: true }] } }), {
    running: true,
    text: "翻译服务在运行，当前配置：DeepSeek（vendor/model-a）",
    help: "",
  });
});

test("an unreachable service says how to start it and where its address is set", () => {
  const view = profiles.statusView({ unreachable: true, serviceUrl: SERVICE });
  assert.equal(view.running, false);
  assert.equal(view.text, `连不上翻译服务（${SERVICE}）`);
  assert.match(view.help, /launchctl kickstart -k gui\/\$\(id -u\)\/local\.zotero-translate\.service/);
  assert.match(view.help, /~\/Library\/Logs\/zotero-translate\/service\.log/);
  assert.match(view.help, /“高级”/);
  const old = profiles.statusView({ noProfiles: true, serviceUrl: SERVICE });
  assert.deepEqual([old.running, old.text], [false, "翻译服务在运行，但版本太旧，没有模型配置：请更新翻译服务"]);
});

test("every write to the service carries the plugin's header and a JSON body", async () => {
  const { fetch, calls } = fakeFetch(
    () => json(201, { ...SAVED, tested: false, last_test: null }),
    () => json(200, answer("succeeded", "连接成功", 0.8)),
    () => json(200, { current: SAVED.id, name: "DeepSeek" }),
    () => json(200, { deleted: SAVED.id, current: BUILTIN.id }),
  );
  const client = profiles.createClient({ fetch, serviceUrl: () => `${SERVICE}/` });
  const tested = await client.saveAndTest(NEW_FORM, null);
  assert.equal(tested.profile.id, SAVED.id);
  assert.deepEqual(tested.view, { ok: true, text: "连接成功（0.8 秒），可以应用", detail: "" });
  assert.deepEqual(await client.apply(SAVED.id), { ok: true });
  assert.deepEqual(await client.remove(SAVED.id), { ok: true });
  assert.deepEqual(
    calls.map(({ url, method, body }) => [method, url, body]),
    [
      ["POST", `${SERVICE}/profiles`, profiles.profileRequest(NEW_FORM, null).body],
      ["POST", `${SERVICE}/profiles/${SAVED.id}/test`, {}],
      ["POST", `${SERVICE}/profiles/${SAVED.id}/apply`, {}],
      ["DELETE", `${SERVICE}/profiles/${SAVED.id}`, {}],
    ],
  );
  for (const call of calls) {
    assert.equal(call.headers["X-Zotero-Translate"], "1");
    assert.equal(call.headers["Content-Type"], "application/json");
  }
});

test("testing an unchanged saved profile sends only the test; an edited one is saved first", async () => {
  const unchanged = fakeFetch(() => json(200, answer("unauthorized", "密钥无效：HTTP 401")));
  const result = await profiles.createClient({ fetch: unchanged.fetch, serviceUrl: () => SERVICE }).saveAndTest(profiles.formFromProfile(SAVED), SAVED);
  assert.deepEqual(unchanged.calls.map((c) => c.url), [`${SERVICE}/profiles/${SAVED.id}/test`]);
  assert.deepEqual(result.view, { ok: false, text: "密钥无效，请检查密钥", detail: "HTTP 401" });
  assert.equal(result.profile, SAVED);

  const edited = fakeFetch(() => json(200, { ...SAVED, model: "vendor/model-b", tested: false }), () => json(200, answer("succeeded", "连接成功", 1)));
  const form = { ...profiles.formFromProfile(SAVED), model: "vendor/model-b" };
  const after = await profiles.createClient({ fetch: edited.fetch, serviceUrl: () => SERVICE }).saveAndTest(form, SAVED);
  assert.deepEqual(edited.calls.map((c) => [c.method, c.url, c.body]), [
    ["PUT", `${SERVICE}/profiles/${SAVED.id}`, { model: "vendor/model-b" }],
    ["POST", `${SERVICE}/profiles/${SAVED.id}/test`, {}],
  ]);
  assert.equal(after.profile.model, "vendor/model-b");
});

test("once the form is saved the page hears of it before the test starts, and the test waits for it", async () => {
  // Saving a current profile's model makes Gemini current again in the
  // service; the page must show that during the test (up to 60 s), not after it.
  const events = [];
  let release;
  const refreshed = new Promise((resolve) => {
    release = resolve;
  });
  const edited = fakeFetch(
    () => json(200, { ...SAVED, current: false, tested: false, model: "vendor/model-b" }),
    () => {
      events.push("test request");
      return json(200, answer("succeeded", "连接成功", 1));
    },
  );
  const client = profiles.createClient({ fetch: edited.fetch, serviceUrl: () => SERVICE });
  const form = { ...profiles.formFromProfile(SAVED), model: "vendor/model-b" };
  const done = client.saveAndTest(form, { ...SAVED, current: true }, {
    onSaved: (profile) => {
      events.push(`saved ${profile.model} current=${profile.current}`);
      return refreshed; // the page asks the service for its profiles here
    },
  });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(events, ["saved vendor/model-b current=false"]); // no test until the page has refreshed
  release();
  const result = await done;
  assert.deepEqual(events, ["saved vendor/model-b current=false", "test request"]);
  assert.equal(result.view.ok, true);

  const unchanged = fakeFetch(() => json(200, answer("succeeded", "连接成功", 1)));
  const calls = [];
  await profiles
    .createClient({ fetch: unchanged.fetch, serviceUrl: () => SERVICE })
    .saveAndTest(profiles.formFromProfile(SAVED), SAVED, { onSaved: () => calls.push("saved") });
  assert.deepEqual(calls, []); // nothing saved, nothing to refresh
});

test("a form the service refuses, or an unreachable service, tests nothing and says why", async () => {
  const invalid = await profiles.createClient({ fetch: fakeFetch().fetch, serviceUrl: () => SERVICE }).saveAndTest({ ...NEW_FORM, apiKey: "" }, null);
  assert.deepEqual(invalid, { error: "请填写密钥" });

  const refusing = fakeFetch(() => json(400, { error: "base_url 无效：没有主机名" }));
  assert.deepEqual(await profiles.createClient({ fetch: refusing.fetch, serviceUrl: () => SERVICE }).saveAndTest(NEW_FORM, null), {
    error: "没有保存：base_url 无效：没有主机名",
  });
  assert.equal(refusing.calls.length, 1);

  const down = profiles.createClient({
    fetch: async () => {
      throw new TypeError("NetworkError when attempting to fetch resource.");
    },
    serviceUrl: () => SERVICE,
  });
  assert.deepEqual(await down.saveAndTest(NEW_FORM, null), { error: `连不上翻译服务（${SERVICE}）` });
  assert.deepEqual(await down.apply(SAVED.id), { error: `连不上翻译服务（${SERVICE}）` });
  assert.deepEqual(await down.status(), { unreachable: true, serviceUrl: SERVICE });
});

test("the status asks the service for its profiles", async () => {
  const { fetch, calls } = fakeFetch(() => json(200, LISTING), () => json(404, { error: "not found" }));
  const client = profiles.createClient({ fetch, serviceUrl: () => SERVICE });
  assert.deepEqual(await client.status(), { listing: LISTING });
  assert.deepEqual(await client.status(), { noProfiles: true, serviceUrl: SERVICE });
  assert.deepEqual(calls.map((c) => [c.method, c.url]), [["GET", `${SERVICE}/profiles`], ["GET", `${SERVICE}/profiles`]]);
});

test("without AbortSignal.timeout (the plugin scope has no window) the caller's timer is used", async () => {
  // bootstrap.js loads profiles.js into the plugin scope, where
  // AbortSignal.timeout throws "Could not find window"; the item menu passes
  // its own signal, and a request without one still goes out.
  const { fetch, calls } = fakeFetch(() => json(200, LISTING));
  const limits = [];
  const client = profiles.createClient({
    fetch,
    serviceUrl: () => SERVICE,
    timeoutSignal: (ms) => {
      limits.push(ms);
      return undefined;
    },
  });
  assert.deepEqual(await client.status(), { listing: LISTING });
  assert.deepEqual(limits, [10000]);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].signal, undefined);

  const signal = new AbortController().signal;
  const withSignal = fakeFetch(() => json(200, answer("succeeded", "连接成功", 1)));
  await profiles
    .createClient({ fetch: withSignal.fetch, serviceUrl: () => SERVICE, timeoutSignal: () => signal })
    .saveAndTest(profiles.formFromProfile(SAVED), SAVED);
  assert.deepEqual(withSignal.calls.map((c) => c.signal), [signal]);
});

test("apply refused by the service says why", async () => {
  const { fetch } = fakeFetch(() => json(409, { error: "这份配置还没有通过测试连接" }));
  assert.deepEqual(await profiles.createClient({ fetch, serviceUrl: () => SERVICE }).apply(SAVED.id), {
    error: "没有应用：这份配置还没有通过测试连接",
  });
});

// M4 ticket 16 item 2: the key goes only to a translation service on this
// computer. The service accepts a Host of 127.0.0.1:<port> or localhost:<port>
// only (service.py, _local_host); a real service is not affected.
test("an address that is not this computer gets no profile request at all", async () => {
  for (const address of [
    "http://192.168.1.5:8920",
    "https://example.com",
    "http://127.0.0.1.example.com:8920",
    "http://localhost.example.com:8920",
    "http://127.0.0.1@example.com:8920",
    "http://example.com/127.0.0.1",
    "http://[::1]:8920",
    "http://0.0.0.0:8920",
    "http://127.0.0.2:8920",
    "not a url",
    "",
  ]) {
    const { fetch, calls } = fakeFetch(() => json(200, LISTING));
    const client = profiles.createClient({ fetch, serviceUrl: () => `  ${address}  ` });
    const error = `翻译服务地址不是本机（${address}），不发送配置和密钥`;
    assert.deepEqual(await client.saveAndTest(NEW_FORM, null), { error }, `new: ${address}`);
    assert.deepEqual(await client.saveAndTest({ ...profiles.formFromProfile(SAVED), model: "vendor/model-b" }, SAVED), { error }, `edit: ${address}`);
    assert.deepEqual(await client.saveAndTest(profiles.formFromProfile(SAVED), SAVED), { error }, `test: ${address}`);
    assert.deepEqual(await client.apply(SAVED.id), { error }, `apply: ${address}`);
    assert.deepEqual(await client.remove(SAVED.id), { error }, `remove: ${address}`);
    assert.deepEqual(await client.status(), { error, serviceUrl: address }, `list: ${address}`);
    assert.deepEqual(calls, [], address);
  }
});

test("this computer's addresses (127.0.0.1, localhost, any port, either case) are used as before", async () => {
  for (const address of [SERVICE, "http://localhost:8920/", "http://LocalHost:9000", "http://127.0.0.1:8941"]) {
    const { fetch, calls } = fakeFetch(
      () => json(201, SAVED),
      () => json(200, answer("succeeded", "连接成功", 1)),
      () => json(200, LISTING),
      () => json(200, {}),
      () => json(200, {}),
    );
    const client = profiles.createClient({ fetch, serviceUrl: () => address });
    assert.equal((await client.saveAndTest(NEW_FORM, null)).profile.id, SAVED.id, address);
    assert.deepEqual(await client.status(), { listing: LISTING }, address);
    assert.deepEqual(await client.apply(SAVED.id), { ok: true }, address);
    assert.deepEqual(await client.remove(SAVED.id), { ok: true }, address);
    assert.equal(calls.length, 5, address);
    const base = address.trim().replace(/\/+$/, "");
    assert.deepEqual(calls.map((c) => c.url.startsWith(base)), [true, true, true, true, true], address);
  }
});

test("the status line says the address is not this computer, with no restart help", () => {
  const error = "翻译服务地址不是本机（https://example.com），不发送配置和密钥";
  assert.deepEqual(profiles.statusView({ error, serviceUrl: "https://example.com" }), { running: false, text: error, help: "" });
});

// ---- round 2 of ticket M4-16: the check is at the point of sending ----

const REMOTE = "https://example.com";
const NOT_LOCAL = (address) => `翻译服务地址不是本机（${address}），不发送配置和密钥`;

test("an address changed to another host while the save is pending: nothing more is sent, the error says so", async () => {
  for (const changeIn of ["the save request", "onSaved"]) {
    let address = SERVICE;
    const change = () => {
      address = REMOTE;
    };
    const { fetch, calls } = fakeFetch(
      () => {
        if (changeIn === "the save request") {
          change();
        }
        return json(201, SAVED);
      },
      () => json(200, answer("succeeded", "连接成功", 1)), // the test request: must stay unused
    );
    const client = profiles.createClient({ fetch, serviceUrl: () => address });
    const outcome = await client.saveAndTest(NEW_FORM, null, {
      onSaved: () => {
        if (changeIn === "onSaved") {
          change();
        }
      },
    });
    assert.deepEqual(calls.map((c) => c.url), [`${SERVICE}/profiles`], changeIn);
    // The profile was saved, and the page needs to know: it refreshes and selects it.
    assert.deepEqual(outcome, { profile: SAVED, error: NOT_LOCAL(REMOTE) }, changeIn);
  }
});

test("one save-and-test goes to one service: an address changed to another local port in between is not followed", async () => {
  let address = SERVICE;
  const { fetch, calls } = fakeFetch(
    () => {
      address = "http://localhost:9000";
      return json(201, SAVED);
    },
    () => json(200, answer("succeeded", "连接成功", 1)),
  );
  const outcome = await profiles.createClient({ fetch, serviceUrl: () => address }).saveAndTest(NEW_FORM, null);
  assert.deepEqual(calls.map((c) => c.url), [`${SERVICE}/profiles`, `${SERVICE}/profiles/${SAVED.id}/test`]);
  assert.equal(outcome.profile.id, SAVED.id);
  assert.equal(outcome.error, undefined);
});

test("a request that starts after the address became remote is not sent either", async () => {
  let address = SERVICE;
  const { fetch, calls } = fakeFetch(() => json(200, LISTING), () => json(200, {}), () => json(200, {}));
  const client = profiles.createClient({ fetch, serviceUrl: () => address });
  assert.deepEqual(await client.status(), { listing: LISTING });
  address = REMOTE;
  assert.deepEqual(await client.status(), { error: NOT_LOCAL(REMOTE), serviceUrl: REMOTE });
  assert.deepEqual(await client.apply(SAVED.id), { error: NOT_LOCAL(REMOTE) });
  assert.deepEqual(await client.remove(SAVED.id), { error: NOT_LOCAL(REMOTE) });
  assert.equal(calls.length, 1);
});

test("addressProblem reads the host that a request would reach", () => {
  const blocked = [
    "http://127.0.0.1.example.com",
    "http://127.0.0.1.example.com:8920/tasks",
    "http://localhost@evil.com",
    "http://localhost:8920@evil.com",
    "http://127.0.0.1@evil.com:8920",
    "http://user:password@evil.com",
    "http://user:password@example.com:8920/x",
    "http://[::1]",
    "http://[::1]:8920",
    "http://localhost.",
    "http://localhost.:8920",
    "http://0.0.0.0:8920",
    "http://LOCALHOST.EXAMPLE.COM:8920",
    "HTTP://EXAMPLE.COM",
    "ftp://127.0.0.1",
    "ftp://localhost:8920",
    "file:///tmp/service",
    "ws://127.0.0.1:8920",
    "wss://localhost:8920",
    "javascript:alert(1)",
    "//127.0.0.1:8920",
    "127.0.0.1:8920",
    "localhost:8920",
    "http://",
    "   ",
  ];
  for (const address of blocked) {
    assert.notEqual(profiles.addressProblem(address), "", address);
  }
  const local = [
    "http://127.0.0.1:8920",
    "http://127.0.0.1",
    "https://127.0.0.1:8920/",
    "http://localhost:8920",
    "HTTP://LOCALHOST:8920",
    "http://LocalHost:9000/path",
    "  http://127.0.0.1:8941  ",
    "http://user:password@127.0.0.1:8920", // the host is this computer
    "http://user:password@localhost:8920",
    // The URL parser turns these into 127.0.0.1, the host a request reaches.
    "http://127.0.0.1.",
    "http://127.1:8920/",
    "http://2130706433:8920",
  ];
  for (const address of local) {
    assert.equal(profiles.addressProblem(address), "", address);
  }
  assert.equal(profiles.addressProblem("https://example.com "), NOT_LOCAL("https://example.com"));
});
