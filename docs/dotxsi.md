# dotXSI (text)

dotXSI is Softimage's interchange format (Crosswalk). A file is a header line followed by nested
templates:

```
xsi 0300txt 0032
SI_Model MDL-Bip01 {
    SI_Transform SRT-Bip01 { 1.0, 1.0, 1.0,  0.0, 0.0, -90.0,  0.0, 37.04, 0.0, }
    ...
}
```

The header gives the format version (`0300` = 3.0, `0360`, `0500`, `0600`...), the encoding (`txt` or
`bin`) and the float size. Binary dotXSI is rejected with a message asking for an ASCII export.

## Parser (`dotxsi_parser.py`)

* One regular expression scans for quoted strings, comments (`//`, `/* */`, and `#` at the start of a
  token — `Material_#1` is a name, not a comment) and braces.
* Before each `{` the template header is matched at the end of the preceding text: an identifier,
  optionally followed by a name separated by whitespace only (values are always separated by `,` or `;`).
* Strings end at the next double quote. **Backslashes are not escape characters**: Softimage writes
  Windows paths with single backslashes (`"U:\hl2\...\Alyx\thigh.tga"`). The previous parser decoded
  `\t` as a tab and broke such paths.
* Each element keeps the spans of its own values; `tokens`, `values`, `floats()` and `ints()` convert on
  demand. Errors report line numbers (computed lazily from a newline index).

## Templates

Coverage on the HL2 corpus (counts from the census in [testing.md](testing.md)):

| Template | Count in corpus | Handling |
| --- | ---: | --- |
| `SI_FileInfo`, `SI_Scene` | 429 | metadata; scene frame range and rate (`FRAMES` or `SECONDS` timing) |
| `SI_CoordinateSystem` | 429 | stored as metadata (see [unknowns.md](unknowns.md)) |
| `SI_Angle` | 429 | 0 = degrees, 1 = radians; radians are converted for transforms, cameras and rotation curves |
| `SI_Ambience` | 429 | stored as metadata |
| `SI_MaterialLibrary` / `SI_Material` | 1,108 | 3.0 material: face colour RGBA, specular power, specular RGB, emissive RGB, shading model, ambient RGB |
| `SI_Texture2D` | 391 | image, mapping, size, crop, UV swap, repeat, alternate, scale, offset, projection matrix, blending, weights |
| `XSI_Material` / `XSI_Shader` | 0 (fixture) | 3.5+ shader parameters by name (`diffuse.red`...), image shaders (`tex`) |
| `XSI_ImageLibrary` / `XSI_Image` | 0 (fixture) | image name → file |
| `SI_Model`, `XSI_Model`, `SI_Null` | 24,192 | hierarchy; `MDL-`, `SRT-`... prefixes stripped from names |
| `SI_Transform` (SRT / BASEPOSE), `XSI_Transform`, `XSI_BasePose` | 24,193 | local transform; base pose used as armature rest |
| `SI_Visibility` | 23,826 | hidden objects are hidden in viewport and render |
| `SI_GlobalMaterial` | 23,823 | object material; `BRANCH` propagates to children, `INHERITED` defers to the parent |
| `SI_Mesh`, `SI_Shape` | 1,883 | `ORDERED` and `INDEXED` layouts; POSITION, NORMAL, COLOR, TEX_COORD_UV (with optional projection name) |
| `SI_PolygonList`, `SI_TriangleList`, `XSI_PolygonList`, `SI_TriStrip` | 1,851 | polygons with per-corner normal / UV / colour indices and a material |
| `SI_ShapeAnimation` | 5 | per-frame `SI_Shape` caches → shape keys keyed by the `SHPANIM` curve |
| `SI_Cluster` | 6 | point clusters → vertex groups |
| `XSI_CustomPSet` | 13 | parameters → custom properties (`<pset>.<name>`) |
| `SI_Camera`, `XSI_Camera` | 197 | position + interest (3.0) or projection settings (5.0+) |
| `SI_Light`, `XSI_Light` | 195 | point / spot / infinite; infinite lights store an orientation instead of an interest |
| `SI_IK_Root`, `SI_IK_Joint`, `SI_IK_Effector` | 154 | marks chain nodes (bones); joint length used for bone length |
| `SI_EnvelopeList`, `SI_Envelope` | 22 | per-deformer vertex weights → vertex groups + Armature modifier |
| `SI_FCurve` | 96,047 | see below |
| `XSI_Mixer`, `XSI_Action` | 363 / 366 | action clips grouping F-curves; ranges in seconds |

Unsupported templates are counted and reported once per file ("Skipped N unsupported X template(s)").

### SI_Material (3.0)

```
SI_Material <name> { r, g, b, a,  specular power,  sr, sg, sb,  er, eg, eb,  shading model,  ar, ag, ab,
                     SI_Texture2D { ... } }
```

Shading model: 0 constant, 1 Lambert, 2 Phong, 3 Blinn, 4 shadow object, 5 vertex colour.
Converted to a Principled BSDF: base colour (alpha → Alpha), roughness = sqrt(2 / (power + 2)) (the
usual Phong-exponent mapping), specular tint / level from the specular colour (0 for constant and
Lambert), emission from the emissive colour. Constant shading becomes an unlit material (colour driven
into Emission, black base). The ambient colour and raw specular power are kept as custom properties.

### SI_Texture2D

41 values: image file, mapping type (0 XY, 1 XZ, 2 YZ, 3 UV, 4 UV wrapped, 5 cylindrical, 6 spherical,
7 reflection), width, height, crop (u min, u max, v min, v max, pixels), UV swap, U/V repeat, U/V alternate,
U/V scale, U/V offset, a 4x4 projection matrix, blending type, blending, then ambient / diffuse / specular /
transparency / reflectivity / roughness weights.

The first texture feeds the base colour through an Image Texture node. Repeat, scale, offset and crop
become a Mapping node; alternate (mirror) sets the extension to MIRROR; blending < 1 mixes the texture
with the material colour; a transparency weight connects the image alpha. Non-UV projections are
approximated with the mesh UVs (noted on the material).

### Meshes

`SI_Shape` holds arrays per semantic. In the `INDEXED` layout every element is prefixed with its index;
elements are scattered into place. Polygon / triangle lists give per-polygon sizes, point indices and,
for each semantic in their information string (`NORMAL|TEX_COORD_UV`), one index per corner into the
shape arrays. All lists of a mesh are concatenated with NumPy; attributes missing from a list are filled
(zero UVs, white colours).

Polygons that reference points outside the shape are dropped with a warning instead of failing the
import (two lists in `models/Combine_Advisor/Advisor.XSI` reference 8 points past the end). Polygons
Blender would reject (repeated consecutive vertices, duplicate faces) are removed before the per-corner
data is written, so normals and UVs stay aligned.

### F-curves

```
SI_FCurve <name> { <object>, <parameter>, <interpolation>, <curve count>, <values per key>, <key count>, keys... }
```

A key is the frame followed by `curve count * values per key` numbers (the frame is not counted in
"values per key", as in the Crosswalk documentation; files that count it are also accepted). Layouts:

* `CONSTANT` / `LINEAR`: value.
* `HERMITE`: value, in slope, out slope → Bézier handles at a third of the neighbouring intervals.
* `CUBIC`: value, in tangent (dx, dy), out tangent (dx, dy), relative to the key → absolute Bézier handles.

Parameter names seen and supported: scripting names (`Bip01.kine.local.rotz`, `kine.local.posx`),
3.0 names (`XPOS`, `YROT`, `ZSCL`), multi-curve names (`POSITION`, `ROTATION`, `SCALING` with 3 curves),
dashed names (`TRANSLATION-X`), visibility (`NODEVIS`, `viewvis`, `rendvis`) and shape animation
(`SHPANIM-1`). Curves are attached to objects by name (full or short). Curves inside `XSI_Mixer` /
`XSI_Action` are grouped into actions; action ranges are converted from seconds with the scene rate.

### Shape animation

MAX2XSI bakes skinned characters into per-frame point caches:

```
SI_ShapeAnimation SHPANIM-joey_mesh { "LINEAR", 40, SI_Shape SHP-joey_mesh-0 {...} ... }
SI_FCurve joey_mesh-SHPANIM-1 { "joey_mesh", "SHPANIM-1", "LINEAR", 1, 1, 40, 1.0, 0.0, 2.0, 1.0, ... }
```

Each shape becomes a shape key `Frame N`. The `SHPANIM` curve maps frames to shape numbers; for every
shape a weight curve is written that is 1 where the curve equals the shape number and falls linearly to 0
at the neighbouring shapes, which reproduces linear blending between consecutive caches. Without the
curve, shape `k` is placed at scene start + k.

Some MAX2XSI animation exports contain an animated Biped and a static copy of its mesh but neither
`SI_Envelope` nor `SI_ShapeAnimation`. They also commonly reference 3ds Max material names without
exporting matching `SI_Material` definitions. The importer reports both conditions: skin weights and
polygon-to-texture assignments cannot be inferred safely from geometry or from nearby image files.

### Envelopes

`SI_Envelope { <mesh>, <deformer>, <count>, (vertex, weight) x count }`. Weights are percentages in
every file seen (detected per file: if any weight exceeds 1, all are divided by 100). Weights become
vertex groups named after the deformer; deformers become bones, and the mesh gets an Armature modifier
and is parented to the armature object (not to a bone), because Softimage envelopes cancel the motion
of the mesh's parents.
