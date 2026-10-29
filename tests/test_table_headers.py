"""Chinese table headers are not shrunk far below the source size (closure ticket 06, T4).

The header cells of MaLiang page 9 table 2 are set at 6.05 pt in narrow
cells: "Calls" is 12 pt wide, "(task-h)" 18 pt. Their Chinese ("调用次数",
"（任务·小时）") is wider than the English, and the engine shrank it until it
fitted the source cell: 3.0 pt and 3.6 pt (C-05). The cells have free space on
both sides within their table row, which a table cell may use.
"""

import json
import unicodedata

import pymupdf

from tests.conftest import FIXTURES
from tests.fake_model import batch_items

EXCERPT = FIXTURES / "maliang-p9.pdf"
# The real model's Chinese for the header of table 2 (C-05 mono.pdf).
CHINESE = {
    "Task Completion": "任务完成情况",
    "Generation Time": "生成时间",
    "Model Usage": "模型用量",
    "Model": "模型",
    "Success": "成功率",
    "Failures": "失败数",
    "Total Time": "总时间",
    "Time/Task": "单任务时间",
    "Calls": "调用次数",
    "Input Tokens": "输入词元",
    "Output Tokens": "输出词元",
    "(task-h)": "（任务·小时）",
    "(min/image)": "（分钟/图像）",
    "(min/case)": "（分钟/例）",
    "(calls/case)": "（次/例）",
    "(k/case)": "（k/例）",
    "(cases)": "（例）",
}
# The two header rows of table 2 (PDF points from the top) and their source size.
HEADER = pymupdf.Rect(100, 128, 510, 148)
SOURCE_SIZE = 6.05
# Ticket 06: a Chinese header character is at least 0.8 of its cell's source size.
MIN_SHARE = 0.8


def header_answer(body: dict) -> str:
    answer = []
    for item in batch_items(body["messages"][-1]["content"]) or []:
        text = item["input"]
        for english in sorted(CHINESE, key=len, reverse=True):
            text = text.replace(english, CHINESE[english])
        answer.append({"id": item["id"], "output": text})
    return json.dumps(answer, ensure_ascii=False)


def header_glyphs(pdf: bytes) -> list[tuple[str, float, pymupdf.Rect]]:
    """The non-blank characters of the header rows: character (NFKC), span
    size, glyph box as high as the font size."""
    glyphs = []
    small = pymupdf.TOOLS.set_small_glyph_heights()
    pymupdf.TOOLS.set_small_glyph_heights(True)
    try:
        with pymupdf.open(stream=pdf, filetype="pdf") as doc:
            raw = doc[0].get_text("rawdict", clip=HEADER)
    finally:
        pymupdf.TOOLS.set_small_glyph_heights(small)
    for block in raw["blocks"]:
        for line in block.get("lines", []):
            for span in line["spans"]:
                for char in span["chars"]:
                    if char["c"].strip():
                        glyphs.append((unicodedata.normalize("NFKC", char["c"]), span["size"], pymupdf.Rect(char["bbox"])))
    return glyphs


def test_narrow_header_cells_keep_their_chinese_near_the_source_size(fake_model, start_service):
    fake_model.respond("translate_batch", header_answer)
    service = start_service(fake_model)
    task_id = service.submit(EXCERPT)
    final = service.wait(task_id)
    assert final["state"] == "succeeded", final["error"]

    glyphs = header_glyphs(service.download(task_id, "mono"))
    text = "".join(c for c, *_ in glyphs)
    for header in ("调用次数", "任务", "小时", "单任务时间"):
        assert header in text, text
    small = sorted({(c, round(size, 2)) for c, size, _ in glyphs if "一" <= c <= "鿿" and size < MIN_SHARE * SOURCE_SIZE})
    assert small == [], small
    # Widened cells do not run into each other.
    touching = [
        (a, b)
        for i, (a, _, box_a) in enumerate(glyphs)
        for b, _, box_b in glyphs[i + 1 :]
        if (box_a & box_b).width > 0.5 and (box_a & box_b).height > 0.5
    ]
    assert touching == [], touching
