"""The lost-text judgment of the task report (closure ticket 15) on synthetic PDFs.

A figure is a form XObject: ``show_pdf_page`` with a clip draws the whole page
into a form whose box is the clip, so text outside the clip is in the content
stream but not on the page, as the translated legend label of AutoRef page 8
was. The judgment is called directly (the page excerpts after the fix lose
nothing, so the service cannot show a positive case); the report and the
scorecard call the same function.
"""

import pymupdf

from zotero_translate.lost_text import lost_text
from zotero_translate.lost_text import report_lost_text

INSIDE = "Visible label"
OUTSIDE = "Clipped tail"


def figure_pdf(path, clip_bottom: float) -> None:
    """A page that draws, as a form clipped at ``clip_bottom``, a page with two lines."""
    with pymupdf.open() as drawing:
        page = drawing.new_page(width=200, height=100)
        page.insert_text((20, 30), INSIDE, fontsize=10)
        page.insert_text((20, 70), OUTSIDE, fontsize=10)
        with pymupdf.open() as doc:
            target = doc.new_page(width=200, height=100)
            clip = pymupdf.Rect(0, 0, 200, clip_bottom)
            target.show_pdf_page(clip, drawing, 0, clip=clip)
            doc.save(path)


def test_text_outside_its_form_is_listed_with_its_page(tmp_path):
    source, mono = tmp_path / "source.pdf", tmp_path / "mono.pdf"
    figure_pdf(source, clip_bottom=100)  # the source shows both lines
    figure_pdf(mono, clip_bottom=50)  # the second line is outside the form's box
    with pymupdf.open(mono) as doc:
        assert OUTSIDE not in doc[0].get_text()
    assert lost_text(source, mono) == [{"page": 1, "text": OUTSIDE.replace(" ", "")}]


def test_text_whose_lower_part_the_form_cuts_is_listed(tmp_path):
    """Review round 1: ``get_text`` keeps a glyph that the form's box cuts
    only partly (the second line's baseline at y 70, its glyph boxes 62.2 to
    72.2, the form ending at 68)."""
    source, mono = tmp_path / "source.pdf", tmp_path / "mono.pdf"
    figure_pdf(source, clip_bottom=100)
    figure_pdf(mono, clip_bottom=68)
    with pymupdf.open(mono) as doc:
        assert OUTSIDE.replace(" ", "") in doc[0].get_text().replace(" ", "")
    assert lost_text(source, mono) == [{"page": 1, "text": OUTSIDE.replace(" ", "")}]


def test_an_error_of_the_check_is_one_report_entry_not_a_failed_task(tmp_path):
    """Review round 1: the task calls the check after both PDFs are written;
    an error of the check becomes one entry of the report's ``lost_text``."""
    source, mono = tmp_path / "source.pdf", tmp_path / "mono.pdf"
    figure_pdf(source, clip_bottom=100)
    mono.write_bytes(b"not a pdf")
    entries = report_lost_text(source, mono)
    assert len(entries) == 1
    assert entries[0]["page"] is None and entries[0]["text"] is None
    assert entries[0]["error"].startswith("检查出错：")
    assert report_lost_text(source, source) == []


def test_text_the_source_hides_at_the_same_place_is_not_listed(tmp_path):
    source, mono = tmp_path / "source.pdf", tmp_path / "mono.pdf"
    figure_pdf(source, clip_bottom=50)
    figure_pdf(mono, clip_bottom=50)
    assert lost_text(source, mono) == []
