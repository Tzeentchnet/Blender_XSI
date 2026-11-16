# Softimage Importer documentation

These notes describe how the importer reads Autodesk Softimage files and converts them to Blender 5.2
data, and record what is still unknown about the formats.

| Document | Contents |
| --- | --- |
| [architecture.md](architecture.md) | Pipeline, modules, the shared scene model, performance design |
| [dotxsi.md](dotxsi.md) | dotXSI text format: parser, supported templates, template-by-template conversion |
| [native-format.md](native-format.md) | Native `.scn` / `.emdl` containers: object streams, XSI 3.x geometry, UVs, materials, envelopes, F-curves |
| [conversion.md](conversion.md) | Coordinate systems, transforms, skeleton/armature reconstruction, animation, materials, textures |
| [unknowns.md](unknowns.md) | Open questions and things to investigate later, with pointers to sample files |
| [testing.md](testing.md) | Unit tests, corpus surveys, Blender verification harness and their results |

The sample corpus used during development is the Half-Life 2 source archive (`hl2/hl2`): 6,500 native
scenes saved by Softimage XSI 3.0 - 3.51 and 429 dotXSI 3.0 files written by XSI 2.x - 3.5 and by
Valve's 3ds Max exporter (MAX2XSI). Paths in these notes are relative to that archive.
