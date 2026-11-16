# Unknowns and things to investigate

Each item names what is known, what is not, and a sample to start from (paths relative to the HL2
archive root, `hl2/hl2`). Items are ordered roughly by expected value for the HL2 corpus.

## Native scenes (XSI 3.x)

### Animation

1. **Action sources and mixer clips.** `TLShell/Sources` holds Action sources (`handpose01`) with item
   names like `Bip01_L_Finger4.kine.local.sclx`, but no key data was found next to the names, and the
   per-item index pairs (`01 <cnum> e0 02`) are not understood. Clips in `TLShell/Clips` and mixer tracks
   are not imported, so scenes whose animation lives only in the mixer import without it.
   Start: `models/Alyx_XSI/Scenes/A_gesture16.scn` (Sources object 347).
2. **F-curve header.** The u32 "curve type" before the key count takes the values 3, 2, 61, 31, 6; its
   meaning (extrapolation? bit flags?) and the bytes between the `UC1010` chunk and the default value are
   not decoded. Curves without the ±DBL_MAX clamp pair (27 in a 1-in-25 sample) use a fallback search.
3. **Curves on other parameters.** Only local position / rotation / scale curves on kinematic states are
   connected. Curves on camera and light parameters, visibility, shape weights, custom parameters and
   global kinematics are not.
4. **Expressions** (class `12723eb2`) are stored as text on the objects. Simple ones (`X = 0.0`,
   `A.kine.local.roty = B.kine.local.rotz * k`) could become drivers.
5. **Constraints and IK.** Every kinematic state references objects of classes `a2c21f92-96e3-11d3-b8a0-
   00a0c92469be` and `90994bb1-d357-11d2-8b80-00a024ee586f` (probably constraint / chain solver links).
   IK is not solved: if a chain is driven only by an animated effector, its bones stay at their stored pose.
6. **Frame rate and range.** The rate is inferred from key times (29.97 fits the HL2 scenes exactly); the
   scene's PlayControl settings were not located in the container.

### Geometry

7. **Blob sections before the node table**: runs starting `21 07 10 00 00 <nedges+1> ...` (bit-packed
   vertex/edge flags?) and `(cnum, f32)` pairs with value 10.0 (creases or hard-edge marks?).
8. **Polygon list header**: the k values after `<nnodes> <npolys>` (e.g. `8, 0, 3630, 2049, 63, 12, 2`).
9. **Edge table**: `(polygon cnum, side byte)` runs separated by `af 00`; not needed for polygons but
   likely holds hard edges. 5% of decoded meshes do not reproduce the stored edge count (reported as
   "not checked"); check whether those are non-manifold or have isolated edges.
   Start: run `tools`-style survey (see [testing.md](testing.md)) and list meshes with `edges_ok is None`.
10. **Normals, vertex colours and hard edges** of 3.x meshes are not decoded (Blender computes normals).
11. **Live operator stacks.** Meshes stored as generator + operators (`movecomponentop` x hundreds on
    ValveBiped bone display cubes, `deletecompop`, `extrudecompaxisop`...) are rebuilt from the generator
    only. Operator `b11c01fa-e464-11d5-81b4-00a0c9ac19a9` (372 occurrences in a 1-in-20 sample) and
    `752fcb3d-43a2-434b-aa4d-ccfa626b0f05` (15) are unidentified.
12. **Cluster shapes.** `ShapeWeights` (class `76332571`, on Alyx's body) and `clustershapecombiner`
    operators (facial shapes) are not decoded.
13. **Implicit objects** (`cube`, `sphere` kinds used as display shapes), NURBS curves (`crvlist`, 1,713 in
    a 1-in-20 sample), lattices and texture supports import as empties.

### Materials

14. **Phong parameters** beyond ambient / diffuse / specular: parameter objects `65020830`, `6512d740`,
    `652fdfd0`, `653da080`, `072c1742` (probably reflectivity, transparency, refraction, shininess...).
15. **Texture layering**: `ambient_blend` / `diffuse_blend` objects (class `3a3bd020`) with 14 colour
    children each; how they mix textures is unknown. Only the first image of a material is connected.
16. **Object-level materials**: the material of polygons outside every cluster comes from the largest
    cluster; the object's own material link (if any) was not found.
17. **Projection definitions**: each UV property has children `e6f26e87` and `c27897e0` (`_Def`), probably
    the projection type and transform. Not needed for stored UVs.

### Envelopes

18. **Weight map header**: after the deformer count comes a second count (`e4 03`), a colour table
    (4 bytes per deformer) and a point-sized block of zero bytes whose meaning is unknown.
19. Several envelope operators on one mesh, envelopes on non-mesh geometry, and envelopes in 2011+ scenes
    were not seen / tested.
    In the 1-in-8 batch, 19 envelopes failed to decode ("envelope weights could not be decoded"):
    likely a different header or a point cluster whose element order is not the mesh order.

### Container

20. `.scntoc` files (37 in the archive) are XML tables of contents (model references, external files);
    referenced models are not loaded. Referenced/external models (`.emdl` references) are not followed.
21. Scenes with only a legacy `Contents` stream (very old saves) import hierarchy and transforms only.
    The HL2 `.emdl` models use this legacy embedded-definition stream: on the 7 distinct models, 408
    objects have unidentified kinds and 50 polygon meshes have no recognized geometry blob, so they import
    as empties. Start: `models/Humans/Female/Models/Female_Rig/ValveBiped.emdl`.

## dotXSI

22. **SI_CoordinateSystem**: MAX2XSI files declare `1,0,1,0,5,2` (Z up, Y out) but store Y-up data under a
    -90° X `MAXSceneRoot`. The declaration is ignored; files without that root but with a Z-up
    declaration may need *Source Up Axis = Z*. No such file is in the corpus.
23. **SI_Texture2D**: the projection matrix (16 values) is ignored; non-UV mapping types are approximated
    with UVs; blending types 2 and 3 (both seen) are treated the same.
24. **SI_Light type 3 orientation**: interpreted as Euler XYZ degrees of the light frame (light looking down
    local -Z). All 195 corpus lights are XSI's default infinite light; verify against Softimage.
25. **SI_IK_Joint** values beyond the length (resolution `2D` / `DEFAULT`, preferred angles, joint limits)
    and **SI_IK_Root** chain lists are kept but not turned into IK constraints.
26. **XSI_Mixer** values (`0,1,0,0,0,0`) and the third **XSI_Action** value are not interpreted.
27. **SI_Cluster** has no element-type field in 3.0; the type is guessed from the name (Point / Polygon /
    Edge), and only point clusters become vertex groups.
28. **dotXSI 5.0 / 6.0** templates (`XSI_Shape`/`XSI_VertexList`, `XSI_PolygonList`, `XSI_Transform`,
    `XSI_Camera`...) follow the Crosswalk headers but no 5.0+ production files were available for testing.
    Binary dotXSI is not supported.
29. `models/props_borealis/iceberg01a.XSI` is an empty file (0 bytes).
