# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).parents[1]
PACKAGE = ROOT / "src" / "io_scene_softimage"


class PackageTests(unittest.TestCase):
    def test_manifest_targets_blender_52(self):
        manifest = (PACKAGE / "blender_manifest.toml").read_text()
        self.assertRegex(manifest, r'(?m)^id\s*=\s*"io_scene_softimage"$')
        self.assertRegex(manifest, r'(?m)^blender_version_min\s*=\s*"5\.2\.0"$')
        self.assertIn('"SPDX:GPL-3.0-or-later"', manifest)

    def test_extension_entry_points_exist(self):
        source = (PACKAGE / "__init__.py").read_text()
        self.assertIn("def register()", source)
        self.assertIn("def unregister()", source)


if __name__ == "__main__":
    unittest.main()
