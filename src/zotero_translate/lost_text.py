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

Partly cut glyphs (review round 1): ``get_text`` keeps a glyph that its
form's box cuts only partly, so a glyph also counts as not shown when it is
cut: the smallest form box (from the page's content stream: ``q``, ``Q``,
``cm``, ``Do``, nested forms followed) holding its box's centre has its
baseline more than ``FORM_EDGE_TOLERANCE`` below the form's bottom edge (the
lower part is cut), or its box past the form's top or a side by more than
``CUT_SHARE`` of its height or width. The legend of AutoRef page 8 at
966baf9 (baseline 0.34 pt below the plot) is such a case.

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

import re
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
# pt: a glyph whose baseline is this far or less below its form's bottom edge
# is whole (rounding).
FORM_EDGE_TOLERANCE = 0.05
# A glyph whose box reaches past its form's top edge or a side by more than
# this share of its height or width is cut.
CUT_SHARE = 0.3
# Depth of nested forms followed in the content stream.
_FORM_DEPTH = 6
# Characters of an error message kept in the task report.
_ERROR_CHARS = 200
_TOKEN = re.compile(
    rb"\((?:\\.|[^\\()])*\)|<[0-9A-Fa-f\s]*>|/[^\s/\[\]()<>{}%]+"
    rb"|[-+]?(?:\d+\.?\d*|\.\d+)|[A-Za-z'\"*]+|\[|\]|<<|>>"
)

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


def report_lost_text(source_pdf: Path, mono_pdf: Path) -> list[dict]:
    """`lost_text` for the task report: the PDFs are already written, so an
    error of the check does not fail the task (review round 1); it becomes
    one entry ``{"page": None, "text": None, "error": "检查出错：..."}`` with
    the exception's type and the first ``_ERROR_CHARS`` characters of its
    message."""
    try:
        return lost_text(source_pdf, mono_pdf)
    except Exception as e:  # noqa: BLE001 - any error of the check is reported, not raised
        message = f"检查出错：{type(e).__name__}: {str(e)[:_ERROR_CHARS]}"
        return [{"page": None, "text": None, "error": message}]


def _lost_pieces(page: pymupdf.Page, source_hidden: list[Glyph]) -> list[str]:
    """The runs of glyphs, consecutive in drawing order, that are not visible;
    a run with a glyph where the source hides text is left out whole (the
    translation of hidden source text can be longer than the source)."""
    runs: list[list[Glyph]] = [[]]
    visible = _by_char(_visible(page))
    for glyph, cut in _drawn_and_cut(page):
        if _near(visible, glyph[0], glyph[1], glyph[2]) and not cut:
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
    return [glyph for glyph, _cut in _drawn_and_cut(page)]


def _drawn_and_cut(page: pymupdf.Page) -> list[tuple[Glyph, bool]]:
    """`_drawn`, each glyph with whether its form's box cuts it partly."""
    forms = _form_boxes(page)
    glyphs = []
    for span in page.get_texttrace():
        if span.get("type") == _INVISIBLE:
            continue
        for code, glyph, origin, bbox in span["chars"]:
            char = chr(code) if code > 0 else ""
            if not char or char.isspace() or char == _NO_UNICODE or glyph == _LIGATURE_TAIL:
                continue
            glyphs.append(((char, origin[0], origin[1], span["size"]), _is_cut(origin, bbox, forms)))
    return glyphs


def _is_cut(origin, bbox, forms: list[tuple[float, float, float, float]]) -> bool:
    """Whether the smallest form box holding the glyph box's centre cuts the
    glyph: its baseline below the bottom edge, or its box past the top edge
    or a side by more than CUT_SHARE (top-down page coordinates)."""
    cx, cy = (bbox[0] + bbox[2]) / 2, (bbox[1] + bbox[3]) / 2
    holding = [f for f in forms if f[0] <= cx <= f[2] and f[1] <= cy <= f[3]]
    if not holding:
        return False
    x0, y0, x1, y1 = min(holding, key=lambda f: (f[2] - f[0]) * (f[3] - f[1]))
    height, width = bbox[3] - bbox[1], bbox[2] - bbox[0]
    return (
        origin[1] > y1 + FORM_EDGE_TOLERANCE
        or y0 - bbox[1] > CUT_SHARE * height
        or max(x0 - bbox[0], bbox[2] - x1) > CUT_SHARE * width
    )


def _form_boxes(page: pymupdf.Page) -> list[tuple[float, float, float, float]]:
    """The boxes, in top-down page coordinates, of the form XObjects the page
    draws (nested forms too), from its content stream's q, Q, cm and Do."""
    boxes: list[tuple[float, float, float, float]] = []
    _walk(page.parent, page.read_contents(), page.xref, (1, 0, 0, 1, 0, 0), boxes, 0)
    height = page.mediabox.height
    return [(b[0], height - b[3], b[2], height - b[1]) for b in boxes]


def _multiply(a, b):
    return (
        a[0] * b[0] + a[1] * b[2],
        a[0] * b[1] + a[1] * b[3],
        a[2] * b[0] + a[3] * b[2],
        a[2] * b[1] + a[3] * b[3],
        a[4] * b[0] + a[5] * b[2] + b[4],
        a[4] * b[1] + a[5] * b[3] + b[5],
    )


def _xobjects(doc: pymupdf.Document, xref: int) -> dict[str, int]:
    kind, value = doc.xref_get_key(xref, "Resources/XObject")
    if kind == "xref":
        value = doc.xref_object(int(value.split()[0]))
    elif kind != "dict":
        return {}
    return {name: int(ref) for name, ref in re.findall(r"/([^\s/<>\[\]]+)\s+(\d+)\s+0\s+R", value)}


def _walk(doc, stream: bytes, resources: int, ctm, boxes: list, depth: int) -> None:
    stack, numbers, name = [], [], None
    for token in _TOKEN.findall(stream or b""):
        if token[:1] == b"/":
            name, numbers = token[1:].decode("latin1"), []
            continue
        try:
            numbers.append(float(token))
            continue
        except ValueError:
            pass
        if token == b"q":
            stack.append(ctm)
        elif token == b"Q":
            ctm = stack.pop() if stack else ctm
        elif token == b"cm" and len(numbers) >= 6:
            ctm = _multiply(tuple(numbers[-6:]), ctm)
        elif token == b"Do" and name and depth < _FORM_DEPTH:
            ref = _xobjects(doc, resources).get(name)
            if ref and doc.xref_get_key(ref, "Subtype")[1] == "/Form":
                bbox = [float(v) for v in doc.xref_get_key(ref, "BBox")[1].strip("[]").split()]
                kind, matrix = doc.xref_get_key(ref, "Matrix")
                m = tuple(float(v) for v in matrix.strip("[]").split()) if kind == "array" else (1, 0, 0, 1, 0, 0)
                full = _multiply(m, ctm)
                points = [
                    (x * full[0] + y * full[2] + full[4], x * full[1] + y * full[3] + full[5])
                    for x in (bbox[0], bbox[2])
                    for y in (bbox[1], bbox[3])
                ]
                boxes.append(
                    (
                        min(p[0] for p in points),
                        min(p[1] for p in points),
                        max(p[0] for p in points),
                        max(p[1] for p in points),
                    )
                )
                inner = ref if doc.xref_get_key(ref, "Resources")[0] != "null" else resources
                _walk(doc, doc.xref_stream(ref), inner, full, boxes, depth + 1)
        if token not in (b"[", b"]", b"<<", b">>"):
            numbers = []


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
    """Glyphs the page draws but does not show, or shows cut."""
    visible = _by_char(_visible(page))
    return [g for g, cut in _drawn_and_cut(page) if cut or not _near(visible, g[0], g[1], g[2])]


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
