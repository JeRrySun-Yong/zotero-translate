"""The project's licence, the engine's modification notice and the test papers' attribution.

The project is AGPL-3.0 because its engine is BabelDOC (AGPL-3.0); AGPL
section 5 a asks for a prominent notice that the engine was modified, with a
date. The test papers are CC BY 4.0 and need attribution. These tests keep
the files that carry that in step with the repository.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pymupdf

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "tests" / "fixtures"

# The fixtures' file-name prefix -> the paper's arXiv number.
PAPERS = {"autodesign": "2608.13560", "autoref": "2609.35530", "maliang": "2609.34309"}
CC_BY = "https://creativecommons.org/licenses/by/4.0/"


def test_license_is_the_engines_agpl_text():
    text = (ROOT / "LICENSE").read_text("utf-8")
    assert text == (ROOT / "engine" / "LICENSE").read_text("utf-8")
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in text
    assert "Version 3, 19 November 2007" in text


def test_pyproject_declares_the_license_and_leaves_the_readme_to_its_own_ticket():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))["project"]
    assert project["license"] == "AGPL-3.0-only"
    assert project["description"].isascii() and len(project["description"]) > 20
    assert "readme" not in project  # the README is written in M5 ticket 03; a missing file breaks the build


def test_engine_modification_notice_names_the_source_and_the_date():
    notice = (ROOT / "engine" / "MODIFICATIONS.md").read_text("utf-8")
    for needed in (
        "BabelDOC",
        "v0.6.4",
        "https://github.com/funstory-ai/BabelDOC",
        "AGPL-3.0",
        "2026-10-17",
        "engine: vendor BabelDOC v0.6.4 upstream source, unmodified",
        "git diff",
    ):
        assert needed in notice, needed
    assert re.search(r"[一-鿿]", notice), "a Chinese paragraph"
    assert re.search(r"[A-Za-z]{20}", notice), "an English paragraph"
    # No ticket numbers, no internal decisions.
    assert not re.search(r"ticket|closure|\bADR\b|\bM[0-9]\b|票", notice, re.IGNORECASE)


def test_every_test_paper_is_attributed():
    readme = (FIXTURES / "README.md").read_text("utf-8")
    assert CC_BY in readme
    for arxiv in PAPERS.values():
        assert f"https://arxiv.org/abs/{arxiv}" in readme, arxiv


def fixture_rows() -> dict[str, tuple[str, int]]:
    """File name -> (arXiv number, number of pages listed) from the README's file table."""
    rows = {}
    for line in (FIXTURES / "README.md").read_text("utf-8").splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 3 and re.fullmatch(r"`[^`]+\.pdf`", cells[0]):
            pages = [page for page in re.split(r"[,\s]+", cells[2]) if page]
            rows[cells[0].strip("`")] = (cells[1], len(pages))
    return rows


def test_every_fixture_belongs_to_one_of_the_three_papers():
    rows = fixture_rows()
    files = sorted(path.name for path in FIXTURES.glob("*.pdf"))
    assert files, "no fixtures found"
    assert sorted(rows) == files, "the README's file table and tests/fixtures/ list the same files"
    for name in files:
        arxiv, listed_pages = rows[name]
        assert arxiv == PAPERS[name.split("-")[0]], name
        with pymupdf.open(FIXTURES / name) as pdf:
            assert pdf.page_count == listed_pages, name
            # Where the extracted file kept the paper's metadata, it agrees.
            info = pdf.xref_get_key(-1, "Info")
            if info[0] == "xref":
                number = int(info[1].split()[0])
                assert pdf.xref_get_key(number, "arXivID")[1].split("/abs/")[1].startswith(arxiv), name
                assert pdf.xref_get_key(number, "License")[1].endswith("/licenses/by/4.0/"), name
