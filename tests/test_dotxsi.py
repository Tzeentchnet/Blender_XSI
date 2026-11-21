# SPDX-License-Identifier: GPL-3.0-or-later

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from io_scene_softimage import dotxsi


FIXTURES = Path(__file__).parent / "fixtures"


class DotXSITests(unittest.TestCase):
    def test_parses_mesh_hierarchy_materials_envelopes_and_animation(self):
        scene = dotxsi.load(FIXTURES / "basic.xsi")

        self.assertEqual(scene.format_version, "0360")
        self.assertEqual(scene.name, "Example")
        self.assertEqual((scene.frame_start, scene.frame_end, scene.frame_rate), (1, 10, 24))
        self.assertEqual(scene.coordinate_system, (1, 0, 1, 0, 2, 5))
        self.assertEqual(scene.images["checker_image"], "checker.png")

        material = scene.materials["DefaultLib.Red"]
        self.assertEqual(material.diffuse, (0.8, 0.1, 0.05, 1.0))
        self.assertEqual(material.image, "checker_image")

        quad = scene.objects[0]
        self.assertEqual(quad.name, "Quad")
        self.assertEqual(quad.transform.position, (1.0, 2.0, 3.0))
        self.assertEqual(quad.children[0].kind, "BONE")
        self.assertEqual(len(quad.animation), 1)
        self.assertEqual(quad.animation[0].keys, [(1.0, 1.0), (10.0, 5.0)])

        mesh = quad.mesh
        self.assertIsNotNone(mesh)
        assert mesh is not None
        self.assertEqual(mesh.polygons, [[0, 1, 2, 3]])
        self.assertEqual(len(mesh.corner_normals), 4)
        self.assertEqual(
            mesh.uv_layers["UV0"].tolist(), [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
        )
        self.assertEqual(len(mesh.color_layers["COLOR"]), 4)
        self.assertEqual(mesh.face_materials, ["DefaultLib.Red"])
        vertices, weights = mesh.envelope_weights["Bone"]
        self.assertEqual(vertices.tolist(), [0, 1])
        self.assertEqual(weights.tolist(), [1.0, 0.5])

        self.assertEqual(scene.objects[1].kind, "CAMERA")
        self.assertEqual(scene.objects[2].kind, "LIGHT")
        self.assertEqual(scene.objects[2].light.intensity, 2.0)
        self.assertEqual(scene.warnings, [])

    def test_accepts_bom_comments_and_semicolons(self):
        scene = dotxsi.loads(
            "\ufeffxsi 0300txt 0064\n"
            "# comment\n"
            "SI_Scene Test { \"FRAMES\"; 0; 1; 30; }\n"
        )
        self.assertEqual(scene.float_bits, 64)
        self.assertEqual(scene.frame_rate, 30)

    def test_rejects_binary_dotxsi_with_actionable_error(self):
        with self.assertRaisesRegex(dotxsi.DotXSIError, "Re-export.*ASCII"):
            dotxsi.loads("xsi 0360bin 0032\n")

    def test_rejects_truncated_polygon_data(self):
        text = (
            "xsi 0360txt 0032\n"
            "SI_Model MDL-Bad {\n"
            " SI_Mesh MSH-Bad {\n"
            "  SI_Shape SHP-Bad { 1; \"ORDERED\"; 3; \"POSITION\";"
            " 0;0;0; 1;0;0; 0;1;0; }\n"
            "  SI_PolygonList Bad { 1; \"\"; \"\"; 3; 3; 0; 1; }\n"
            " }\n"
            "}\n"
        )
        with self.assertRaisesRegex(dotxsi.DotXSIError, "truncated"):
            dotxsi.loads(text)

    def test_reads_triangle_lists(self):
        scene = dotxsi.loads(
            "xsi 0300txt 0032\n"
            "SI_Model MDL-Triangles {\n"
            " SI_Mesh MSH-Triangles {\n"
            "  SI_Shape SHP-Triangles { 2; \"ORDERED\";"
            "   4; \"POSITION\"; 0;0;0; 1;0;0; 1;1;0; 0;1;0;"
            "   6; \"NORMAL\"; 0;0;1; 0;0;1; 0;0;1; 0;0;1; 0;0;1; 0;0;1; }\n"
            "  SI_TriangleList Triangles { 2; \"NORMAL\"; \"Material\";"
            "   0;1;2; 0;2;3; 0;1;2;3;4;5; }\n"
            " }\n"
            "}\n"
        )
        mesh = scene.objects[0].mesh
        self.assertIsNotNone(mesh)
        assert mesh is not None
        self.assertEqual(mesh.polygons, [[0, 1, 2], [0, 2, 3]])
        self.assertEqual(mesh.face_materials, ["Material", "Material"])
        self.assertEqual(len(mesh.corner_normals), 6)

    def test_warns_when_animated_biped_export_omits_skin_and_material_definitions(self):
        scene = dotxsi.loads(
            "xsi 0300txt 0032\n"
            "SI_MaterialLibrary { 1;"
            " SI_Material Scene_Root { .7;.7;.7;1; 50; 1;1;1; 0;0;0; 2; .3;.3;.3; }"
            "}\n"
            "SI_Model MDL-Root {\n"
            " SI_Model MDL-Bip01 {\n"
            "  SI_FCurve { \"Bip01\"; \"Bip01.kine.local.rotx\"; \"LINEAR\"; 1; 1; 2;"
            "   0;0; 1;10; }\n"
            " }\n"
            " SI_Model MDL-Body {\n"
            "  SI_GlobalMaterial { \"MissingSkin\"; \"NODE\"; }\n"
            "  SI_Mesh MSH-Body {\n"
            "   SI_Shape SHP-Body { 1; \"ORDERED\"; 3; \"POSITION\";"
            "    0;0;0; 1;0;0; 0;1;0; }\n"
            "   SI_TriangleList Body { 1; \"\"; \"Scene_Root\"; 0;1;2; }\n"
            "  }\n"
            " }\n"
            "}\n"
        )
        warnings = "\n".join(scene.warnings)
        self.assertIn("no SI_Envelope or SI_ShapeAnimation", warnings)
        self.assertIn("'MissingSkin'", warnings)
        body = next(obj for obj in scene.walk() if obj.name == "Body")
        self.assertEqual(body.properties["softimage_global_material"], "MissingSkin")

    def test_reads_crosswalk_transform_and_camera_templates(self):
        transform_values = [
            1, 2, 3,
            10, 20, 30,
            5,
            2, 3, 4,
            1,
        ] + [0] * 21 + [0, 0, 0, 0, 0, 0, 1, 1, 1, 0, 0, 0]
        self.assertEqual(len(transform_values), 44)
        body = ";".join(str(value) for value in transform_values)
        scene = dotxsi.loads(
            "xsi 0500txt 0032\n"
            f"XSI_Model Model {{ XSI_Transform Transform {{ {body}; }} }}\n"
            "XSI_Camera Camera { 0; 1.777777; 45; 1; 1; 10; 5; 0.25; 500; "
            "0; 1.4; 0.8; 0; 0; }\n"
        )
        model, camera = scene.objects
        self.assertEqual(model.transform.position, (1.0, 2.0, 3.0))
        self.assertEqual(model.transform.rotation, (10.0, 20.0, 30.0))
        self.assertEqual(model.transform.rotation_order, "ZYX")
        self.assertEqual(model.transform.scale, (2.0, 3.0, 4.0))
        self.assertEqual(camera.camera.projection, "PERSP")
        self.assertEqual(camera.camera.field_of_view, 45.0)
        self.assertEqual(camera.camera.near, 0.25)
        self.assertEqual(camera.camera.far, 500.0)

    def test_read_header_identifies_native_scene(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "native.xsi"
            path.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
            with self.assertRaisesRegex(dotxsi.DotXSIError, "native Softimage"):
                dotxsi.read_header(path)


if __name__ == "__main__":
    unittest.main()
