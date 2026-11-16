# SPDX-License-Identifier: GPL-3.0-or-later
"""Coordinate conversion, batched transform math, skeleton planning, image lookup and primitives."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import numpy as np

from io_scene_softimage import convert, images, primitives, skeleton
from io_scene_softimage.model import Scene, SceneObject, Transform


class ConvertTests(unittest.TestCase):
    def test_euler_matrices_match_rotation_order(self):
        angles = np.array([[30.0, 45.0, 60.0]])
        rx, ry, rz = (convert.euler_matrices(np.array([[a if i == j else 0 for j in range(3)]]))[0]
                      for i, a in enumerate(angles[0]))
        np.testing.assert_allclose(convert.euler_matrices(angles, "XYZ")[0], rz @ ry @ rx, atol=1e-12)
        np.testing.assert_allclose(convert.euler_matrices(angles, "ZYX")[0], rx @ ry @ rz, atol=1e-12)

    def test_decompose_round_trip_and_quaternion_continuity(self):
        rng = np.random.default_rng(3)
        angles = rng.uniform(-180, 180, size=(50, 3))
        rotations = convert.euler_matrices(angles)
        matrices = convert.compose(rng.normal(size=(50, 3)), rotations, rng.uniform(0.5, 2, size=(50, 3)))
        locations, quaternions, scales = convert.decompose(matrices)
        rebuilt = convert.compose(locations, convert.quaternion_matrices(quaternions), scales)
        np.testing.assert_allclose(rebuilt, matrices, atol=1e-9)
        smooth = convert.make_continuous(np.array([[1, 0, 0, 0], [-1, 0, 0, 0.01]], dtype=float))
        self.assertGreater(float(np.dot(smooth[0], smooth[1])), 0)

    def test_matrix_eulers_inverts_euler_matrices(self):
        angles = np.radians(np.array([[10.0, -20.0, 30.0], [80.0, 5.0, -170.0]]))
        for order in ("XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"):
            back = convert.matrix_eulers(convert.euler_matrices(np.degrees(angles), order), order)
            np.testing.assert_allclose(
                convert.euler_matrices(np.degrees(back), order),
                convert.euler_matrices(np.degrees(angles), order), atol=1e-9, err_msg=order,
            )

    def test_component_remap_equals_matrix_conjugation(self):
        conv = convert.AxisConversion.from_up_axis("Y", 2.0)
        position, rotation, scale = (1.0, 2.0, 3.0), (10.0, 20.0, 30.0), (1.0, 2.0, 3.0)
        expected = conv.transform(convert.transform_matrix(position, rotation, scale))
        loc = [0.0] * 3
        rot = [0.0] * 3
        scl = [0.0] * 3
        for axis in range(3):
            k, f = conv.channel("location", axis)
            loc[k] = position[axis] * f
            k, f = conv.channel("rotation", axis)
            rot[k] = rotation[axis] * f
            k, _f = conv.channel("scale", axis)
            scl[k] = scale[axis]
        order = conv.euler_order("XYZ")
        self.assertEqual(order, "XZY")
        actual = convert.transform_matrix(loc, rot, scl, order)
        np.testing.assert_allclose(actual, expected, atol=1e-9)
        np.testing.assert_allclose(conv.points(np.array([[1.0, 2.0, 3.0]])), [[2.0, -6.0, 4.0]])


class SkeletonTests(unittest.TestCase):
    def _biped(self):
        scene = Scene("x.xsi", "0300", "txt", 32)
        root = SceneObject("MAXSceneRoot")
        pelvis = SceneObject("Bip01_Pelvis", transform=Transform(position=(0.0, 10.0, 0.0)))
        thigh = SceneObject("Bip01_L_Thigh", transform=Transform(position=(1.0, 0.0, 0.0)))
        helper = SceneObject("Knee_Helper", transform=Transform(position=(4.0, 0.0, 0.0)))
        foot = SceneObject("Bip01_L_Foot", transform=Transform(position=(3.0, 0.0, 0.0)))
        weapon = SceneObject("Weapon", kind="MESH")
        root.children = [pelvis]
        pelvis.children = [thigh]
        thigh.children = [helper, weapon]
        helper.children = [foot]
        scene.objects = [root]
        return scene, (root, pelvis, thigh, helper, foot, weapon)

    def test_biped_names_seed_one_armature_and_promote_intermediate_nodes(self):
        scene, (root, pelvis, thigh, helper, foot, weapon) = self._biped()
        plan = skeleton.plan(scene, "AUTO", convert.AxisConversion.from_up_axis("Y"))
        self.assertEqual(len(plan.groups), 1)
        group = plan.groups[0]
        self.assertIs(group.parent, root)
        self.assertEqual([n.name for n in group.nodes], ["Bip01_Pelvis", "Bip01_L_Thigh", "Knee_Helper",
                                                        "Bip01_L_Foot"])
        self.assertNotIn(id(weapon), plan.group_of)
        self.assertAlmostEqual(plan.length[id(thigh)], 4.0)      # child along local X
        self.assertGreater(plan.length[id(foot)], 0.0)           # leaf: inherited fallback
        self.assertEqual(skeleton.plan(scene, "NONE", convert.AxisConversion.from_up_axis("Y")).groups, [])

    def test_model_containers_are_not_bones(self):
        scene, (root, *_rest) = self._biped()
        root.name = "ValveBiped"
        root.properties["softimage_model"] = True
        plan = skeleton.plan(scene, "AUTO", convert.AxisConversion.from_up_axis("Y"))
        self.assertNotIn(id(root), plan.group_of)


class ImageResolverTests(unittest.TestCase):
    def test_reroots_stored_windows_paths_and_searches_by_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "materialsrc" / "Models" / "Alyx").mkdir(parents=True)
            (root / "materialsrc" / "Models" / "Alyx" / "thigh.tga").write_bytes(b"x")
            (root / "materialsrc" / "Other").mkdir(parents=True)
            (root / "materialsrc" / "Other" / "leg.png").write_bytes(b"x")
            scene_dir = root / "models" / "Humans" / "dotXSI"
            scene_dir.mkdir(parents=True)
            resolver = images.ImageResolver(scene_dir / "female.xsi", search=True)
            found = resolver.resolve("U:\\hl2\\hl2\\materialsrc\\Models\\Alyx\\thigh.tga")
            self.assertEqual(found, (root / "materialsrc" / "Models" / "Alyx" / "thigh.tga").resolve())
            self.assertEqual(resolver.resolve("U:\\elsewhere\\leg.tga").name, "leg.png")
            self.assertIsNone(images.ImageResolver(scene_dir / "f.xsi", search=False).resolve("U:\\x\\y.tga"))

    def test_split_stored_path(self):
        self.assertEqual(images.split_stored_path("U:\\a\\b.tga"), ["a", "b.tga"])
        self.assertEqual(images.split_stored_path("[Project Path]\\Pictures\\c.pic"), ["Pictures", "c.pic"])


class PrimitiveTests(unittest.TestCase):
    def test_primitives_are_closed_where_expected(self):
        for kind in ("cube", "sphere", "cylinder", "cone", "torus", "icosahedron", "dodecahedron"):
            points, faces = primitives.generate(kind, {})
            self.assertTrue(faces, kind)
            edges = {}
            for face in faces:
                for a, b in zip(face, face[1:] + face[:1]):
                    key = (min(a, b), max(a, b))
                    edges[key] = edges.get(key, 0) + 1
            self.assertTrue(all(count == 2 for count in edges.values()), kind)
            self.assertTrue(all(0 <= i < len(points) for f in faces for i in f), kind)


if __name__ == "__main__":
    unittest.main()
