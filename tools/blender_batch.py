# SPDX-License-Identifier: GPL-3.0-or-later
"""Import many Softimage files in one Blender session and log one JSON line per file.

usage:
    blender -b --factory-startup --python tools/blender_batch.py -- FOLDER OUT.jsonl [--step N]

Each line holds: file, object count (null on failure), time, errors, warnings, object types, bone count,
action count, image counts and the number of skinned meshes. The scene is reset between files.
"""

import argparse
import json
from pathlib import Path
import sys
import time
import traceback

import bpy

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import io_scene_softimage  # noqa: E402
from io_scene_softimage import blender_scene, operators  # noqa: E402


def main() -> None:
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("folder", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--step", type=int, default=1)
    args = parser.parse_args(argv)
    files = sorted(p for p in args.folder.rglob("*")
                   if p.is_file() and p.suffix.casefold() in operators.EXTENSIONS)[::args.step]
    io_scene_softimage.register()
    with open(args.output, "w", encoding="utf-8") as out:
        for path in files:
            bpy.ops.wm.read_homefile(use_empty=True)
            messages = []
            started = time.time()
            record = {"file": str(path)}
            try:
                record["objects"] = operators.import_file(
                    bpy.context, path, blender_scene.ImportOptions(),
                    lambda kind, msg: messages.append((next(iter(kind)), msg)),
                )
            except Exception:  # noqa: BLE001
                record["crash"] = traceback.format_exc()[-1500:]
            record["time"] = round(time.time() - started, 3)
            record["errors"] = [m for k, m in messages if k == "ERROR"]
            record["warnings"] = [m[:200] for k, m in messages if k == "WARNING"][:30]
            types: dict[str, int] = {}
            for obj in bpy.data.objects:
                types[obj.type] = types.get(obj.type, 0) + 1
            record["types"] = types
            record["bones"] = sum(len(a.bones) for a in bpy.data.armatures)
            record["actions"] = len(bpy.data.actions)
            record["images"] = len(bpy.data.images)
            record["images_missing"] = sum(
                1 for img in bpy.data.images
                if img.source == "FILE" and not img.has_data and not Path(bpy.path.abspath(img.filepath)).exists()
            )
            record["skinned"] = sum(1 for o in bpy.data.objects
                                    if o.type == "MESH" and any(m.type == "ARMATURE" for m in o.modifiers))
            out.write(json.dumps(record) + "\n")
            out.flush()


main()
