"""Review and mechanical checks.

Spec "计划覆盖的行为" 8: every translation batch is followed by one review
request. 11: a review answer that changes a number gives way to the draft
that passes the checks; when draft and review both change a number, the
review is resent within the batch's retries and then the paragraph keeps
its source text, counted in ``kept_source_paragraphs``.
"""

import json
import re

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
