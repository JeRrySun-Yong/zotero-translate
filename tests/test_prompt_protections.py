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
"""

import json
import threading

from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import PSEUDO_PREFIX
from tests.fake_model import FakeModel
from tests.fake_model import batch_items
from tests.test_paper_task import pdf_text

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
