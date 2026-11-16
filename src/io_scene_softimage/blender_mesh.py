# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender meshes from `model.Mesh` with bulk `foreach_set` calls (no per-element Python loops)."""

from __future__ import annotations

from typing import Callable

import bpy
import numpy as np

from .convert import AxisConversion
from .model import Mesh

Report = Callable[[set[str], str], None]


def _polygon_mask(sizes: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """True for polygons Blender accepts: at least 3 corners and no repeated consecutive vertex."""
    count = len(sizes)
    if count == 0:
        return np.zeros(0, dtype=bool)
    starts = np.zeros(count, dtype=np.int64)
    np.cumsum(sizes[:-1], out=starts[1:])
    following = np.arange(1, len(corners) + 1)
    ends = starts + sizes - 1
    valid_sizes = sizes > 0
    following[ends[valid_sizes]] = starts[valid_sizes]
    repeated = corners == corners[np.minimum(following, len(corners) - 1)]
    repeated_per_polygon = np.zeros(count, dtype=bool)
    if len(corners):
        polygon_of_corner = np.repeat(np.arange(count), sizes)
        np.logical_or.at(repeated_per_polygon, polygon_of_corner, repeated)
    valid = (sizes >= 3) & ~repeated_per_polygon
    return valid & ~_duplicate_polygons(sizes, corners, valid)


def _duplicate_polygons(sizes: np.ndarray, corners: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """True for polygons that use exactly the same vertices as an earlier polygon (Blender's validation
    would delete them, which would shift every per-corner array)."""
    duplicate = np.zeros(len(sizes), dtype=bool)
    starts = np.zeros(len(sizes), dtype=np.int64)
    if len(sizes) > 1:
        np.cumsum(sizes[:-1], out=starts[1:])
    for size in np.unique(sizes[valid]):
        members = np.flatnonzero(valid & (sizes == size))
        if len(members) < 2:
            continue
        rows = corners[starts[members][:, None] + np.arange(size)[None, :]]
        keys = np.sort(rows, axis=1)
        _unique, first = np.unique(keys, axis=0, return_index=True)
        repeated = np.ones(len(members), dtype=bool)
        repeated[first] = False
        duplicate[members[repeated]] = True
    return duplicate


def build_mesh(name: str, source: Mesh, conv: AxisConversion, report: Report) -> tuple[bpy.types.Mesh, np.ndarray | None]:
    """The Blender mesh and, when polygons had to be dropped, the kept-polygon mask."""
    sizes = np.asarray(source.polygon_sizes, dtype=np.int64)
    corners = np.asarray(source.corner_vertices, dtype=np.int64)
    keep_polygons = _polygon_mask(sizes, corners)
    dropped = int((~keep_polygons).sum())
    keep_corners = None
    if dropped:
        keep_corners = np.repeat(keep_polygons, sizes)
        sizes = sizes[keep_polygons]
        corners = corners[keep_corners]
        report({"INFO"}, f"{name}: dropped {dropped} degenerate polygon(s).")
    mesh = bpy.data.meshes.new(name)
    points = conv.points(source.points).astype(np.float32)
    mesh.vertices.add(len(points))
    mesh.vertices.foreach_set("co", points.ravel())
    mesh.loops.add(len(corners))
    mesh.loops.foreach_set("vertex_index", corners.astype(np.int32))
    mesh.polygons.add(len(sizes))
    starts = np.zeros(len(sizes), dtype=np.int32)
    if len(sizes) > 1:
        np.cumsum(sizes[:-1], out=starts[1:])
    mesh.polygons.foreach_set("loop_start", starts)
    mesh.update(calc_edges=True)

    def corner_data(array: np.ndarray) -> np.ndarray:
        return array if keep_corners is None else array[keep_corners]

    for uv_name, values in source.uv_layers.items():
        values = corner_data(np.asarray(values))
        if len(values) != len(corners):
            report({"WARNING"}, f"{name}: skipped UV set {uv_name!r} ({len(values)} values for "
                                f"{len(corners)} corners).")
            continue
        layer = mesh.uv_layers.new(name=uv_name[:63] or "UVMap")
        if layer is None:   # Blender allows 8 UV maps per mesh
            skipped = [n for n in source.uv_layers if n not in {u.name for u in mesh.uv_layers}]
            report({"WARNING"}, f"{name}: Blender supports 8 UV maps; skipped {len(skipped)}: "
                                f"{', '.join(skipped)[:200]}")
            break
        mesh.attributes[layer.name].data.foreach_set("vector", values.astype(np.float32).ravel())
    for color_name, values in source.color_layers.items():
        values = corner_data(np.asarray(values))
        if len(values) != len(corners):
            continue
        attribute = mesh.color_attributes.new(name=color_name[:63] or "Color", type="FLOAT_COLOR",
                                              domain="CORNER")
        attribute.data.foreach_set("color", values.astype(np.float32).ravel())
    if source.face_material_index is not None and len(source.material_names) > 1:
        index = np.asarray(source.face_material_index)
        if keep_corners is not None:
            index = index[keep_polygons]
        mesh.polygons.foreach_set("material_index", index.astype(np.int32))
    changed = mesh.validate(clean_customdata=False)
    if changed:
        report({"INFO"}, f"{name}: Blender corrected invalid mesh data.")
    if source.corner_normals is not None and len(source.corner_normals):
        normals = corner_data(np.asarray(source.corner_normals))
        if len(normals) == len(mesh.loops):
            normals = conv.directions(normals)
            lengths = np.linalg.norm(normals, axis=1, keepdims=True)
            normals = np.where(lengths > 1e-12, normals / np.where(lengths > 1e-12, lengths, 1), 0.0)
            mesh.polygons.foreach_set("use_smooth", np.ones(len(mesh.polygons), dtype=bool))
            mesh.normals_split_custom_set(normals.astype(np.float32))
        else:
            report({"WARNING"}, f"{name}: skipped {len(normals)} corner normals for "
                                f"{len(mesh.loops)} mesh loops.")
    return mesh, (None if keep_corners is None else keep_polygons)


def add_vertex_groups(obj: bpy.types.Object, groups: dict[str, tuple[np.ndarray, np.ndarray]]) -> None:
    vertex_count = len(obj.data.vertices)
    for group_name, (vertices, weights) in groups.items():
        group = obj.vertex_groups.get(group_name[:63]) or obj.vertex_groups.new(name=group_name[:63])
        vertices = np.asarray(vertices, dtype=np.int64)
        weights = np.asarray(weights, dtype=np.float64)
        valid = (vertices >= 0) & (vertices < vertex_count)
        vertices, weights = vertices[valid], weights[valid]
        if not len(vertices):
            continue
        # One add() call per distinct weight value.
        rounded = np.round(weights, 6)
        unique, inverse = np.unique(rounded, return_inverse=True)
        order = np.argsort(inverse, kind="stable")
        bounds = np.searchsorted(inverse[order], np.arange(len(unique) + 1))
        for slot, weight in enumerate(unique):
            members = vertices[order[bounds[slot]:bounds[slot + 1]]]
            group.add(members.tolist(), float(weight), "REPLACE")


def add_shape_keys(obj: bpy.types.Object, source: Mesh, conv: AxisConversion) -> list[str | None]:
    """Shape keys from source.shape_keys; their Blender names, aligned with source.shape_keys (None for
    a shape whose point count does not match the mesh)."""
    names: list[str | None] = []
    if not source.shape_keys:
        return names
    vertex_count = len(obj.data.vertices)
    if obj.data.shape_keys is None:
        obj.shape_key_add(name="Basis", from_mix=False)
    for key in source.shape_keys:
        points = np.asarray(key.points)
        if len(points) != vertex_count:
            names.append(None)
            continue
        block = obj.shape_key_add(name=key.name[:63], from_mix=False)
        block.points.foreach_set("co", conv.points(points).astype(np.float32).ravel())
        names.append(block.name)
    return names
