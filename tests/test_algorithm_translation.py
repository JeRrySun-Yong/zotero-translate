"""Line-by-line translation of algorithms (M2 ticket 02).

Spec "计划覆盖的行为" 1-6, judged only through the translation service's
HTTP interface, the text and positions in the downloaded mono PDF and the
requests the fake pipeline model received:

1. the line numbers of the algorithm region are the source's;
2. as many lines as the source, each starting where the source line starts;
3. natural-language rows hold the pseudo-translation; keywords, identifiers
   (function call arguments included, D5) and formulas are unchanged;
4. an answer that drops a row makes the algorithm keep its source text,
   while the rest of the page is translated;
5. algorithm requests that fail until the retries are used up make only the
   algorithm keep its source text; the task succeeds;
6. the algorithm request has no first-mention glosses field and carries the
   glossary entries found in the algorithm.
"""

import json
import re

import pymupdf
import pytest

from tests.conftest import FIXTURES
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import PSEUDO_PREFIX
from tests.fake_model import FakeModel
from tests.fake_model import algorithm_rows
from tests.fake_model import pseudo_translate_algorithm
from tests.test_algorithm_detection import ALGORITHMS
from tests.test_algorithm_detection import page_text
from tests.test_algorithm_detection import region_words

# A key concept and a translated basic term that occur in the algorithms.
TERM_LIST = {
    "terms": [
        {
            "english": ["Canvas"],
            "category": "key_concept",
            "chinese": None,
            "gloss_zh": "假释义：生成图像的程序化画布",
            "reason": "假依据：本文的核心对象。",
        },
        {
            "english": ["petal"],
            "category": "basic_term",
            "chinese": "花瓣",
            "gloss_zh": None,
            "reason": "假依据：已有通行译法。",
        },
    ]
}
X_TOLERANCE = 1.5  # pt: the layout's 1.0 plus text extraction


def term_list(body):
    return json.dumps(TERM_LIST, ensure_ascii=False)


def words(data: bytes, rect) -> list[tuple]:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return doc[0].get_text("words", clip=pymupdf.Rect(*rect))


def region_text(data: bytes, rect) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return doc[0].get_text(clip=pymupdf.Rect(*rect))


def line_starts(data: bytes, rect) -> dict[int, float]:
    """Line number -> x where the first word after the ``N:`` starts.

    Only the leftmost ``N:`` of each number is the line number (the comment
    "▷ Layer 1: white background" also holds a "1:").
    """
    found = words(data, rect)
    starts: dict[int, tuple[float, float]] = {}
    for number in found:
        match = re.fullmatch(r"(\d+):", number[4])
        if not match:
            continue
        middle = (number[1] + number[3]) / 2
        after = [
            w[0]
            for w in found
            if w[0] >= number[2] - 0.1 and w[1] <= middle <= w[3] and w is not number
        ]
        value = int(match.group(1))
        if after and (value not in starts or number[0] < starts[value][0]):
            starts[value] = (number[0], min(after))
    return {value: x for value, (_, x) in starts.items()}


def run_tasks(model: FakeModel, root, names: list[str]) -> dict:
    results = {}
    service = launch_service(model, root)
    try:
        tasks = {name: service.submit(FIXTURES / name) for name in names}
        for name, task_id in tasks.items():
            results[name] = {
                "status": service.wait(task_id),
                "report": service.report(task_id),
                "mono": service.download(task_id, "mono"),
            }
    finally:
        stop_service(service)
    return results


@pytest.fixture(scope="module")
def translated(tmp_path_factory):
    with FakeModel() as model:
        model.respond("term_list", term_list)
        results = run_tasks(model, tmp_path_factory.mktemp("translated"), list(ALGORITHMS))
        results["requests"] = model.requests_of("algorithm")
    return results


@pytest.mark.parametrize("name", list(ALGORITHMS))
def test_the_algorithm_is_translated(translated, name):
    result = translated[name]
    assert result["status"]["state"] == "succeeded", result["status"]
    assert result["status"]["kept_source_algorithms"] == 0
    algorithm = result["report"]["algorithms"][0]
    assert (algorithm["result"], algorithm["reason"]) == ("translated", None)


@pytest.mark.parametrize("name", list(ALGORITHMS))
def test_line_numbers_line_count_and_line_starts_are_the_sources(translated, name):
    lines, _, _, rect = ALGORITHMS[name]
    source = line_starts((FIXTURES / name).read_bytes(), rect)
    mono = line_starts(translated[name]["mono"], rect)
    assert sorted(source) == list(range(1, lines + 1))
    assert sorted(mono) == sorted(source)
    moved = {n: (source[n], mono[n]) for n in source if abs(source[n] - mono[n]) > X_TOLERANCE}
    assert not moved


@pytest.mark.parametrize("name", list(ALGORITHMS))
def test_natural_language_is_translated_and_code_is_kept(translated, name):
    _, _, inside, rect = ALGORITHMS[name]
    source = region_text((FIXTURES / name).read_bytes(), rect)
    mono = region_text(translated[name]["mono"], rect)
    assert PSEUDO_PREFIX in mono
    assert inside not in mono
    # In Algorithm 2 every "for" is a keyword; elsewhere "for" is also an
    # English word ("program for T2I").
    keywords = [r"\bdo\b", r"\bend for\b", r"\breturn\b", "←", r"Require:"]
    if name == "maliang-p17.pdf":
        keywords.append(r"\bfor\b")
    for keyword in keywords:
        assert len(re.findall(keyword, mono)) == len(re.findall(keyword, source)), keyword
    # The rest of the page is translated as before.
    assert PSEUDO_PREFIX in page_text(translated[name]["mono"]).replace(mono, "")


def test_function_calls_keep_their_arguments_and_the_title_its_number(translated):
    rect = ALGORITHMS["maliang-p17.pdf"][3]
    mono = re.sub(r"\s+", "", region_text(translated["maliang-p17.pdf"]["mono"], rect))
    for kept in [
        "SEEDEDGENERATORS(s,foliage,woman,flowers)",
        "FILLBEZIERSHAPE(C,hairsilhouette,browngradient)",
        "DRAWFACIALPATHS(C,brows,closedeyes,lashes,nose,lips)",
        "Algorithm2",
    ]:
        assert kept in mono
    assert "Proceduralgeneration" not in mono


def test_translated_rows_take_their_own_rows_style(translated):
    """Algorithm 2 opens with a bold title: its translated rows are not bold."""
    rect = ALGORITHMS["maliang-p17.pdf"][3]
    with pymupdf.open(stream=translated["maliang-p17.pdf"]["mono"], filetype="pdf") as doc:
        spans = [
            span
            for block in doc[0].get_text("dict", clip=pymupdf.Rect(*rect))["blocks"]
            for line in block.get("lines", [])
            for span in line["spans"]
            if PSEUDO_PREFIX in span["text"]
        ]
    assert spans
    assert not [s["font"] for s in spans if "bold" in s["font"].lower()]


def test_the_request_has_rows_glossary_entries_and_no_glosses(translated):
    requests = translated["requests"]
    assert len(requests) == 3  # one per algorithm
    p6 = next(r for r in requests if "Generated Canvas program" in r.prompt)
    assert "first_mention_glosses" not in p6.prompt and "gloss_zh" not in p6.prompt
    assert "| Canvas | Canvas |" in p6.prompt
    assert "| petal | 花瓣 |" in p6.prompt
    rows = algorithm_rows(p6.body)
    assert [r["line"] for r in rows if r["line"] is not None] == list(range(1, 22))
    assert all({"id", "line", "source", "keep"} <= r.keys() for r in rows)
    keeps = {v for r in rows for v in r["keep"].values()}
    assert "Algorithm 1:" in keeps
    assert "for" in "\n".join(p6.prompt.split("## Keep exactly as in the source")[1:])


def drop_last_row(body):
    answer = json.loads(pseudo_translate_algorithm(body))
    return json.dumps(answer[:-1], ensure_ascii=False)


def test_an_answer_without_a_row_keeps_the_source(tmp_path):
    name = "maliang-p6.pdf"
    _, _, inside, rect = ALGORITHMS[name]
    with FakeModel() as model:
        model.respond("algorithm", drop_last_row)
        result = run_tasks(model, tmp_path, [name])[name]
    assert result["status"]["state"] == "succeeded"
    assert result["status"]["kept_source_algorithms"] == 1
    algorithm = result["report"]["algorithms"][0]
    assert algorithm["result"] == "kept_source"
    assert "结构校验失败" in algorithm["reason"] and "行数" in algorithm["reason"]
    source = (FIXTURES / name).read_bytes()
    assert region_words(result["mono"], rect) == region_words(source, rect)
    assert PSEUDO_PREFIX in page_text(result["mono"])


def test_failed_requests_keep_only_the_algorithm_as_source(tmp_path):
    name = "maliang-p17.pdf"
    _, _, inside, rect = ALGORITHMS[name]
    with FakeModel() as model:
        model.fail_same_request(["500"] * 4, kind="algorithm")
        result = run_tasks(model, tmp_path, [name])[name]
    assert result["status"]["state"] == "succeeded"
    assert result["status"]["kept_source_algorithms"] == 1
    algorithm = result["report"]["algorithms"][0]
    assert algorithm["result"] == "kept_source"
    assert "请求失败" in algorithm["reason"]
    source = (FIXTURES / name).read_bytes()
    assert region_words(result["mono"], rect) == region_words(source, rect)
    assert PSEUDO_PREFIX in page_text(result["mono"])


def move_a_placeholder(body):
    """Every placeholder count stays right for the whole block, but one moves to the row above."""
    answer = json.loads(pseudo_translate_algorithm(body))
    for above, row in zip(answer, answer[1:]):
        moved = re.search(r"\{v\d+\}", row["output"])
        if moved and re.search(r"[^\W\d_]{2,}", row["output"]):
            row["output"] = row["output"].replace(moved.group(0), "", 1)
            above["output"] += moved.group(0)
            break
    return json.dumps(answer, ensure_ascii=False)


def test_a_placeholder_in_another_row_keeps_the_source(tmp_path):
    """AutoDesign p7 lines 5-6 lost text that a whole-block placeholder check let through."""
    name = "maliang-p13.pdf"
    rect = ALGORITHMS[name][3]
    with FakeModel() as model:
        model.respond("algorithm", move_a_placeholder)
        result = run_tasks(model, tmp_path, [name])[name]
    assert result["status"]["state"] == "succeeded"
    algorithm = result["report"]["algorithms"][0]
    assert algorithm["result"] == "kept_source"
    assert "占位符与原文同一行不同" in algorithm["reason"]
    source = (FIXTURES / name).read_bytes()
    assert region_words(result["mono"], rect) == region_words(source, rect)


# -- review round 1: AutoDesign p7 (kept fragments whole, source glyphs kept) --

AUTODESIGN = "autodesign-p7.pdf"


def keep_named_rows(body):
    """Rows holding the small caps Accept/Reject or the typewriter AutoDesign come back
    unchanged (the model keeps those words); the other rows are pseudo-translated."""
    answer = json.loads(pseudo_translate_algorithm(body))
    sources = {r["id"]: r["source"] for r in algorithm_rows(body)}
    for row in answer:
        if any(word in sources[row["id"]] for word in ("Accept", "Reject", "AutoDesign")):
            row["output"] = sources[row["id"]]
    return json.dumps(answer, ensure_ascii=False)


@pytest.fixture(scope="module")
def autodesign(tmp_path_factory):
    with FakeModel() as model:
        model.respond("algorithm", keep_named_rows)
        return run_tasks(model, tmp_path_factory.mktemp("autodesign"), [AUTODESIGN])[AUTODESIGN]


def algorithm_rect(result) -> pymupdf.Rect:
    x0, y0, x1, y1 = result["report"]["algorithms"][0]["fragments"][0]["box"]
    with pymupdf.open(stream=result["mono"], filetype="pdf") as doc:
        height = doc[0].rect.height
    return pymupdf.Rect(x0 - 1, height - y1 - 1, x1 + 1, height - y0 + 1)


def chars(data: bytes, rect) -> list[tuple[str, str, float]]:
    """(character, font without subset prefix, x0) of page 1 inside ``rect``."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        found = []
        for block in doc[0].get_text("rawdict", clip=rect)["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    font = span["font"].split("+")[-1]
                    found += [(c["c"], font, c["bbox"][0]) for c in span["chars"]]
    return found


def word_fonts(data: bytes, rect, word: str) -> set[str]:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return {
            span["font"].split("+")[-1]
            for block in doc[0].get_text("dict", clip=rect)["blocks"]
            for line in block.get("lines", [])
            for span in line["spans"]
            if word in span["text"]
        }


def test_a_formula_running_past_the_rule_moves_whole(autodesign):
    """Line 4 ends in H'_{t+1} beyond the other lines; the +1 moves with the H'."""
    assert autodesign["report"]["algorithms"][0]["result"] == "translated"
    rect = algorithm_rect(autodesign)
    source = chars((FIXTURES / AUTODESIGN).read_bytes(), rect)
    mono = chars(autodesign["mono"], rect)
    assert [c for c, _, x in source if x > 500]  # the source line 4 runs to x 537
    assert not [c for c, _, x in mono if x > 500]  # every translated line is short
    assert sum(c == "+" for c, _, _ in mono) == sum(c == "+" for c, _, _ in source)


def test_kept_words_keep_their_small_caps_and_typewriter_glyphs(autodesign):
    rect = algorithm_rect(autodesign)
    source = (FIXTURES / AUTODESIGN).read_bytes()
    for word in ("Accept", "Reject", "AutoDesign"):
        expected = word_fonts(source, rect, word)
        assert expected and word_fonts(autodesign["mono"], rect, word) == expected, word
    assert "SFCC1000" in word_fonts(source, rect, "Accept")
    assert "SFVT1000" in word_fonts(source, rect, "AutoDesign")


# -- ticket 16: constants stay as they are, by placeholders ----------------------

CONSTANTS = {"Accept": "接受", "Reject": "拒绝"}  # AutoDesign p7 lines 8 and 11, small caps


@pytest.fixture(scope="module")
def autodesign_translated(tmp_path_factory):
    """AutoDesign p7 with a model that translates every row with natural language."""
    with FakeModel() as model:
        result = run_tasks(model, tmp_path_factory.mktemp("constants"), [AUTODESIGN])[AUTODESIGN]
        result["requests"] = model.requests_of("algorithm")
    return result


def holds(row: dict, word: str) -> bool:
    return word in row["source"] or any(word in value for value in row["keep"].values())


def test_constants_are_placeholders_in_the_request(autodesign_translated):
    result = autodesign_translated
    assert result["report"]["algorithms"][0]["result"] == "translated"
    rows = algorithm_rows(result["requests"][0].body)
    for word in CONSTANTS:
        row = next(r for r in rows if holds(r, word))
        assert word not in row["source"], row  # no word for the model to translate
        assert any(word in value for value in row["keep"].values()), row
        assert word_fonts(result["mono"], algorithm_rect(result), word) == {"SFCC1000"}


def translate_the_constants(body):
    """A model that translates Accept and Reject anyway: it writes the placeholder's
    content, translated, instead of the placeholder."""
    answer = json.loads(pseudo_translate_algorithm(body))
    rows = {r["id"]: r for r in algorithm_rows(body)}
    for item in answer:
        row = rows[item["id"]]
        if not any(holds(row, word) for word in CONSTANTS):
            continue
        output = row["source"]
        for name, value in row["keep"].items():
            if any(word in value for word in CONSTANTS):
                output = output.replace(name, value)
        for word, chinese in CONSTANTS.items():
            output = output.replace(word, chinese)
        item["output"] = output
    return json.dumps(answer, ensure_ascii=False)


def test_an_answer_that_translates_a_constant_is_resent_then_keeps_the_source(tmp_path):
    with FakeModel() as model:
        model.respond("algorithm", translate_the_constants)
        result = run_tasks(model, tmp_path, [AUTODESIGN])[AUTODESIGN]
        requests = model.requests_of("algorithm")
    assert result["status"]["state"] == "succeeded"
    algorithm = result["report"]["algorithms"][0]
    assert algorithm["result"] == "kept_source"
    assert "占位符与原文同一行不同" in algorithm["reason"]
    # Resent within the client's retries (ADR 0002), the same request each time.
    assert len(requests) > 1 and len({r.prompt for r in requests}) == 1
    rect = algorithm_rect(result)
    for word in CONSTANTS:
        assert word_fonts(result["mono"], rect, word) == {"SFCC1000"}
    assert PSEUDO_PREFIX in page_text(result["mono"])


# -- review round 1: review, mechanical checks and layout fallback ------------


def lengthen_last_row(extra: str):
    def respond(body):
        answer = json.loads(pseudo_translate_algorithm(body))
        answer[-1]["output"] += extra
        return json.dumps(answer, ensure_ascii=False)

    return respond


def mark_reviewed(body):
    rows = algorithm_rows(body)
    return json.dumps(
        [
            {"id": r["id"], "line": r["line"], "output": r["draft"].replace(PSEUDO_PREFIX, PSEUDO_PREFIX + "审校过", 1)}
            for r in rows
        ],
        ensure_ascii=False,
    )


def change_literal(body):
    answer = json.loads(pseudo_translate_algorithm(body))
    for row in answer:
        row["output"] = row["output"].replace("T2I", "T3I")
    return json.dumps(answer, ensure_ascii=False)


def change_literal_in_review(body):
    return json.dumps(
        [{"id": r["id"], "line": r["line"], "output": r["draft"].replace("T2I", "T3I")} for r in algorithm_rows(body)],
        ensure_ascii=False,
    )


def test_a_row_too_long_even_when_shrunk_keeps_the_source(tmp_path):
    name = "maliang-p6.pdf"
    rect = ALGORITHMS[name][3]
    with FakeModel() as model:
        model.respond("algorithm", lengthen_last_row("很" * 40))
        result = run_tasks(model, tmp_path, [name])[name]
    algorithm = result["report"]["algorithms"][0]
    assert result["status"]["state"] == "succeeded"
    assert algorithm["result"] == "kept_source" and "排版失败" in algorithm["reason"]
    assert region_words(result["mono"], rect) == region_words((FIXTURES / name).read_bytes(), rect)


def test_the_review_is_sent_and_its_rows_are_used(tmp_path):
    name = "maliang-p6.pdf"
    with FakeModel() as model:
        model.respond("algorithm_review", mark_reviewed)
        result = run_tasks(model, tmp_path, [name])[name]
        reviews = model.requests_of("algorithm_review")
    assert len(reviews) == 1 and all("draft" in r for r in algorithm_rows(reviews[0].body))
    algorithm = result["report"]["algorithms"][0]
    assert algorithm["result"] == "translated"
    assert any("审校过" in t["output"] for t in algorithm["translations"])


def test_a_review_that_fails_the_checks_falls_back_to_the_draft(tmp_path):
    name = "maliang-p6.pdf"
    with FakeModel() as model:
        model.respond("algorithm_review", change_literal_in_review)
        result = run_tasks(model, tmp_path, [name])[name]
    algorithm = result["report"]["algorithms"][0]
    assert algorithm["result"] == "translated"
    assert any("T2I" in t["output"] for t in algorithm["translations"] if "T2I" in t["source"])


def test_draft_and_review_failing_the_checks_keep_the_source(tmp_path):
    name = "maliang-p6.pdf"
    rect = ALGORITHMS[name][3]
    with FakeModel() as model:
        model.respond("algorithm", change_literal)
        model.respond("algorithm_review", change_literal_in_review)
        result = run_tasks(model, tmp_path, [name])[name]
    algorithm = result["report"]["algorithms"][0]
    assert result["status"]["state"] == "succeeded"
    assert algorithm["result"] == "kept_source" and "机械检查失败" in algorithm["reason"]
    assert region_words(result["mono"], rect) == region_words((FIXTURES / name).read_bytes(), rect)
    assert PSEUDO_PREFIX in page_text(result["mono"])
