"""Lost text: glyphs the mono PDF draws but a reader cannot see (closure ticket 15).

The engine writes a translated figure label into the figure's form XObject;
the PDF viewer clips everything outside the form's bounding box. A label that
ran past that box (AutoRef page 8: "搭配 Qwen3-VL-32B" wrapped to a second line
below the plot; page 26: "MultiBanana（4张参考图）" ran past the plot's right
edge) kept its glyphs in the content stream but showed only "搭配" and
"MultiBanana（4". The checks on the answers could not see this.

Judgment, per page: a glyph is *drawn* when the page's content draws it
(PyMuPDF ``get_texttrace``: every glyph, clipped or not) and *visible* when the
page's text (``get_text("rawdict")``, which MuPDF limits to the clip region
and the media box) has the same character with its origin within
``VISIBLE_TOLERANCE``. A drawn glyph that is not visible is lost, unless it
lies where the source page also hides text: within ``SOURCE_HIDDEN_MARGIN_EM``
(of that glyph's size) of a glyph the source page draws without showing it.
The source PDF itself clips some text (a figure cropped from a larger one
keeps its title outside the crop), and MuPDF leaves some figure text out of
``get_text`` that a viewer shows (MaLiang pages 4 and 12); its translation is
hidden or shown the same way, so it is not lost by the translation. A label
that keeps its source glyphs is at a source place and is excused the same
way. Glyphs that are not visible and consecutive in drawing order make one
run; a run with any glyph where the source hides text is left out whole (the
translation of a hidden source title is longer than the title: AutoDesign
page 10, MaLiang page 12). Every other run is one piece of lost text, listed
with its page.

Left out: invisible text (render mode 3, an OCR layer), the second and later
characters of a ligature (glyph -1; "fi" is one glyph) and glyphs without a
Unicode value (U+FFFD: icons of symbol fonts), which ``get_text`` does not
report at the drawn origin.

Known limits:

- Answer text the engine never draws (a character no font has) is not seen:
  only drawn glyphs are compared.
- A glyph hidden under an image or a filled shape counts as visible; only
  clipping and the media box hide text from ``get_text``.
- A translation lost near text the source already hides is excused (a miss);
  a translation that MuPDF leaves out of ``get_text`` but a viewer shows,
  farther than the margin from any hidden source glyph, is listed (a false
  positive).

The task report (``lost_text``) and the scorecard's "lost_text" metric both
call ``lost_text``.
"""

from __future__ import annotations

from pathlib import Path

import pymupdf

# pt: a drawn glyph is visible when the page text has the same character with
# its origin this close.
VISIBLE_TOLERANCE = 0.5
# A lost glyph within this many ems (its own font size) of a glyph that the
# source page hides, horizontally and vertically, is where the source hides
# text and is not counted.
SOURCE_HIDDEN_MARGIN_EM = 1.0
# get_texttrace span type of invisible text (render mode 3, an OCR layer).
_INVISIBLE = 3
# get_texttrace glyph id of the second and later characters of a ligature.
_LIGATURE_TAIL = -1
_NO_UNICODE = "�"

# (char, origin x, origin y, font size)
Glyph = tuple[str, float, float, float]


def lost_text(source_pdf: Path, mono_pdf: Path) -> list[dict]:
    """``[{"page": n, "text": piece}]`` for every piece of lost text in
    ``mono_pdf`` (1-based pages), compared with ``source_pdf``."""
    lost = []
    with pymupdf.open(source_pdf) as source, pymupdf.open(mono_pdf) as mono:
        for index, page in enumerate(mono):
            hidden = _hidden(source[index]) if index < source.page_count else []
            for piece in _lost_pieces(page, hidden):
                lost.append({"page": index + 1, "text": piece})
    return lost


def _lost_pieces(page: pymupdf.Page, source_hidden: list[Glyph]) -> list[str]:
    """The runs of glyphs, consecutive in drawing order, that are not visible;
    a run with a glyph where the source hides text is left out whole (the
    translation of hidden source text can be longer than the source)."""
    runs: list[list[Glyph]] = [[]]
    visible = _by_char(_visible(page))
    for glyph in _drawn(page):
        if _near(visible, glyph[0], glyph[1], glyph[2]):
            if runs[-1]:
                runs.append([])
        else:
            runs[-1].append(glyph)
    return [
        "".join(g[0] for g in run)
        for run in runs
        if run and not any(_where_hidden(source_hidden, x, y, size) for _c, x, y, size in run)
    ]


def _drawn(page: pymupdf.Page) -> list[Glyph]:
    """Every glyph the page draws that ``get_text`` could report, in drawing order."""
    glyphs = []
    for span in page.get_texttrace():
        if span.get("type") == _INVISIBLE:
            continue
        for code, glyph, origin, _bbox in span["chars"]:
            char = chr(code) if code > 0 else ""
            if not char or char.isspace() or char == _NO_UNICODE or glyph == _LIGATURE_TAIL:
                continue
            glyphs.append((char, origin[0], origin[1], span["size"]))
    return glyphs


def _visible(page: pymupdf.Page) -> list[Glyph]:
    glyphs = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for char in span["chars"]:
                    if not char["c"].isspace():
                        glyphs.append((char["c"], char["origin"][0], char["origin"][1], span["size"]))
    return glyphs


def _hidden(page: pymupdf.Page) -> list[Glyph]:
    """Glyphs the page draws but does not show."""
    visible = _by_char(_visible(page))
    return [g for g in _drawn(page) if not _near(visible, g[0], g[1], g[2])]


def _by_char(glyphs: list[Glyph]) -> dict[str, list[tuple[float, float]]]:
    places: dict[str, list[tuple[float, float]]] = {}
    for char, x, y, _size in glyphs:
        places.setdefault(char, []).append((x, y))
    return places


def _near(places: dict[str, list[tuple[float, float]]], char: str, x: float, y: float) -> bool:
    return any(
        abs(gx - x) <= VISIBLE_TOLERANCE and abs(gy - y) <= VISIBLE_TOLERANCE
        for gx, gy in places.get(char, ())
    )


def _where_hidden(source_hidden: list[Glyph], x: float, y: float, size: float) -> bool:
    margin = SOURCE_HIDDEN_MARGIN_EM * size
    return any(abs(hx - x) <= margin and abs(hy - y) <= margin for _c, hx, hy, _s in source_hidden)
