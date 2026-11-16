# SPDX-License-Identifier: GPL-3.0-or-later
"""dotXSI 3.0 templates found in production files (3ds Max MAX2XSI exports, XSI 2.x - 3.5 exports)."""

from __future__ import annotations

import unittest

import numpy as np

from io_scene_softimage import dotxsi


HEADER = "xsi 0300txt 0032\n"


def model(name: str, body: str) -> str:
    return f"SI_Model MDL-{name} {{ SI_Transform SRT-{name} {{ 1,1,1, 0,0,0, 0,0,0, }} {body} }}\n"


class LegacyMaterialTests(unittest.TestCase):
    def test_si_material_and_texture2d(self):
        texture = ['"U:\\hl2\\materialsrc\\Models\\Alyx\\thigh.tga"', "3", "256", "128",
                   "0", "255", "0", "127", "0", "2", "3", "0", "1", "1.0", "1.0", "0.25", "0.5"]
        texture += ["1", "0", "0", "0", "0", "1", "0", "0", "0", "0", "1", "0", "0", "0", "0", "1"]
        texture += ["3", "1.0", "0.0", "1.0", "0.0", "0.0", "0.0", "0.0"]
        scene = dotxsi.loads(
            HEADER
            + "SI_MaterialLibrary { 1, SI_Material Skin { 0.5,0.4,0.3,1.0, 78.75, 0.1,0.1,0.1, 0,0,0, 2, "
            + "0.2,0.2,0.2, SI_Texture2D { " + ", ".join(texture) + ", } } }\n"
        )
        material = scene.materials["Skin"]
        self.assertEqual(material.shading_model, "PHONG")
        self.assertEqual(material.diffuse, (0.5, 0.4, 0.3, 1.0))
        self.assertAlmostEqual(material.parameters["specular_power"], 78.75)
        layer = material.textures[0]
        # Single backslashes are kept: "\t" must not become a tab.
        self.assertEqual(layer.image, "U:\\hl2\\materialsrc\\Models\\Alyx\\thigh.tga")
        self.assertEqual(layer.mapping, "UV")
        self.assertEqual(layer.size, (256, 128))
        self.assertEqual(layer.repeat, (2.0, 3.0))
        self.assertEqual(layer.alternate, (False, True))
        self.assertEqual(layer.offset, (0.25, 0.5))
        self.assertEqual(layer.weights["diffuse"], 1.0)


class AnimationTests(unittest.TestCase):
    def test_kine_local_channels_cubic_tangents_and_mixer_actions(self):
        text = HEADER + model("Bip01", "") + (
            "SI_Model MDL-Root { XSI_Mixer Root { 0,1,0,0,0,0, XSI_Action MAX_Action { 0.0, 1.0, 0, "
            'SI_FCurve { "Bip01", "Bip01.kine.local.rotz", "LINEAR", 1, 1, 2, 0, 10, 30, 20, } '
            'SI_FCurve { "Bip01", "Bip01.kine.local.posx", "CUBIC", 1, 5, 2, '
            "0, 1, 0, 0, 10, 0,  30, 5, -10, 0, 0, 0, } } } }\n"
        )
        scene = dotxsi.loads(text)
        bip = scene.objects[0]
        rot = next(c for c in bip.animation if c.prop == "rotation")
        self.assertEqual((rot.index, rot.frames.tolist(), rot.values.tolist()), (2, [0.0, 30.0], [10.0, 20.0]))
        pos = next(c for c in bip.animation if c.prop == "location")
        self.assertEqual(pos.interpolation, "CUBIC")
        self.assertEqual(pos.handles_right.tolist()[0], [10.0, 1.0])
        self.assertEqual(pos.handles_left.tolist()[1], [20.0, 5.0])
        self.assertAlmostEqual(float(pos.evaluate(np.array([15.0]))[0]), 3.0, places=5)
        self.assertEqual([a.name for a in scene.actions], ["MAX_Action"])
        self.assertEqual((scene.actions[0].start, scene.actions[0].end), (0.0, 24.0))

    def test_radians_scene_converts_rotations(self):
        scene = dotxsi.loads(
            HEADER + "SI_Angle { 1, }\n"
            "SI_Model MDL-A { SI_Transform SRT-A { 1,1,1, 3.14159265358979, 0, 0, 0,0,0, } }\n"
        )
        self.assertAlmostEqual(scene.objects[0].transform.rotation[0], 180.0, places=6)

    def test_channel_names(self):
        cases = {
            "XPOS": ("location", 0), "YROT": ("rotation", 1), "ZSCL": ("scale", 2),
            "TRANSLATION-X": ("location", 0), "Bip01.kine.local.scly": ("scale", 1),
            "kine.local.posz": ("location", 2), "NODEVIS": ("visibility", 0), "SHPANIM-1": ("shape_animation", 0),
        }
        for channel, expected in cases.items():
            self.assertEqual(dotxsi.resolve_channel(channel), expected, channel)


class MeshTests(unittest.TestCase):
    def test_indexed_shape_animation(self):
        shape = '3, "ORDERED", 3, "POSITION", 0,0,0, 1,0,0, 0,1,0, 1, "NORMAL", 0,0,1, 3, "TEX_COORD_UV", 0,0, 1,0, 0,1,'
        frame = '1, "INDEXED", 3, "POSITION", 0, 0,0,1, 1, 1,0,1, 2, 0,1,1,'
        text = HEADER + model("Tri", (
            "SI_Mesh MSH-Tri { SI_Shape SHP-Tri-ORG { " + shape + " } "
            'SI_TriangleList { 1, "NORMAL|TEX_COORD_UV", "Mat", 0,1,2, 0,0,0, 0,1,2, } '
            'SI_ShapeAnimation SHPANIM-Tri { "LINEAR", 2, '
            "SI_Shape SHP-Tri-0 { " + frame + " } SI_Shape SHP-Tri-1 { " + frame + " } } }"
        )) + (
            'SI_FCurve Tri-SHPANIM-1 { "Tri", "SHPANIM-1", "LINEAR", 1, 1, 2, 1, 0, 2, 1, }\n'
        )
        scene = dotxsi.loads(text)
        tri = scene.objects[0]
        mesh = tri.mesh
        self.assertEqual(mesh.polygons, [[0, 1, 2]])
        self.assertEqual(mesh.uv_layers["UV"].tolist(), [[0, 0], [1, 0], [0, 1]])
        self.assertEqual(len(mesh.shape_keys), 2)
        self.assertEqual(mesh.shape_keys[1].points[:, 2].tolist(), [1.0, 1.0, 1.0])
        self.assertEqual(tri.animation[0].prop, "shape_animation")

    def test_polygons_with_missing_points_are_dropped(self):
        text = HEADER + model("Q", (
            'SI_Mesh MSH-Q { SI_Shape SHP-Q { 1, "ORDERED", 3, "POSITION", 0,0,0, 1,0,0, 0,1,0, } '
            'SI_PolygonList { 2, "", "M", 6, 3, 3, 0,1,2, 0,1,7, } }'
        ))
        scene = dotxsi.loads(text)
        self.assertEqual(scene.objects[0].mesh.polygons, [[0, 1, 2]])
        self.assertTrue(any("dropped 1 polygon" in w for w in scene.warnings))

    def test_cluster_custom_pset_and_infinite_light(self):
        text = HEADER + (
            "SI_Model MDL-cube { "
            'SI_Mesh MSH-cube { SI_Shape SHP { 1, "ORDERED", 3, "POSITION", 0,0,0, 1,0,0, 0,1,0, } '
            'SI_TriangleList { 1, "", "M", 0,1,2, } } '
            'SI_Cluster Point { "cube", "AVERAGE", "", 2, 0, 2, } '
            'XSI_CustomPSet cube.Info { "NODE", 1, "Subdivision","Small Integer Number",2, } '
            "SI_Light cube.light { 3, 1,1,1, 1,1,1, -45, 35.26, -15, } }\n"
        )
        cube = dotxsi.loads(text).objects[0]
        self.assertEqual(cube.mesh.clusters["Point"][1].tolist(), [0, 2])
        self.assertEqual(cube.properties["Info.Subdivision"], 2)
        self.assertEqual(cube.light.kind, "SUN")
        self.assertEqual(cube.transform.rotation, (-45.0, 35.26, -15.0))


class ParserTests(unittest.TestCase):
    def test_atoms_with_hash_are_not_comments(self):
        scene = dotxsi.loads(
            HEADER + "SI_MaterialLibrary { 1, SI_Material Material_#1 { 1,1,1,1, 50, 0,0,0, 0,0,0, 2, 0,0,0, } }\n"
        )
        self.assertIn("Material_#1", scene.materials)

    def test_reports_unclosed_template(self):
        with self.assertRaisesRegex(dotxsi.DotXSIError, "Unclosed SI_Model"):
            dotxsi.loads(HEADER + "SI_Model MDL-A { SI_Null A { }\n")


if __name__ == "__main__":
    unittest.main()
