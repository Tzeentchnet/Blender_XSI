# Native `.scn` / `.emdl` files

Native scenes and models are Microsoft Compound File Binary (OLE2 "structured storage") containers.
The vendored reader (`native_scn/`, originally from Silica, reverse engineered on Softimage 6.x - 2015
scenes) was extended for scenes and models saved by **XSI 1.x - 4.x**, including the versions used for
the Half-Life 2 sources (`TLShell/VersionInfo` lists saves such as `3.01.2002.1127`,
`3.5.2003.0520`, `3.51.2003.1208`).

Notation: `cnum` is the compact unsigned integer (high nibble F/E/D/C = 4/12/20/28 value bits), `cdouble`
the compact double (`ff` 0, `fe` 1, `fd` -1, `fc` f64, `fb` f32, `fa` i32, `f9` i16, `f8` i8). GUIDs are
written in full where they identify a class or parameter.

## Container layout

```
Version                          "0030"
TLShell/VersionInfo              save history (UTF-16)
TLShell/Objects/Scene.Root/{Objects, GUIDS, Version, List.Model}
TLShell/Objects/Scene.Root/Scene.Models/InternalModelN/{Objects, GUIDS, ...}   one storage per model
TLShell/Sources                  image sources, action sources, audio sources
TLShell/Clips                    image clips (and other clips)
TLShell/Properties, Passes, Views, Layers, ...
```

Each `Objects` stream starts with `IGAM`, a root block, then nested object blocks
`<marker> <u32 size> 03 <handle cnum> <class u16> <pbase u16> ...`. Class and parameter ids index the
stream's `GUIDS` table. See `native_scn/records.py` for the record grammar.

### Block markers in XSI 3.x

Markers are numbered per stream in first-use order. In 3.x streams the object/reference marker is `04`
(the root object writes an extra block first) but **the parameter block keeps marker `01`**. The reader
derived the parameter marker as `object marker - 2` and so found no parameters in any 3.x object
(no names, transforms or references). Both markers are now accepted; parameter blocks are searched up to
512 bytes into the object (3.x texture nodes put their UV projection name first), and references are
scanned even when no parameter block is found.

Effect on a 1-in-20 sample of the corpus: objects of unknown kind went from 29,133 to 90; meshes found
from 10 to 3,172.

## Polygon meshes (XSI 1.x BMSH layout)

Early `.emdl` files can store frozen geometry in nested `BMSH0010` / `CMSH2010` chunks. The
`MSHI2010` table is followed by polygon-node, point, edge and polygon tables:

```
<nnodes> <capacity> <stride>
nnodes x (<vertex> <signed oriented edge> <reverse node>)
<npts> <capacity> <stride>
3 * npts cdouble
12 compact point-metadata values
<nedges + 1> <capacity> <stride>
(nedges + 1) x (4 compact values)                 entry zero is a sentinel
<npolys> <capacity> <stride>
npolys x (<size> <flag> <flag> <size node ids>)
```

These streams add signed compact integers: high nibbles A/9/8/7 are sign-extended 4/12/20/28-bit
values. The oriented edge ids and reverse-node permutation make the topology self-validating.

The mesh object itself can have a target bind matrix before the deformer bind matrices in its envelope
operator. This matrix is required: BMSH points are stored in a small object-local space, while the
skeleton uses the model's full bind space. Applying only the saved kinematic state leaves the mesh at
the wrong scale and orientation.

## Polygon meshes (XSI 3.x layout)

The polymsh primitive keeps its geometry in a sized blob (`01 <A> <u32> <B> <u32>`) as in later versions,
but the content is sequential compact numbers instead of `f0 f1` arrays:

```
<npts> <nedges + 1> <npolys> <nnodes>              header (as in later versions)
...                                                vertex flag runs, crease lists (not decoded)
f1 <npts> <k>  k x (<cnum>, <f32>)                  k = 0 usually; pairs look like vertex creases (value 10.0)
<npts> <nnodes> <x>                                node table header
nnodes x (<vertex cnum> <slot byte>)               node -> vertex; slot = the node's rank around its vertex
<npts> <capacity> <x>                              point header (x = 64 or 10 seen)
3 * npts cdouble                                   point positions
<npolys> <nedges + 1> ...                          edge table: (polygon cnum, side byte) runs, not needed
<nnodes> <npolys> <k values>                       polygon list header (k from 2 to 36 values seen)
npolys x (<size> <size node cnums>)                polygons as node indices
```

Compact numbers are **not always written at their shortest width** (3486 appears as `de d9 00`, a 20-bit
form), so every search tries all widths. The decode is self-validating: node vertices are below `npts`,
the point header immediately follows the node table, and the polygon list uses every node exactly once.
On the 1-in-20 sample, 2,486 of 2,489 frozen meshes decode; 95% reproduce the stored edge count exactly
(the rest are reported as "not checked", see [unknowns.md](unknowns.md)).

Live primitives (generator + operator stack, e.g. a cube with hundreds of `movecomponentop` used as bone
display geometry in the ValveBiped rigs) store no frozen geometry; they are rebuilt from generator
parameters (`native_scn/rebuild.py`, `primitives.py`) without the stacked operators.

## UVs (texture projections)

UV property objects (class `6630d811-0699-11d1-b723-00aa0068bf56`, named like the projection) hold the
distinct UV values `<marker> <u32> <count> count x (u, v) f32` and an index array. In 3.x the index array
is `<marker> <u32> <nnodes> <count> <entries>` (no `01 00` between the counts, unlike 2015) with 1/2/4-byte
entries; all-bits-set entries mean "no UV here".

**The entries follow the element order of the sample cluster that owns the projection**
(class `600e50f2-52fc-11d2-a924-00a024190119`, e.g. `Head_and_Hair`), not node order. A sample cluster
lists every node in its own order: `<marker> <u32> 01 e5 05 00 ff 00 ff <count> <count node cnums>` (the
byte after the size is `01`, where later versions write `00`). Ordered this way, Alyx's projections give
1.08 - 1.26 distinct UVs per vertex (real seams) instead of ~4.5 (noise), and the two main projections
partition the mesh (8,378 + 11,248 nodes of 19,567).

When a mesh has several partial projections (merged meshes), each becomes a UV map with zeros where it
has no value, and a merged `UVMap` (first projection that covers a corner wins) is added first.

## Materials (XSI 3.x)

There is no `TLShell/Shader Definitions` stream and no render-tree `MFDS` chunks. Materials and shaders
are objects in the model's own stream:

```
polymsh -> cluster container (60f7ada0) -> polygon cluster (600e50f1, e.g. "tex_face_hair")
        -> Material (4c4f7500-51ef-11d0-854e-00a02417d029)
        -> shader:  7e5bfb8c-455d-11d1-864b-00a0c91412d9  "Material__N" (Softimage|3D-style shader)
                    67120bbe-c98c-11d1-9723-00a0243e3672  Phong
                    5b003a9d-868a-11d4-9330-00e0293d48c6  Constant
```

* Colour parameters are separate objects whose class identifies the parameter: `3515cc71` ambient,
  `3515cc72` diffuse, `3515cc73` specular (Material__N shader); `64e4ffa0` ambient, `64e99530` diffuse,
  `64efb1f0` specular (Phong). They hold RGBA as parameters `8c80deda`, `8c80dedb`, `8c80dedc`, `8c80ded9`.
* The texture input (`38d56c24`) leads to a texture node (`cf28e1e4`) that stores its UV projection name
  in UTF-16 before its parameter block, and connects to an image node (`7e5bfb8d`, "SoftColor").
* The image node's input is a cross-stream reference `0e b0 ff ff ff ff <handle cnum> <class u16>` to an
  **image clip in `TLShell/Clips`**. Clips and image **sources in `TLShell/Sources`** share a name
  (`alyx_faceandhair_tga`); the source stores the path twice (one copy runs on into a length byte).
  Softimage's placeholder clips (`noIcon_pic*`) are ignored.
* Polygon clusters list their polygon indices with the same element-list block as sample clusters.
  The largest cluster's material is used as the object material.

XSI 1.x BMSH models can instead embed named Lambert, Phong, Cook-Torrance and Constant shaders below
their material and polygon-cluster objects. Their diffuse colours are imported directly; external
images are not inferred when the package contains no image sources.

## Visibility

The visibility property (class `11ebe301-a20c-11d0-8478-00a024c7919c`) holds boolean parameters;
`d636f4d0-d94b-11d1-b0ed-00a024c79287` is view visibility and `a9d242e0-d948-11d1-b0ed-00a024c79287`
render visibility (both off on cameras and chain effectors in the HL2 rigs).

## Animation (F-curves)

A driven parameter is recorded in its owner's **connection table**, written after the owner's parameter
records: entries `01 00 <parameter id u16> <flags> <x> ...`, flags `07` = connected input, `03` = not
connected. The owner's references after the table belong to the connected entries, in table order.
For kinematic states (`kine_local`, class `77f74c81-7010-11d1-96f5-00a0243e3463`), parameter ids resolve
through the GUID table to `pos x/y/z`, `rot x/y/z` (degrees), `scl x/y/z`.

F-curve objects (class `e2a86051-f669-11d1-8d60-080036f3cc02`):

```
... UC1010 chunk ...
f64 default value, f64 minimum, f64 maximum      (usually -DBL_MAX / DBL_MAX)
u32 curve type                                   (3, 2, 61, 31, 6 seen; meaning not decoded)
u32 key count
key count x { f64 time (seconds), f64 value, f64 value (repeated), f64 left handle time, f64 left handle
              value, f64 right handle time, f64 right handle value, u32 interpolation }
```

Key interpolation: 1 constant, 2 linear, 3 cubic. Handles are absolute (slopes on both sides of a key
agree). Times are seconds; in the HL2 scenes they are whole frames at **29.97 fps** (NTSC, XSI's default
then), e.g. 0.46713 s = frame 14. The importer picks the frame rate that puts all key times closest to
whole frames (29.97, 30, 25, 24, 60, ...).

Expressions (class `12723eb2-7ddb-11d0-a57f-00a0c91412de`) carry their text in a string parameter
(`ValveBiped.HandControlPosL.kine.local.roty = 0.0`); they are stored on the objects, not evaluated.

Action sources in `TLShell/Sources` (e.g. `handpose01`) list item names (`Bip01_L_Finger4.kine.local.sclx`)
but no keys were found next to them; see [unknowns.md](unknowns.md).

## Envelopes (skinning)

The envelope operator (class `470cf890-d3aa-11d0-996c-00a0243f0e60`) refers to: the polymsh, the mesh's
global kinematic state (twice), the point cluster it applies to (`e4dd8e40`), the weight map
(`c0fbfe80-e7f4-11d0-996d-00a0243f0e60`), then **one pair per deformer**: the deformer's bind matrix object
(`12f18b50-d380-11d0-9225-08003629ea02`) followed by the deformer's global kinematic state (which
identifies the deformer object).

* **Bind matrix**: 16 compact doubles, a 4x4 matrix in row-vector convention (translation in the last
  row): the deformer's global transform when the envelope was set.
* **Weight map**: a header (deformer count, per-deformer display colours, point count, a point-sized
  block of zeros), then a run-length encoded **deformer x point** matrix of float32 weights in percent.
  Each run is a compact number `v = (skip << 16) | count` followed by `count` float32 values: skip `skip`
  cells, then store `count` cells. Rows are deformers in operator order, columns the cluster's points.

On Alyx (`models/Alyx_XSI/Scenes/A_gesture16.scn`) the matrix is 76 x 4,020, the runs consume the object
to its last byte, and every point's weights sum to 100.

Bind matrices become the armature rest pose. Bind globals are derived top-down: nodes without a bind
matrix (chain roots, effectors, helpers between deformers) keep their saved local transform relative to
their parent's bind global.

In early BMSH envelopes, `For_envelope` lists deformers in the same order as `EnvelopSelCls`,
`EnvelopSelCls1`, ... and the operator's bind matrices. Populated selection clusters are disjoint and
therefore represent explicit rigid 100% assignments. The first matrix is the target mesh bind matrix;
the following matrices establish the deformer rest transforms. Legacy kinematic records store saved
global transforms, so the importer derives parent-relative locals before building armatures.
