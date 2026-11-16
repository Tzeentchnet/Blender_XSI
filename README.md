# Softimage Importer for Blender 5.2

This Blender extension imports Autodesk Softimage interchange files and native
scene containers without requiring Softimage:

- ASCII dotXSI (`.xsi`)
- Native Softimage scenes (`.scn`)
- Native Softimage models (`.emdl`)

## Install

Build an extension archive:

```sh
python3 tools/build_extension.py
```

In Blender 5.2, open **Edit > Preferences > Extensions**, choose
**Install from Disk**, and select `dist/io_scene_softimage-0.1.0.zip`.
Import files from **File > Import > Softimage (.xsi, .scn, .emdl)**.

## Supported data

### dotXSI

The text parser reads the dotXSI grammar rather than relying on line offsets.
It supports comments, comma or semicolon separators, quoted paths, arbitrary
template nesting, n-gons, and large files.

| Data | Status |
| --- | --- |
| ASCII dotXSI 3.0, 3.5, 3.6 | Supported; 3.6 tested with a 29 MB production scene |
| dotXSI 5.0/6.0 transforms, base poses, cameras | Recognized |
| Binary dotXSI | Not supported; the importer asks for an ASCII re-export |
| Hierarchy and local SRT transforms | Supported |
| Polygon meshes and n-gons | Supported |
| Split normals, multiple UV/color sets | Supported |
| Polygon-list material assignments | Supported |
| Phong/Lambert-style material values and image textures | Best-effort |
| Cameras and point/sun/spot lights | Supported |
| Transform F-curves | Linear and constant curves supported |
| Envelope weights | Imported as vertex groups |
| IK roots, joints, and effectors | Preserved as named hierarchy empties |
| Automatic armature reconstruction | Not yet supported |

The importer converts Softimage's right-handed Y-up coordinates to Blender's
right-handed Z-up coordinates with a +90 degree X rotation. Local transforms
are conjugated (`C * M * C^-1`), so nested transforms remain local and no
conversion root is added.

### Native `.scn` and `.emdl`

Native files are OLE Compound File Binary containers. The vendored,
dependency-free reader currently handles:

- object hierarchy and local transforms
- frozen polygon meshes
- a subset of live polygon primitives
- cameras and lights
- materials, polygon-cluster assignments, UV maps, and image textures

Native scene animation, envelopes, Mixer data, ICE behavior, and most live
modeling operators are preserved only as warnings or import notes.

## Texture lookup

Stored absolute and relative paths are tried first. The importer also checks
the scene directory and the Softimage project's `Pictures` directory. Optional
missing-image search is bounded to four directory levels.

## Development

Run the Blender-independent parser and package tests:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
python3 -m compileall -q src
```

The package is intentionally split into:

- `dotxsi.py`: text lexer, parser, and dotXSI template conversion
- `model.py`: Blender-independent scene representation
- `blender_scene.py`: dotXSI-to-Blender data creation
- `native_scn/`: isolated native scene reader
- `native_import.py` and `native_materials.py`: native-to-Blender adapters

## Source documentation and prior art

Implementation was cross-checked against direct format/API sources:

- [Autodesk: Importing and Exporting dotXSI Files](https://download.autodesk.com/global/docs/softimage2012/en_us/userguide/files/import_export_ImportingandExportingdotXSIFiles.htm)
- [Autodesk Softimage 2012 SDK documentation](https://download.autodesk.com/global/docs/softimage2012/en_us/sdkguide/files/intro.htm)
- Autodesk Softimage 2015 SDK `dotXSITemplates3_0.h` through
  `dotXSITemplates6_0.h`
- [Microsoft Compound File Binary specification](https://learn.microsoft.com/openspecs/windows_protocols/ms-cfb/)
- [Blender extension packaging](https://docs.blender.org/manual/en/latest/advanced/extensions/getting_started.html)
- [Blender Python API](https://docs.blender.org/api/current/)

Existing importers reviewed for design ideas:

- [Silica](https://github.com/whoisit/silica), GPL-3.0-or-later, native
  `.scn` reverse engineering
- [TTO Softimage XSI Importer](https://github.com/chrismoliere-prog/TTO-Softimage-XSI-Importer),
  Apache-2.0, classic dotXSI skeletal workflow

See `NOTICE` for incorporated-source attribution and `LICENSE` for terms.
