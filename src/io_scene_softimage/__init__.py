# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender 5.2 extension for importing Autodesk Softimage files."""

bl_info = {
    "name": "Softimage Importer",
    "author": "Blender XSI contributors",
    "version": (0, 1, 0),
    "blender": (5, 2, 0),
    "location": "File > Import > Softimage",
    "description": "Import dotXSI, native Softimage scenes, and Softimage models",
    "category": "Import-Export",
}


def register():
    from . import operators

    operators.register()


def unregister():
    from . import operators

    operators.unregister()
