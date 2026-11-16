#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Build the Blender extension archive without external dependencies."""

from __future__ import annotations

from pathlib import Path
import re
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "src" / "io_scene_softimage"
EXTRA_FILES = ("LICENSE", "NOTICE", "README.md")


def main() -> None:
    manifest = (SOURCE / "blender_manifest.toml").read_text()
    extension_id = _manifest_string(manifest, "id")
    version = _manifest_string(manifest, "version")
    archive = ROOT / "dist" / f"{extension_id}-{version}.zip"
    archive.parent.mkdir(exist_ok=True)
    with ZipFile(archive, "w", ZIP_DEFLATED, compresslevel=9) as output:
        for path in sorted(SOURCE.rglob("*")):
            if (
                not path.is_file()
                or "__pycache__" in path.parts
                or path.suffix in {".pyc", ".pyo"}
            ):
                continue
            output.write(path, path.relative_to(SOURCE).as_posix())
        for name in EXTRA_FILES:
            output.write(ROOT / name, name)
    print(archive)


def _manifest_string(manifest: str, key: str) -> str:
    match = re.search(rf'(?m)^{re.escape(key)}\s*=\s*"([^"]+)"\s*$', manifest)
    if match is None:
        raise ValueError(f"Manifest is missing string field {key!r}")
    return match.group(1)


if __name__ == "__main__":
    main()
