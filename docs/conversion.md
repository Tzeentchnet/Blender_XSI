# Conversion to Blender

## Coordinates and units

Softimage is right-handed, Y up; Blender is right-handed, Z up. The conversion is the similarity
`A = s * P`, where `P` is a +90° rotation about X (`(x, y, z) -> (x, -z, y)`) and `s` the import scale
(option *Scale*, default 1). With *Source Up Axis = Z*, `P` is the identity.

* Points and directions: `p' = A p` (directions without `s`).
* Transforms: `M' = A M A^-1`. Because every transform is conjugated, nested transforms stay local to their
  parents and no conversion root object is needed.
* Because `P` is a signed axis permutation, the conjugated transform can be written component-wise:
  location `(x, -z, y) * s`, scale `(sx, sz, sy)`, and Euler rotation `(rx, -rz, ry)` in Blender order
  `XZY` for a Softimage `XYZ` order (each axis letter is mapped through `P`). Objects therefore keep
  editable Euler channels and their F-curves (including Bézier handles) are copied channel by channel.
* Cameras and lights look down their local -Z axis in both applications. Their own frame gets an extra
  `P` on the right (`M' = A M A^-1 P`) and their children a `P^-1` on the left. Such objects and objects
  with quaternion transforms are animated by sampling matrices (see below).
* SI_Camera / SI_Light with position + interest are placed with a look-at matrix in the parent's space.

MAX2XSI exports (366 of the 429 dotXSI files) wrap the scene in `MAXSceneRoot` with a -90° X rotation
that turns 3ds Max's Z-up data into Y-up, so the default conversion gives upright characters. Their
`SI_CoordinateSystem` nevertheless declares Z up (`1,0,1,0,5,2`); the declaration is recorded but not
applied (see [unknowns.md](unknowns.md)).

## Objects

| Softimage | Blender |
| --- | --- |
| Model, null, effector without geometry | Empty (plain axes; single arrow for chain nodes when not in an armature) |
| Polygon mesh | Mesh object; materials per polygon cluster; UV maps; colour attributes; custom normals |
| Camera | Camera (perspective / orthographic, horizontal or vertical FOV, clipping) |
| Light | Point / Sun (infinite) / Spot (cone, spread → blend); energy from intensity |
| Chain root / joint / effector, deformer, biped node | Bone (see below) |
| Custom parameter sets, expressions, unapplied operators, import notes | Custom properties |

Invisible objects (`SI_Visibility 0`, native view visibility off) are hidden; render visibility off sets
`hide_render`.

## Skeletons and armatures

With *Skeletons = Armatures* (default), `skeleton.plan` chooses bones:

1. **Seeds**: IK chain parts (`SI_IK_*`, native `root` / `bone` / `eff`), envelope deformers, and nodes named
   like 3ds Max Biped / Valve skeletons (`Bip01...`, `ValveBiped...`). Model containers are never seeds.
2. **Promotion**: a node with a seed above and a seed below becomes a bone too, so bone chains are
   contiguous (e.g. `LLegRoot` between `Bip01_Pelvis` and `Bip01_L_Thigh`).
3. **Grouping**: bones sharing the same non-bone parent form one armature object, parented to that
   node's object (or the scene root).

Geometry:

* Armature space is the parent node's frame. A bone's node matrix `N` is the product of converted local
  rest transforms (base pose when present, otherwise the stored transform) down from the skeleton root.
* Softimage chains point down local X, Blender bones down local Y, so the bone rest matrix is
  `B = orthonormalize(N R)` with `R = Rz(-90°)`.
* Length: the joint length when given; otherwise the largest projection of a child's offset onto the
  node's X axis; otherwise half the parent's length (or half the median length).
* Meshes, cameras or lights carried by a bone node become objects parented to that bone; bone display
  meshes without UVs or weights are shown as wireframe and hidden from renders. Other nodes below a bone
  are objects parented to the bone with `matrix_parent_inverse = T(0, -length, 0) R^-1`, which cancels
  Blender's tail offset and the axis swap, so their basis stays their Softimage local transform.

Pose: for a bone with parent bone `p`, the pose basis is

```
basis(t) = B^-1 · B_p · R^-1 · L(t) · R          (root bones: B^-1 · L(t) · R)
```

where `L(t)` is the node's converted local matrix at time `t`. This reproduces the Softimage world
matrices exactly: the verification harness compares every bone's evaluated world matrix with an
independent evaluation of the parsed file (worst error 3.3e-5 units / 3.4e-6 in rotation, float32
precision) — see [testing.md](testing.md).

Enveloped meshes get vertex groups named after their deformers and an Armature modifier, and are parented
to the armature object rather than to a bone (Softimage envelopes ignore the motion of the mesh's
parents). With *Skeletons = Empties*, every node stays an object and weights are kept as vertex groups.

## Animation

All animation of a file goes into one layered action (`<file>` or `<file>|<clip>` when the file has a
single mixer action), with one slot per animated datablock: objects, armatures and shape-key blocks.
Keyframes are inserted with `keyframe_points.add` + `foreach_set`.

* **Component-compatible objects**: each Softimage curve becomes one F-curve after the axis remap
  (values and handle values multiplied by the remap sign, scale and degrees → radians). CUBIC / HERMITE
  keys keep their Bézier handles (FREE handles); LINEAR and CONSTANT map to the same interpolation.
* **Bones and other objects**: sampled. Frames are the union of all key frames; when any curve has
  tangents, every integer frame in the range is added. Local matrices are built for all frames at once,
  converted, combined with the rest matrices and decomposed into location / quaternion / scale
  (quaternion signs kept continuous). Channels that never leave their identity value are not written.
* **Visibility** curves key `hide_viewport` and `hide_render` (constant).
* **Shape animation** keys shape-key values (see [dotxsi.md](dotxsi.md)).
* Native curves: key times in seconds are multiplied by the detected frame rate; handles likewise.

With *Set Frame Range*, the scene frame range and rate come from `SI_Scene` (dotXSI) or from the key
range and detected rate (native). Non-integer rates (29.97) use `fps = 30, fps_base = 30 / 29.97`.

## Materials and textures

dotXSI materials: see [dotxsi.md](dotxsi.md). Native materials: render trees of 2011+ scenes are converted
by `native_materials` (Phong, Blinn, Lambert, Constant, Architectural, Arnold/Softimage Standard, Standard
Surface families); XSI 3.x materials become Principled BSDF materials with the diffuse / specular colours
of their Phong or Material__N shader, an Image Texture on the base colour and a UV Map node set to the
texture's projection.

Image lookup (`images.ImageResolver`), in order:

1. the stored path (with `[Project Path]` substituted);
2. **re-rooting**: every trailing part of the stored path (`materialsrc/Models/Alyx/alyxboot.tga`,
   `Models/Alyx/alyxboot.tga`, ...) below the scene folder, its Softimage project, `Pictures`, and each
   ancestor folder (up to six levels) — this finds `U:\hl2\hl2\materialsrc\...` inside the archive;
3. the same with other image extensions (`.tga` → `.png`, `.psd`, ...);
4. with *Search for Missing Images*, a file-name index of the nearest ancestor that contains a content
   folder (`materialsrc`, `Pictures`, `textures`, ...), ranked by how many trailing folder names match.

Missing images are created as placeholder images that point at the stored path, so they can be
relinked in Blender.
