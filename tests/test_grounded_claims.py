"""The paper dossier and the term list's glosses say only what the paper supports.

Ticket 13: a "double-blind" (双盲) that the paper never wrote went from the
dossier into a gloss and then into the translation. The instructions of the
dossier request and of the term list request, as the fake pipeline model
records them, must ask for statements grounded in the paper, forbid
qualifiers, evaluation settings and terms the paper does not use, and give
blind / double-blind as the example.
"""

import re

import pytest

from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import FakeModel

DOSSIER_GROUNDED = "Every statement in the dossier must be supported by the text of the paper."
GLOSS_GROUNDED = "Base every gloss and every Chinese rendering on the text of the paper alone."
NO_ADDITIONS = "Do not add qualifiers, evaluation settings or terms that the paper does not use."
BLIND_EXAMPLE = re.compile(r'when the paper says "blind", do not write "double-blind" \(双盲\)')
PAPER_OVER_DOSSIER = "where it differs from the text of the paper, follow the paper"


def instructions_of(model: FakeModel, kind: str) -> str:
    """The system message of the one request of ``kind``, whitespace squeezed."""
    [request] = model.requests_of(kind)
    system = request.body["messages"][0]
    assert system["role"] == "system"
    return " ".join(system["content"].split())


@pytest.fixture(scope="module")
def model(tmp_path_factory):
    with FakeModel() as model:
        service = launch_service(model, tmp_path_factory.mktemp("grounded"))
        try:
            final = service.wait(service.submit())
        finally:
            stop_service(service)
    assert final["state"] == "succeeded", final["error"]
    return model


def test_dossier_instructions_ask_for_grounded_claims_and_no_additions(model):
    instructions = instructions_of(model, "dossier")

    assert DOSSIER_GROUNDED in instructions
    assert NO_ADDITIONS in instructions
    assert BLIND_EXAMPLE.search(instructions)


def test_term_list_instructions_ask_for_glosses_from_the_paper_alone(model):
    instructions = instructions_of(model, "term_list")

    assert GLOSS_GROUNDED in instructions
    assert NO_ADDITIONS in instructions
    assert BLIND_EXAMPLE.search(instructions)
    # The dossier also goes into this request; the paper wins over it.
    assert PAPER_OVER_DOSSIER in instructions
