# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender adapter for the vendored native Softimage scene reader."""

from __future__ import annotations

from math import radians

import bpy
from mathutils import Euler, Matrix, Quaternion, Vector

from . import native_materials
from .blender_scene import LIGHT_POWER, convert_point
from .native_scn import rebuild


C = Matrix.Rotation(radians(90.0), 4, "X")
C_INV = C.inverted()
FIXED_KINDS = {"camera", "light"}


def _local_matrix(transform) -> Matrix:
    quaternion = Quaternion(transform.quat)
    if quaternion.magnitude < 1e-9:
        quaternion = Euler(
            tuple(radians(value) for value in transform.rot),
            "XYZ",
        ).to_quaternion()
    else:
        quaternion.normalize()
    return Matrix.LocRotScale(
        Vector(transform.pos),
        quaternion,
        Vector(transform.scl),
    )


def _blender_matrix(transform, own_fix: bool, parent_fix: bool) -> Matrix:
    matrix = C @ _local_matrix(transform) @ C_INV
    if parent_fix:
        matrix = C_INV @ matrix
    if own_fix:
        matrix = matrix @ C
    return matrix


def build(context, scene, report, use_collection: bool = True):
    collection = context.collection
    if use_collection:
        name = bpy.path.display_name_from_filepath(scene.path)
        collection = bpy.data.collections.new(name)
        context.collection.children.link(collection)
    for selected in context.selected_objects:
        selected.select_set(False)
    made: dict[int, bpy.types.Object] = {}
    objects: list[bpy.types.Object] = []

    def make(source):
        if source.kind == "polymsh" and source.mesh is not None:
            mesh = bpy.data.meshes.new(source.name)
            mesh.from_pydata(
                [convert_point(point) for point in source.mesh.points],
                [],
                source.mesh.polygons,
            )
            mesh.update()
            obj = bpy.data.objects.new(source.name, mesh)
            if source.ops:
                obj["softimage_unapplied_operators"] = ", ".join(source.ops)
            return obj
        if source.kind == "polymsh" and source.generator is not None:
            rebuilt = rebuild.primitive_mesh(source.generator)
            if rebuilt is not None:
                points, polygons = rebuilt
                mesh = bpy.data.meshes.new(source.name)
                mesh.from_pydata(
                    [convert_point(point) for point in points],
                    [],
                    polygons,
                )
                mesh.update()
                obj = bpy.data.objects.new(source.name, mesh)
                if source.generator.get("ops"):
                    obj["softimage_unapplied_operators"] = ", ".join(
                        source.generator["ops"]
                    )
                return obj
            fallback = _fallback_primitive(source)
            if fallback is not None:
                fallback["softimage_live_primitive"] = source.generator["type"]
                fallback["softimage_import_notes"] = (
                    "Rebuilt from generator parameters; exact Softimage point order "
                    "and stacked operators were not preserved."
                )
                return fallback
            report(
                {"WARNING"},
                f"{source.name}: live {source.generator['type']} could not be rebuilt; "
                "imported as an empty.",
            )
        if source.kind == "camera":
            settings = source.camera or {}
            camera = bpy.data.cameras.new(source.name)
            if settings.get("proj", 1) == 0:
                camera.type = "ORTHO"
                camera.ortho_scale = settings.get("orthoheight", 10.0)
            elif settings.get("fov"):
                camera.sensor_fit = (
                    "VERTICAL" if settings.get("fovtype", 1) == 0 else "HORIZONTAL"
                )
                if camera.sensor_fit == "VERTICAL":
                    camera.angle_y = radians(settings["fov"])
                else:
                    camera.angle_x = radians(settings["fov"])
            camera.clip_start = settings.get("near", camera.clip_start)
            camera.clip_end = settings.get("far", camera.clip_end)
            return bpy.data.objects.new(source.name, camera)
        if source.kind == "light":
            settings = source.light or {}
            kind = {0: "POINT", 1: "SUN", 2: "SPOT"}.get(
                settings.get("Type", 0), "POINT"
            )
            light = bpy.data.lights.new(source.name, kind)
            shader = settings.get("soft_light")
            intensity = 0.75
            if shader:
                light.color = (shader["red"], shader["green"], shader["blue"])
                light.use_shadow = bool(shader["shadow"])
                intensity = shader["intensity"]
            light.energy = LIGHT_POWER[kind] * intensity / 0.75
            if kind == "SPOT":
                cone = settings.get("LightCone", 60.0)
                light.spot_size = radians(cone)
                if shader and cone:
                    light.spot_blend = max(
                        0.0, min(1.0, shader["spread"] / cone)
                    )
            return bpy.data.objects.new(source.name, light)
        return bpy.data.objects.new(source.name, None)

    def walk(source, parent_source):
        obj = make(source)
        collection.objects.link(obj)
        if parent_source is not None:
            obj.parent = made[id(parent_source)]
        obj.matrix_basis = _blender_matrix(
            source.local,
            own_fix=source.kind in FIXED_KINDS,
            parent_fix=(
                parent_source is not None and parent_source.kind in FIXED_KINDS
            ),
        )
        made[id(source)] = obj
        objects.append(obj)
        for child in source.children:
            walk(child, source)

    for root in scene.roots:
        walk(root, None)
    native_materials.apply(scene, made, report)
    for obj in objects:
        obj.select_set(True)
    if objects:
        context.view_layer.objects.active = objects[0]
    return objects


def _fallback_primitive(source):
    import bmesh

    generator = source.generator
    kind = generator["type"]
    params = generator["params"]
    mesh = bpy.data.meshes.new(source.name)
    bmesh_data = bmesh.new()
    freed = False
    try:
        if kind == "cube":
            bmesh.ops.create_cube(
                bmesh_data,
                size=float(params.get("length", 8.0)),
            )
        elif kind == "sphere":
            bmesh.ops.create_uvsphere(
                bmesh_data,
                u_segments=max(3, int(params.get("subdivu", 8))),
                v_segments=max(3, int(params.get("subdivv", 8))),
                radius=float(params.get("radius", 4.0)),
            )
        elif kind == "grid":
            u = max(1, int(params.get("subdivu", 8)))
            v = max(1, int(params.get("subdivv", 8)))
            bmesh.ops.create_grid(
                bmesh_data,
                x_segments=u + 1,
                y_segments=v + 1,
                size=1.0,
            )
            scale_x = float(params.get("ulength", 8.0)) / 2.0
            scale_y = float(params.get("vlength", 8.0)) / 2.0
            for vertex in bmesh_data.verts:
                vertex.co.x *= scale_x
                vertex.co.y *= scale_y
        elif kind in {"cylinder", "cone"}:
            radius = float(params.get("radius", 4.0))
            bmesh.ops.create_cone(
                bmesh_data,
                cap_ends=True,
                cap_tris=False,
                segments=max(3, int(params.get("subdivu", 8))),
                radius1=radius,
                radius2=radius if kind == "cylinder" else 0.0,
                depth=float(params.get("height", 8.0)),
            )
        elif kind == "disc":
            bmesh.ops.create_circle(
                bmesh_data,
                cap_ends=True,
                cap_tris=False,
                segments=max(3, int(params.get("subdivu", 8))),
                radius=float(params.get("outerradius", 4.0)),
            )
        elif kind == "icosahedron":
            bmesh.ops.create_icosphere(
                bmesh_data,
                subdivisions=1,
                radius=float(params.get("radius", 4.0)),
            )
        else:
            bmesh_data.free()
            freed = True
            bpy.data.meshes.remove(mesh)
            return None
        bmesh_data.to_mesh(mesh)
    finally:
        if not freed:
            bmesh_data.free()
    return bpy.data.objects.new(source.name, mesh)
