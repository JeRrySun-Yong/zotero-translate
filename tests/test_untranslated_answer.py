"""Spec behaviour 12: an answer that repeats the English is accepted, not redone.

Keeping key concepts in English makes many paragraphs look untranslated; the
engine's "same as input" fallback must not send them again.
"""

import collections

from tests.fake_model import PSEUDO_PREFIX
from tests.fake_model import echo_batch
from tests.test_paper_task import SOURCE_PHRASE
from tests.test_paper_task import pdf_text


def test_english_answer_is_kept_without_a_second_request(fake_model, start_service):
    fake_model.respond("translate_batch", echo_batch)
    service = start_service(fake_model)

    task_id = service.submit()
    final = service.wait(task_id)

    assert final["state"] == "succeeded", final["error"]
    assert fake_model.requests_of("translate_batch")
    assert fake_model.requests_of("single_paragraph") == []
    prompts = collections.Counter(r.prompt for r in fake_model.requests)
    assert max(prompts.values()) == 1, "a batch was sent twice"
    mono = pdf_text(service.download(task_id, "mono"))
    assert SOURCE_PHRASE.replace(" ", "") in mono
    assert PSEUDO_PREFIX not in mono
