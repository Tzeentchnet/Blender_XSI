#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Parse every Softimage file under a folder without Blender and summarize what was decoded.

usage: PYTHONPATH=src python tools/survey.py FOLDER [--step N] [--native-only | --xsi-only]

Reports parse failures, object kinds, decoded / undecoded meshes, materials, animation and warnings.
"""

from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
import sys
import time
import traceback

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from io_scene_softimage import dotxsi, native_import  # noqa: E402
from io_scene_softimage.native_scn import scene as native_scene  # noqa: E402


def survey_xsi(files: list[Path]) -> None:
    errors: Counter = Counter()
    stats: Counter = Counter()
    warnings: Counter = Counter()
    started = time.time()
    for path in files:
        try:
            scene = dotxsi.load(path)
        except Exception as error:  # noqa: BLE001
            errors[f"{type(error).__name__}: {str(error)[:100]}"] += 1
            continue
        stats["files ok"] += 1
        for obj in scene.walk():
            stats["objects"] += 1
            stats[f"kind {obj.kind}"] += 1
            stats["F-curves"] += len(obj.animation)
            if obj.mesh is not None:
                stats["shape keys"] += len(obj.mesh.shape_keys)
                stats["enveloped meshes"] += bool(obj.mesh.envelope_weights)
        stats["materials"] += len(scene.materials)
        stats["textured materials"] += sum(1 for m in scene.materials.values() if m.textures)
        stats["actions"] += len(scene.actions)
        for warning in scene.warnings:
            warnings[warning.split(":", 1)[-1][:90]] += 1
    print(f"dotXSI: {len(files)} files in {time.time() - started:.1f}s")
    _print(stats, errors, warnings)


def survey_native(files: list[Path]) -> None:
    errors: Counter = Counter()
    stats: Counter = Counter()
    warnings: Counter = Counter()
    started = time.time()
    for path in files:
        try:
            scene = native_scene.read(str(path))
            model = native_import.to_model(scene)
        except Exception as error:  # noqa: BLE001
            errors[f"{type(error).__name__}: {str(error)[:100]}"] += 1
            traceback.print_exc(limit=1)
            continue
        stats["files ok"] += 1
        for so in scene.objects:
            stats[f"kind {so.kind}"] += 1
            if so.kind == "polymsh":
                if so.mesh is not None:
                    stats[f"mesh decoded ({so.mesh.layout})"] += 1
                    stats["meshes with UVs"] += bool(getattr(so, "uvs", None))
                    stats["enveloped meshes"] += bool(getattr(so, "envelope", None))
                elif so.generator is not None:
                    stats["live primitive"] += 1
                else:
                    stats["mesh not decoded"] += 1
            stats["F-curves"] += len(getattr(so, "animation", []))
        stats["materials (render tree)"] += len(scene.materials)
        stats["materials (XSI 3.x)"] += len(getattr(scene, "legacy_materials", {}))
        stats["frame rate %.3f" % model.frame_rate] += 1
        for warning in model.warnings:
            warnings[str(warning).split(":", 1)[-1][:90]] += 1
    print(f"native: {len(files)} files in {time.time() - started:.1f}s")
    _print(stats, errors, warnings)


def _print(stats: Counter, errors: Counter, warnings: Counter) -> None:
    for key, value in sorted(stats.items()):
        print(f"  {key}: {value}")
    if errors:
        print("  errors:")
        for key, value in errors.most_common(20):
            print(f"    {value} x {key}")
    if warnings:
        print("  warnings:")
        for key, value in warnings.most_common(20):
            print(f"    {value} x {key}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--step", type=int, default=1, help="survey every Nth file")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--native-only", action="store_true")
    group.add_argument("--xsi-only", action="store_true")
    args = parser.parse_args()
    files = sorted(p for p in args.folder.rglob("*") if p.is_file())
    xsi = [p for p in files if p.suffix.casefold() == ".xsi"][::args.step]
    native = [p for p in files if p.suffix.casefold() in {".scn", ".emdl"}][::args.step]
    if not args.native_only:
        survey_xsi(xsi)
    if not args.xsi_only:
        survey_native(native)


if __name__ == "__main__":
    main()
