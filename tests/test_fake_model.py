"""The fake pipeline model fixture itself: recording, responders, faults, in-flight count."""

import json
import threading

import httpx
import pytest

from tests.fake_model import PSEUDO_PREFIX

BATCH_PROMPT = 'Translate.\n\n## Here is the input:\n\n[\n  {"id": 0, "input": "Hello {v1} 42", "layout_label": "text"}\n]'


def post(model, content, timeout=10):
    return httpx.post(
        f"{model.base_url}/chat/completions",
        json={"model": "m", "temperature": 1.0, "messages": [{"role": "user", "content": content}]},
        timeout=timeout,
    )


def test_records_requests_and_answers_batches_with_pseudo_translation(fake_model):
    response = post(fake_model, BATCH_PROMPT)

    content = response.json()["choices"][0]["message"]["content"]
    assert json.loads(content) == [{"id": 0, "output": PSEUDO_PREFIX + "Hello {v1} 42"}]
    [request] = fake_model.requests
    assert request.kind == "translate_batch"
    assert request.body["temperature"] == 1.0


def test_custom_responder_per_kind(fake_model):
    fake_model.respond("other", lambda body: "custom")

    assert post(fake_model, "anything").json()["choices"][0]["message"]["content"] == "custom"


@pytest.mark.parametrize("error, status", [("500", 500), ("429", 429)])
def test_injects_http_errors_on_the_nth_request_of_a_kind(fake_model, error, status):
    fake_model.inject_error(error, kind="translate_batch", nth=2)

    codes = [post(fake_model, BATCH_PROMPT).status_code for _ in range(3)]

    assert codes == [200, status, 200]


@pytest.mark.parametrize("error, content", [("empty", ""), ("invalid_json", "this is not json {")])
def test_injects_bad_content(fake_model, error, content):
    fake_model.inject_error(error)

    assert post(fake_model, BATCH_PROMPT).json()["choices"][0]["message"]["content"] == content


def test_injects_a_timeout(fake_model):
    fake_model.inject_error("timeout")

    with pytest.raises(httpx.ReadTimeout):
        post(fake_model, BATCH_PROMPT, timeout=0.5)


def test_counts_max_requests_in_flight_under_delay(fake_model):
    fake_model.delay = 0.5
    threads = [threading.Thread(target=post, args=(fake_model, "x")) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert fake_model.max_in_flight == 3


def test_fails_the_same_request_on_each_resend_then_answers(fake_model):
    fake_model.fail_same_request(["429", "500"], kind="translate_batch")
    other = BATCH_PROMPT.replace("Hello", "Bye")

    codes = [post(fake_model, p).status_code for p in (BATCH_PROMPT, other, BATCH_PROMPT, BATCH_PROMPT)]

    assert codes == [429, 200, 500, 200]
    assert [r.fault for r in fake_model.requests] == ["429", None, "500", None]


def test_term_list_request_gets_the_default_term_list(fake_model):
    from tests.fake_model import FAKE_TERM_LIST
    from tests.fake_model import TERM_LIST_MARKER

    response = httpx.post(
        f"{fake_model.base_url}/chat/completions",
        json={
            "model": "m",
            "messages": [
                {"role": "system", "content": f"{TERM_LIST_MARKER} as JSON."},
                {"role": "user", "content": "Paper dossier: ... Return the paper dossier ..."},
            ],
        },
        timeout=10,
    )

    content = response.json()["choices"][0]["message"]["content"]
    assert json.loads(content) == FAKE_TERM_LIST
    assert fake_model.requests[0].kind == "term_list"
