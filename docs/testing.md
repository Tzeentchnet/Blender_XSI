# Testing and validation

## Unit tests (no Blender needed)

```sh
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

| File | Covers |
| --- | --- |
| `tests/test_dotxsi.py` | Original fixture: hierarchy, mesh attributes, envelopes, cameras, lights, error messages |
| `tests/test_dotxsi_templates.py` | 3.0 production templates: `SI_Material` + `SI_Texture2D`, `kine.local` channels, CUBIC tangents, mixer actions, `SI_Angle`, indexed shapes, shape animation, dropped invalid polygons, clusters, custom parameter sets, infinite lights, parser edge cases |
| `tests/test_conversion.py` | Euler / quaternion / matrix math, component remap = matrix conjugation, skeleton planning, image re-rooting and search, primitives |
| `tests/test_native_legacy.py` | XSI 3.x geometry layout (incl. non-minimal compact numbers), F-curve objects, frame-rate detection, envelope runs, bind matrices — on synthetic bytes |
| `tests/test_native_scn.py` | Native container details (marker detection, class definitions) |
| `tests/test_package.py` | Manifest and entry points |

## Corpus

Development used the Half-Life 2 source archive: 6,500 `.scn`, 429 non-empty `.xsi`, 10 `.emdl`.

### Parsing without Blender (`tools/survey.py`)

| Measure | Before | After |
| --- | --- | --- |
| dotXSI files parsed | 428 / 430 | 429 / 429 non-empty (one file is 0 bytes) |
| dotXSI parse time, whole corpus | 451 s | 37 s |
| dotXSI F-curves attached to a transform channel | 0 (channel names not recognized) | 96,042 |
| Native objects of unknown kind (1-in-20 sample) | 29,133 of ~30,000 | 90 |
| Native frozen meshes decoded (1-in-20 sample) | 1 | 2,486 of 2,489 |

### Importing in Blender (`tools/blender_batch.py`)

All 429 dotXSI files:

| Result | Value |
| --- | --- |
| Files imported / failed / crashed | 429 / 0 / 0 |
| Total time (parse + Blender data) | 113 s, slowest 8.8 s (`female_02.XSI`, 46 per-frame shape caches) |
| Armatures / bones | 281 / 15,279 |
| Files with animation | 354 |
| Images referenced / missing | 182 / 0 |
| Warnings | 2 (Advisor polygons referencing missing points) |

Native scenes, every 8th file (813 files): see the table at the end of this document.

### World-matrix verification (`tools/verify_animation.py`)

For each node and frame, the Blender world matrix (object `matrix_world`, or for bones
`armature.matrix_world @ pose_bone.matrix @ R^-1`) is compared with `A · Π L_i(t) · A^-1` computed
directly from the parsed file.

| File | Node/frame pairs | Worst position error | Worst rotation error |
| --- | ---: | ---: | ---: |
| `models/Alyx_XSI/dotXSI/MaxAnims/Walk.xsi` | 284 | 3.3e-5 | 3.4e-6 |
| `models/Humans/Female/dotXSI/female_01.XSI` | 440 | 1.0e-5 | 2.5e-6 |
| `models/Barney_XSI/dotXSI/MaxAnims/Run_turn90_right.xsi` | 276 | 1.3e-4 | 2.5e-6 |

(Units are scene units; the characters are ~70 units tall, so errors are float32 rounding.)

### Visual checks

Rendered with Workbench from background Blender: `female_01.XSI` (textured, upright, shape animation
posing the mesh at frame 25), `Advisor.XSI` (armature + 22 envelope deformers), and the native
`models/Alyx_XSI/Scenes/A_gesture16.scn` (XSI 3.5): geometry, all textures (face/hair via sample-cluster
UV ordering, body via a renamed clip), 53-deformer skinning from bind matrices, and the gesture animation
at 29.97 fps.

## Native batch results

Every 8th native scene (813 files, XSI 3.0 – 3.51), imported in Blender with default options:

| Result | Value |
| --- | --- |
| Files imported / failed / crashed | 813 / 0 / 0 |
| Objects | 9,108 meshes, 30,619 empties, 965 cameras, 795 lights |
| Armatures / bones | 544 / 35,476 (428 files) |
| Skinned meshes (Armature modifier from decoded envelopes) | 598 |
| Files with animation | 342 |
| Images referenced / missing | 1,390 / 62 (the missing files, e.g. `materialsrc\Models\fassassin\foot.TGA`, are not in the archive) |

Remaining warnings: 377 files have no decodable materials (no shader definitions and no 3.x material
clusters); 77 meshes have more than Blender's 8 UV maps (the extra projections are skipped, the merged map
is kept); 19 envelopes could not be decoded; 48 files contain a legacy object stream (19 of them with no
decodable objects); 21 meshes were not decoded (see [unknowns.md](unknowns.md)).

The first run took 727 s, with one scene at 168 s: the UV reader tested every byte after each of ~13,500
candidate value blocks. Searching only for the encodings of the node count brought the 23 slowest scenes
from 444 s to 42.5 s (worst 3.9 s) with identical UVs.
