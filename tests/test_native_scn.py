# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

import struct
import unittest

from io_scene_softimage.native_scn import materials, records


class _EmptyCompoundFile:
    @staticmethod
    def exists(_path):
        return False


class NativeSceneTests(unittest.TestCase):
    def test_material_reader_handles_files_without_source_streams(self):
        self.assertEqual(materials.read(_EmptyCompoundFile()), ({}, {}, []))

    def test_detects_shifted_object_marker(self):
        def object_block(handle):
            payload = b"\x03" + bytes([0xF0 | handle]) + struct.pack("<HH", 1, 2) + b"\0"
            return b"\x04" + struct.pack("<I", len(payload)) + payload

        data = records.MAGIC + b"\0" * 8 + object_block(1) + object_block(2)
        self.assertEqual(
            records.detect_markers(data),
            {"list": 3, "object": 4},
        )

    def test_accepts_older_class_definition_tag(self):
        self.assertIsNotNone(records.CLASSDEF.search(b"\x05\x00\x01\x00POT 0010"))


if __name__ == "__main__":
    unittest.main()
