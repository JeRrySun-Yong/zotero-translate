"""Algorithm detection: pseudocode keeps its source text (M2 ticket 01).

Spec "计划覆盖的行为" 7 and ticket 01: each MaLiang excerpt holds one
algorithm with as many numbered lines as the source; an ordinary numbered
list is not an algorithm and is translated as ordinary paragraphs; the text
of an algorithm's region in the mono PDF is the source text; the task status
counts the algorithms that kept their source text.

Fixtures: single pages of MaLiang-Harness (p6 Algorithm 1, p13 Figure 10(b)
without title or rules, p17 Algorithm 2) and AutoDesign p4 (numbered
contributions list "1. … 4.").
"""

import pymupdf
import pytest

from tests.conftest import FIXTURES
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import PSEUDO_PREFIX
from tests.fake_model import FakeModel

# Fixture -> (numbered lines, first numbered line's text, a line inside the
# algorithm, the region in the source page: x0, y0, x1, y1 from the top).
ALGORITHMS = {
    "maliang-p6.pdf": (21, "c ← (512,288)", "Draw a curved branch at", (278, 395, 506, 655)),
    "maliang-p13.pdf": (20, "INITCANVAS", "Draw architecture and arrows in", (321, 86, 505, 278)),
    "maliang-p17.pdf": (40, "NEWCANVAS", "for each position along the branch do", (107, 72, 505, 592)),
}
NUMBERED_LIST = "autodesign-p4.pdf"
# The numbered list "1. AutoDesign, …" to "4. Under a fully autonomous …".
LIST_RECT = (70, 158, 542, 420)


def region_words(data: bytes, rect: tuple) -> list[str]:
    """The words of page 1 inside ``rect``, in reading order."""
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        words = doc[0].get_text("words", clip=pymupdf.Rect(*rect))
    return [w[4] for w in sorted(words, key=lambda w: (round(w[3]), w[0]))]


def page_text(data: bytes) -> str:
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        return doc[0].get_text()


@pytest.fixture(scope="module")
def finished(tmp_path_factory):
    """Every excerpt translated by the fake model: final status, report, mono PDF."""
    results = {}
    with FakeModel() as model:
        # The line-by-line translation (ticket 02) gets an answer without
        # rows, so every algorithm falls back to its source text: these tests
        # are about detection and the source fallback.
        model.respond("algorithm", lambda body: "[]")
        service = launch_service(model, tmp_path_factory.mktemp("algorithms"))
        try:
            names = [*ALGORITHMS, NUMBERED_LIST]
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


@pytest.mark.parametrize("name", list(ALGORITHMS))
def test_each_excerpt_holds_one_algorithm_with_the_source_line_count(finished, name):
    lines, first_line, _, _ = ALGORITHMS[name]
    result = finished[name]
    assert result["status"]["state"] == "succeeded", result["status"]
    algorithms = result["report"]["algorithms"]
    assert len(algorithms) == 1, algorithms
    algorithm = algorithms[0]
    assert algorithm["pages"] == [1]
    assert algorithm["numbered_lines"] == lines
    assert (algorithm["first_line"], algorithm["last_line"]) == (1, lines)
    assert algorithm["result"] == "kept_source"
    assert algorithm["reason"]
    numbered = [line for line in algorithm["fragments"][0]["lines"] if line["number"]]
    assert [line["number"] for line in numbered] == list(range(1, lines + 1))
    assert first_line in numbered[0]["text"]


@pytest.mark.parametrize("name", list(ALGORITHMS))
def test_the_status_counts_the_algorithm_that_kept_its_source(finished, name):
    assert finished[name]["status"]["kept_source_algorithms"] == 1


@pytest.mark.parametrize("name", list(ALGORITHMS))
def test_the_algorithm_region_keeps_its_source_text(finished, name):
    _, _, inside, rect = ALGORITHMS[name]
    source = (FIXTURES / name).read_bytes()
    mono = finished[name]["mono"]
    assert inside in page_text(mono)
    assert region_words(mono, rect) == region_words(source, rect)
    # The rest of the page is still translated.
    assert PSEUDO_PREFIX in page_text(mono)


def test_lines_record_where_they_start_and_their_glyphs(finished):
    """Algorithm 2: indentation levels, and small caps function names."""
    lines = finished["maliang-p17.pdf"]["report"]["algorithms"][0]["fragments"][0]["lines"]
    by_number = {line["number"]: line for line in lines if line["number"]}
    # Line 4 "for i = 0 to 25 do" opens a loop; line 5 is one level deeper,
    # line 9 two levels deeper, line 12 "end for" back at line 4's level.
    assert by_number[5]["x"] - by_number[4]["x"] > 10
    assert by_number[9]["x"] - by_number[5]["x"] > 10
    assert abs(by_number[12]["x"] - by_number[4]["x"]) < 1
    runs = by_number[1]["runs"]
    small_caps = [run for run in runs if run["small_caps"]]
    assert small_caps and "NEWCANVAS" in small_caps[0]["text"]
    assert all({"font", "size", "bold"} <= run.keys() for run in runs)
    # Rows without a number inside the block: the title, Require, Ensure and
    # the four "▷ Layer" comments.
    unnumbered = [line["text"] for line in lines if line["number"] is None]
    assert sum("▷" in text for text in unnumbered) == 4
    assert any(text.startswith("Require") for text in unnumbered)


def test_a_numbered_list_is_not_an_algorithm(finished):
    result = finished[NUMBERED_LIST]
    assert result["status"]["state"] == "succeeded"
    assert result["report"]["algorithms"] == []
    assert result["status"]["kept_source_algorithms"] == 0
    with pymupdf.open(stream=result["mono"], filetype="pdf") as doc:
        assert PSEUDO_PREFIX in doc[0].get_text(clip=pymupdf.Rect(*LIST_RECT))
