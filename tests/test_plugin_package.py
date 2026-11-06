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


def test_the_only_zotero_setting_is_the_service_address(xpi):
    # The model profiles' form (M4 ticket 04) is sent to the service, not
    # kept in Zotero's settings: no field of it is bound to a preference.
    prefs = xpi.read("prefs.js").decode("utf-8")
    assert re.findall(r'pref\("([^"]+)"', prefs) == ["extensions.zotero-translate.serviceUrl"]
    page = xpi.read("content/preferences.xhtml").decode("utf-8")
    assert re.findall(r'preference="([^"]+)"', page) == ["extensions.zotero-translate.serviceUrl"]
    key_field = re.search(r"<html:input[^>]*\bid=\"zotero-translate-key\"[^>]*>", page).group(0)
    assert 'type="password"' in key_field
    for name in SETTINGS_PAGE:
        assert "Prefs.set" not in xpi.read(name).decode("utf-8"), name


# The settings page's files: the form of a model profile names its fields
# (ADR 0005); the key itself goes to the service once and is never in the package.
SETTINGS_PAGE = {"content/profiles.js", "content/preferences.js", "content/preferences.xhtml"}


FORBIDDEN = re.compile(
    r"OPENAI_|AI_DEFAULT_MODEL|ZT_CONCURRENCY|Bearer|sk-[A-Za-z0-9]|gemini|temperature|reasoning",
    re.IGNORECASE,
)

def test_package_holds_no_credentials_or_translation_parameters(xpi):
    profile_fields = re.compile(r"api[_-]?key|concurrency", re.IGNORECASE)
    assert SETTINGS_PAGE <= set(xpi.namelist())
    for name in xpi.namelist():
        text = xpi.read(name).decode("utf-8", "replace")
        assert not FORBIDDEN.search(text), name
        if name not in SETTINGS_PAGE:
            assert not profile_fields.search(text), name


def test_the_package_names_gemini_nowhere(xpi):
    # The scan above reads every file: the settings page's example for a
    # profile's name used to be let through it (it said DeepSeek Fast's
    # quality is below Gemini's); since the user's decision of 2026-11-06
    # the name field has no example, and nothing in the package says so.
    assert FORBIDDEN.search("例如 DeepSeek Fast（质量低于 Gemini）")
    for name in xpi.namelist():
        text = xpi.read(name).decode("utf-8", "replace")
        assert "质量低于" not in text, name
        assert "gemini" not in text.lower(), name


def test_package_leaves_out_tests_and_dev_tools(xpi):
    assert all(not name.startswith(("test/", "dev/")) for name in xpi.namelist())
    assert "build.py" not in xpi.namelist()


def test_package_has_no_test_switch(xpi):
    # The acceptance's test switch (scenario 7: quit Zotero before an
    # attachment's identity is written) runs through the dev bridge in the
    # test profile only (plugin/dev/acceptance/quit_before_identity.js); the
    # package neither holds it nor reads any test setting.
    switch = (ROOT / "plugin" / "dev" / "acceptance" / "quit_before_identity.js").read_text("utf-8")
    forbidden = re.compile(r"ZT_TEST_|zotero-translate-dev|zotero-translate acceptance|eAttemptQuit")
    assert forbidden.search(switch), "the pattern must match the switch itself"
    for name in xpi.namelist():
        assert not forbidden.search(xpi.read(name).decode("utf-8", "replace")), name


def test_settings_page_suggests_deepseek_fast_only(xpi):
    # User decision 2026-11-05 (M4 tickets 06 and 14): of Command Code's
    # DeepSeek models only DeepSeek Fast is offered; the form's examples name
    # it, and no longer the plain flash model. User decision 2026-11-06 (M5
    # ticket 02): the name field gives no example at all.
    page = xpi.read("content/preferences.xhtml").decode("utf-8")
    placeholders = dict(re.findall(r'<html:input id="([^"]+)"[^>]*\bplaceholder="([^"]*)"', page))
    assert placeholders["zotero-translate-model"] == "例如 deepseek/deepseek-v4.1-flash-fast"
    assert not re.search(r"deepseek-v4\.1-flash(?!-fast)", page)


def test_the_name_field_has_no_placeholder(xpi):
    page = xpi.read("content/preferences.xhtml").decode("utf-8")
    [name_input] = re.findall(r"<html:input[^>]*\bid=\"zotero-translate-name\"[^>]*>", page)
    assert "placeholder" not in name_input
    # No script puts a hint into it either.
    for name in ("content/preferences.js", "content/profiles.js"):
        script = xpi.read(name).decode("utf-8")
        assert not re.search(r"FIELDS\.name\)?\.placeholder|zotero-translate-name[^\n]*placeholder", script), name
