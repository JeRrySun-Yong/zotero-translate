"""Algorithms without line numbers (closure ticket 12).

An algorithm float without line numbers (``algorithmic`` without
``[1]``, algorithm2e ruled) has its ``Algorithm N`` title between two
horizontal rules and its body down to a third rule of the same width. Such
an algorithm is found and translated row by row, like a numbered one: each
row stays at its source start x and baseline. An answer that breaks the
structure makes the whole algorithm keep its source text, with the reason in
the task report.

Not algorithms: a paragraph that starts with "Algorithm 1" and has no rules,
a table between rules, a code listing under a ruled ``Algorithm N`` title,
and a numbered algorithm is still found once, with its line numbers.

Detection: synthetic glyphs as in the red team's probe
(``evidence/redteam/synth.py``, ``text_glyphs``) with rules. Translation: a
synthetic one-page PDF through the translation service and the fake model.
"""

import json

import pymupdf
import pytest

from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import PSEUDO_PREFIX
from tests.fake_model import FakeModel
from tests.fake_model import pseudo_translate_algorithm
from tests.test_algorithm_columns import text_glyphs
from zotero_translate.algorithm_translation import parts_of
from zotero_translate.algorithm_translation import where
from zotero_translate.algorithms import AlgorithmDetector

LEFT, RIGHT_END = 54.0, 288.0  # the left column of a two-column page


def Rule(x0: float, x1: float, y: float) -> tuple[float, float, float]:  # noqa: N802 - reads like the detector's rules
    """A horizontal rule from x0 to x1 at height y, as the detector takes it."""
    return (x0, x1, y)
# (indent level, text): "Input:" row, a loop, its body, the loop's end, the return.
BODY = [
    (0, "Input: rewards r and a step size h"),
    (0, "for each reward in the list do"),
    (1, "add the scaled reward to the total"),
    (0, "end for"),
    (0, "return the total"),
]
TITLE = "Algorithm 1 Sum the rewards"


def ruled(title: str = TITLE, body=BODY, font: str = "NimbusRomNo9L-Regu", rules: bool = True):
    """An algorithm float: rule, title, rule, rows 11 pt apart, rule; its glyphs and rules."""
    glyphs = text_glyphs(title, LEFT, 500)
    for k, (level, text) in enumerate(body):
        glyphs += text_glyphs(text, LEFT + 2 + 10 * level, 484 - 11 * k, font=font)
    bottom = 484 - 11 * (len(body) - 1) - 6
    found = [Rule(LEFT, RIGHT_END, 510.0), Rule(LEFT, RIGHT_END, 495.5), Rule(LEFT, RIGHT_END, bottom)]
    return glyphs, (found if rules else [])


def detect(glyphs, rules) -> AlgorithmDetector:
    detector = AlgorithmDetector()
    detector(1, glyphs, rules)
    return detector


def test_a_ruled_algorithm_without_line_numbers_is_found_row_by_row():
    (unit,) = detect(*ruled()).units
    assert unit.title == TITLE
    assert unit.numbers == []
    lines = unit.fragments[0].lines
    assert [line.number for line in lines] == [None] * 6
    assert [line.text for line in lines] == [TITLE] + [text for _, text in BODY]
    # Each row's start x is the source's: indentation is kept.
    assert [line.x for line in lines] == [LEFT] + [LEFT + 2 + 10 * level for level, _ in BODY]
    report = unit.report()
    assert (report["numbered_lines"], report["first_line"], report["last_line"]) == (0, None, None)


def test_the_algorithm_stays_between_its_rules_beside_another_column():
    """Prose of the right column at the same heights, 18 pt right of the rules' end."""
    glyphs, rules = ruled()
    prose = []
    for k in range(9):
        prose += text_glyphs("the right column prose continues here", RIGHT_END + 18, 505 - 11 * k)
    (unit,) = detect(glyphs + prose, rules).units
    fragment = unit.fragments[0]
    assert fragment.box[2] <= RIGHT_END
    assert [line.text for line in fragment.lines] == [TITLE] + [text for _, text in BODY]
    assert all(i < len(glyphs) for i in fragment.glyphs)


def test_two_algorithms_one_above_the_other_are_two():
    glyphs, rules = ruled()
    below, below_rules = ruled(title="Algorithm 2 Average the rewards")
    shift = 120
    for g in below:
        g.y0 -= shift
        g.y1 -= shift
    below_rules = [Rule(x0, x1, y - shift) for x0, x1, y in below_rules]
    units = detect(glyphs + below, rules + below_rules).units
    assert [unit.title for unit in units] == [TITLE, "Algorithm 2 Average the rewards"]
    assert [len(unit.fragments[0].lines) for unit in units] == [6, 6]


def test_without_rules_a_paragraph_that_starts_with_algorithm_n_is_no_algorithm():
    assert detect(*ruled(title="Algorithm 3 breaks at line 8 and the loop ends", rules=False)).units == []


def test_a_table_between_rules_is_no_algorithm():
    table = [(0, "Method for each task return rate"), (0, "Ours 0.91 0.88"), (0, "Baseline 0.72 0.70")]
    assert detect(*ruled(title="Table 1: Results for each task", body=table)).units == []


def test_a_code_listing_under_a_ruled_title_is_no_algorithm():
    """2605.03984 p4: Python in NimbusMonL under "Algorithm 1" (M2 user story 16)."""
    code = [(0, "for _ in range(outer_loops):"), (1, "x_0 = randn(bz, *shape)"), (1, "buffer.push(x_1)"), (0, "return model")]
    assert detect(*ruled(body=code, font="NimbusMonL-Regu")).units == []


def test_a_big_parenthesis_set_apart_by_height_stays_with_its_formula():
    """2412.16318 p7: the parentheses of "Õ(T)" sit 8 pt above "Play bad arms"; they must move with "Õ" and "T"."""
    body = [(0, "Play bad arms"), (0, "for each arm do"), (1, "pull the arm"), (0, "end for")]
    glyphs, rules = ruled(body=body)
    x = LEFT + 2 + 13 * 4.5 + 3
    glyphs += text_glyphs("O", x, 484, font="CMMI10")
    glyphs += text_glyphs("(", x + 4.5, 492, font="CMEX10")
    glyphs += text_glyphs("T", x + 9, 484, font="CMMI10")
    glyphs += text_glyphs(")", x + 13.5, 492, font="CMEX10")
    (unit,) = detect(glyphs, rules).units
    lines = unit.fragments[0].lines
    assert [line.text for line in lines] == [TITLE, "Play bad arms O(T)", "for each arm do", "pull the arm", "end for"]


def test_a_reason_in_the_task_report_names_the_row_by_its_text():
    """Every row of such an algorithm has no line number: the reason says which row by its text."""
    (unit,) = detect(*ruled()).units
    parts = parts_of(unit)
    assert [where(part) for part in parts] == ["标题"] + [
        f"无行号的行「{text if len(text) <= 20 else text[:20] + '…'}」" for _, text in BODY
    ]


def test_a_numbered_ruled_algorithm_is_found_once_with_its_numbers():
    glyphs, rules = ruled(body=[])
    texts = ["x ← 0", "for i = 1 to n do", "x ← x + i", "end for", "return x"]
    for k, text in enumerate(texts):
        glyphs += text_glyphs(f"{k + 1}:", LEFT, 484 - 11 * k)
        glyphs += text_glyphs(text, LEFT + 14, 484 - 11 * k)
    rules[2] = Rule(LEFT, RIGHT_END, 484 - 11 * 4 - 6)
    (unit,) = detect(glyphs, rules).units
    assert unit.numbers == [1, 2, 3, 4, 5]


# A synthetic one-page PDF, translated by the service with the fake model.

PROSE = [
    "Reinforcement learning agents collect rewards while they interact with an environment, and",
    "the total reward of an episode is the quantity that the agent tries to make as large as it can.",
    "We describe the procedure that sums the rewards of one episode in the algorithm below.",
]
ROWS = [
    (0, "Input:", "rewards r and a step size h"),
    (0, "for", "each reward in the list do"),
    (1, "", "add the scaled reward to the running total"),
    (1, "", "keep the largest reward seen so far"),
    (0, "end for", ""),
    (0, "return", "the running total and the largest reward"),
]
X0, X1 = 72.0, 540.0
TOP = 200.0  # top rule, in pymupdf coordinates (y down)


def ruled_pdf(path) -> tuple[pymupdf.Rect, list[tuple[float, float]]]:
    """Prose, a ruled algorithm without line numbers, prose; the algorithm's rect and row (baseline, x)."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    y = 90.0
    for text in PROSE:
        page.insert_text((X0, y), text, fontname="tiro", fontsize=10)
        y += 12
    rows = []

    def rule(y, width):
        page.draw_rect(pymupdf.Rect(X0, y - width / 2, X1, y + width / 2), color=None, fill=(0, 0, 0))

    rule(TOP, 0.8)
    page.insert_text((X0, TOP + 11), "Algorithm 1", fontname="tibo", fontsize=10)
    page.insert_text((X0 + 52, TOP + 11), "Sum the rewards of one episode", fontname="tiro", fontsize=10)
    rule(TOP + 15, 0.4)
    y = TOP + 27
    for level, keyword, text in ROWS:
        x = X0 + 2 + 12 * level
        rows.append((y, x))
        if keyword:
            page.insert_text((x, y), keyword, fontname="tibo", fontsize=10)
            x += pymupdf.get_text_length(keyword + " ", fontname="tibo", fontsize=10)
        if text:
            page.insert_text((x, y), text, fontname="tiro", fontsize=10)
        y += 12
    bottom = y - 7
    rule(bottom, 0.8)
    y = bottom + 24
    for text in PROSE:
        page.insert_text((X0, y), text, fontname="tiro", fontsize=10)
        y += 12
    doc.save(path)
    return pymupdf.Rect(X0 - 1, TOP - 1, X1 + 1, bottom + 1), rows


def row_starts(data: bytes, rect) -> dict[int, float]:
    """Baseline (rounded) -> x where the row's first word starts, inside ``rect``."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        words = doc[0].get_text("words", clip=rect)
    starts: dict[int, float] = {}
    for w in words:
        key = round(w[3])
        starts[key] = min(starts.get(key, w[0]), w[0])
    return starts


def translate(tmp_path, responder=None) -> tuple[dict, bytes, bytes, pymupdf.Rect]:
    pdf = tmp_path / "ruled.pdf"
    rect, _ = ruled_pdf(pdf)
    with FakeModel() as model:
        if responder is not None:
            model.respond("algorithm", responder)
        (tmp_path / "service").mkdir()
        service = launch_service(model, tmp_path / "service")
        try:
            task = service.submit(pdf)
            status = service.wait(task)
            report = service.report(task)
            mono = service.download(task, "mono")
        finally:
            stop_service(service)
    assert status["state"] == "succeeded", status
    return report, pdf.read_bytes(), mono, rect


@pytest.fixture(scope="module")
def translated(tmp_path_factory):
    return translate(tmp_path_factory.mktemp("ruled"))


def test_the_algorithm_without_line_numbers_is_translated_row_by_row(translated):
    report, source, mono, rect = translated
    (algorithm,) = report["algorithms"]
    assert (algorithm["result"], algorithm["reason"]) == ("translated", None)
    assert algorithm["numbered_lines"] == 0
    assert algorithm["title"].startswith("Algorithm 1")
    rows = [line for fragment in algorithm["fragments"] for line in fragment["lines"]]
    assert len(rows) == 1 + len(ROWS)
    # Every row with natural language holds the pseudo-translation.
    translated_rows = {t["source"] for t in algorithm["translations"] if t["output"].startswith(PSEUDO_PREFIX)}
    assert len(translated_rows) == 6  # the title and the five rows with words ("end for" has none)


def test_rows_keep_their_count_and_their_source_start_x(translated):
    _, source, mono, rect = translated
    before, after = row_starts(source, rect), row_starts(mono, rect)
    assert sorted(before) == sorted(after)
    for baseline, x in before.items():
        assert abs(after[baseline] - x) <= 1.5, (baseline, x, after[baseline])
    with pymupdf.open(stream=mono, filetype="pdf") as doc:
        assert PSEUDO_PREFIX in doc[0].get_text(clip=rect)


def drop_last_row(body):
    answer = json.loads(pseudo_translate_algorithm(body))
    return json.dumps(answer[:-1], ensure_ascii=False)


def test_an_answer_that_breaks_the_structure_keeps_the_whole_algorithm(tmp_path):
    report, source, mono, rect = translate(tmp_path, drop_last_row)
    (algorithm,) = report["algorithms"]
    assert algorithm["result"] == "kept_source"
    assert "结构校验失败" in algorithm["reason"] and "行数" in algorithm["reason"]
    with pymupdf.open(stream=source, filetype="pdf") as a, pymupdf.open(stream=mono, filetype="pdf") as b:
        assert b[0].get_text("words", clip=rect) and [w[4] for w in a[0].get_text("words", clip=rect)] == [
            w[4] for w in b[0].get_text("words", clip=rect)
        ]
