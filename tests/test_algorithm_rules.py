"""Algorithm detection rules on synthetic PDFs (M2 ticket 01, review round 1).

These call the detector directly on the characters of small generated PDFs,
for rules the acceptance papers do not exercise: algorithms without ``←``,
a ``Procedure`` title, and an algorithm that continues on the next page
(none of the three acceptance papers has one).
"""

import pymupdf

from zotero_translate.algorithm_translation import INSTRUCTIONS
from zotero_translate.algorithm_translation import REVIEW_INSTRUCTIONS
from zotero_translate.algorithms import KEYWORDS
from zotero_translate.algorithms import PROMPT_KEYWORDS
from zotero_translate.algorithms import AlgorithmDetector
from zotero_translate.algorithms import Glyph
from zotero_translate.algorithms import keep_flags


def glyphs_of(page: pymupdf.Page) -> list[Glyph]:
    """The page's characters as the detector sees them (y grows upwards)."""
    height = page.rect.height
    glyphs = []
    for block in page.get_text("rawdict")["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for char in span["chars"]:
                    x0, y0, x1, y1 = char["bbox"]
                    glyphs.append(
                        Glyph(char["c"], x0, height - y1, x1, height - y0, span["font"], span["size"])
                    )
    return glyphs


def detect(pages: list[list[tuple[int | None, str]]]) -> AlgorithmDetector:
    """Write each page's rows (line number or None, text) into a PDF and detect."""
    doc = pymupdf.open()
    for rows in pages:
        page = doc.new_page()
        y = 100
        for number, text in rows:
            if number is not None:
                page.insert_text((100, y), f"{number}:", fontname="helv", fontsize=10)
            page.insert_text((118, y), text, fontname="helv", fontsize=10)
            y += 12
    detector = AlgorithmDetector()
    for index, page in enumerate(doc):
        detector(index + 1, glyphs_of(page))
    return detector


def test_keywords_without_an_arrow_make_an_algorithm():
    detector = detect(
        [[(1, "x = 0"), (2, "for i = 1 to n do"), (3, "x = x + i"), (4, "end for"), (5, "return x")]]
    )
    assert [(u.report()["pages"], u.report()["numbered_lines"]) for u in detector.units] == [([1], 5)]


def test_a_require_row_alone_is_a_keyword():
    detector = detect([[(None, "Require: a list L"), (1, "sort L"), (2, "print L"), (3, "stop")]])
    assert len(detector.units) == 1


def test_numbered_lines_without_a_keyword_are_not_an_algorithm():
    detector = detect([[(1, "Apples are red."), (2, "Bananas are yellow."), (3, "Grapes are purple.")]])
    assert detector.units == []


def test_a_procedure_row_is_the_title():
    detector = detect(
        [[(None, "Procedure Draw(x)"), (1, "y = 0"), (2, "for i = 1 to x do"), (3, "end for")]]
    )
    (unit,) = detector.units
    assert unit.title == "Procedure Draw(x)"
    assert unit.fragments[0].lines[0].text == "Procedure Draw(x)"


def test_numbers_that_continue_on_the_next_page_are_one_algorithm():
    detector = detect(
        [
            [(1, "x = 0"), (2, "for i = 1 to n do"), (3, "x = x + i")],
            [(4, "end for"), (5, "return x"), (6, "y = x")],
        ]
    )
    (unit,) = detector.units
    report = unit.report()
    assert report["pages"] == [1, 2]
    assert (report["numbered_lines"], report["first_line"], report["last_line"]) == (6, 1, 6)


def test_a_new_title_on_the_next_page_starts_another_algorithm():
    detector = detect(
        [
            [(1, "x = 0"), (2, "for i = 1 to n do"), (3, "x = x + i")],
            [(None, "Algorithm 2 Next"), (4, "end for"), (5, "return x"), (6, "y = x")],
        ]
    )
    assert [u.report()["pages"] for u in detector.units] == [[1], [2]]
    assert detector.units[1].title == "Algorithm 2 Next"


def row(*runs: tuple[str, str, bool, float]) -> list[Glyph]:
    """Glyphs of one row from (text, font, bold, size) runs; a space is a word gap."""
    glyphs, x = [], 100.0
    for text, font, bold, size in runs:
        for ch in text:
            if ch == " ":
                x += 3
                continue
            glyphs.append(Glyph(ch, x, 100, x + 0.5 * size, 100 + size, font, size, bold))
            x += 0.5 * size
    return glyphs


def kept(glyphs: list[Glyph]) -> str:
    return "".join(g.text if k else "_" for g, k in zip(glyphs, keep_flags(glyphs)))


TEXT, BOLD, MATH = "NimbusRomNo9L-Regu", "NimbusRomNo9L-Medi", "CMMI10"


def test_the_prompt_names_the_m2_keywords_only():
    """Closure ticket 08 review: the words that are keywords only in bold ("and",
    "or", "not") are natural language in plain text and stay out of the prompt."""
    names = (
        "for, for each, for all, while, do, if, then, else, else if, end, end for, end if, "
        "end while, repeat, until, return, to, in, Require, Ensure, Input, Output, function, procedure"
    )
    assert ", ".join(PROMPT_KEYWORDS) == names
    for instructions in (INSTRUCTIONS, REVIEW_INSTRUCTIONS):
        assert f"- Keywords stay in English, never translated: {names}. Most of them" in instructions
    assert {"and", "or", "not", "forall", "downto"} <= KEYWORDS


def test_keywords_formulas_and_calls_keep_their_source_text():
    """Ticket 02: what a translated row keeps (D5: call arguments are identifiers)."""
    loop = row(("for", BOLD, True, 10), (" each position ", TEXT, False, 10), ("do", BOLD, True, 10))
    assert kept(loop) == "for" + "_" * 12 + "do"
    call = row(
        ("F", TEXT, False, 10), ("ILL", TEXT, False, 8), ("(", TEXT, False, 10),
        ("C,", MATH, False, 10), (" hair silhouette)", TEXT, False, 10),
    )
    assert kept(call) == "FILL(C,hairsilhouette)"
    to = row(
        ("for ", BOLD, True, 10), ("i=0 ", MATH, False, 10), ("to ", TEXT, False, 10),
        ("25 ", MATH, False, 10), ("do", BOLD, True, 10),
    )
    assert "_" not in kept(to)
    braces = row(("d∈{", MATH, False, 10), ("left", TEXT, False, 10), ("}", MATH, False, 10))
    assert "_" not in kept(braces)
    heading = row(("Asset generation and selection", BOLD, True, 10))
    assert set(kept(heading)) == {"_"}
    title = row(("Algorithm 2 ", BOLD, True, 10), ("Procedural generation", TEXT, False, 10))
    assert kept(title).startswith("Algorithm2_")


ITALIC = "NimbusRomNo9L-ReguItal"


def kept_words(glyphs: list[Glyph]) -> list[str]:
    """The row's words (split at word gaps, not at font changes) whose glyphs all keep their source text."""
    keep = keep_flags(glyphs)
    words: list[list[int]] = []
    for k, g in enumerate(glyphs):
        if words and g.x0 - glyphs[words[-1][-1]].x1 <= 0.2 * g.size:
            words[-1].append(k)
        else:
            words.append([k])
    return ["".join(glyphs[k].text for k in word) for word in words if all(keep[k] for k in word)]


def test_constants_and_identifiers_keep_their_source_text():
    """Ticket 16: constants and identifiers become placeholders, so a model cannot translate them."""
    # AutoDesign p7 line 8: "Accept" is set in a small caps font whose text layer reads "Accept".
    accept = row(
        ("d", MATH, False, 10), ("t ", "CMMI7", False, 7), ("← ", "CMSY10", False, 10),
        ("Accept", "JICVUE+SFCC1000", False, 10),
    )
    assert kept_words(accept) == ["dt", "←", "Accept"]
    assert kept_words(row(("set ", TEXT, False, 10), ("Done", "LMRomanCaps10-Regular", False, 10))) == ["Done"]
    # Capitals: a word of _CONSTANT_LETTERS letters or more is a constant; a shorter one is an abbreviation.
    capitals = row(("return ACCEPT if the LLM and FAISS agree on IDs", TEXT, False, 10))
    assert kept_words(capitals) == ["ACCEPT"]
    # Identifiers: an underscore inside a word (across font changes), or camelCase; a call keeps its arguments.
    valid = row(("if ", BOLD, True, 10), ("c.is_valid() ", ITALIC, False, 10), ("then", BOLD, True, 10))
    assert kept_words(valid) == ["if", "c.is_valid()", "then"]
    retry = row(
        ("while ", BOLD, True, 10), ("retry<max", MATH, False, 10), ("_", TEXT, False, 10),
        ("retry ", MATH, False, 10), ("and children remain", TEXT, False, 10),
    )
    assert kept_words(retry) == ["while", "retry<max_retry"]
    call = row(("P ", MATH, False, 10), ("← ", "CMSY10", False, 10), ("SelectParents(archive, k) from the archive", TEXT, False, 10))
    assert kept_words(call) == ["P", "←", "SelectParents(archive,", "k)"]
    score = row(("avgScore ", TEXT, False, 10), ("← ", "CMSY10", False, 10), ("the mean of all scores", TEXT, False, 10))
    assert kept_words(score) == ["avgScore", "←"]
    # A name with a capital inside that is no call (AutoDesign, AutoRef: key concepts) is left to
    # the glossary; the natural language around it is translated as before.
    run = row(("Run ", TEXT, False, 10), ("H", MATH, False, 10), ("0 ", "CMR7", False, 7), ("on the AutoDesign harness", TEXT, False, 10))
    assert kept_words(run) == ["H0"]
    assert kept_words(row(("Diffusion Rejection Sampling (DiffRS)", TEXT, False, 10))) == []
    # A call needs the "(" right after the name; after a word gap it opens ordinary text.
    assert kept_words(row(("Run AutoDesign (with the default settings)", TEXT, False, 10))) == []


def test_words_that_look_like_constants_but_are_not_stay_translatable():
    """Ticket 16 exceptions: a Chinese font's "SC" (Simplified Chinese), headings in capitals."""
    assert kept_words(row(("Accept", "PingFangSC-Regular", False, 10))) == []
    assert kept_words(row(("TRAINING SCHEDULE", TEXT, False, 10))) == []
    assert kept_words(row(("Training SCHEDULE", BOLD, True, 10))) == []
    assert kept_words(row(("STAGE 2: SDE Integration", BOLD, True, 9))) == []
