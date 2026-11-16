# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender operators and registration."""

from __future__ import annotations

from pathlib import Path
import time
import traceback

import bpy
from bpy.props import BoolProperty, CollectionProperty, EnumProperty, FloatProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from . import blender_scene, dotxsi, native_import
from .native_scn import scene as native_scene

EXTENSIONS = {".xsi", ".scn", ".emdl"}


class SOFTIMAGE_OT_import(bpy.types.Operator, ImportHelper):
    """Import dotXSI interchange files or native Softimage scenes and models"""

    bl_idname = "import_scene.softimage"
    bl_label = "Import Softimage"
    bl_options = {"REGISTER", "UNDO", "PRESET"}

    filename_ext = ""
    filter_glob: StringProperty(default="*.xsi;*.scn;*.emdl", options={"HIDDEN"}, maxlen=255)
    files: CollectionProperty(type=bpy.types.OperatorFileListElement, options={"HIDDEN", "SKIP_SAVE"})
    directory: StringProperty(subtype="DIR_PATH", options={"HIDDEN", "SKIP_SAVE"})

    use_collection: BoolProperty(
        name="Create Collection",
        description="Put imported objects in a collection named after the source file",
        default=True,
    )
    import_materials: BoolProperty(
        name="Materials", description="Create materials and assign polygon clusters", default=True,
    )
    import_textures: BoolProperty(
        name="Image Textures", description="Load image files referenced by imported materials", default=True,
    )
    search_textures: BoolProperty(
        name="Search for Missing Images",
        description="Look for referenced images under the scene's content folders when the stored path "
                    "does not exist (re-rooting the stored path, then a file-name index)",
        default=True,
    )
    import_animation: BoolProperty(
        name="Animation", description="Import F-curves, mixer actions and shape animation", default=True,
    )
    import_shape_animation: BoolProperty(
        name="Shape Animation",
        description="Create shape keys for SI_ShapeAnimation (per-frame vertex caches) and key them",
        default=True,
    )
    skeleton_mode: EnumProperty(
        name="Skeletons",
        description="How Softimage chains, deformers and biped hierarchies are imported",
        items=(
            ("AUTO", "Armatures", "Build armatures from IK chains, envelope deformers and Bip01/ValveBiped "
                                  "hierarchies; bind enveloped meshes with an Armature modifier"),
            ("NONE", "Empties", "Keep every Softimage node as an object (no armatures)"),
        ),
        default="AUTO",
    )
    up_axis: EnumProperty(
        name="Source Up Axis",
        description="Up axis of the data in the file (Softimage is Y up)",
        items=(("Y", "Y Up (Softimage)", ""), ("Z", "Z Up", "")),
        default="Y",
    )
    global_scale: FloatProperty(
        name="Scale", description="Uniform scale applied to positions", default=1.0, min=1e-6, max=1e6,
        soft_min=0.001, soft_max=1000.0,
    )
    set_scene_frames: BoolProperty(
        name="Set Frame Range", description="Use the file's frame range and rate for the scene", default=True,
    )

    def draw(self, _context):
        layout = self.layout
        layout.use_property_split = True
        layout.prop(self, "use_collection")
        layout.prop(self, "up_axis")
        layout.prop(self, "global_scale")
        layout.prop(self, "skeleton_mode")
        header, body = layout.panel("SOFTIMAGE_materials", default_closed=False)
        header.label(text="Materials")
        if body:
            body.prop(self, "import_materials")
            column = body.column()
            column.enabled = self.import_materials
            column.prop(self, "import_textures")
            search = column.column()
            search.enabled = self.import_textures
            search.prop(self, "search_textures")
        header, body = layout.panel("SOFTIMAGE_animation", default_closed=False)
        header.label(text="Animation")
        if body:
            body.prop(self, "import_animation")
            column = body.column()
            column.enabled = self.import_animation
            column.prop(self, "import_shape_animation")
            column.prop(self, "set_scene_frames")

    def _options(self) -> blender_scene.ImportOptions:
        return blender_scene.ImportOptions(
            import_materials=self.import_materials,
            import_textures=self.import_textures,
            search_textures=self.search_textures,
            import_animation=self.import_animation,
            import_shape_animation=self.import_shape_animation,
            use_collection=self.use_collection,
            skeleton_mode=self.skeleton_mode,
            up_axis=self.up_axis,
            global_scale=self.global_scale,
            set_scene_frames=self.set_scene_frames,
        )

    def _paths(self) -> list[Path]:
        if self.files and self.directory:
            paths = [Path(self.directory) / item.name for item in self.files if item.name]
            if paths:
                return paths
        return [Path(self.filepath)]

    def execute(self, context):
        total = 0
        imported_any = False
        for path in self._paths():
            count = import_file(context, path, self._options(), self.report)
            if count is not None:
                imported_any = True
                total += count
        if not imported_any:
            return {"CANCELLED"}
        self.report({"INFO"}, f"Imported {total} Softimage object(s)")
        return {"FINISHED"}


def import_file(context, path: Path, options: blender_scene.ImportOptions, report) -> int | None:
    """Import one file; returns the number of created objects or None on failure."""
    extension = path.suffix.casefold()
    started = time.perf_counter()
    try:
        if extension == ".xsi":
            parsed = dotxsi.load(path)
            for warning in parsed.warnings:
                report({"WARNING"}, f"{path.name}: {warning}")
            objects = blender_scene.build(context, parsed, report=report, options=options)
        elif extension in {".scn", ".emdl"}:
            parsed = native_scene.read(str(path))
            for warning in parsed.warnings:
                report({"WARNING"}, str(warning))
            objects = native_import.build(context, parsed, report, options=options)
        else:
            report({"ERROR"}, f"Unsupported Softimage file extension: {extension or '(none)'}")
            return None
    except (OSError, dotxsi.DotXSIError, ValueError) as error:
        report({"ERROR"}, f"Cannot import {path.name}: {error}")
        return None
    except Exception as error:  # noqa: BLE001 - report anything unexpected instead of crashing the UI
        traceback.print_exc()
        report({"ERROR"}, f"Unexpected error while importing {path.name}: {error}")
        return None
    print(f"Softimage import: {path.name}: {len(objects)} objects in {time.perf_counter() - started:.2f}s")
    return len(objects)


def _menu_import(self, _context):
    self.layout.operator(SOFTIMAGE_OT_import.bl_idname, text="Softimage (.xsi, .scn, .emdl)")


class SOFTIMAGE_FH_import(bpy.types.FileHandler):
    """Drag and drop of Softimage files into the 3D viewport."""

    bl_idname = "SOFTIMAGE_FH_import"
    bl_label = "Softimage"
    bl_import_operator = SOFTIMAGE_OT_import.bl_idname
    bl_file_extensions = ".xsi;.scn;.emdl"

    @classmethod
    def poll_drop(cls, context):
        return context.area is not None and context.area.type == "VIEW_3D"


CLASSES = (SOFTIMAGE_OT_import, SOFTIMAGE_FH_import)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
