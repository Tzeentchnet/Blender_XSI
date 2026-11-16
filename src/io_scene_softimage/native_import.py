# SPDX-License-Identifier: GPL-3.0-or-later
"""Native .scn / .emdl scenes: conversion of the vendored reader's result into `model.Scene`.

The conversion is Blender independent (`to_model`); `build` then hands the scene to the shared
`blender_scene.SceneBuilder`, with native render-tree materials supplied by `native_materials`.
"""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np

from . import convert, primitives
from .model import AnimationCurve, Camera, Light, Mesh, Scene, SceneObject, Transform

FPS_CANDIDATES = (29.97, 30.0, 25.0, 24.0, 60.0, 59.94, 50.0, 23.976, 48.0, 120.0)
BONE_KINDS = {"root": "IK_ROOT", "bone": "IK_JOINT", "eff": "IK_EFFECTOR"}
CHANNEL_PROP = {"pos": "location", "rot": "rotation", "scl": "scale"}


def detect_frame_rate(times: list[float]) -> float:
    """The frame rate that puts the key times (seconds) closest to whole frames (XSI 3.x used 29.97)."""
    values = np.asarray([t for t in times if t != 0.0], dtype=np.float64)
    if not len(values):
        return 30.0
    best, best_error = 30.0, float("inf")
    for fps in FPS_CANDIDATES:
        frames = values * fps
        error = float(np.mean(np.abs(frames - np.round(frames))))
        if error < best_error - 1e-6:
            best, best_error = fps, error
    return best


def _transform(native) -> Transform:
    position = tuple(float(v) for v in native.pos)
    rotation = tuple(float(v) for v in native.rot)
    scale = tuple(float(v) for v in native.scl)
    quaternion = tuple(float(v) for v in native.quat)
    transform = Transform(position=position, rotation=rotation, scale=scale)
    if np.linalg.norm(quaternion) > 1e-9:
        from_quat = convert.quaternion_matrices(np.array([quaternion]))[0]
        from_euler = convert.euler_matrices(np.array([rotation]))[0]
        if np.abs(from_quat - from_euler).max() > 1e-4:
            transform.quaternion = quaternion   # the Euler values are stale or use another order
    return transform


def _camera(settings: dict | None) -> Camera:
    settings = settings or {}
    camera = Camera()
    if settings.get("proj", 1) == 0:
        camera.projection = "ORTHO"
        camera.ortho_height = float(settings.get("orthoheight", 10.0))
    if settings.get("fov"):
        camera.field_of_view = float(settings["fov"])
        camera.fov_type = "VERTICAL" if settings.get("fovtype", 1) == 0 else "HORIZONTAL"
    camera.near = float(settings.get("near", camera.near))
    camera.far = float(settings.get("far", camera.far))
    if settings.get("aspect"):
        camera.aspect = float(settings["aspect"])
    return camera


def _light(settings: dict | None) -> Light:
    settings = settings or {}
    light = Light(kind={0: "POINT", 1: "SUN", 2: "SPOT"}.get(settings.get("Type", 0), "POINT"))
    shader = settings.get("soft_light")
    intensity = 0.75
    if shader:
        light.color = (shader["red"], shader["green"], shader["blue"])
        light.shadows = bool(shader["shadow"])
        intensity = shader["intensity"]
        light.spread_angle = float(shader.get("spread", 0.0))
    light.intensity = intensity / 0.75
    light.cone_angle = float(settings.get("LightCone", 60.0))
    return light


def _mesh(so, notes: list[str]) -> Mesh | None:
    source = so.mesh
    points = polygons = None
    if source is not None:
        points, polygons = source.points, source.polygons
    elif so.generator is not None:
        from .native_scn import rebuild
        rebuilt = rebuild.primitive_mesh(so.generator)
        if rebuilt is None:
            rebuilt = primitives.generate(so.generator["type"], so.generator.get("params", {}))
            if rebuilt is not None:
                notes.append("rebuilt from generator parameters; Softimage point order and stacked "
                             "operators were not reproduced")
        if rebuilt is not None:
            points, polygons = rebuilt
    if points is None or not polygons:
        return None
    sizes = np.fromiter((len(p) for p in polygons), dtype=np.int32, count=len(polygons))
    corners = np.fromiter((i for p in polygons for i in p), dtype=np.int32, count=int(sizes.sum()))
    mesh = Mesh(points=np.asarray(points, dtype=np.float64).reshape(-1, 3), polygon_sizes=sizes,
                corner_vertices=corners)
    # Materials: the object's material, then cluster materials (polygon clusters).
    names = [so.material] if so.material else []
    index = np.zeros(len(sizes), dtype=np.int32)
    for cluster in getattr(so, "cluster_materials", []) or []:
        if cluster.material not in names:
            names.append(cluster.material)
        slot = names.index(cluster.material)
        members = np.asarray([i for i in cluster.polygons if 0 <= i < len(sizes)], dtype=np.int64)
        if len(members):
            index[members] = slot
    mesh.material_names = names
    mesh.face_material_index = index if names else None
    # UV sets (frozen meshes only): a merged map first when projections only cover parts of the mesh.
    uv_sets = list(getattr(so, "uvs", []) or [])
    if uv_sets and source is not None:
        from .native_scn import uvs as native_uvs
        merged = native_uvs.merged(uv_sets)
        if merged is not None:
            uv_sets.insert(0, merged)
        for uv in uv_sets:
            if len(uv.corners) == len(corners):
                name = uv.name
                while name in mesh.uv_layers:
                    name += "_"
                mesh.uv_layers[name] = np.asarray(uv.corners, dtype=np.float64)
    weights = getattr(so, "envelope", None)
    if weights:
        mesh.envelope_weights = {
            name: (np.asarray(v, dtype=np.int64), np.asarray(w, dtype=np.float64))
            for name, (v, w) in weights.items()
        }
    return mesh


def _curves(so, fps: float) -> list[AnimationCurve]:
    out = []
    for channel, index, native in getattr(so, "animation", []) or []:
        prop = CHANNEL_PROP.get(channel)
        if prop is None or not native.times:
            continue
        frames = np.asarray(native.times, dtype=np.float64) * fps
        values = np.asarray(native.values, dtype=np.float64)
        modes = set(native.interpolation)
        interpolation = "CONSTANT" if modes == {"CONSTANT"} else "LINEAR" if modes == {"LINEAR"} else "CUBIC"
        curve = AnimationCurve(target=so.name, channel=f"{so.name}.kine.local.{channel}{'xyz'[index]}",
                               interpolation=interpolation, frames=frames, values=values,
                               prop=prop, index=index)
        if interpolation == "CUBIC":
            left = np.asarray(native.left, dtype=np.float64)
            right = np.asarray(native.right, dtype=np.float64)
            left[:, 0] *= fps
            right[:, 0] *= fps
            curve.handles_left, curve.handles_right = left, right
        out.append(curve)
    return out


def _global_matrix(native) -> np.ndarray:
    """Softimage global matrix of a native object's saved global kinematic state."""
    transform = _transform(native)
    return convert.transform_matrix(transform.position, transform.rotation, transform.scale,
                                    transform.rotation_order, transform.quaternion)


def _matrix_transform(matrix: np.ndarray) -> Transform:
    locations, quaternions, scales = convert.decompose(matrix[None])
    return Transform(position=tuple(locations[0]), scale=tuple(scales[0]),
                     quaternion=tuple(quaternions[0]))


def _bind_poses(sc, converted: dict[int, SceneObject]) -> int:
    """Base poses (armature rest) from envelope bind matrices.

    Bind globals are derived top-down: a node with a bind matrix uses it; a node without one (chain roots
    and effectors between deformers, helpers) keeps its saved local transform relative to its parent's
    bind global; a top-level node without one uses its saved global pose. Local bind = parent bind^-1 @
    bind."""
    if not any(getattr(so, "bind_matrix", None) is not None for so in sc.objects):
        return 0
    bind_global: dict[int, np.ndarray] = {}
    count = 0

    def local_of(node: SceneObject) -> np.ndarray:
        t = node.transform
        return convert.transform_matrix(t.position, t.rotation, t.scale, t.rotation_order, t.quaternion)

    def visit(so, parent_bind):
        nonlocal count
        node = converted.get(id(so))
        if node is None:
            return
        own = getattr(so, "bind_matrix", None)
        if own is not None:
            global_bind = own
            local = np.linalg.solve(parent_bind, own) if parent_bind is not None else own
            if not np.allclose(local, local_of(node), atol=1e-4):
                node.base_pose = _matrix_transform(local)
                count += 1
        elif parent_bind is not None:
            global_bind = parent_bind @ local_of(node)
        else:
            global_bind = _global_matrix(so.glob)
        bind_global[id(so)] = global_bind
        for child in so.children:
            visit(child, global_bind)

    for root in sc.roots:
        visit(root, None)
    return count


def to_model(sc) -> Scene:
    """Blender-independent description of a native scene."""
    scene = Scene(source_path=str(sc.path), format_version="native", encoding="bin", float_bits=64,
                  name=Path(sc.path).stem)
    scene.warnings.extend(str(w) for w in sc.warnings)
    times = [t for so in sc.objects for _c, _i, curve in getattr(so, "animation", []) for t in curve.times]
    fps = detect_frame_rate(times)
    scene.frame_rate = fps
    if times:
        scene.frame_start = math.floor(min(times) * fps + 1e-6)
        scene.frame_end = math.ceil(max(times) * fps - 1e-6)
        scene.metadata["frame_rate_detected"] = fps
    converted: dict[int, SceneObject] = {}

    def convert_node(so) -> SceneObject:
        notes: list[str] = []
        node = SceneObject(name=so.name or "Object", transform=_transform(so.local),
                           source_template=f"native:{so.kind}")
        kind = so.kind
        if kind == "polymsh":
            node.mesh = _mesh(so, notes)
            node.kind = "MESH" if node.mesh is not None else "EMPTY"
            if node.mesh is None:
                notes.append("polygon mesh not decoded")
            ops = so.ops or (so.generator or {}).get("ops") or []
            if ops:
                node.properties["softimage_unapplied_operators"] = ", ".join(ops)
        elif kind == "camera":
            node.kind, node.camera = "CAMERA", _camera(so.camera)
        elif kind == "light":
            node.kind, node.light = "LIGHT", _light(so.light)
        elif kind in BONE_KINDS:
            node.kind, node.bone_role = "BONE", BONE_KINDS[kind]
        elif kind == "model":
            node.kind = "EMPTY"
            node.properties["softimage_model"] = True
        elif kind not in (None, "null"):
            node.properties["softimage_kind"] = kind
            if kind not in {"CameraRoot", "CameraInterest", "SpotRoot", "SpotInterest"}:
                notes.append(f"{kind} imported as an empty")
        node.visible = getattr(so, "view_visible", True)
        node.render_visible = getattr(so, "render_visible", True)
        if so.material:
            node.material = so.material
        node.animation = _curves(so, fps)
        if so.expressions:
            node.properties["softimage_expressions"] = "; ".join(so.expressions)[:4000]
        if notes:
            node.properties["softimage_import_notes"] = "; ".join(notes)
        converted[id(so)] = node
        for child in so.children:
            node.children.append(convert_node(child))
        return node

    for root in sc.roots:
        scene.objects.append(convert_node(root))
    scene.link_parents()
    deformers = {name.casefold() for so in sc.objects for name in (getattr(so, "envelope", None) or {})}
    for node in scene.walk():
        if node.name.casefold() in deformers and not node.bone_role:
            node.bone_role = "DEFORMER"
    moved = _bind_poses(sc, converted)
    if moved:
        scene.metadata["bind_pose_differs"] = moved
    expressions = sum(len(so.expressions) for so in sc.objects)
    if expressions:
        scene.warnings.append(f"{expressions} expression(s) drive parameters; they are stored on the objects "
                              "as text (softimage_expressions) and not evaluated.")
    return scene


def build(context, sc, report, use_collection: bool = True, options=None):
    from . import blender_scene, native_materials

    if options is None:
        options = blender_scene.ImportOptions(use_collection=use_collection)
    scene = to_model(sc)
    lookup = None
    if options.import_materials:
        builder = native_materials.Builder(sc, report, import_textures=options.import_textures,
                                           search=options.search_textures)
        lookup = builder.material
    objects = blender_scene.build(context, scene, report=report, options=options, material_lookup=lookup)
    return objects
