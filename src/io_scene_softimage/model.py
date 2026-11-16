# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender-independent scene data shared by the Softimage importers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator


Vec2 = tuple[float, float]
Vec3 = tuple[float, float, float]
Vec4 = tuple[float, float, float, float]


@dataclass
class Transform:
    position: Vec3 = (0.0, 0.0, 0.0)
    rotation: Vec3 = (0.0, 0.0, 0.0)
    scale: Vec3 = (1.0, 1.0, 1.0)
    rotation_order: str = "XYZ"


@dataclass
class AnimationCurve:
    target: str
    channel: str
    interpolation: str
    keys: list[tuple[float, ...]]


@dataclass
class Material:
    name: str
    shader: str = ""
    diffuse: Vec4 = (0.8, 0.8, 0.8, 1.0)
    specular: Vec3 = (0.5, 0.5, 0.5)
    roughness: float = 0.5
    metallic: float = 0.0
    alpha: float = 1.0
    emission: Vec3 = (0.0, 0.0, 0.0)
    emission_strength: float = 0.0
    image: str | None = None
    parameters: dict[str, object] = field(default_factory=dict)


@dataclass
class Mesh:
    points: list[Vec3] = field(default_factory=list)
    polygons: list[list[int]] = field(default_factory=list)
    corner_normals: list[Vec3] = field(default_factory=list)
    uv_layers: dict[str, list[Vec2]] = field(default_factory=dict)
    color_layers: dict[str, list[Vec4]] = field(default_factory=dict)
    face_materials: list[str] = field(default_factory=list)
    envelope_weights: dict[str, list[tuple[int, float]]] = field(default_factory=dict)


@dataclass
class Camera:
    projection: str = "PERSP"
    field_of_view: float = 50.0
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


@dataclass
class SceneObject:
    name: str
    kind: str = "EMPTY"
    transform: Transform = field(default_factory=Transform)
    base_pose: Transform | None = None
    visible: bool = True
    material: str | None = None
    mesh: Mesh | None = None
    camera: Camera | None = None
    light: Light | None = None
    animation: list[AnimationCurve] = field(default_factory=list)
    children: list["SceneObject"] = field(default_factory=list)
    bone_length: float | None = None
    source_template: str = ""

    def walk(self) -> Iterator["SceneObject"]:
        yield self
        for child in self.children:
            yield from child.walk()


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
    coordinate_system: tuple[int, int, int, int, int, int] | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    materials: dict[str, Material] = field(default_factory=dict)
    images: dict[str, str] = field(default_factory=dict)
    objects: list[SceneObject] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def walk(self) -> Iterator[SceneObject]:
        for obj in self.objects:
            yield from obj.walk()
