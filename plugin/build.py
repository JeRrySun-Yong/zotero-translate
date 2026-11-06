"""Build the translation plugin xpi from plugin/addon/.

Usage: python plugin/build.py [--output PATH]

The default output is plugin/dist/zotero-translate.xpi, which git ignores.
Next to the xpi the build writes update.json, Zotero's update manifest: the
files a GitHub Release carries (the manifest's update_url points at the
latest Release's update.json). Its id, version and Zotero versions are the
manifest's own; the download link is the project's Release for that version.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

ADDON = Path(__file__).resolve().parent / "addon"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "dist" / "zotero-translate.xpi"
RELEASE_FILE = "zotero-translate.xpi"  # the xpi's name among the Release's files
UPDATE_FILE = "update.json"


def update_manifest(xpi: Path, manifest: dict) -> dict:
    """Zotero's update manifest for the built ``xpi``, from the plugin ``manifest``."""
    zotero = manifest["applications"]["zotero"]
    release = f"{manifest['homepage_url']}/releases/download/v{manifest['version']}"
    return {
        "addons": {
            zotero["id"]: {
                "updates": [
                    {
                        "version": manifest["version"],
                        "update_link": f"{release}/{RELEASE_FILE}",
                        "update_hash": "sha256:" + hashlib.sha256(xpi.read_bytes()).hexdigest(),
                        "applications": {
                            "zotero": {
                                "strict_min_version": zotero["strict_min_version"],
                                "strict_max_version": zotero["strict_max_version"],
                            }
                        },
                    }
                ]
            }
        }
    }


def build(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as xpi:
        for file in sorted(ADDON.rglob("*")):
            if file.is_file() and file.name != ".DS_Store":
                xpi.write(file, file.relative_to(ADDON).as_posix())
    manifest = json.loads((ADDON / "manifest.json").read_text("utf-8"))
    (output.parent / UPDATE_FILE).write_text(
        json.dumps(update_manifest(output, manifest), indent=2) + "\n", encoding="utf-8"
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    print(build(parser.parse_args().output))


if __name__ == "__main__":
    main()
