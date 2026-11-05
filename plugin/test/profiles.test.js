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
};

/** A fake fetch: answers in turn, recording each request. */
function fakeFetch(...answers) {
  const calls = [];
  const fetch = async (url, options = {}) => {
    calls.push({ url, method: options.method || "GET", headers: options.headers || {}, body: options.body && JSON.parse(options.body) });
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
    },
  });
});

test("an empty concurrency or timeout leaves the service's default", () => {
  const { body } = profiles.profileRequest({ ...NEW_FORM, concurrency: "", timeout: " " }, null);
  assert.deepEqual(Object.keys(body).sort(), ["api_key", "base_url", "model", "name"]);
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
