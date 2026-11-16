# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender operators and registration."""

from __future__ import annotations

from pathlib import Path
import traceback

import bpy
from bpy.props import BoolProperty, StringProperty
from bpy_extras.io_utils import ImportHelper

from . import blender_scene, dotxsi, native_import
from .native_scn import scene as native_scene


class SOFTIMAGE_OT_import(bpy.types.Operator, ImportHelper):
    """Import dotXSI interchange files or native Softimage scenes and models"""

    bl_idname = "import_scene.softimage"
    bl_label = "Import Softimage"
    bl_options = {"REGISTER", "UNDO", "PRESET"}

    filename_ext = ""
    filter_glob: StringProperty(
        default="*.xsi;*.scn;*.emdl",
        options={"HIDDEN"},
        maxlen=255,
    )
    import_materials: BoolProperty(
        name="Materials",
        description="Create materials and assign polygon clusters",
        default=True,
    )
    import_textures: BoolProperty(
        name="Image Textures",
        description="Load image files referenced by imported materials",
        default=True,
    )
    search_textures: BoolProperty(
        name="Search for Missing Images",
        description="Search the source project to a bounded depth for referenced images",
        default=True,
    )
    import_animation: BoolProperty(
        name="Transform Animation",
        description="Import supported dotXSI transform F-curves",
        default=True,
    )
    use_collection: BoolProperty(
        name="Create Collection",
        description="Put imported objects in a collection named after the source file",
        default=True,
    )

    def draw(self, _context):
        layout = self.layout
        layout.prop(self, "use_collection")
        layout.prop(self, "import_materials")
        column = layout.column()
        column.enabled = self.import_materials
        column.prop(self, "import_textures")
        search = column.column()
        search.enabled = self.import_textures
        search.prop(self, "search_textures")
        layout.prop(self, "import_animation")

    def execute(self, context):
        extension = Path(self.filepath).suffix.casefold()
        try:
            if extension == ".xsi":
                parsed = dotxsi.load(self.filepath)
                for warning in parsed.warnings:
                    self.report({"WARNING"}, warning)
                objects = blender_scene.build(
                    context,
                    parsed,
                    import_materials=self.import_materials,
                    import_textures=self.import_textures,
                    search_textures=self.search_textures,
                    import_animation=self.import_animation,
                    use_collection=self.use_collection,
                    report=self.report,
                )
            elif extension in {".scn", ".emdl"}:
                parsed = native_scene.read(self.filepath)
                for warning in parsed.warnings:
                    self.report({"WARNING"}, str(warning))
                objects = native_import.build(
                    context,
                    parsed,
                    self.report,
                    use_collection=self.use_collection,
                )
            else:
                self.report(
                    {"ERROR"},
                    f"Unsupported Softimage file extension: {extension or '(none)'}",
                )
                return {"CANCELLED"}
        except (OSError, dotxsi.DotXSIError, ValueError) as error:
            self.report({"ERROR"}, f"Cannot import {Path(self.filepath).name}: {error}")
            return {"CANCELLED"}
        except Exception as error:
            traceback.print_exc()
            self.report(
                {"ERROR"},
                f"Unexpected error while importing {Path(self.filepath).name}: {error}",
            )
            return {"CANCELLED"}
        self.report({"INFO"}, f"Imported {len(objects)} Softimage object(s)")
        return {"FINISHED"}


def _menu_import(self, _context):
    self.layout.operator(
        SOFTIMAGE_OT_import.bl_idname,
        text="Softimage (.xsi, .scn, .emdl)",
    )


CLASSES = (SOFTIMAGE_OT_import,)


def register():
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.TOPBAR_MT_file_import.append(_menu_import)


def unregister():
    bpy.types.TOPBAR_MT_file_import.remove(_menu_import)
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
