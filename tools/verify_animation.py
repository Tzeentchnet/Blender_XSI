# SPDX-License-Identifier: GPL-3.0-or-later
"""Check imported world matrices against an independent evaluation of a parsed dotXSI file.

usage:
    blender -b --factory-startup --python tools/verify_animation.py -- FILE.xsi [FRAME ...]

For every node and frame, the expected Blender world matrix is A * (product of Softimage local matrices
evaluated from the file's curves) * A^-1; the actual one is the object's evaluated matrix_world or, for
bones, armature.matrix_world @ pose_bone.matrix @ R^-1. Prints the worst position and rotation errors.
"""

from pathlib import Path
import sys

import bpy
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import io_scene_softimage  # noqa: E402
from io_scene_softimage import blender_scene, convert, dotxsi, operators  # noqa: E402


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1:]
    path = Path(argv[0])
    frames = [float(f) for f in argv[1:]] or [0.0]
    io_scene_softimage.register()
    bpy.ops.wm.read_homefile(use_empty=True)
    operators.import_file(bpy.context, path, blender_scene.ImportOptions(), lambda kind, msg: None)
    scene = dotxsi.load(path)
    scene.link_parents()
    conv = convert.AxisConversion.from_up_axis("Y")

    def local_at(node, frame):
        t = node.transform
        channels = {"location": list(t.position), "rotation": list(t.rotation), "scale": list(t.scale)}
        for curve in node.animation:
            if curve.prop in channels:
                channels[curve.prop][curve.index] = float(curve.evaluate(np.array([frame]))[0])
        return convert.transform_matrix(channels["location"], channels["rotation"], channels["scale"],
                                        t.rotation_order, t.quaternion)

    def world_at(node, frame):
        matrix = local_at(node, frame)
        parent = node.parent
        while parent is not None:
            matrix = local_at(parent, frame) @ matrix
            parent = parent.parent
        return conv.transform(matrix)

    armatures = [o for o in bpy.data.objects if o.type == "ARMATURE"]
    worst = {"position": (0.0, None), "rotation": (0.0, None)}
    checked = 0
    for frame in frames:
        bpy.context.scene.frame_set(int(frame))
        depsgraph = bpy.context.evaluated_depsgraph_get()
        for node in scene.walk():
            if node.kind in {"CAMERA", "LIGHT"}:
                continue
            expected = world_at(node, frame)
            arm = next((a for a in armatures if node.name[:63] in a.pose.bones), None)
            if arm is not None:
                evaluated = arm.evaluated_get(depsgraph)
                actual = (np.array(evaluated.matrix_world) @ np.array(evaluated.pose.bones[node.name[:63]].matrix)
                          @ blender_scene.R_BONE_INV)
            else:
                obj = bpy.data.objects.get(node.name)
                if obj is None:
                    continue
                actual = np.array(obj.evaluated_get(depsgraph).matrix_world)
            position = float(np.linalg.norm(actual[:3, 3] - expected[:3, 3]))
            a = actual[:3, :3] / np.linalg.norm(actual[:3, :3], axis=0)
            e = expected[:3, :3] / np.linalg.norm(expected[:3, :3], axis=0)
            rotation = float(np.abs(a - e).max())
            checked += 1
            if position > worst["position"][0]:
                worst["position"] = (position, f"{node.name} @ {frame}")
            if rotation > worst["rotation"][0]:
                worst["rotation"] = (rotation, f"{node.name} @ {frame}")
    print(f"checked {checked} node/frame pairs")
    print("worst position error: %.3g (%s)" % worst["position"])
    print("worst rotation error: %.3g (%s)" % worst["rotation"])


main()
