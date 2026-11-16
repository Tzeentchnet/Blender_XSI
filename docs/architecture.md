# Architecture

## Pipeline

```
          .xsi                                   .scn / .emdl
            │                                         │
  dotxsi_parser.py  (structure scan, lazy values)   native_scn/  (CFB container, object streams)
            │                                         │   cfb.py, records.py, scene.py
  dotxsi.py         (template interpretation)         │   mesh.py, uvs.py, materials*.py,
            │                                         │   envelope.py, animation.py, camlight.py
            │                                    native_import.py (to_model)
            ▼                                         ▼
                      model.Scene  (Blender-independent, NumPy arrays)
                                     │
                     skeleton.py (which nodes become bones, rest geometry)
                     convert.py  (axis conversion, batched matrix math)
                                     │
                     blender_scene.py  ──  blender_mesh.py, blender_anim.py,
                                           blender_materials.py / native_materials.py,
                                           images.py
                                     ▼
                           Blender objects, meshes, armatures, actions
```

Both importers produce the same `model.Scene`, so hierarchy, armatures, skinning, animation, shape keys,
cameras and lights are built by one code path (`blender_scene.SceneBuilder`). Materials are the one
exception: native scenes carry render trees (2011+) or XSI 3.x shader objects that `native_materials`
turns into node trees, supplied to the builder through a `material_lookup` callback.

## Modules

| Module | Blender needed | Role |
| --- | --- | --- |
| `dotxsi_parser.py` | no | Header handling and one-pass structural parser; lazy tokenization and NumPy number conversion |
| `dotxsi.py` | no | Converts templates into `model` objects |
| `model.py` | no | Scene description: objects, transforms, meshes, materials, curves, shape keys, actions |
| `convert.py` | no | Axis conversion and batched (F, 4, 4) matrix math: Euler/quaternion matrices, compose, decompose |
| `skeleton.py` | no | Chooses bones, groups them into armatures, computes rest matrices and bone lengths |
| `images.py` | no | Finds image files from stored (often absolute Windows) paths |
| `primitives.py` | no | Polygon versions of live primitives when no frozen geometry is stored |
| `native_scn/*` | no | Native container reader (vendored from Silica, extended for XSI 3.x) |
| `native_import.py` | `build` only | Native scene → `model.Scene` (`to_model` is Blender independent) |
| `blender_scene.py` | yes | Objects, parenting, cameras, lights, armatures, envelopes, animation |
| `blender_mesh.py` | yes | Mesh creation with `foreach_set`, vertex groups, shape keys |
| `blender_anim.py` | yes | Layered (slotted) actions, bulk keyframe insertion |
| `blender_materials.py` | yes | Principled BSDF materials for dotXSI materials |
| `native_materials.py` | yes | Node trees for native render trees and XSI 3.x shaders |
| `operators.py` | yes | `import_scene.softimage` operator (multi-file, drag and drop via a FileHandler) |

## Performance design

The original implementation parsed dotXSI with a Python generator that created a `Token` object per
number, and built meshes and keyframes element by element. On the HL2 corpus that took 451 s for 429
files. The current design:

* **Structure-only scan.** `dotxsi_parser` runs one regular expression over the file that only matches
  braces, quoted strings and comments. Every template records the character spans of its own values.
* **Lazy, bulk value conversion.** Values are split with `str.split` (or a regex when quotes are present)
  only when a converter asks. Arrays (points, normals, UVs, indices, F-curve keys, envelope weights) are
  converted with a single `numpy.array(tokens, dtype=float64)` call.
* **NumPy end to end.** Meshes hold point / corner / attribute arrays; Blender data is filled with
  `foreach_set` (vertices, loops, polygons, UVs, colours, material indices, normals, shape keys).
* **Batched animation.** Bones are sampled for all frames at once: local matrices `(F, 4, 4)` from
  vectorized Euler matrices, converted with matrix products, decomposed with vectorized quaternion
  extraction, and written with `keyframe_points.foreach_set("co", ...)`.
* **Envelope groups** are added with one `VertexGroup.add` call per distinct weight value.

The same 429 files now parse in about 37 s (the remaining time is dominated by five 30 MB per-frame shape
caches). Native 3.x scenes read in 0.05 - 0.5 s each.

## Data flow details

* `model.SceneObject.kind` is one of `EMPTY`, `MESH`, `CAMERA`, `LIGHT`, `BONE`. `bone_role` marks IK
  roots / joints / effectors and envelope deformers. `properties` become custom properties.
* `model.AnimationCurve` stores frames and values as arrays, an interpolation, absolute Bézier handles
  (when the source has tangents), and the resolved target property (`location`, `rotation`, `scale`,
  `visibility`, `shape_animation`) and component.
* `model.Mesh` stores `points (N, 3)`, `polygon_sizes (P,)`, `corner_vertices (L,)`, per-corner normals,
  UV and colour layers `(L, k)`, a per-polygon material index, envelope weights per deformer, clusters and
  shape keys.
