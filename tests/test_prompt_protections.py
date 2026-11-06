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
  the scope of the revision); the shared text stays as it is. The service
  turns them on for every user profile's task; ``run`` with
  ``--user-profile-rules``.
"""

import json
import subprocess
import sys

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
    assert load_pipeline_settings(path, path, user_profile_rules=True).user_profile_rules is True


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


def test_run_sends_the_rules_with_its_development_option(tmp_path):
    with FakeModel() as model:
        env = write_env_file(tmp_path, model.base_url, FAKE_API_KEY)
        result = subprocess.run(
            [sys.executable, "-m", "zotero_translate", "run", str(EXCERPT)]
            + ["--output-dir", str(tmp_path / "out"), "--data-dir", str(tmp_path / "data")]
            + ["--env-file", str(env), "--argon-env-file", str(env), "--user-profile-rules"],
            capture_output=True,
            text=True,
            timeout=300,
        )
    assert result.returncode == 0, result.stderr[-2000:]
    batches = model.requests_of("translate_batch")
    assert batches and all(profile_rules.BATCH_RULES in r.prompt for r in batches)
    report = json.loads((tmp_path / "out" / "report.json").read_text("utf-8"))
    assert report["model"]["user_profile_rules"] is True
