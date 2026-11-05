"""Streamed requests for user profiles (M4 ticket 12).

Command Code's DeepSeek Fast answers a plain request that thinks longer
than about 125 seconds with HTTP 524; the same request with ``"stream":
true`` succeeds. A model setting turns streaming on (``ZT_STREAM=1`` in an
endpoint file, ``stream`` in a profile): the request body then has
``"stream": true`` and the client joins the ``delta.content`` of the event
stream into the answer, which then goes through the caller's check as
before. With streaming off, the body is exactly what it was before (the
built-in Gemini's cache keys do not change).

These tests drive the pipeline model client against the fake model; the
profile setting through the service is in ``test_model_profiles.py``.
"""

import json
import time
from pathlib import Path

from tests.conftest import FAKE_API_KEY
from tests.fake_model import STREAM_THINKING
from tests.fake_model import FakeModel
from zotero_translate.model_client import ModelClient
from zotero_translate.settings import ModelSettings

MESSAGES = [{"role": "user", "content": "hello"}]
ANSWER = '{"terms": ["meta-harness", "代码智能体"], "note": "一段足够长、要分成好几块发送的回答。"}'


def _settings(fake: FakeModel, tmp_path: Path, **changes) -> ModelSettings:
    values = dict(
        base_url=fake.base_url,
        api_key=FAKE_API_KEY,
        model="fake-deepseek",
        concurrency=4,
        timeout_seconds=5.0,
        retry_backoff_seconds=0.01,
        connection_retry_seconds=(0.01, 0.01, 0.01),
        cache_dir=tmp_path / "cache",
    )
    values.update(changes)
    return ModelSettings(**values)


def test_a_streamed_answer_is_the_content_deltas_joined_without_the_thinking(tmp_path):
    with FakeModel() as fake:
        fake.respond("other", lambda body: ANSWER)
        client = ModelClient(_settings(fake, tmp_path, stream=True))
        try:
            content = client.complete(MESSAGES, validate=json.loads)
        finally:
            client.close()

    assert content == ANSWER
    assert all(part not in content for part in STREAM_THINKING)
    [request] = fake.requests
    assert request.body["stream"] is True
    assert request.fault is None and not request.disconnected
