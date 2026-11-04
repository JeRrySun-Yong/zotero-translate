"""The hedging switch through the translation service (M3 ticket 15; spec section 8, seam 2).

The service hedges unless it is started with ``--no-hedging``; the test-only
``ZT_TEST_HEDGE_AFTER_SECONDS`` replaces the 400-second threshold. A hedged
copy is told apart in the attempt's request records (``hedge``), and the
task report gives the threshold and the number of copies. The rows of the
decision record that need finer control of timing and budgets (2 sends at
most, the fourth send, a copy waiting for a slot, the error priority) are
tested on the pipeline model client in ``test_hedging.py``.
"""

import json
import time
from pathlib import Path

import pytest

from tests.conftest import EXCERPT
from tests.conftest import FAKE_API_KEY
from tests.conftest import launch_service
from tests.conftest import stop_service
from tests.conftest import write_env_file
from tests.fake_model import FAKE_DOSSIER
from tests.fake_model import FAKE_TERM_LIST
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


def _sends(records: list[dict], kind: str) -> list[tuple]:
    """(request, send, endpoint, result, hedge) of the records of ``kind``, by request and send."""
    chosen = sorted((r for r in records if r["kind"] == kind), key=lambda r: (r["request"], r["send"]))
    return [(r["request"], r["send"], r["endpoint"], r["result"], r["hedge"]) for r in chosen]


def _later(seconds: float, content: str):
    def respond(body: dict) -> str:
        time.sleep(seconds)
        return content

    return respond


# 「对冲哪些请求」 (a review) and 「副模型处理的请求（论文档案、术语清单）是否对冲」 (no).
def test_a_hung_review_gets_a_copy_and_a_slow_dossier_and_term_list_get_none(start_service):
    with FakeModel() as flash, FakeModel() as argon:
        flash.inject_error("timeout", kind="review")  # the first review hangs
        argon.respond("dossier", _later(HEDGE * 2, json.dumps(FAKE_DOSSIER, ensure_ascii=False)))
        argon.respond("term_list", _later(HEDGE * 2, json.dumps(FAKE_TERM_LIST, ensure_ascii=False)))
        service = start_service(
            flash, argon=argon, request_timeout=SMALL_TIMEOUT * 2, env={HEDGE_AFTER_SETTING: str(HEDGE)}
        )

        final = service.wait(service.submit(EXCERPT))

        assert final["state"] == "succeeded", final["error"]
        records = _records(service, final["task_id"])

    [hung] = [r for r in records if r["kind"] == "review" and r["result"] != "succeeded"]
    assert [s[1:] for s in _sends(records, "review") if s[0] == hung["request"]] == [
        (1, "flash", "cancelled_for_winner", False),
        (2, "argon", "succeeded", True),
    ]
    for kind in ("dossier", "term_list"):
        [send] = _sends(records, kind)
        assert send[1:] == (1, "argon", "succeeded", False)
    assert flash.requests_of("dossier") == flash.requests_of("term_list") == []
    assert len(argon.requests_of("dossier")) == len(argon.requests_of("term_list")) == 1


# 「与"连续 2 次超时后改发 argon"的关系」: with hedging on, one flash timeout is
# enough; the next send goes straight to argon (here the threshold is past
# the request timeout, so no copy goes out first).
def test_after_one_flash_timeout_the_next_send_goes_to_argon(start_service):
    with FakeModel() as flash, FakeModel() as argon:
        flash.fail_same_request(["timeout"], kind="translate_batch")
        service = start_service(
            flash, argon=argon, request_timeout=SMALL_TIMEOUT, env={HEDGE_AFTER_SETTING: "60"}
        )

        final = service.wait(service.submit(EXCERPT))

        assert final["state"] == "succeeded", final["error"]
        records = _records(service, final["task_id"])
        report = service.report(final["task_id"])

    [timed_out] = [r for r in records if r["result"] == "timeout"]
    assert [s[1:] for s in _sends(records, "translation_batch") if s[0] == timed_out["request"]] == [
        (1, "flash", "timeout", False),
        (2, "argon", "succeeded", False),
    ]
    assert report["model"]["forwarded"] == {"requests": 1, "pages": [1]}


# 「单模型配置是否对冲」: no; a timeout is resent on the same endpoint.
def test_a_single_model_configuration_sends_no_copy(tmp_path):
    with FakeModel() as model:
        model.inject_error("timeout", kind="translate_batch")
        same = write_env_file(tmp_path, model.base_url, FAKE_API_KEY, None, SMALL_TIMEOUT)
        service = launch_service(
            model,
            tmp_path,
            request_timeout=SMALL_TIMEOUT,
            argon_env_file=same,  # the same address and key: one endpoint
            env={HEDGE_AFTER_SETTING: str(HEDGE)},
        )
        try:
            final = service.wait(service.submit(EXCERPT))
            assert final["state"] == "succeeded", final["error"]
            records = _records(service, final["task_id"])
            report = service.report(final["task_id"])
        finally:
            stop_service(service)

    [timed_out] = [r for r in records if r["result"] == "timeout"]
    assert [s[1:] for s in _sends(records, "translation_batch") if s[0] == timed_out["request"]] == [
        (1, "flash", "timeout", False),
        (2, "flash", "succeeded", False),
    ]
    assert not any(r["hedge"] for r in records)
    assert report["model"]["hedging"] == {"after_seconds": None, "copies": 0}
