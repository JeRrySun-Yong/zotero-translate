"""Copy the source PDF's link annotations to the translated PDFs.

The engine loses every link in two places:

- ``fix_null_xref`` (high_level.py) sets ``/Annots`` to null on every page of
  the working copy ``input.pdf``; ``PDFCreater.write`` builds the mono PDF
  from that copy. With a split strategy, ``do_translate`` also nulls the
  ``/Annots`` of each part's input.
- The side-by-side dual PDF draws each page with ``show_pdf_page``, which
  copies the page content but never its annotations.

``restore_links`` runs after the PDFs are written. It reads the links of the
untouched input file and adds them to each mono page (same page, same
geometry) and to the source half of each side-by-side dual page. URL links
keep their address. Internal jumps, named or explicit, are written as
explicit destinations (target page and point), because the dual PDF has no
named destinations of its own. Other link kinds (other files, launch) are
not copied. Links on the translated half of the dual PDF are not made.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

import pymupdf

logger = logging.getLogger(__name__)

_MONO_ATTRS = ("mono_pdf_path", "no_watermark_mono_pdf_path")
_DUAL_ATTRS = ("dual_pdf_path", "no_watermark_dual_pdf_path")
# Page sizes in the dual PDF are compared with this tolerance (pt).
_TOLERANCE = 0.5


def restore_links(translation_config, translate_result) -> None:
    """Add the input file's links to the mono PDFs and to the dual PDFs' source halves."""
    from babeldoc.format.pdf.high_level import safe_save

    outputs: dict[Path, object] = {}
    for attrs, add in ((_MONO_ATTRS, _add_to_mono), (_DUAL_ATTRS, _add_to_dual)):
        for attr in attrs:
            path = getattr(translate_result, attr, None)
            if path:
                outputs.setdefault(Path(path), add)
    if translation_config.use_alternating_pages_dual:
        logger.info("links are not restored in alternating-pages dual PDFs")
        outputs = {path: add for path, add in outputs.items() if add is _add_to_mono}
    with pymupdf.open(translation_config.input_file) as source:
        links = [_page_links(source, page) for page in source]
        if not any(links):
            return
        for path, add in outputs.items():
            temp_path = translation_config.get_working_file_path(f"{path.stem}.links.pdf")
            with pymupdf.open(path) as doc:
                add(doc, source, links, translation_config)
                safe_save(doc, temp_path)
            shutil.move(temp_path, path)


def _page_links(doc: pymupdf.Document, page: pymupdf.Page) -> list[dict]:
    """The page's URL links and resolved internal jumps, in page coordinates."""
    links = []
    for link in page.get_links():
        if link["kind"] == pymupdf.LINK_URI and link.get("uri"):
            links.append({"kind": pymupdf.LINK_URI, "from": link["from"], "uri": link["uri"]})
            continue
        if link["kind"] not in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED):
            continue
        target = link.get("page")
        if type(target) is not int or not 0 <= target < doc.page_count:
            continue
        point = pymupdf.Point(link.get("to") or (0, 0))
        if link["kind"] == pymupdf.LINK_NAMED:
            # PyMuPDF gives a named destination's point in PDF coordinates
            # (origin at the bottom left); explicit ones in page coordinates.
            point = point * doc[target].transformation_matrix
        links.append(
            {
                "kind": pymupdf.LINK_GOTO,
                "from": link["from"],
                "page": target,
                "to": point,
                "zoom": link.get("zoom") or 0.0,
            }
        )
    return links


def _add_to_mono(doc, source, links, translation_config) -> None:
    if doc.page_count != source.page_count:
        logger.warning(
            "mono PDF has %d pages, the input %d; links are not restored",
            doc.page_count,
            source.page_count,
        )
        return
    placements = {page_no: pymupdf.Identity for page_no in range(doc.page_count)}
    _insert(doc, links, placements)


def _add_to_dual(doc, source, links, translation_config) -> None:
    """Links on the source half: the left half, or the right one with ``dual_translate_first``."""
    if doc.page_count != source.page_count:
        logger.warning(
            "dual PDF has %d pages, the input %d; links are not restored",
            doc.page_count,
            source.page_count,
        )
        return
    placements = {}
    for page_no in range(doc.page_count):
        page, src = doc[page_no].rect, source[page_no]
        # The engine puts the source page and the translated page (same
        # size) side by side; other geometries are not mapped.
        if (
            src.rotation
            or abs(page.width - 2 * src.rect.width) > _TOLERANCE
            or abs(page.height - src.rect.height) > _TOLERANCE
        ):
            logger.warning("dual page %d: links are not restored", page_no + 1)
            continue
        offset = src.rect.width if translation_config.dual_translate_first else 0
        placements[page_no] = pymupdf.Matrix(1, 0, 0, 1, offset, 0)
    _insert(doc, links, placements)


def _insert(doc, links, placements: dict[int, pymupdf.Matrix]) -> None:
    """Insert each source page's links on its output page, moved by its placement."""
    for page_no, matrix in placements.items():
        page = doc[page_no]
        for link in links[page_no]:
            placed = {**link, "from": link["from"] * matrix}
            if link["kind"] == pymupdf.LINK_GOTO:
                if link["page"] not in placements:
                    continue
                placed["to"] = link["to"] * placements[link["page"]]
            page.insert_link(placed)
