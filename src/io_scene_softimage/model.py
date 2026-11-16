# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender-independent scene data shared by the Softimage importers.

Bulk data (points, corner indices, per-corner attributes, animation keys, weights) is stored in NumPy
arrays so that the Blender side can hand it to `foreach_set` without Python loops. Everything is in
Softimage's own conventions (Y up, degrees); `blender_scene` converts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

import numpy as np


Vec2 = tuple[float, float]
Vec3 = tuple[float, float, float]
Vec4 = tuple[float, float, float, float]


@dataclass
class Transform:
    position: Vec3 = (0.0, 0.0, 0.0)
    rotation: Vec3 = (0.0, 0.0, 0.0)          # degrees
    scale: Vec3 = (1.0, 1.0, 1.0)
    rotation_order: str = "XYZ"
    quaternion: Vec4 | None = None             # (w, x, y, z); overrides `rotation` when set


@dataclass
class AnimationCurve:
    """One animated scalar channel.

    `channel` is the raw Softimage name; `target` the object it belongs to; `prop` and `index` the
    resolved transform component ('location' / 'rotation' / 'scale' / 'visibility' / custom, 0..2), or
    None when the channel is not understood. Tangents, when present, are absolute (frame, value) handle
    positions for Bezier interpolation.
    """

    target: str
    channel: str
    interpolation: str
    frames: np.ndarray
    values: np.ndarray
    prop: str | None = None
    index: int = 0
    handles_left: np.ndarray | None = None     # (K, 2)
    handles_right: np.ndarray | None = None    # (K, 2)
    action: str = ""

    @property
    def keys(self) -> list[tuple[float, float]]:
        return [(float(f), float(v)) for f, v in zip(self.frames, self.values)]

    def evaluate(self, frames: np.ndarray) -> np.ndarray:
        """Value at each frame (vectorized). Bezier segments are evaluated on the handles."""
        return evaluate_curve(self, frames)


@dataclass
class Material:
    name: str
    shader: str = ""
    diffuse: Vec4 = (0.8, 0.8, 0.8, 1.0)
    specular: Vec3 = (0.5, 0.5, 0.5)
    specular_level: float = 0.5
    roughness: float = 0.5
    metallic: float = 0.0
    alpha: float = 1.0
    emission: Vec3 = (0.0, 0.0, 0.0)
    emission_strength: float = 0.0
    ambient: Vec3 | None = None
    shading_model: str = "PHONG"               # CONSTANT, LAMBERT, PHONG, BLINN, SHADOW, VERTEX_COLOR
    image: str | None = None
    textures: list["TextureLayer"] = field(default_factory=list)
    parameters: dict[str, object] = field(default_factory=dict)


@dataclass
class TextureLayer:
    """An image applied to a material (SI_Texture2D / XSI image shader)."""

    image: str
    uv_set: str | None = None
    mapping: str = "UV"
    repeat: Vec2 = (1.0, 1.0)
    scale: Vec2 = (1.0, 1.0)
    offset: Vec2 = (0.0, 0.0)
    crop: tuple[int, int, int, int] | None = None   # u min, u max, v min, v max (pixels)
    size: tuple[int, int] | None = None
    swap_uv: bool = False
    alternate: tuple[bool, bool] = (False, False)
    blending: float = 1.0
    blending_mode: int = 1
    weights: dict[str, float] = field(default_factory=dict)   # ambient, diffuse, specular, ...
    use_alpha: bool = False


@dataclass
class ShapeKey:
    name: str
    points: np.ndarray                         # (N, 3) absolute positions
    frame: float | None = None                 # for shape animation: frame at which weight is 1


@dataclass
class Mesh:
    points: np.ndarray = field(default_factory=lambda: np.zeros((0, 3)))
    polygon_sizes: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int32))
    corner_vertices: np.ndarray = field(default_factory=lambda: np.zeros(0, np.int32))
    corner_normals: np.ndarray | None = None   # (L, 3)
    uv_layers: dict[str, np.ndarray] = field(default_factory=dict)       # name -> (L, 2)
    color_layers: dict[str, np.ndarray] = field(default_factory=dict)    # name -> (L, 4)
    material_names: list[str] = field(default_factory=list)
    face_material_index: np.ndarray | None = None                       # (P,) into material_names
    envelope_weights: dict[str, tuple[np.ndarray, np.ndarray]] = field(default_factory=dict)
    shape_keys: list[ShapeKey] = field(default_factory=list)
    shape_interpolation: str = "LINEAR"
    clusters: dict[str, tuple[str, np.ndarray]] = field(default_factory=dict)   # name -> (kind, indices)
    sharp_edges: np.ndarray | None = None

    # Convenience views (lists) used by tests and small tools.
    @property
    def polygons(self) -> list[list[int]]:
        out = []
        start = 0
        corners = self.corner_vertices.tolist()
        for size in self.polygon_sizes.tolist():
            out.append(corners[start:start + size])
            start += size
        return out

    @property
    def face_materials(self) -> list[str]:
        if self.face_material_index is None:
            return []
        return [self.material_names[i] for i in self.face_material_index.tolist()]

    @property
    def loop_starts(self) -> np.ndarray:
        starts = np.zeros(len(self.polygon_sizes), dtype=np.int32)
        if len(self.polygon_sizes) > 1:
            np.cumsum(self.polygon_sizes[:-1], out=starts[1:])
        return starts


@dataclass
class Camera:
    projection: str = "PERSP"
    field_of_view: float = 50.0
    fov_type: str = "HORIZONTAL"
    near: float = 0.1
    far: float = 1000.0
    ortho_height: float = 10.0
    aspect: float | None = None
    position: Vec3 | None = None
    target: Vec3 | None = None
    roll: float = 0.0


@dataclass
class Light:
    kind: str = "POINT"
    color: Vec3 = (1.0, 1.0, 1.0)
    intensity: float = 1.0
    cone_angle: float = 45.0
    spread_angle: float = 0.0
    position: Vec3 | None = None
    target: Vec3 | None = None
    shadows: bool = True


@dataclass
class SceneObject:
    name: str
    kind: str = "EMPTY"                         # EMPTY, MESH, CAMERA, LIGHT, BONE, MODEL
    transform: Transform = field(default_factory=Transform)
    base_pose: Transform | None = None
    visible: bool = True
    render_visible: bool = True
    material: str | None = None
    mesh: Mesh | None = None
    camera: Camera | None = None
    light: Light | None = None
    animation: list[AnimationCurve] = field(default_factory=list)
    children: list["SceneObject"] = field(default_factory=list)
    bone_length: float | None = None
    bone_role: str = ""                         # IK_ROOT, IK_JOINT, IK_EFFECTOR, DEFORMER, ...
    source_template: str = ""
    properties: dict[str, object] = field(default_factory=dict)
    parent: "SceneObject | None" = field(default=None, repr=False, compare=False)

    def walk(self) -> Iterator["SceneObject"]:
        yield self
        for child in self.children:
            yield from child.walk()


@dataclass
class Action:
    """A named group of curves (XSI_Action / mixer clip)."""

    name: str
    start: float | None = None
    end: float | None = None
    curves: list[AnimationCurve] = field(default_factory=list)


@dataclass
class Scene:
    source_path: str
    format_version: str
    encoding: str
    float_bits: int
    name: str = "Softimage Scene"
    frame_start: float = 1.0
    frame_end: float = 250.0
    frame_rate: float = 24.0
    angles_in_radians: bool = False
    coordinate_system: tuple[int, int, int, int, int, int] | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    materials: dict[str, Material] = field(default_factory=dict)
    images: dict[str, str] = field(default_factory=dict)
    objects: list[SceneObject] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def walk(self) -> Iterator[SceneObject]:
        for obj in self.objects:
            yield from obj.walk()

    def link_parents(self) -> None:
        for root in self.objects:
            root.parent = None
            stack = [root]
            while stack:
                node = stack.pop()
                for child in node.children:
                    child.parent = node
                    stack.append(child)


# ---------------------------------------------------------------------- curve evaluation
def evaluate_curve(curve: AnimationCurve, frames: np.ndarray) -> np.ndarray:
    keys_f = np.asarray(curve.frames, dtype=np.float64)
    keys_v = np.asarray(curve.values, dtype=np.float64)
    frames = np.asarray(frames, dtype=np.float64)
    if len(keys_f) == 0:
        return np.zeros_like(frames)
    if len(keys_f) == 1:
        return np.full_like(frames, keys_v[0])
    order = np.argsort(keys_f, kind="stable")
    keys_f, keys_v = keys_f[order], keys_v[order]
    segment = np.clip(np.searchsorted(keys_f, frames, side="right") - 1, 0, len(keys_f) - 2)
    f0, f1 = keys_f[segment], keys_f[segment + 1]
    v0, v1 = keys_v[segment], keys_v[segment + 1]
    span = np.where(f1 > f0, f1 - f0, 1.0)
    t = np.clip((frames - f0) / span, 0.0, 1.0)
    mode = curve.interpolation.upper()
    if "CONSTANT" in mode:
        result = np.where(frames >= f1, v1, v0)
    elif curve.handles_left is not None and curve.handles_right is not None and mode in {
        "CUBIC", "BEZIER", "HERMITE"
    }:
        hr = np.asarray(curve.handles_right, dtype=np.float64)[order]
        hl = np.asarray(curve.handles_left, dtype=np.float64)[order]
        x0, x3 = f0, f1
        x1, x2 = hr[segment, 0], hl[segment + 1, 0]
        y1, y2 = hr[segment, 1], hl[segment + 1, 1]
        # Solve x(s) = frame for s with a few Newton steps from the linear guess (handles are
        # clamped inside the segment so x(s) is monotonic).
        x1 = np.clip(x1, x0, x3)
        x2 = np.clip(x2, x0, x3)
        s = t.copy()
        for _ in range(8):
            inv = 1.0 - s
            x = inv ** 3 * x0 + 3 * inv ** 2 * s * x1 + 3 * inv * s ** 2 * x2 + s ** 3 * x3
            dx = 3 * inv ** 2 * (x1 - x0) + 6 * inv * s * (x2 - x1) + 3 * s ** 2 * (x3 - x2)
            step = np.where(np.abs(dx) > 1e-12, (x - frames) / np.where(dx == 0, 1, dx), 0.0)
            s = np.clip(s - step, 0.0, 1.0)
        inv = 1.0 - s
        result = inv ** 3 * v0 + 3 * inv ** 2 * s * y1 + 3 * inv * s ** 2 * y2 + s ** 3 * v1
    else:
        result = v0 + (v1 - v0) * t
    result = np.where(frames <= keys_f[0], keys_v[0], result)
    return np.where(frames >= keys_f[-1], keys_v[-1], result)
