"""Real pipeline model smoke test: skipped unless ZT_REAL_MODEL_SMOKE=1.

Runs the excerpt through the service against the real endpoint in the
environment file (ZT_ENV_FILE, else the service's default). It checks that
the endpoint accepts all request parameters and that real output parses into
both PDFs. Run it with:

    ZT_REAL_MODEL_SMOKE=1 ZT_ENV_FILE=../unpacked/zotero-pdf2zh/.env uv run pytest tests/test_real_model_smoke.py
"""

import os
import re
from pathlib import Path

import pytest

from tests.conftest import EXCERPT
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.test_paper_task import pdf_text

pytestmark = [
    pytest.mark.real_model,
    pytest.mark.skipif(
        os.environ.get("ZT_REAL_MODEL_SMOKE") != "1",
        reason="set ZT_REAL_MODEL_SMOKE=1 to call the real pipeline model",
    ),
]


def test_real_model_translates_the_excerpt_into_both_pdfs(tmp_path):
    from zotero_translate.settings import DEFAULT_ENV_FILE
    from zotero_translate.settings import DEFAULT_CONCURRENCY

    env_file = Path(os.environ.get("ZT_ENV_FILE", DEFAULT_ENV_FILE))
    service = launch_service(None, tmp_path, DEFAULT_CONCURRENCY, env_file=env_file.resolve())
    try:
        task_id = service.submit(EXCERPT)
        final = service.wait(task_id, timeout=900)
        assert final["state"] == "succeeded", final["error"]
        mono = pdf_text(service.download(task_id, "mono"))
        dual = pdf_text(service.download(task_id, "dual"))
    finally:
        stop_service(service)

    chinese = len(re.findall(r"[一-鿿]", mono))
    assert chinese > 500, f"mono PDF has only {chinese} Chinese characters"
    assert len(re.findall(r"[一-鿿]", dual)) >= chinese
