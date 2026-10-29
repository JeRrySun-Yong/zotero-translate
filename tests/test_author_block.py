"""The author block on page 1 keeps its source lines (closure ticket 05, A1-A3).

The authors, their affiliations and the notes under them are translated
(user decision 2026-10-28), but each source line stays a line: an
affiliation line does not join an author line, a line break does not fall
inside an institution name, and no comma is left on its own.

The layout model can put author lines and an affiliation line in one box
(MaLiang page 1), or two affiliation lines and a note (AutoDesign page 1),
and the engine made one paragraph of each box, so the translation was set as
running text. On AutoDesign page 1 the comma that ends the first author line
lies outside the authors' box and was a paragraph of its own: it stayed at
its source place while the translated line before it got shorter.
"""

import json
import re

import pymupdf

from tests.conftest import FIXTURES
from tests.fake_model import batch_items

CJK = re.compile(r"[一-鿿]")
# What the real model writes for the institutions and notes of the two
# excerpts (C-04 output); the author names stay as they are.
CHINESE = {
    "National University of Singapore": "新加坡国立大学",
    "Fudan University": "复旦大学",
    "Tencent": "腾讯",
    "Meituan": "美团",
    "Huazhong University of Science and Technology": "华中科技大学",
    "Peking University": "北京大学",
    "Tsinghua University": "清华大学",
    "The Chinese University of Hong Kong": "香港中文大学",
    "Shanghai Jiao Tong University": "上海交通大学",
    "Equal contribution": "同等贡献",
    "Project lead": "项目负责人",
}
# The author block of each excerpt's page 1, below the title and above the
# abstract, right of the arXiv side line (PDF points from the top).
AUTHOR_AREA = {
    "maliang-p1.pdf": pymupdf.Rect(100, 160, 560, 225),
    "autodesign-p1.pdf": pymupdf.Rect(60, 115, 560, 195),
}
# A character smaller than this share of the area's largest one is a
# superscript (an affiliation number, "*", "†").
SUPERSCRIPT_SHARE = 0.7
# Baselines of a line's characters lie within a few points of each other
# (the comma left at its source place was 2 pt lower than its line); the next
# line is about 10 pt below.
ROW_GAP = 4
# A superscript sits about 3 pt above the baseline of its line.
SUPERSCRIPT_RISE = 6
# A gap wider than this between two characters of a row separates two runs.
RUN_GAP = 8


def chinese_answer(body: dict) -> str:
    """A batch answer like the real model's: the institutions and notes in
    Chinese, Chinese commas, the author names kept."""
    answer = []
    for item in batch_items(body["messages"][-1]["content"]) or []:
        text = item["input"]
        for english, chinese in CHINESE.items():
            text = text.replace(english, chinese)
        answer.append({"id": item["id"], "output": text.replace(", ", "，")})
    return json.dumps(answer, ensure_ascii=False)


def author_rows(mono: bytes, name: str) -> list[list[tuple[float, float, str]]]:
    """The text rows of the author block, top first; each row is its
    characters ``(left x, right x, character)`` from left to right.

    Full-size characters make the rows: a new row starts where the next
    baseline is more than ``ROW_GAP`` lower. A superscript joins the row
    whose top baseline is at most ``SUPERSCRIPT_RISE`` below it.
    """
    area = AUTHOR_AREA[name]
    chars = []  # (baseline, left x, right x, size, character)
    with pymupdf.open(stream=mono, filetype="pdf") as doc:
        raw = doc[0].get_text("rawdict", clip=area)
    for block in raw["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for char in span["chars"]:
                    baseline = char["origin"][1]
                    if char["c"].strip() and area.y0 <= baseline <= area.y1:
                        x0, _, x1, _ = char["bbox"]
                        chars.append((baseline, x0, x1, span["size"], char["c"]))
    largest = max(size for *_, size, _ in chars)
    rows: list[list[tuple[float, float, float, float, str]]] = []
    for char in sorted(c for c in chars if c[3] >= SUPERSCRIPT_SHARE * largest):
        if rows and char[0] - rows[-1][-1][0] <= ROW_GAP:
            rows[-1].append(char)
        else:
            rows.append([char])
    for char in (c for c in chars if c[3] < SUPERSCRIPT_SHARE * largest):
        below = [row for row in rows if 0 <= row[0][0] - char[0] <= SUPERSCRIPT_RISE]
        if below:
            below[0].append(char)
        else:
            rows.append([char])
    rows.sort(key=lambda row: min(c[0] for c in row))
    return [sorted((x0, x1, c) for _, x0, x1, _, c in row) for row in rows]


def text(row: list[tuple[float, float, str]]) -> str:
    return "".join(c for _, _, c in row)


def runs(row: list[tuple[float, float, str]]) -> list[str]:
    """The row cut where two characters are more than ``RUN_GAP`` apart."""
    parts = [[row[0]]]
    for before, char in zip(row, row[1:], strict=False):
        if char[0] - before[1] > RUN_GAP:
            parts.append([])
        parts[-1].append(char)
    return [text(part) for part in parts]


def translate(fake_model, start_service, name: str) -> bytes:
    fake_model.respond("translate_batch", chinese_answer)
    service = start_service(fake_model)
    task_id = service.submit(FIXTURES / name)
    final = service.wait(task_id)
    assert final["state"] == "succeeded", final["error"]
    return service.download(task_id, "mono")


def test_maliang_affiliations_stay_a_line_of_their_own(fake_model, start_service):
    name = "maliang-p1.pdf"
    rows = [text(row) for row in author_rows(translate(fake_model, start_service, name), name)]

    # Two author lines and the affiliation line, as in the source: the
    # affiliations do not follow "Shuicheng Yan1", and "新加坡国立大学" is
    # not broken ("新加/坡国立大学" in C-04).
    assert len(rows) == 3, rows
    assert rows[-1] == "1新加坡国立大学2复旦大学3腾讯", rows
    assert not any(CJK.search(row) for row in rows[:-1]), rows


def test_autodesign_affiliations_note_and_line_end_comma(fake_model, start_service):
    name = "autodesign-p1.pdf"
    rows = author_rows(translate(fake_model, start_service, name), name)
    texts = [text(row) for row in rows]

    # Two author lines, two affiliation lines and the note line, as in the
    # source: "上海交通大学" whole and not on the note's line (C-04 had
    # "…7上海交通大" / "学*同等贡献，†项目负责人").
    assert len(texts) == 5, texts
    assert texts[2:] == [
        "1美团，2MBZUAI，3华中科技大学，4北京大学,",
        "5清华大学，6香港中文大学，7上海交通大学",
        "*同等贡献，†项目负责人",
    ], texts
    # The comma that ends the first author line follows "Yue1,7", not
    # alone at the right end of the line.
    assert texts[0].endswith("Yue1,7,"), texts
    assert [part for row in rows for part in runs(row) if part in {",", "，"}] == [], [
        runs(row) for row in rows
    ]
