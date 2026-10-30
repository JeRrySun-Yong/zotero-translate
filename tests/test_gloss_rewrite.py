"""Glosses over 15 Chinese characters or ending with 的 are written again (closure ticket 17).

C-10: 7 of AutoDesign's 14 glosses had 16-17 Chinese characters, and three
ended with 的 ("跨越多步工具调用与多轮修改的"), which reads like an unfinished
modifier. The term list instructions ask for 12; argon goes over it.

- After the term list is final, a gloss over ``GLOSS_LIMIT`` (15) Chinese
  characters (embedded English not counted) or ending with 的 (final
  punctuation ignored) does not pass. One gloss rewrite request (argon) lists
  only those, with the dossier and the key concepts; the term list request
  is not resent.
- An answer with a gloss that still does not pass is resent within the
  client's retries (ADR 0002). When they are used up, the passing glosses of
  the last answer that parsed are used, the others keep their old gloss
  (nothing is cut), and the task goes on.
- ``terms.gloss_rewrite`` in the task report lists what was asked, what was
  rewritten and what was kept.
"""

import json

import pytest

from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import FakeModel
from tests.fake_model import gloss_rewrite_request
from tests.test_first_mention_gloss import AUTODESIGN
from tests.test_first_mention_gloss import gloss_requests
from tests.test_first_mention_gloss import key_concept

# 17 Chinese characters (C-10's meta-harness).
LONG = "跨任务迭代优化底层系统的外循环系统"
# Ends with 的 (C-10's agentic).
ENDS_WITH_DE = "由智能体自主规划与调用工具的"
GOOD = "围绕固定模型运行的外围程序"
# Every spelling appears in the abstract on page 1 of AutoDesign (pages 1 and
# 16: the first-mention plan finds the abstract there).
TERMS = [
    key_concept(["meta-harness", "Meta-Harness"], LONG),
    key_concept(["AutoDesign"], ENDS_WITH_DE),
    key_concept(["harness"], GOOD),
]
STILL_LONG = "跨任务迭代优化底层系统的外循环框架系统"
SHORT = "迭代优化外围程序的外循环"
NOUN = "生成学术海报的设计框架"


def answer(glosses: dict) -> str:
    return json.dumps(
        {"glosses": [{"english": e, "gloss_zh": g} for e, g in glosses.items()]}, ensure_ascii=False
    )


def run_task(tmp_path_factory, rewrite):
    with FakeModel() as model:
        model.respond("term_list", lambda body: json.dumps({"terms": TERMS}, ensure_ascii=False))
        model.respond("gloss_rewrite", rewrite)
        service = launch_service(model, tmp_path_factory.mktemp("rewrite"))
        try:
            task_id = service.submit(AUTODESIGN)
            final = service.wait(task_id)
            saved = service.http.get(f"/tasks/{task_id}/files/terms").json()["terms"]
            report = service.report(task_id)
        finally:
            stop_service(service)
    assert final["state"] == "succeeded", final["error"]
    glosses = {t["english"][0]: t["gloss_zh"] for t in saved}
    return model, glosses, report


@pytest.fixture(scope="module")
def second_answer_passes(tmp_path_factory):
    answers = iter(
        [
            # meta-harness still too long: the answer is rejected and resent.
            answer({"meta-harness": STILL_LONG, "AutoDesign": NOUN}),
            answer({"meta-harness": SHORT, "AutoDesign": NOUN}),
        ]
    )
    return run_task(tmp_path_factory, lambda body: next(answers))


@pytest.fixture(scope="module")
def never_passes(tmp_path_factory):
    return run_task(
        tmp_path_factory, lambda body: answer({"meta-harness": STILL_LONG, "AutoDesign": NOUN})
    )


def test_the_second_answer_is_used(second_answer_passes):
    model, glosses, _ = second_answer_passes
    assert len(model.requests_of("term_list")) == 1
    assert len(model.requests_of("gloss_rewrite")) == 2
    assert glosses == {"meta-harness": SHORT, "AutoDesign": NOUN, "harness": GOOD}
    asked = gloss_requests(model.requests_of("translate_batch"))
    assert SHORT in asked and NOUN in asked
    assert not {LONG, ENDS_WITH_DE, STILL_LONG} & set(asked)


def test_the_request_lists_only_the_glosses_that_do_not_pass(second_answer_passes):
    model, _, _ = second_answer_passes
    request = model.requests_of("gloss_rewrite")[0]
    listed = gloss_rewrite_request(request.body)
    assert [(r["english"], r["gloss_zh"]) for r in listed["rewrite"]] == [
        (["meta-harness", "Meta-Harness"], LONG),
        (["AutoDesign"], ENDS_WITH_DE),
    ]
    assert [r["problems"] for r in listed["rewrite"]] == [
        ["17 Chinese characters, more than 15"],
        ["ends with 的"],
    ]
    assert [c["english"][0] for c in listed["key_concepts"]] == ["meta-harness", "AutoDesign", "harness"]
    instructions = " ".join(request.body["messages"][0]["content"].split())
    for phrase in [
        "more than 15 Chinese characters, or it ends with 的",
        "12 Chinese characters is a hard limit",
        "never ends with 的",
    ]:
        assert phrase in instructions
    assert request.body["model"] == model.requests_of("term_list")[0].body["model"]


def test_the_report_lists_the_rewrite(second_answer_passes):
    _, _, report = second_answer_passes
    assert report["terms"]["gloss_rewrite"] == {
        "requested": [
            {"english": ["meta-harness", "Meta-Harness"], "gloss_zh": LONG, "problems": ["17 Chinese characters, more than 15"]},
            {"english": ["AutoDesign"], "gloss_zh": ENDS_WITH_DE, "problems": ["ends with 的"]},
        ],
        "succeeded": True,
        "error": None,
        "rewritten": [
            {"english": ["meta-harness", "Meta-Harness"], "before": LONG, "after": SHORT},
            {"english": ["AutoDesign"], "before": ENDS_WITH_DE, "after": NOUN},
        ],
        "kept": [],
    }
    assert report["terms"]["gloss_checks"]["long_glosses"] == []


def test_without_a_passing_answer_the_passing_glosses_are_used_and_the_rest_kept(never_passes):
    model, glosses, report = never_passes
    assert len(model.requests_of("gloss_rewrite")) == 4  # 1 + 3 retries
    assert glosses == {"meta-harness": LONG, "AutoDesign": NOUN, "harness": GOOD}
    rewrite = report["terms"]["gloss_rewrite"]
    assert rewrite["succeeded"] is False
    assert "meta-harness" in rewrite["error"]
    assert rewrite["rewritten"] == [{"english": ["AutoDesign"], "before": ENDS_WITH_DE, "after": NOUN}]
    assert rewrite["kept"] == [
        {"english": ["meta-harness", "Meta-Harness"], "gloss_zh": LONG, "problems": ["17 Chinese characters, more than 15"]}
    ]
    assert report["terms"]["gloss_checks"]["long_glosses"] == [
        {"english": ["meta-harness", "Meta-Harness"], "gloss_zh": LONG, "characters": 17}
    ]


def test_no_request_when_every_gloss_passes(tmp_path_factory):
    with FakeModel() as model:
        service = launch_service(model, tmp_path_factory.mktemp("no-rewrite"))
        try:
            task_id = service.submit()
            final = service.wait(task_id)
            report = service.report(task_id)
        finally:
            stop_service(service)
    assert final["state"] == "succeeded", final["error"]
    assert model.requests_of("gloss_rewrite") == []
    assert report["terms"]["gloss_rewrite"] is None
