"""The batch retry path, prompt rules for user profiles, and short labels (M4 ticket 15).

Ticket 15 redoes the parts of ticket 14 that leave Gemini (the built-in
profile) unchanged:

- A blank translation fails (1b): a batch item that is empty or only
  whitespace is not applied; like any item that fails the engine's checks it
  is redone alone with the engine's simple one-paragraph prompt. The
  mechanical checks report a blank text as 译文为空, so a blank review gives
  way to the draft.
- A null draft is not a source the review kept (1c): the review hands back
  the ids whose source it kept and recorded; any other null is redone alone
  with the simple prompt.
- The built-in Gemini profile's translation batch and review request bodies
  are pinned by their cache keys from before the ticket.
- Prompt rules for user profiles only (2a, 2b; ADR 0005 supplement): a
  user profile's task appends a rules block to every translation batch
  prompt (literal values) and to every review prompt (literal values, and
  the scope of the revision); the shared text stays as it is. They are on
  exactly when a user profile is loaded: every user profile's task in the
  service, and ``run --profile-file``; the built-in env files cannot turn
  them on.
- Short labels are only let through more often, never less (3): an input
  under 10 tokens fails when its output is blank or has no letter or digit;
  otherwise it passes when the old token ratio (0.3-3) passes, and, when
  that fails, when the label width rule passes (the width limits never veto
  what the ratio passes). A label that fails is redone with
  the engine's simple one-paragraph prompt, as before. The samples are the
  labels ``m4-15-analysis/a-short-labels.md`` traced and codex's
  adversarial ones; what passes is what the accepted Gemini output used.
"""

import inspect
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import tiktoken
from babeldoc.format.pdf.document_il.midend.il_translator_llm_only import (
    translation_length_ok,
)

from tests import sample_prompts
from tests.conftest import EXCERPT
from tests.conftest import FAKE_API_KEY
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.conftest import write_env_file
from tests.fake_model import PSEUDO_PREFIX
from tests.fake_model import FakeModel
from tests.fake_model import batch_items
from tests.test_model_profile_tasks import _apply_new_profile
from tests.test_paper_task import pdf_text
from zotero_translate import profile_rules
from zotero_translate.settings import load_pipeline_settings
from zotero_translate.settings import load_profile_settings

SHORT_LABEL = "Demo Page"  # a short label on page 1 of the AutoDesign excerpt
FAIL_SIMPLE_RETRY = Path(__file__).parent / "fail_simple_retry"  # see its sitecustomize


def answer_short_label_with(output):
    """Batch answers: ``output`` for the short label, a pseudo-translation for the rest."""

    def respond(body: dict) -> str:
        items = batch_items(body["messages"][-1]["content"]) or []
        return json.dumps(
            [
                {
                    "id": i["id"],
                    "output": output if SHORT_LABEL in i["input"] else PSEUDO_PREFIX + i["input"],
                }
                for i in items
            ],
            ensure_ascii=False,
        )

    return respond


def run_excerpt(tmp_path_factory, name: str, batch_responder) -> dict:
    with FakeModel() as model:
        model.respond("translate_batch", batch_responder)
        service = launch_service(model, tmp_path_factory.mktemp(name))
        try:
            task_id = service.submit()
            final = service.wait(task_id)
            report = service.report(task_id)
            mono = pdf_text(service.download(task_id, "mono"))
        finally:
            stop_service(service)
    return {"model": model, "final": final, "report": report, "mono": mono}


def short_label_redos(model) -> list:
    return [r for r in model.requests_of("single_paragraph") if SHORT_LABEL in r.prompt]


def test_a_blank_batch_translation_is_redone_alone_with_the_simple_prompt(tmp_path_factory):
    run = run_excerpt(tmp_path_factory, "blank-batch", answer_short_label_with("   "))

    assert run["final"]["state"] == "succeeded", run["final"]["error"]
    assert short_label_redos(run["model"]), "the blank label was not redone alone"
    # The simple prompt's answer (the fake model's pseudo-translation) is used.
    assert PSEUDO_PREFIX + SHORT_LABEL.replace(" ", "") in run["mono"]
    assert run["report"]["kept_source_paragraphs"] == []
    # A blank draft is not sent to the review.
    for request in run["model"].requests_of("review"):
        assert '"draft": "   "' not in request.prompt


def test_a_null_draft_is_redone_alone_not_taken_for_a_kept_source(tmp_path_factory):
    # The review has no draft to judge, so it keeps nothing: the null is the
    # batch's failure, and the paragraph is redone alone like any other.
    run = run_excerpt(tmp_path_factory, "null-draft", answer_short_label_with(None))

    assert run["final"]["state"] == "succeeded", run["final"]["error"]
    assert short_label_redos(run["model"]), "the null draft was not redone alone"
    assert PSEUDO_PREFIX + SHORT_LABEL.replace(" ", "") in run["mono"]
    assert run["report"]["kept_source_paragraphs"] == []


# -- the built-in profile's prompts, pinned ----------------------------------

# Cache keys of the sample prompts' request bodies, computed before ticket 15
# with the integration branch's code (ce39e0b): Gemini's cached answers still
# hit when these hold.
GEMINI_BATCH_KEY = "836bf41dfb7519af9d681a50752014b7589960e5a6adc236282232a5dd76ac50"
GEMINI_REVIEW_KEY = "4598d5f69572e413351a5db7db6885fe8336aa40b7095c1e1a5de24df167df58"


def test_the_built_in_profile_sends_the_batch_and_review_prompts_of_before():
    assert sample_prompts.key(sample_prompts.batch_prompt(batch_extra_rules=None)) == GEMINI_BATCH_KEY
    assert sample_prompts.key(sample_prompts.review_prompt()) == GEMINI_REVIEW_KEY


LITERAL_RULE = "set notation"
LITERAL_EXAMPLES = ("{low, medium, high}", '"status": "ready"')


def test_a_user_profile_appends_the_literal_values_rule_to_the_batch_prompt_only():
    builtin = sample_prompts.batch_prompt(batch_extra_rules=None)
    block = profile_rules.BATCH_RULES
    user = sample_prompts.batch_prompt(batch_extra_rules=block)
    assert LITERAL_RULE in block and all(example in block for example in LITERAL_EXAMPLES)
    assert LITERAL_RULE not in builtin
    assert user.count(block) == 1
    assert user.replace(block, "", 1) == builtin


def test_a_user_profile_appends_the_literal_values_rule_to_the_review_prompt_only():
    builtin = sample_prompts.review_prompt()
    block = profile_rules.REVIEW_RULES
    user = sample_prompts.review_prompt(extra_rules=block)
    assert LITERAL_RULE in block and all(example in block for example in LITERAL_EXAMPLES)
    assert LITERAL_RULE not in builtin
    assert user.count(block) == 1
    assert user.replace(block + "\n\n", "", 1) == builtin


SCOPE_RULES = (
    "Never move, copy or repeat text from one item into another",
    "around the same text as in the draft",
)


def test_a_user_profile_appends_the_scope_of_the_revision_to_the_review_prompt_only():
    builtin = sample_prompts.review_prompt()
    block = profile_rules.REVIEW_RULES
    user = sample_prompts.review_prompt(extra_rules=block)
    for rule in SCOPE_RULES:
        assert rule in block
        assert rule not in builtin
        assert rule not in profile_rules.BATCH_RULES
    assert user.replace(block + "\n\n", "", 1) == builtin


# Values of the three acceptance papers: a rule's examples must not come from
# them (they would bias the papers the rules are judged on).
ACCEPTANCE_PAPER_VALUES = ("pass, fail", "uncertain", "scored", "evaluation_status")


def test_the_rules_examples_do_not_come_from_the_acceptance_papers():
    for block in (profile_rules.BATCH_RULES, profile_rules.REVIEW_RULES):
        for value in ACCEPTANCE_PAPER_VALUES:
            assert value not in block


def _endpoint_file(tmp_path, name, extra=""):
    path = tmp_path / name
    path.write_text(f"OPENAI_BASE_URL=http://127.0.0.1:9/v1\nOPENAI_API_KEY=sk-x\nAI_DEFAULT_MODEL=m\n{extra}")
    return path


def test_the_rules_are_on_for_a_user_profile_and_off_for_the_built_in_one(tmp_path):
    path = _endpoint_file(tmp_path, "endpoint.env")
    assert load_profile_settings(path).user_profile_rules is True
    assert load_pipeline_settings(path, path).user_profile_rules is False
    # Code review round 1: the built-in profile's files have no way to turn them on.
    assert "user_profile_rules" not in inspect.signature(load_pipeline_settings).parameters


def test_a_user_profile_task_sends_the_rules_and_a_gemini_task_does_not(start_service):
    with FakeModel() as gemini, FakeModel() as deepseek:
        service = start_service(gemini)
        gemini_task = service.submit()
        assert service.wait(gemini_task)["state"] == "succeeded"
        _apply_new_profile(service, deepseek)
        user_task = service.submit()
        final = service.wait(user_task)
        user_report = service.report(user_task)
        gemini_report = service.report(gemini_task)

    assert final["state"] == "succeeded", final["error"]
    for model, rules in ((gemini, False), (deepseek, True)):
        batches = model.requests_of("translate_batch")
        reviews = model.requests_of("review")
        assert batches and reviews
        assert all((profile_rules.BATCH_RULES in r.prompt) is rules for r in batches)
        assert all((profile_rules.REVIEW_RULES in r.prompt) is rules for r in reviews)
    assert user_report["model"]["user_profile_rules"] is True
    assert gemini_report["model"]["user_profile_rules"] is False


def _run(tmp_path, *options, env=None) -> subprocess.CompletedProcess:
    """``run`` on the excerpt. The default endpoint files point at files that do
    not exist, so a ``run`` without its own files never reaches a real endpoint."""
    missing = str(tmp_path / "no-default-endpoint.env")
    return subprocess.run(
        [sys.executable, "-m", "zotero_translate", "run", str(EXCERPT)]
        + ["--output-dir", str(tmp_path / "out"), "--data-dir", str(tmp_path / "data")]
        + list(options),
        capture_output=True,
        text=True,
        timeout=300,
        env={**os.environ, "ZT_ENV_FILE": missing, "ZT_ARGON_ENV_FILE": missing, **(env or {})},
    )


def test_run_with_a_user_profile_file_sends_the_rules(tmp_path):
    # Code review round 1: ``run`` picks the rules by the kind of file it
    # loads, a user profile's (``--profile-file``, as the worker), not by a
    # flag; ``--concurrency`` replaces the profile's limit.
    with FakeModel() as model:
        profile = _endpoint_file(tmp_path, "profile.env").read_text().replace(
            "http://127.0.0.1:9/v1", model.base_url
        )
        (tmp_path / "profile.env").write_text(profile)
        result = _run(tmp_path, "--profile-file", str(tmp_path / "profile.env"), "--concurrency", "3")
    assert result.returncode == 0, result.stderr[-2000:]
    batches = model.requests_of("translate_batch")
    reviews = model.requests_of("review")
    assert batches and all(profile_rules.BATCH_RULES in r.prompt for r in batches)
    assert reviews and all(profile_rules.REVIEW_RULES in r.prompt for r in reviews)
    assert {r.body["model"] for r in model.requests} == {"m"}
    report = json.loads((tmp_path / "out" / "report.json").read_text("utf-8"))
    assert report["model"]["user_profile_rules"] is True
    assert report["model"]["shared_budget"]["concurrency"] == 3
    assert model.max_in_flight <= 3


@pytest.mark.parametrize(
    "options",
    [
        # The flag of the first round is gone; the files are closed local ports.
        ["--env-file", "CLOSED", "--argon-env-file", "CLOSED", "--user-profile-rules"],
        ["--profile-file", "p.env", "--env-file", "e.env"],
        ["--profile-file", "p.env", "--argon-env-file", "e.env"],
    ],
    ids=["no-free-flag", "profile-and-flash-file", "profile-and-argon-file"],
)
def test_run_refuses_a_free_rules_flag_and_mixed_settings(tmp_path, options):
    closed = _endpoint_file(tmp_path, "closed.env")  # 127.0.0.1:9, a fake key
    result = _run(tmp_path, *[str(closed) if o == "CLOSED" else o for o in options])
    assert result.returncode == 2
    assert not (tmp_path / "out").exists()


def test_a_null_draft_whose_simple_redo_fails_keeps_its_source_on_record(tmp_path):
    # Null draft -> simple one-paragraph redo -> it fails -> the source is kept
    # and recorded: the report and the run's closing line both count it.
    with FakeModel() as model:
        model.respond("translate_batch", answer_short_label_with(None))
        env = write_env_file(tmp_path, model.base_url, FAKE_API_KEY)
        result = _run(
            tmp_path,
            "--env-file",
            str(env),
            "--argon-env-file",
            str(env),
            env={
                "PYTHONPATH": os.pathsep.join(
                    [str(FAIL_SIMPLE_RETRY)]
                    + ([os.environ["PYTHONPATH"]] if os.environ.get("PYTHONPATH") else [])
                ),
                "ZT_TEST_FAIL_SIMPLE_RETRY": SHORT_LABEL,
            },
        )
    assert result.returncode == 0, result.stderr[-2000:]
    report = json.loads((tmp_path / "out" / "report.json").read_text("utf-8"))
    kept = report["kept_source_paragraphs"]
    assert [k["page"] for k in kept] == [1]
    assert SHORT_LABEL in kept[0]["source"]
    assert "逐段翻译出错" in kept[0]["reason"]
    assert f"保留原文 {len(kept)} 段" in result.stdout
    mono = pdf_text((tmp_path / "out" / "mono.pdf").read_bytes())
    assert SHORT_LABEL.replace(" ", "") in mono
    assert PSEUDO_PREFIX + SHORT_LABEL.replace(" ", "") not in mono
    # The built-in profile's files: no user profile rules.
    batches = model.requests_of("translate_batch")
    assert batches and not any(profile_rules.BATCH_RULES in r.prompt for r in batches)
    assert report["model"]["user_profile_rules"] is False


# -- short labels -------------------------------------------------------------

ENCODING = tiktoken.encoding_for_model("gpt-4o")  # the engine's token count


def tokens(text: str) -> int:
    return len(ENCODING.encode(text, disallowed_special=()))


def length_ok(source: str, output: str) -> bool:
    return translation_length_ok(source, output, tokens(source), tokens(output))


# What the accepted Gemini output used (the old ratio check passed them) or
# what the old check's simple redo gave (Object, Local, Global); and
# References -> 参考图像, which the ratio check rejected (1 -> 3 tokens) and
# the simple redo turned into 参考文献 (ticket 06's and ticket 14's target).
SHORT_ACCEPTED = [
    ("Calls", "调用次数"),  # MaLiang pages 9 and 10
    ("Replan", "重新规划"),  # MaLiang page 7
    ("grounded", "基于参考的"),  # AutoRef page 19
    ("grounded", "基于参考"),
    ("+ Ours", "+ 本文方法"),  # AutoRef pages 1 and 8
    ("Critic VLM", "视觉语言模型评审器"),
    ("VLM focus", "视觉语言模型关注点"),  # AutoDesign page 10
    ("mAP", "平均精度均值"),
    ("Object", "物体"),  # AutoRef pages 6 and 25
    ("Local", "局部"),
    ("Global", "全局"),
    ("References", "参考图像"),  # AutoRef pages 7, 27, 29-31
    ("The agent searches for suitable images", "智能体搜索合适的图像"),
    ("2.5", "2.5"),
    ("Demo Page", PSEUDO_PREFIX + "Demo Page"),  # kept Latin text is not growth
]
# The expanded table headers the old check rejected too, so that the simple
# redo gave the short names; codex's adversarial samples.
SHORT_REJECTED = [
    ("Object", "物体组合"),
    ("Local", "局部属性迁移"),
    ("Global", "全局风格"),
    ("Harness", "智能体套件"),
    ("Layout", "版面布局"),
    ("Background", "背景替换与融合"),
    ("OBJECT", "物体组合"),
    ("OBJECT", "甲" * 100),
    ("Object", "Object " * 30),
    ("The agent searches for suitable images", "."),
    ("The agent searches for suitable images", "，"),
    ("Demo Page", "。"),  # the old ratio check passed it (2 -> 1 tokens)
    ("Demo Page", "演示页面" * 3),
    ("References", "   "),
    ("References", ""),
]


@pytest.mark.parametrize(("source", "output"), SHORT_ACCEPTED)
def test_a_short_label_that_the_baseline_used_passes(source, output):
    assert length_ok(source, output)


@pytest.mark.parametrize(("source", "output"), SHORT_REJECTED)
def test_a_short_label_that_outgrows_its_box_or_translates_nothing_fails(source, output):
    assert not length_ok(source, output)


# Code review round 1 of ticket 15: the old ratio check passes these (an
# abbreviation's Chinese full name, a kept model name), and they are wider
# than twice the source. Width must never veto what the ratio check passes.
RATIO_PASSED_WIDE = [
    ("MLLMs", "多模态大语言模型"),  # 3 -> 6 tokens
    ("BiLSTM", "双向长短期记忆网络"),  # 3 -> 8
    ("ViT", "视觉Transformer"),  # 2 -> 2
]


@pytest.mark.parametrize(("source", "output"), RATIO_PASSED_WIDE)
def test_a_wide_short_label_that_the_ratio_check_passes_passes(source, output):
    assert length_ok(source, output)


def _old_ratio_ok(source: str, output: str) -> bool:
    return 0.3 < tokens(output) / tokens(source) < 3 if tokens(source) else False


@pytest.mark.parametrize(
    ("source", "output"), SHORT_ACCEPTED + SHORT_REJECTED + RATIO_PASSED_WIDE
)
def test_only_a_blank_or_punctuation_output_fails_where_the_ratio_check_passes(source, output):
    """Short labels are only let through more often: of what the old ratio
    check passes, only a blank output or one with no letter or digit fails."""
    if not _old_ratio_ok(source, output):
        return
    translates_nothing = not output.strip() or (
        any(c.isalpha() for c in source) and not any(c.isalnum() for c in output)
    )
    assert length_ok(source, output) is not translates_nothing


def test_a_normal_input_keeps_the_token_ratio():
    assert translation_length_ok("a" * 80, "b" * 80, 40, 50)
    assert not translation_length_ok("a" * 80, "b" * 80, 40, 130)
    assert not translation_length_ok("a" * 80, "b" * 80, 40, 5)


def test_a_short_label_the_width_rule_lets_through_is_not_redone(tmp_path_factory):
    # 2 -> 6 tokens: the old ratio check rejected it and redid it alone.
    label = PSEUDO_PREFIX + SHORT_LABEL
    run = run_excerpt(tmp_path_factory, "short-pass", answer_short_label_with(label))

    assert run["final"]["state"] == "succeeded", run["final"]["error"]
    assert short_label_redos(run["model"]) == []
    assert label.replace(" ", "") in run["mono"]


def test_a_short_label_that_fails_is_redone_with_the_simple_prompt(tmp_path_factory):
    run = run_excerpt(tmp_path_factory, "short-fail", answer_short_label_with("演示页面" * 3))

    assert run["final"]["state"] == "succeeded", run["final"]["error"]
    assert short_label_redos(run["model"]), "the runaway label was not redone"
    # Not as a one-item batch with the paper context.
    assert not [
        r
        for r in run["model"].requests_of("translate_batch")
        if len(batch_items(r.prompt)) == 1 and SHORT_LABEL in batch_items(r.prompt)[0]["input"]
    ]
    assert "演示页面演示页面" not in run["mono"]
    assert PSEUDO_PREFIX + SHORT_LABEL.replace(" ", "") in run["mono"]
