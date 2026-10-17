"""The translation plugin package: what the built xpi contains.

The plugin's own logic is tested with ``node --test plugin/test/``; these
tests build the xpi with ``plugin/build.py`` and check the package as Zotero
receives it.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import urlparse

import pytest

from zotero_translate.settings import DEFAULT_PORT

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def xpi(tmp_path_factory) -> zipfile.ZipFile:
    output = tmp_path_factory.mktemp("xpi") / "zotero-translate.xpi"
    subprocess.run(
        [sys.executable, str(ROOT / "plugin" / "build.py"), "--output", str(output)], check=True
    )
    with zipfile.ZipFile(output) as package:
        yield package


def test_manifest_declares_a_bootstrap_plugin_for_zotero_7_to_10(xpi):
    manifest = json.loads(xpi.read("manifest.json"))
    zotero = manifest["applications"]["zotero"]
    assert zotero["strict_min_version"] == "6.999"
    assert zotero["strict_max_version"] == "10.*"
    assert "bootstrap.js" in xpi.namelist()
    # Zotero rejects a plugin without update_url; ours points at this machine,
    # so update checks never reach an outside host.
    assert urlparse(zotero["update_url"]).hostname == "localhost"


def test_default_service_address_is_the_services_default_port(xpi):
    prefs = xpi.read("prefs.js").decode("utf-8")
    urls = re.findall(r'pref\("extensions\.zotero-translate\.serviceUrl",\s*"([^"]+)"\)', prefs)
    assert urls == [f"http://127.0.0.1:{DEFAULT_PORT}"]
    assert DEFAULT_PORT not in (8916, 8890)  # old development line, upstream plugin


def test_settings_page_has_only_the_service_address(xpi):
    prefs = xpi.read("prefs.js").decode("utf-8")
    assert re.findall(r'pref\("([^"]+)"', prefs) == ["extensions.zotero-translate.serviceUrl"]
    page = xpi.read("content/preferences.xhtml").decode("utf-8")
    assert re.findall(r'preference="([^"]+)"', page) == ["extensions.zotero-translate.serviceUrl"]


def test_package_holds_no_credentials_or_translation_parameters(xpi):
    forbidden = re.compile(
        r"api[_-]?key|OPENAI|AI_DEFAULT_MODEL|Bearer|sk-[A-Za-z0-9]|gemini|temperature|"
        r"reasoning|concurrency",
        re.IGNORECASE,
    )
    for name in xpi.namelist():
        assert not forbidden.search(xpi.read(name).decode("utf-8", "replace")), name


def test_package_leaves_out_tests_and_dev_tools(xpi):
    assert all(not name.startswith(("test/", "dev/")) for name in xpi.namelist())
    assert "build.py" not in xpi.namelist()
