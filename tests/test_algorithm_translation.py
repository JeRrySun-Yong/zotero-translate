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
