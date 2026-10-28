"""Link annotations: the mono PDF and the source half of the dual PDF keep the source links.

Ticket 17 (M1): requirement 7 keeps links. Each mono page has the same
links as its source page, with the same targets; the left (source) half of
each dual page has the source page's links, with internal jumps landing on
the left half of the target page.

Fixture: AutoDesign pages 13, 14 and 20. Page 13 holds two jumps to Figure 11
on page 14; page 14 holds a jump to Figure 12 on the same page; page 20 (a
reference) holds two URL links. All jumps are named destinations, as in the
source papers.
"""

import pymupdf
import pytest

from tests.conftest import FIXTURES
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import FakeModel

LINKED_EXCERPT = FIXTURES / "autodesign-p13-p14-p20.pdf"
URL = "https://proceedings.mlr.press/v267/zhuge25a.html"


def page_links(doc: pymupdf.Document, page_no: int, max_x: float | None = None) -> list[tuple]:
    """The links of one page as (rectangle, target), sorted; only links left of ``max_x``.

    A target is ("uri", address) or ("page", page index, x, y) with the point
    in page coordinates from the top left. Values are rounded to 0.1 pt.
    """
    links = []
    for link in doc[page_no].get_links():
        rect = tuple(round(v, 1) for v in link["from"])
        if max_x is not None and link["from"].x1 > max_x:
            continue
        if link["kind"] == pymupdf.LINK_URI:
            target = ("uri", link["uri"])
        elif link["kind"] == pymupdf.LINK_NAMED:
            # MuPDF's own resolution of the name, in page coordinates.
            page, x, y = doc.resolve_link("#nameddest=" + link["nameddest"])
            target = ("page", page, round(x, 1), round(y, 1))
        elif link["kind"] == pymupdf.LINK_GOTO:
            target = ("page", link["page"], round(link["to"].x, 1), round(link["to"].y, 1))
        else:
            target = ("other", link["kind"])
        links.append((rect, target))
    return sorted(links)


@pytest.fixture(scope="module")
def outputs(tmp_path_factory):
    """The linked excerpt translated by the fake model: status, mono and dual PDFs."""
    with FakeModel() as model:
        service = launch_service(model, tmp_path_factory.mktemp("links"))
        try:
            task_id = service.submit(LINKED_EXCERPT)
            status = service.wait(task_id)
            assert status["state"] == "succeeded", status
            mono = service.download(task_id, "mono")
            dual = service.download(task_id, "dual")
        finally:
            stop_service(service)
    with (
        pymupdf.open(LINKED_EXCERPT) as source,
        pymupdf.open(stream=mono, filetype="pdf") as mono_doc,
        pymupdf.open(stream=dual, filetype="pdf") as dual_doc,
    ):
        width = source[0].rect.width
        yield {
            "source": [page_links(source, i) for i in range(source.page_count)],
            "mono": [page_links(mono_doc, i) for i in range(mono_doc.page_count)],
            "dual_left": [page_links(dual_doc, i, max_x=width) for i in range(dual_doc.page_count)],
        }


def test_the_excerpt_holds_url_links_and_jumps_between_pages():
    with pymupdf.open(LINKED_EXCERPT) as source:
        links = [page_links(source, i) for i in range(source.page_count)]
    targets = [[target for _, target in page] for page in links]
    # Figure 11 starts 57.8 pt below the top of page 14; Figure 12 lower on that page.
    assert targets[0] == [("page", 1, 70.9, 57.8), ("page", 1, 70.9, 57.8)]
    assert targets[1] == [("page", 1, 70.9, 324.1)]
    assert targets[2] == [("uri", URL), ("uri", URL)]


def test_each_mono_page_has_the_source_links_and_targets(outputs):
    assert len(outputs["mono"]) == len(outputs["source"])
    for page_no, (mono, source) in enumerate(zip(outputs["mono"], outputs["source"])):
        assert mono == source, f"page {page_no + 1}"


def test_the_source_half_of_each_dual_page_has_the_source_links_and_targets(outputs):
    # The source page is the left half of the dual page, at offset 0: the
    # rectangles and the target points are those of the source page.
    assert len(outputs["dual_left"]) == len(outputs["source"])
    for page_no, (dual, source) in enumerate(zip(outputs["dual_left"], outputs["source"])):
        assert dual == source, f"page {page_no + 1}"
