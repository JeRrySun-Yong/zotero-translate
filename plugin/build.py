"""Build the translation plugin xpi from plugin/addon/.

Usage: python plugin/build.py [--output PATH]

The default output is plugin/dist/zotero-translate.xpi, which git ignores.
"""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path

ADDON = Path(__file__).resolve().parent / "addon"
DEFAULT_OUTPUT = Path(__file__).resolve().parent / "dist" / "zotero-translate.xpi"


def build(output: Path) -> Path:
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as xpi:
        for file in sorted(ADDON.rglob("*")):
            if file.is_file() and file.name != ".DS_Store":
                xpi.write(file, file.relative_to(ADDON).as_posix())
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    print(build(parser.parse_args().output))


if __name__ == "__main__":
    main()
