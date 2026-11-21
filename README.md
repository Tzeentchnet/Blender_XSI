# Softimage Importer for Blender 5.2

This Blender extension imports Autodesk Softimage files without requiring Softimage:

- ASCII dotXSI (`.xsi`), versions 3.0 – 6.0
- Native Softimage scenes (`.scn`), including scenes saved by XSI 2.x – 4.x
- Native Softimage models (`.emdl`)

Geometry, materials and textures, skeletons (as armatures), skinning, shape animation and transform
animation are converted. How each piece is read and converted, and what is still unknown, is documented
in [`docs/`](docs/README.md).

## Install

Build a release archive with Blender's extension command:

```sh
blender --command extension build --source-dir src/io_scene_softimage --output-dir dist
```

In Blender 5.2, open **Edit > Preferences > Extensions**, choose **Install from Disk**, and select
`dist/io_scene_softimage-0.2.1.zip`. Import from **File > Import > Softimage (.xsi, .scn, .emdl)**, or
drag files into the 3D viewport. Several files can be imported at once.

## Import options

| Option | Default | Effect |
| --- | --- | --- |
| Create Collection | on | One collection per imported file |
| Source Up Axis | Y | Softimage is Y up; choose Z for Z-up data |
| Scale | 1.0 | Uniform scale applied to positions |
| Skeletons | Armatures | Build armatures from IK chains, envelope deformers and `Bip01` / `ValveBiped` hierarchies, or keep every node as an object (Empties) |
| Materials / Image Textures / Search for Missing Images | on | Materials, image textures, and a bounded search for images whose stored path does not exist |
| Animation / Shape Animation / Set Frame Range | on | F-curves, mixer actions, per-frame shape caches, scene frame range and rate |

## Supported data

### dotXSI

| Data | Status |
| --- | --- |
| Hierarchy, transforms, base poses, visibility | Supported |
| Polygon, triangle and triangle-strip lists; `ORDERED` and `INDEXED` shapes | Supported, with split normals, UV and colour sets |
| `SI_Material` + `SI_Texture2D` (3.0) | Supported: colours, specular power, shading model, image, repeat / offset / crop / mirror |
| `XSI_Material` / `XSI_Shader` (3.5+) | Best effort (Phong-style parameters, image shaders) |
| `SI_FCurve` (constant, linear, Hermite, cubic) incl. `kine.local.*` names | Supported, tangents preserved |
| `XSI_Mixer` / `XSI_Action` | Curves imported; clips merged into one action |
| `SI_ShapeAnimation` | Shape keys keyed by the `SHPANIM` curve |
| `SI_Envelope`, IK chains, Biped hierarchies | Armature with vertex groups and Armature modifier |
| Cameras, lights, clusters, custom parameter sets | Supported |
| Binary dotXSI | Not supported (re-export as ASCII) |

The importer reports animated Biped exports that contain meshes but no `SI_Envelope` or
`SI_ShapeAnimation`: those files do not contain enough data to make geometry follow the armature.
Undefined material references are also reported because nearby image files cannot reconstruct missing
polygon-to-material assignments.

### Native `.scn` / `.emdl`

| Data | XSI 2.x – 4.x | Softimage 6.x – 2015 |
| --- | --- | --- |
| Hierarchy, names, transforms, visibility | Supported | Supported |
| Frozen polygon meshes | Supported (sequential 3.x layout) | Supported |
| UV maps | Supported (sample-cluster ordering, partial projections merged) | Supported |
| Materials and textures | Phong / Constant / Material__N shaders, image clips | Render trees (Phong, Blinn, Lambert, Arnold, ...) |
| Envelopes (skinning) | Supported, bind matrices used as rest pose | Not yet decoded |
| Transform F-curves | Supported (frame rate detected, 29.97 for HL2) | Not tested |
| Live primitives | Rebuilt from generator parameters (stacked operators ignored) | Same |
| Expressions, constraints, mixer clips, NURBS | Stored as notes / not imported | Same |

Validation on the Half-Life 2 source archive (6,500 native scenes, 429 dotXSI files): every dotXSI file
imports in Blender without errors, and imported bone and object world matrices match an independent
evaluation of the files to float precision. See [`docs/testing.md`](docs/testing.md).

## Texture lookup

Stored absolute paths are tried first, then every trailing part of the stored path below the scene
folder, its Softimage project, nearby `Pictures` / `Source` asset folders and the scene's ancestor folders
(so `U:\hl2\hl2\materialsrc\...` is found inside a copied archive), then other image extensions, then
(optionally) a file-name index of the nearest content folder. Missing images become placeholder images
that point at the stored path.

## Development

Run the Blender-independent tests:

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

Corpus tools:

```sh
PYTHONPATH=src python3 tools/survey.py <folder> [--step N]                      # parse without Blender
blender -b --factory-startup --python tools/blender_batch.py -- <folder> out.jsonl   # import in Blender
blender -b --factory-startup --python tools/verify_animation.py -- file.xsi 0 10 20  # check world matrices
```

The package is split into a Blender-independent half (`dotxsi_parser`, `dotxsi`, `model`, `convert`,
`skeleton`, `images`, `primitives`, `native_scn/`, `native_import.to_model`) and the Blender builders
(`blender_scene`, `blender_mesh`, `blender_anim`, `blender_materials`, `native_materials`). See
[`docs/architecture.md`](docs/architecture.md).

## Source documentation and prior art

- [Autodesk: Importing and Exporting dotXSI Files](https://download.autodesk.com/global/docs/softimage2012/en_us/userguide/files/import_export_ImportingandExportingdotXSIFiles.htm)
- [Autodesk Softimage 2012 SDK documentation](https://download.autodesk.com/global/docs/softimage2012/en_us/sdkguide/files/intro.htm)
- Autodesk Softimage 2015 SDK `dotXSITemplates3_0.h` through `dotXSITemplates6_0.h`
- [Microsoft Compound File Binary specification](https://learn.microsoft.com/openspecs/windows_protocols/ms-cfb/)
- [Blender extension packaging](https://docs.blender.org/manual/en/latest/advanced/extensions/getting_started.html)
- [Blender Python API](https://docs.blender.org/api/current/)

Existing importers reviewed for design ideas:

- [Silica](https://github.com/whoisit/silica), GPL-3.0-or-later, native `.scn` reverse engineering
- [TTO Softimage XSI Importer](https://github.com/chrismoliere-prog/TTO-Softimage-XSI-Importer),
  Apache-2.0, classic dotXSI skeletal workflow

See `NOTICE` for incorporated-source attribution and `LICENSE` for terms.
