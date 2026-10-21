"""Key concepts are narrowed: two kinds only, at most 20, with their spelling variants.

Ticket 14 (spec 5 "关键概念的判断"): the term list instructions limit key
concepts to what the paper itself names or defines and the concepts that
frame its paradigm; general terms with a mature Chinese rendering are basic
terms, also when either category would fit. The model lists at most 20 key
concepts, most important first, with every spelling the paper uses. When
the model still returns more than 20, the paper task keeps the first 20 and
reports the rest in ``terms.discarded`` of the task report.
"""

import json
import re

import pymupdf
import pytest

from tests.conftest import EXCERPT
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import FakeModel
from tests.test_term_list import glossary_rows
from tests.test_term_list import term

LIMIT = 20
OVER_LIMIT = "超过 20 个上限"


def words_of_the_excerpt(count: int) -> list[str]:
    """``count`` distinct long words of the excerpt, so that every one passes the source check."""
    with pymupdf.open(EXCERPT) as doc:
        text = " ".join(page.get_text() for page in doc)
    words = dict.fromkeys(w.lower() for w in re.findall(r"\b[A-Za-z]{8,}\b", text))
    assert len(words) >= count
    return list(words)[:count]


KEY_SPELLINGS = words_of_the_excerpt(25)
KEY_TERMS = [term([s], "key_concept", gloss_zh=f"测试释义{i}") for i, s in enumerate(KEY_SPELLINGS)]
MODEL_TERMS = {"terms": KEY_TERMS + [term(["rollout"], "basic_term", chinese="试跑")]}


@pytest.fixture(scope="module")
def finished_task(tmp_path_factory):
    with FakeModel() as model:
        model.respond("term_list", lambda body: json.dumps(MODEL_TERMS, ensure_ascii=False))
        service = launch_service(model, tmp_path_factory.mktemp("limit"))
        try:
            task_id = service.submit()
            final = service.wait(task_id)
            terms = service.http.get(f"/tasks/{task_id}/files/terms").json()["terms"]
            report = service.report(task_id)
        finally:
            stop_service(service)
    assert final["state"] == "succeeded", final["error"]
    return {"model": model, "terms": terms, "report": report}


def instructions(model: FakeModel) -> str:
    [request] = model.requests_of("term_list")
    return " ".join(request.body["messages"][0]["content"].split())


def test_only_the_first_20_key_concepts_are_kept(finished_task):
    terms = finished_task["terms"]
    kept = [t["english"][0] for t in terms if t["category"] == "key_concept"]
    assert kept == KEY_SPELLINGS[:LIMIT]
    # Basic terms are not limited.
    assert [t["english"] for t in terms if t["category"] == "basic_term"] == [["rollout"]]


def test_the_other_5_are_reported_as_over_the_limit(finished_task):
    report = finished_task["report"]["terms"]
    assert report["key_concepts"] == LIMIT
    over = [item for item in report["discarded"] if item["reason"] == OVER_LIMIT]
    assert [item["english"] for item in over] == [[s] for s in KEY_SPELLINGS[LIMIT:]]
    assert all(item["category"] == "key_concept" and item["entry_dropped"] for item in over)


def test_key_concepts_over_the_limit_are_not_injected(finished_task):
    batches = finished_task["model"].requests_of("translate_batch")
    injected = {source.lower() for r in batches for source, _ in glossary_rows(r.prompt)}
    assert injected & set(KEY_SPELLINGS[:LIMIT])
    assert not injected & set(KEY_SPELLINGS[LIMIT:])


def test_instructions_ask_for_at_most_20_key_concepts_most_important_first(finished_task):
    text = instructions(finished_task["model"])
    assert "at most 20 key concepts" in text
    assert "most important first" in text


def test_instructions_prefer_basic_term_when_either_category_fits(finished_task):
    text = instructions(finished_task["model"])
    assert 'could be either category, choose "basic_term"' in text
    assert 'choose "key_concept"' not in text


def test_instructions_name_the_two_kinds_of_key_concepts(finished_task):
    text = instructions(finished_task["model"])
    own_names = "systems, methods, benchmarks, mechanisms and metrics that this paper itself names"
    assert own_names + " or defines" in text
    assert "concepts that this paper uses to frame its own paradigm" in text
    assert "a mature Chinese rendering is always a basic term" in text


def test_instructions_ask_for_the_spelling_variants_of_key_concepts(finished_task):
    text = instructions(finished_task["model"])
    assert "with and without a hyphen" in text
    assert "singular and plural" in text
