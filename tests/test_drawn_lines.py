"""Drawn lines move with the formula they belong to (closure ticket 14).

A radical's bar, a fraction's bar and the underscore TeX draws in an
identifier (``num\\_training\\_steps``) are not glyphs: they are short
horizontal lines drawn on the page. Before this ticket the engine only moved
glyphs, so when a translated line moved a formula the line stayed where it
was in the source (MaLiang p18: the radical's bar), the engine could break
the line inside a formula that its text-font part split into several pieces
(``√`` | ``min(`` | ``|x−1|`` | ``1)``), and the algorithm translator saw an
identifier with drawn underscores as three words of natural language
(2603.19222 p4: "num training steps" translated, its underscores left behind).

Now a drawn line that belongs to a formula (both its ends touch the
formula's glyphs) or that is an identifier's underscore makes the glyphs it
belongs with one formula (body text) or one kept run (algorithms): it is
typeset as a whole, never broken across lines, and the line moves with it.
Rules above or below a line of text, table rules and an algorithm's three
rules are not a formula's.

Body text: a synthetic one-page PDF (math glyphs in fonts named like TeX's)
and the MaLiang p18 excerpt, through the translation service and the fake
model. Algorithms: synthetic glyphs for the detector, and a synthetic ruled
algorithm through the service.
"""

import pymupdf
import pytest

from tests.conftest import FIXTURES
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.fake_model import PSEUDO_PREFIX
from tests.fake_model import FakeModel
from tests.fake_model import algorithm_rows
from tests.fake_model import batch_items
from tests.test_algorithm_columns import text_glyphs
from zotero_translate.algorithm_translation import parts_of
from zotero_translate.algorithms import AlgorithmDetector
from zotero_translate.algorithms import Glyph

# Fonts of the synthetic pages: (pymupdf base font, the name the PDF gives it).
MATH_FONTS = {"mathit": ("tiit", "CMMI10"), "mathsy": ("tiro", "CMSY10")}
SIZE = 10.0
X0 = 72.0
FILLER = "We measure the relative change of every reported value against its baseline value and the "


class Page:
    """Writes runs of text left to right on one synthetic page; ``x`` is where the next run starts."""

    def __init__(self, page: pymupdf.Page):
        self.page = page
        self.x = X0
        for name, (base, _) in MATH_FONTS.items():
            page.insert_font(fontname=name, fontbuffer=pymupdf.Font(base).buffer)

    @staticmethod
    def width(text: str, font: str, size: float = SIZE) -> float:
        return pymupdf.Font(MATH_FONTS.get(font, (font,))[0]).text_length(text, size)

    def put(self, text: str, y: float, font: str = "tiro", size: float = SIZE, dy: float = 0.0) -> float:
        self.page.insert_text((self.x, y + dy), text, fontname=font, fontsize=size)
        self.x += self.width(text, font, size)
        return self.x

    def line(self, x0: float, x1: float, y: float) -> None:
        self.page.draw_line((x0, y), (x1, y), width=0.4)

    def radical(self, y: float) -> tuple[float, float]:
        """√min(x−1): the radicand partly in a text font, as in MaLiang p18; the bar's ends."""
        self.put("√", y, "mathsy")
        start = self.x
        self.put("min(", y)
        self.put("x", y, "mathit")
        self.put("−", y, "mathsy")
        end = self.put("1)", y)
        self.line(start, end, y - 7.6)
        return start, end

    def fraction(self, y: float) -> tuple[float, float]:
        """a over b, 7 pt, with the bar drawn between; the bar's ends."""
        start = self.x
        width = max(self.width("a", "mathit", 7), self.width("b", "mathit", 7))
        self.put("a", y, "mathit", 7, dy=-3.5)
        self.x = start
        self.put("b", y, "mathit", 7, dy=3.0)
        self.x = start + width
        self.line(start, start + width, y - 2.5)
        return start, start + width

    def identifier(self, y: float, words=("num", "training", "steps"), font: str = "tiro") -> list[tuple]:
        """Words joined by drawn underscores (TeX's \\_: a 0.06 em kern, a 0.3 em rule on the baseline)."""
        underscores = []
        for k, word in enumerate(words):
            if k:
                underscores.append((self.x + 0.6, self.x + 3.6))
                self.line(self.x + 0.6, self.x + 3.6, y - 0.2)
                self.x += 3.6
            self.put(word, y, font)
        return underscores


def rename_math_fonts(source, path) -> None:
    """Save ``source`` to ``path`` with the embedded fonts named like TeX's math fonts."""
    doc = pymupdf.open(source)
    for xref in range(1, doc.xref_length()):
        obj = doc.xref_object(xref)
        if "Nimbus" not in obj:
            continue
        name = "CMMI10" if "Italic" in obj else "CMSY10"
        for key in ("BaseFont", "FontName"):
            if f"/{key}" in obj:
                doc.xref_set_key(xref, key, f"/ABCDEF+{name}")
    doc.save(path)
    doc.close()


def body_pdf(path) -> None:
    """A paragraph whose last lines hold a radical, a fraction and an identifier, each with drawn lines."""
    doc = pymupdf.open()
    page = Page(doc.new_page(width=612, height=792))
    y = 100.0
    for _ in range(3):
        page.x = X0
        page.put(FILLER, y)
        y += 12
    page.x = X0
    page.put("intensity is ", y)
    page.radical(y)
    page.put(", where ", y)
    page.put("x", y, "mathit")
    page.put(" is a reported value; the share ", y)
    page.fraction(y)
    page.put(" is set by ", y)
    page.identifier(y)
    page.put(" in every run of the study.", y)
    y += 12
    page.x = X0
    page.put("The remaining text of the paragraph explains how the scale is drawn in the two tables.", y)
    tmp = str(path) + ".tmp"
    doc.save(tmp)
    rename_math_fonts(tmp, path)


def chars(data: bytes, page: int = 0) -> list[dict]:
    """Every character of a page: c, font, bbox (x0, y0, x1, y1; y down), origin."""
    out = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for block in doc[page].get_text("rawdict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    for c in span["chars"]:
                        if c["c"].strip():
                            out.append({"c": c["c"], "font": span["font"], "bbox": c["bbox"], "origin": c["origin"]})
    return out


def drawn_lines(data: bytes, page: int = 0) -> list[tuple[float, float, float]]:
    """The short thin horizontal lines drawn on a page: (x0, x1, y), y down."""
    found = []
    with pymupdf.open(stream=data, filetype="pdf") as doc:
        for drawing in doc[page].get_drawings():
            r = drawing["rect"]
            if r.height < 2 and 0.5 < r.width < 120:
                found.append((r.x0, r.x1, (r.y0 + r.y1) / 2))
    return found


def line_at(lines, x0: float, tolerance: float = 1.0):
    """The drawn line that starts within ``tolerance`` of ``x0``."""
    matches = [line for line in lines if abs(line[0] - x0) <= tolerance]
    assert len(matches) == 1, (x0, lines)
    return matches[0]


def run(tmp_path, pdf) -> tuple[dict, bytes, FakeModel]:
    with FakeModel() as model:
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
    return report, mono, model


@pytest.fixture(scope="module")
def body(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("body")
    pdf = tmp / "body.pdf"
    body_pdf(pdf)
    report, mono, model = run(tmp, pdf)
    return pdf.read_bytes(), mono, model


def test_a_radical_bar_moves_with_its_formula_kept_on_one_line(body):
    source, mono, _ = body
    for data in (source, mono):  # the same relation before and after translation
        glyphs = chars(data)
        (sign,) = [c for c in glyphs if c["c"] == "√"]
        bar = line_at(drawn_lines(data), sign["bbox"][2])
        # The bar sits on the sign, at the height it had in the source.
        assert abs((sign["origin"][1] - bar[2]) - 7.6) <= 0.5, (sign, bar)
        # The whole formula is on the sign's line, under the bar: never broken after "min(".
        radicand = [c for c in glyphs if c["bbox"][0] >= sign["bbox"][2] - 0.5 and c["bbox"][2] <= bar[1] + 0.5]
        assert "".join(c["c"] for c in radicand if abs(c["origin"][1] - sign["origin"][1]) < 1) == "min(x−1)"
    assert abs(chars(mono)[0]["bbox"][0] - X0) < 1  # the paragraph was retypeset ...
    (moved,) = [c for c in chars(mono) if c["c"] == "√"]
    (still,) = [c for c in chars(source) if c["c"] == "√"]
    assert (moved["bbox"][0], moved["origin"][1]) != pytest.approx((still["bbox"][0], still["origin"][1]), abs=1)


def test_a_fraction_bar_moves_with_its_fraction(body):
    source, mono, _ = body
    for data in (source, mono):
        glyphs = chars(data)
        (a,) = [c for c in glyphs if c["c"] == "a" and c["font"].endswith("CMMI10")]
        (b,) = [c for c in glyphs if c["c"] == "b" and c["font"].endswith("CMMI10")]
        bar = line_at(drawn_lines(data), min(a["bbox"][0], b["bbox"][0]))
        assert a["origin"][1] < bar[2] < b["origin"][1], (a, b, bar)


def test_an_identifier_with_drawn_underscores_keeps_its_source(body):
    source, mono, model = body
    inputs = [item["input"] for r in model.requests_of("translate_batch") for item in batch_items(r.body["messages"][-1]["content"])]
    (paragraph,) = [text for text in inputs if "is set by" in text]
    # The identifier is a placeholder, not three words to translate.
    assert "training" not in paragraph and "num" not in paragraph.split()
    for data in (source, mono):
        glyphs = chars(data)
        text = "".join(c["c"] for c in glyphs)
        start = text.index("numtrainingsteps")
        m, t, g, s = glyphs[start + 2], glyphs[start + 3], glyphs[start + 10], glyphs[start + 11]
        lines = drawn_lines(data)
        for left, right in ((m, t), (g, s)):
            underscore = line_at(lines, left["bbox"][2] + 0.6)
            assert abs(underscore[1] - right["bbox"][0]) <= 1.0, (left, right, underscore)
            assert abs(underscore[2] - left["origin"][1]) <= 0.5


def test_maliang_p18_radical_bars_stay_on_their_radical_signs(tmp_path):
    _, mono, _ = run(tmp_path, FIXTURES / "maliang-p18.pdf")
    glyphs = chars(mono)
    signs = [c for c in glyphs if "CMEX" in c["font"]]
    assert len(signs) == 2
    lines = drawn_lines(mono)
    for sign in signs:
        bar = line_at(lines, sign["bbox"][2])
        # On the sign's top, as in the source (0.4 pt below the top of its box).
        assert abs(bar[2] - sign["bbox"][1]) <= 1.0, (sign, bar)
        # The radicand ends under the bar's end, on the sign's line: the formula was not broken.
        ends = [c for c in glyphs if abs(c["bbox"][2] - bar[1]) <= 1.0 and 0 < c["origin"][1] - bar[2] < 12]
        assert ends, (sign, bar)


# Algorithms: the detector on synthetic glyphs (``text_glyphs``: 0.5 x size wide,
# from 0.2 x size below the baseline to 0.8 x size above it).

LEFT, RIGHT_END = 54.0, 288.0
RULES = [(LEFT, RIGHT_END, 510.0), (LEFT, RIGHT_END, 495.5), (LEFT, RIGHT_END, 435.0)]


def glyph_run(pieces, x: float, base: float, size: float = 9.0) -> tuple[list[Glyph], float]:
    """Glyphs of (text, font) pieces from x on one baseline; the x after them."""
    glyphs = []
    for text, font in pieces:
        run = text_glyphs(text, x, base, size, font)
        glyphs += run
        x = run[-1].x1 if run else x
    return glyphs, x


def ruled_with_drawn_lines():
    """A ruled algorithm whose row 2 holds num\\_training\\_steps and row 3 a radical and a fraction."""
    text = "NimbusRomNo9L-Regu"
    glyphs = text_glyphs("Algorithm 1 Train the model", LEFT, 500)
    drawn = list(RULES)
    row, x = glyph_run([("for each step up to ", text), ("num", text)], LEFT + 2, 484)
    for word in ("training", "steps"):
        drawn.append((x + 0.6, x + 3.6, 484 + 0.2))
        more, x = glyph_run([(word, text)], x + 3.6, 484)
        row += more
    more, x = glyph_run([(" do", "NimbusRomNo9L-Medi")], x, 484)
    glyphs += row + more
    row, x = glyph_run([("scale the reward by ", text), ("√", "CMSY10")], LEFT + 12, 473)
    start = x
    more, x = glyph_run([("n", "CMMI10")], x, 473)
    drawn.append((start, x, 473 + 0.8 * 9 - 0.3))  # on the sign's top
    row += more
    more, x = glyph_run([(" and by ", text)], x, 473)
    row += more
    fraction = x
    row += text_glyphs("a", x, 476, 6.3, "CMMI7") + text_glyphs("b", x, 470, 6.3, "CMMI7")
    drawn.append((fraction, fraction + 3.15, 474.5))
    more, x = glyph_run([(" then stop", text)], fraction + 3.15, 473)
    glyphs += row + more
    glyphs += text_glyphs("end for", LEFT + 2, 462, font="NimbusRomNo9L-Medi")
    glyphs += text_glyphs("return the model", LEFT + 2, 451)
    return glyphs, drawn


def test_an_identifier_with_drawn_underscores_is_one_kept_run_in_an_algorithm():
    glyphs, drawn = ruled_with_drawn_lines()
    detector = AlgorithmDetector()
    detector(1, glyphs, RULES, drawn)
    (unit,) = detector.units
    parts = {part.line.text.split()[0]: part for part in parts_of(unit)}
    loop = parts["for"]
    names = [name for name, positions in loop.keeps.items() if "num_training_steps" == loop.item()["keep"][name]]
    assert names, loop.item()
    assert "training" not in loop.source and "num" not in loop.source.split()
    # Both underscores belong to the row, with the identifier's glyphs.
    assert sorted(index for index, _ in loop.line.drawn) == [3, 4]


def test_a_radical_bar_and_a_fraction_bar_belong_to_their_row_and_the_rules_to_none():
    glyphs, drawn = ruled_with_drawn_lines()
    detector = AlgorithmDetector()
    detector(1, glyphs, RULES, drawn)
    (unit,) = detector.units
    lines = unit.fragments[0].lines
    owned = {index: line.text for line in lines for index, _ in line.drawn}
    assert owned == {
        3: lines[1].text, 4: lines[1].text, 5: lines[2].text, 6: lines[2].text
    }, owned  # the three rules (0, 1, 2) belong to no row
    for line in lines:
        for _, positions in line.drawn:
            assert all(line.keep[k] for k in positions), line.text


# Algorithms through the service: a synthetic ruled algorithm.

TOP = 200.0  # top rule, pymupdf coordinates (y down)


def algorithm_pdf(path) -> None:
    doc = pymupdf.open()
    page = Page(doc.new_page(width=612, height=792))
    x1 = 300.0

    def rule(y, width):
        page.page.draw_rect(pymupdf.Rect(X0, y - width / 2, x1, y + width / 2), color=None, fill=(0, 0, 0))

    y = 90.0
    for _ in range(3):
        page.x = X0
        page.put(FILLER[:60], y)
        y += 12
    rule(TOP, 0.8)
    page.x = X0
    page.put("Algorithm 1", TOP + 11, "tibo")
    page.put(" Train the model on every step", TOP + 11)
    rule(TOP + 15, 0.4)
    y = TOP + 27
    page.x = X0 + 2
    page.put("for", y, "tibo")
    page.put(" each step up to ", y)
    page.identifier(y)
    page.put(" do", y, "tibo")
    y += 12
    page.x = X0 + 14
    page.put("scale the reward by ", y)
    page.radical(y)
    page.put(" and add it to the total", y)
    y += 12
    page.x = X0 + 2
    page.put("end for", y, "tibo")
    y += 12
    page.x = X0 + 2
    page.put("return", y, "tibo")
    page.put(" the total of the run", y)
    rule(y + 5, 0.8)
    y += 29
    for _ in range(3):
        page.x = X0
        page.put(FILLER[:60], y)
        y += 12
    tmp = str(path) + ".tmp"
    doc.save(tmp)
    rename_math_fonts(tmp, path)


@pytest.fixture(scope="module")
def algorithm(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("algorithm")
    pdf = tmp / "algorithm.pdf"
    algorithm_pdf(pdf)
    report, mono, model = run(tmp, pdf)
    return report, pdf.read_bytes(), mono, model


def test_the_algorithm_is_translated_with_the_identifier_as_one_placeholder(algorithm):
    report, _, _, model = algorithm
    (unit,) = report["algorithms"]
    assert (unit["result"], unit["reason"]) == ("translated", None)
    (request,) = model.requests_of("algorithm")
    (row,) = [r for r in algorithm_rows(request.body) if "each step" in r["source"]]
    # One placeholder for the identifier (with the bold keyword next to it, as any kept run).
    assert any(value.startswith("num_training_steps") for value in row["keep"].values()), row
    assert "training" not in row["source"]


def test_drawn_lines_move_with_their_kept_glyphs_in_an_algorithm(algorithm):
    _, source, mono, _ = algorithm
    for data in (source, mono):
        glyphs = chars(data)
        text = "".join(c["c"] for c in glyphs)
        assert PSEUDO_PREFIX in text if data is mono else True
        start = text.index("numtrainingsteps")
        m, t, g, s = glyphs[start + 2], glyphs[start + 3], glyphs[start + 10], glyphs[start + 11]
        lines = drawn_lines(data)
        for left, right in ((m, t), (g, s)):
            underscore = line_at(lines, left["bbox"][2] + 0.6)
            assert abs(underscore[1] - right["bbox"][0]) <= 1.0, (left, right, underscore)
        (sign,) = [c for c in glyphs if c["c"] == "√"]
        bar = line_at(lines, sign["bbox"][2])
        assert abs((sign["origin"][1] - bar[2]) - 7.6) <= 0.5, (sign, bar)
    # The rows were retypeset: the radical moved (the pseudo-translation drops the words before it).
    (before,) = [c for c in chars(source) if c["c"] == "√"]
    (after,) = [c for c in chars(mono) if c["c"] == "√"]
    assert abs(before["bbox"][0] - after["bbox"][0]) > 5
    # The algorithm's three rules stay where they were.
    with pymupdf.open(stream=source, filetype="pdf") as a, pymupdf.open(stream=mono, filetype="pdf") as b:
        rules = lambda doc: sorted(round(d["rect"].y0, 1) for d in doc[0].get_drawings() if d["rect"].width > 200)  # noqa: E731
        assert rules(a) == rules(b)
