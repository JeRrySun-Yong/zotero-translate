"""Line numbers written ``N`` or ``N.`` (closure ticket 08, red team A1).

algorithm2e numbers lines ``1``, ``2`` without a colon, other styles ``1.``;
such an algorithm is found like one numbered ``1:``, with the same line
numbers as the source. An ordinary list numbered ``1.``, ``2.`` is not an
algorithm, and neither is a sentence that cites "Algorithm 1" above one.
Synthetic glyphs as in the red team's probe (``evidence/redteam/synth.py``,
``text_glyphs``).
"""

from tests.test_algorithm_columns import text_glyphs
from zotero_translate.algorithms import AlgorithmDetector

LEFT = 54.0
ALGORITHM = ["x ← 0", "for i = 1 to n do", "x ← x + i", "end for", "return x"]
# A numbered list with algorithm keywords ("for", "Return") in its items.
LIST = [
    "Read the task description for the agent.",
    "Plan the steps you need for the task.",
    "Return the final answer as JSON.",
]


def rows(numbers: list[str], texts: list[str], above: str | None = None, number_size: float = 9.0):
    """Glyphs of numbered rows 11 pt apart, under an optional row ``above``."""
    glyphs = text_glyphs(above, LEFT, 500) if above else []
    for k, (number, text) in enumerate(zip(numbers, texts)):
        base = 488 - 11 * k
        glyphs += text_glyphs(number, LEFT, base, size=number_size)
        glyphs += text_glyphs(text, LEFT + 14, base)
    return glyphs


def detect(glyphs) -> AlgorithmDetector:
    detector = AlgorithmDetector()
    detector(1, glyphs)
    return detector


def test_algorithm2e_numbers_without_a_colon():
    glyphs = rows([str(n) for n in range(1, 6)], ALGORITHM, above="Algorithm 1: Sum", number_size=6.0)
    (unit,) = detect(glyphs).units
    assert unit.title == "Algorithm 1: Sum"
    assert unit.numbers == [1, 2, 3, 4, 5]
    lines = unit.fragments[0].lines
    assert [(line.number, line.text) for line in lines] == [(None, "Algorithm 1: Sum")] + [
        (k + 1, text) for k, text in enumerate(ALGORITHM)
    ]


def test_numbers_with_a_period():
    glyphs = rows([f"{n}." for n in range(1, 6)], ALGORITHM, above="Algorithm 1 Sum")
    (unit,) = detect(glyphs).units
    assert unit.title == "Algorithm 1 Sum"
    assert unit.numbers == [1, 2, 3, 4, 5]
    lines = unit.fragments[0].lines
    assert [(line.number, line.text) for line in lines] == [(None, "Algorithm 1 Sum")] + [
        (k + 1, text) for k, text in enumerate(ALGORITHM)
    ]


def test_a_numbered_list_without_a_title_is_not_an_algorithm():
    assert detect(rows(["1.", "2.", "3."], LIST)).units == []
    assert detect(rows(["1", "2", "3"], LIST)).units == []


def test_a_sentence_citing_an_algorithm_is_not_a_title():
    above = "As shown in Algorithm 1, the agent does the following:"
    assert detect(rows(["1.", "2.", "3."], LIST, above=above)).units == []


def test_a_code_listing_is_not_an_algorithm():
    """SWE-agent (2405.15793) numbers its code listings ``1:`` and sets them in NimbusMonL."""
    code = ["for line in lines:", "    total += len(line)", "return total"]
    glyphs = []
    for k, text in enumerate(code):
        base = 488 - 11 * k
        glyphs += text_glyphs(f"{k + 1}:", LEFT, base, font="NimbusMonL-Regu")
        glyphs += text_glyphs(text, LEFT + 14, base, font="NimbusMonL-Regu")
    assert detect(glyphs).units == []
