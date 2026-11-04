"""Review and mechanical checks.

Spec "计划覆盖的行为" 8: every translation batch is followed by one review
request. 11: a review answer that changes a number gives way to the draft
that passes the checks; when draft and review both change a number, the
review is resent within the batch's retries and then the paragraph keeps
its source text, counted in ``kept_source_paragraphs``. Ticket 12: a
literal the engine used to glue to the word before it (``Fig. 6``,
``Appendix H.1``, ``Introducing GPT-5.5``) passes when the translation writes
it normally; since closure ticket 02 the engine keeps that space.
"""

import json
import re

import pytest

from tests.conftest import FIXTURES
from tests.fake_model import FAKE_DOSSIER
from tests.fake_model import PSEUDO_PREFIX
from tests.fake_model import batch_items
from tests.fake_model import pseudo_translate_batch
from tests.fake_model import review_items
from tests.test_paper_task import SOURCE_PHRASE
from tests.test_paper_task import pdf_text

REVIEW_PREFIX = "审校译文"
# A number in the AutoDesign abstract on page 1 of the excerpt.
NUMBER = "78.32"
WRONG_NUMBER = "87.32"
ABSTRACT_PHRASE = "achievesthehighestscoreof78.32"
STYLE_TAG = re.compile(r"</?style[^>]*>")


def reviewed(change_number: bool):
    """A review answer: every draft with the review prefix; optionally the number changed."""

    def respond(body: dict) -> str:
        items = review_items(body["messages"][-1]["content"]) or []
        outputs = []
        for item in items:
            text = item["draft"].replace(PSEUDO_PREFIX, REVIEW_PREFIX)
            if change_number:
                text = text.replace(NUMBER, WRONG_NUMBER)
            outputs.append({"id": item["id"], "output": text})
        return json.dumps(outputs, ensure_ascii=False)

    return respond


def draft_with_wrong_number(body: dict) -> str:
    return pseudo_translate_batch(body).replace(NUMBER, WRONG_NUMBER)


def test_every_batch_is_followed_by_one_review_request(fake_model, start_service):
    fake_model.respond("review", reviewed(change_number=False))
    service = start_service(fake_model)

    task_id = service.submit()
    final = service.wait(task_id)

    assert final["state"] == "succeeded", final["error"]
    batches = fake_model.requests_of("translate_batch")
    reviews = fake_model.requests_of("review")
    assert batches and len(reviews) == len(batches)
    # Each review carries the sources of one batch and the drafts the model
    # returned for it, plus the condensed dossier.
    batch_sources = sorted(
        tuple(item["input"] for item in batch_items(r.prompt)) for r in batches
    )
    review_sources = sorted(
        tuple(item["source"] for item in review_items(r.prompt)) for r in reviews
    )
    assert review_sources == batch_sources
    for request in reviews:
        assert FAKE_DOSSIER["paper_summary_zh"] in request.prompt
        for item in review_items(request.prompt):
            assert item["draft"] == PSEUDO_PREFIX + item["source"]
    # The reviewed text is what the mono PDF shows. (Very short paragraphs
    # are redone one by one after the batch, without review.)
    mono = pdf_text(service.download(task_id, "mono"))
    assert REVIEW_PREFIX + SOURCE_PHRASE.replace(" ", "") in mono
    report = service.report(task_id)
    assert report["review"]["changed_paragraphs"] == sum(
        len(review_items(r.prompt)) for r in reviews
    )
    assert final["kept_source_paragraphs"] == 0


def test_review_that_changes_a_number_gives_way_to_the_draft(fake_model, start_service):
    fake_model.respond("review", reviewed(change_number=True))
    service = start_service(fake_model)

    task_id = service.submit()
    final = service.wait(task_id)

    assert final["state"] == "succeeded", final["error"]
    assert final["kept_source_paragraphs"] == 0
    mono = pdf_text(service.download(task_id, "mono"))
    # The abstract shows the draft with the right number; other paragraphs
    # show the review.
    assert PSEUDO_PREFIX + SOURCE_PHRASE.replace(" ", "") in mono
    assert ABSTRACT_PHRASE in mono
    assert WRONG_NUMBER not in mono
    assert REVIEW_PREFIX in mono
    # One answer per batch was enough: nothing was resent.
    assert len(fake_model.requests_of("review")) == len(fake_model.requests_of("translate_batch"))
    drafts_used = service.report(task_id)["review"]["drafts_used"]
    assert [d["page"] for d in drafts_used] == [1]
    assert NUMBER in drafts_used[0]["reason"]


def test_draft_and_review_both_changing_a_number_keep_the_source_after_the_retries(
    fake_model, start_service
):
    fake_model.respond("translate_batch", draft_with_wrong_number)
    fake_model.respond("review", reviewed(change_number=True))
    service = start_service(fake_model)

    task_id = service.submit()
    final = service.wait(task_id)

    assert final["state"] == "succeeded", final["error"]
    assert final["kept_source_paragraphs"] == 1
    # The review of the abstract's batch was sent once and resent 3 times.
    abstract_reviews = [
        r for r in fake_model.requests_of("review") if NUMBER in r.prompt
    ]
    assert len(abstract_reviews) == 4
    assert len({r.prompt for r in abstract_reviews}) == 1
    # The abstract keeps its English source; the wrong number is nowhere.
    mono = pdf_text(service.download(task_id, "mono"))
    assert ABSTRACT_PHRASE in mono
    assert PSEUDO_PREFIX + SOURCE_PHRASE.replace(" ", "") not in mono
    assert WRONG_NUMBER not in mono
    report = service.report(task_id)
    kept = report["kept_source_paragraphs"]
    assert len(kept) == 1
    assert kept[0]["page"] == 1
    assert NUMBER in kept[0]["reason"]
    assert re.sub(r"\s+", "", kept[0]["source"]).find(ABSTRACT_PHRASE) >= 0


def test_a_resubmit_on_the_same_data_judges_the_cached_review(fake_model, start_service):
    fake_model.respond("review", reviewed(change_number=True))
    service = start_service(fake_model)
    first = service.wait(service.submit())
    assert first["state"] == "succeeded", first["error"]
    sent_first = len(fake_model.requests)

    task_id = service.submit()
    final = service.wait(task_id)

    assert final["state"] == "succeeded", final["error"]
    # Every answer is in the response cache: nothing is sent again, and no
    # paragraph is redone one by one.
    assert fake_model.requests[sent_first:] == []
    report = service.report(task_id)
    assert report["review"]["batches"] == service.report(first["task_id"])["review"]["batches"]
    assert [d["page"] for d in report["review"]["drafts_used"]] == [1]
    mono = pdf_text(service.download(task_id, "mono"))
    assert PSEUDO_PREFIX + SOURCE_PHRASE.replace(" ", "") in mono
    assert WRONG_NUMBER not in mono
    assert REVIEW_PREFIX in mono


def unglue(spellings: dict[str, str]):
    """A draft answer: the pseudo-translation with each glued spelling written normally."""

    def respond(body: dict) -> str:
        text = pseudo_translate_batch(body)
        for glued, normal in spellings.items():
            text = text.replace(glued, normal)
        return text

    return respond


# Excerpts whose literals the engine used to glue to the word before
# (ticket 12; closure ticket 02 fixed the word spaces): the spelling the
# check now sees, how the draft writes it, and what the mono PDF then shows
# (whitespace removed).
SPACED_LITERALS = {
    # AutoRef page 7 "Appendix H.1", page 14 "OpenAI. Introducing GPT-5.5."
    "autoref-p7-p14.pdf": (
        ["Appendix H.1", "Introducing GPT-5.5"],
        {"Appendix": "附录"},
        ["附录H.1", "IntroducingGPT-5.5"],
    ),
    # MaLiang page 18 "Fig. 6", "Fig. 9"
    "maliang-p18.pdf": (
        ["Fig. 6", "Fig. 9"],
        {"Fig.": "图"},
        ["图6", "图9"],
    ),
}


@pytest.mark.parametrize("excerpt", sorted(SPACED_LITERALS))
def test_a_literal_after_a_word_passes_when_the_translation_writes_it_normally(
    excerpt, fake_model, start_service
):
    spaced_literals, spellings, shown = SPACED_LITERALS[excerpt]
    fake_model.respond("translate_batch", unglue(spellings))
    service = start_service(fake_model)

    task_id = service.submit(FIXTURES / excerpt)
    final = service.wait(task_id)

    assert final["state"] == "succeeded", final["error"]
    # The batches carry the literal apart from the word before it.
    inputs = [
        STYLE_TAG.sub("", item["input"])
        for r in fake_model.requests_of("translate_batch")
        for item in batch_items(r.prompt)
    ]
    for spaced in spaced_literals:
        assert any(spaced in text for text in inputs), spaced
        assert not any(spaced.replace(" ", "") in text for text in inputs), spaced
    # Draft and review pass the checks: nothing is resent or kept in English.
    assert final["kept_source_paragraphs"] == 0
    assert service.report(task_id)["review"]["drafts_used"] == []
    assert len(fake_model.requests_of("review")) == len(fake_model.requests_of("translate_batch"))
    mono = pdf_text(service.download(task_id, "mono"))
    for text in shown:
        assert text in mono


def test_a_review_request_failing_for_good_fails_the_task(fake_model, start_service):
    fake_model.fail_same_request(["500", "500", "500", "500"], kind="review")
    service = start_service(fake_model)

    task_id = service.submit()
    final = service.wait_first_failure(task_id)

    assert final["state"] == "waiting_rerun"  # the attempt failed; retryable (M3 ticket 05)
    assert "第 1 页" in final["error"], final["error"]
    report = service.report(task_id)
    assert [b["page"] for b in report["failed_batches"]][:1] == [1]
