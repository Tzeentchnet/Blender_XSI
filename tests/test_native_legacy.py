# SPDX-License-Identifier: GPL-3.0-or-later
"""XSI 3.x native layouts (geometry, F-curves, envelopes) on synthetic byte streams."""

from __future__ import annotations

import struct
from types import SimpleNamespace
import unittest

import numpy as np

from io_scene_softimage import native_import
from io_scene_softimage.native_scn import animation, envelope, mesh


def cnum(value: int, width: int | None = None) -> bytes:
    variants = mesh._cnum_variants(value)
    if width is None:
        return variants[0]
    return next(v for v in variants if len(v) == width)


def cdouble(value: float) -> bytes:
    if value == 0.0:
        return b"\xff"
    if value == 1.0:
        return b"\xfe"
    return b"\xfc" + struct.pack("<d", value)


def snum(value: int) -> bytes:
    if value >= 0:
        return cnum(value)
    if value >= -8:
        return bytes([0xA0 | (value & 0xF)])
    raise ValueError(value)


def obj(data: bytes, start: int = 0):
    return SimpleNamespace(start=start, body=start, end=len(data))


class LegacyGeometryTests(unittest.TestCase):
    def _quad(self, wide: bool) -> bytes:
        # Two triangles sharing an edge: 4 points, 5 edges, 2 polygons, 6 nodes.
        points = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)]
        node_vertex = [0, 1, 2, 0, 2, 3]
        slots = [0, 0, 0, 1, 1, 0]
        width = 3 if wide else None            # writers do not always use the shortest form
        blob = cnum(4) + cnum(6) + cnum(2) + cnum(6) + b"\x21\x07\x10\x00\x00"
        blob += b"\xf1" + cnum(4, width) + b"\xf0" + cnum(4, width) + cnum(6, width) + b"\xf0"
        blob += b"".join(cnum(v) + bytes([s]) for v, s in zip(node_vertex, slots))
        blob += cnum(4) + cnum(4) + cnum(64)
        blob += b"".join(cdouble(c) for p in points for c in p)
        blob += cnum(2) + cnum(6) + b"\xf0\xf0\x00"                        # edge table (skipped)
        blob += cnum(6) + cnum(2) + cnum(8) + cnum(0)                       # polygon list header
        blob += cnum(3) + cnum(0) + cnum(1) + cnum(2) + cnum(3) + cnum(3) + cnum(4) + cnum(5)
        return blob

    def test_decodes_sequential_layout(self):
        for wide in (False, True):
            data = self._quad(wide)
            result = mesh.read_polymsh_v3(data, 0, len(data))
            self.assertEqual(result.layout, "3.x")
            self.assertEqual(result.polygons, [[0, 1, 2], [0, 2, 3]])
            self.assertEqual(result.nodes, [[0, 1, 2], [3, 4, 5]])
            self.assertEqual(result.points[2], (1.0, 1.0, 0.0))
            self.assertTrue(result.edges_ok)

    def test_decodes_bmsh_layout(self):
        points = [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
                  (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)]
        nodes = [
            (0, 1, 5), (1, 2, 4), (2, 3, 3),
            (0, -3, 2), (2, 4, 1), (3, 5, 0),
        ]
        edges = [
            (0, 0, 0, 0), (0, 1, 0, 0), (1, 2, 0, 0),
            (2, 0, 0, 0), (2, 3, 0, 0), (3, 0, 0, 0),
        ]
        payload = b"IHSM2010" + struct.pack("<I", 0)
        payload += cnum(6) + cnum(10) + cnum(10)
        payload += b"".join(snum(value) for node in nodes for value in node)
        payload += cnum(4) + cnum(10) + cnum(10)
        payload += b"".join(cdouble(value) for point in points for value in point)
        payload += b"".join(cnum(value) for value in [0, 0, 1] * 4)
        payload += cnum(6) + cnum(10) + cnum(10)
        payload += b"".join(cnum(value) for edge in edges for value in edge)
        payload += cnum(2) + cnum(10) + cnum(10)
        for polygon in ([0, 1, 2], [3, 4, 5]):
            payload += cnum(3) + cnum(4) + cnum(4)
            payload += b"".join(cnum(value) for value in polygon)
        data = b"HSMB0010" + struct.pack("<I", len(payload)) + payload

        result = mesh.read_polymsh_bmsh(data, obj(data))

        self.assertEqual(result.layout, "BMSH")
        self.assertEqual(result.counts, (4, 6, 2, 6))
        self.assertEqual(result.polygons, [[0, 1, 2], [0, 2, 3]])
        self.assertEqual(result.nodes, [[0, 1, 2], [3, 4, 5]])
        self.assertTrue(result.edges_ok)


class LegacyAnimationTests(unittest.TestCase):
    def test_reads_fcurve_keys(self):
        clamp = struct.pack("<3d", 2.5, -1.7976931348623157e308, 1.7976931348623157e308)
        keys = struct.pack("<7dI", 0.0, 1.0, 1.0, -0.1, 1.0, 0.1, 1.0, 3)
        keys += struct.pack("<7dI", 1.0, 5.0, 5.0, 0.9, 4.0, 1.1, 6.0, 2)
        data = b"\x00" * 10 + b"UC1010" + b"\x00" * 6 + clamp + struct.pack("<II", 3, 2) + keys
        curve = animation.read_fcurve(data, obj(data))
        self.assertEqual(curve.default, 2.5)
        self.assertEqual(curve.times, [0.0, 1.0])
        self.assertEqual(curve.values, [1.0, 5.0])
        self.assertEqual(curve.right[0], (0.1, 1.0))
        self.assertEqual(curve.interpolation, ["CUBIC", "LINEAR"])

    def test_detects_ntsc_frame_rate(self):
        times = [frame / 29.97 for frame in (0, 7, 14, 22, 39)]
        self.assertEqual(native_import.detect_frame_rate(times), 29.97)
        self.assertEqual(native_import.detect_frame_rate([f / 30.0 for f in (3, 10, 17)]), 30.0)
        self.assertEqual(native_import.detect_frame_rate([]), 30.0)


class LegacyEnvelopeTests(unittest.TestCase):
    def test_run_length_weights(self):
        # 2 deformers x 3 points: [[100, 0, 50], [0, 100, 50]]
        def run(skip, values):
            return cnum((skip << 16) | len(values)) + struct.pack("<%df" % len(values), *values)

        body = cnum(3) + b"\x00" * 12 + run(0, [100.0]) + run(1, [50.0]) + run(1, [100.0, 50.0])
        matrix = envelope.decode_weights(body, obj(body), 2, 3)
        np.testing.assert_allclose(matrix, [[100, 0, 50], [0, 100, 50]])

    def test_bind_matrix(self):
        values = [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 2.0, 37.5, 0.75, 1]
        data = b"\x00" * 80 + b"".join(cdouble(float(v)) for v in values)
        matrix = envelope.bind_matrix(data, obj(data))
        np.testing.assert_allclose(matrix[:3, 3], [2.0, 37.5, 0.75])
        np.testing.assert_allclose(matrix[3], [0, 0, 0, 1])

    def test_associates_bmsh_target_and_deformer_bind_matrices(self):
        def matrix_blob(translation):
            values = [
                1, 0, 0, 0,
                0, 1, 0, 0,
                0, 0, 1, 0,
                *translation, 1,
            ]
            return b"\x00" * 72 + b"".join(cdouble(float(v)) for v in values)

        data = b""
        objects = []
        root = SimpleNamespace()
        group = SimpleNamespace()
        for handle, parent, translation in (
            (20, root, (10.0, 20.0, 30.0)),
            (21, group, (1.0, 2.0, 3.0)),
            (22, group, (4.0, 5.0, 6.0)),
        ):
            blob = matrix_blob(translation)
            objects.append(SimpleNamespace(
                handle=handle,
                parent=parent,
                body=len(data),
                end=len(data) + len(blob),
            ))
            data += blob
        primitive = SimpleNamespace(handle=10, parent=root, body=0, end=0)
        op = SimpleNamespace(refs=[
            (primitive.handle, 0, 0),
            (objects[0].handle, 0, 0),
            (objects[1].handle, 0, 0),
            (objects[2].handle, 0, 0),
        ])
        stream = SimpleNamespace(
            data=data,
            objects=[op],
            by_handle={
                primitive.handle: primitive,
                **{item.handle: item for item in objects},
            },
        )

        target, deformers = envelope._bmsh_bind_matrices(stream, primitive, 2)

        np.testing.assert_allclose(target[:3, 3], [10.0, 20.0, 30.0])
        np.testing.assert_allclose(deformers[0][:3, 3], [1.0, 2.0, 3.0])
        np.testing.assert_allclose(deformers[1][:3, 3], [4.0, 5.0, 6.0])


if __name__ == "__main__":
    unittest.main()
