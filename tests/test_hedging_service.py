"""The hedging switch through the translation service (M3 ticket 15; spec section 8, seam 2).

The service hedges unless it is started with ``--no-hedging``; the test-only
``ZT_TEST_HEDGE_AFTER_SECONDS`` replaces the 400-second threshold. A hedged
copy is told apart in the attempt's request records (``hedge``), and the
task report gives the threshold and the number of copies.
"""

from pathlib import Path

import pytest

from tests.conftest import EXCERPT
from tests.fake_model import FakeModel
from zotero_translate.request_log import REQUESTS_FILE
from zotero_translate.request_log import load_records
from zotero_translate.settings import HEDGE_AFTER_SETTING

HEDGE = 1.0  # seconds; production uses 400
SMALL_TIMEOUT = 3.0  # seconds; production uses 480


def _records(service, task_id: str) -> list[dict]:
    path = Path(service.data_dir) / "tasks" / task_id / "attempts" / "1" / REQUESTS_FILE
    return load_records(path)


@pytest.mark.parametrize("hedging", [True, False], ids=["on", "off"])
def test_the_switch_decides_whether_a_hung_batch_gets_an_argon_copy(start_service, hedging):
    with FakeModel() as flash, FakeModel() as argon:
        flash.inject_error("timeout", kind="translate_batch")  # the first batch hangs
        service = start_service(
            flash,
            argon=argon,
            request_timeout=SMALL_TIMEOUT,
            env={HEDGE_AFTER_SETTING: str(HEDGE)},
            hedging=hedging,
        )

        final = service.wait(service.submit(EXCERPT))

        assert final["state"] == "succeeded", final["error"]
        report = service.report(final["task_id"])
        records = _records(service, final["task_id"])
        [hung] = [r for r in flash.requests if r.fault == "timeout"]
        assert hung.disconnected

    [request] = {
        r["request"] for r in records if r["kind"] == "translation_batch" and r["result"] != "succeeded"
    }
    sends = sorted((r for r in records if r["request"] == request), key=lambda r: r["send"])
    summary = [(r["send"], r["endpoint"], r["result"], r["hedge"]) for r in sends]
    if hedging:
        assert summary == [(1, "flash", "cancelled_for_winner", False), (2, "argon", "succeeded", True)]
        assert report["model"]["hedging"] == {"after_seconds": HEDGE, "copies": 1}
        assert [r.kind for r in argon.requests_of("translate_batch")] == ["translate_batch"]
    else:
        assert summary == [(1, "flash", "timeout", False), (2, "flash", "succeeded", False)]
        assert report["model"]["hedging"] == {"after_seconds": None, "copies": 0}
        assert argon.requests_of("translate_batch") == []
    assert sum(1 for r in records if r["hedge"]) == (1 if hedging else 0)
