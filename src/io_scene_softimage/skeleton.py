# SPDX-License-Identifier: GPL-3.0-or-later
"""Choosing which Softimage nodes become armature bones, and their rest geometry (no bpy).

Seeds (mode AUTO):
* IK chain parts: chain roots, joints and effectors (dotXSI SI_IK_*, native root / bone / eff);
* envelope deformers (any node that deforms a mesh);
* biped-style names (`Bip01...`, `ValveBiped...`) used by 3ds Max Biped / Valve skeletons, whose dotXSI
  exports carry no IK templates and often no envelopes (skinning baked to shape animation).

A node between two seeds (seed ancestor and seed descendant) is promoted to a bone so the bone hierarchy
is contiguous. Bones that share the same non-bone parent form one armature, parented to that node's
object. Rest geometry is computed in armature space (the parent node's frame) from base poses when the
file has them, otherwise from the stored transforms.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import re

import numpy as np

from . import convert
from .convert import AxisConversion
from .model import Scene, SceneObject

BIPED_NAME = re.compile(r"(?i)^(bip\d*|valvebiped)([ _.\-]|$)")
R_BONE = np.array([[0.0, 1.0, 0.0, 0.0], [-1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0],
                   [0.0, 0.0, 0.0, 1.0]])
R_BONE_INV = R_BONE.T


@dataclass(eq=False)
class Group:
    name: str
    parent: SceneObject | None
    roots: list[SceneObject] = field(default_factory=list)
    nodes: list[SceneObject] = field(default_factory=list)


@dataclass
class Plan:
    groups: list[Group] = field(default_factory=list)
    group_of: dict[int, Group] = field(default_factory=dict)
    rest_arm: dict[int, np.ndarray] = field(default_factory=dict)
    bone_rest: dict[int, np.ndarray] = field(default_factory=dict)
    bone_parent: dict[int, SceneObject] = field(default_factory=dict)
    length: dict[int, float] = field(default_factory=dict)
    static_basis: dict[int, np.ndarray] = field(default_factory=dict)
    all_nodes: list[SceneObject] = field(default_factory=list)


def is_seed(node: SceneObject) -> bool:
    if node.camera is not None or node.light is not None:
        return False
    if node.kind == "MODEL" or node.properties.get("softimage_model"):
        return False   # model containers (e.g. a `ValveBiped` model holding the rig) are not bones
    if node.kind == "BONE" or node.bone_role:
        return True
    return bool(BIPED_NAME.match(node.name))


def plan(scene: Scene, mode: str, conv: AxisConversion) -> Plan:
    result = Plan()
    if mode.upper() == "NONE":
        return result
    scene.link_parents()
    seeds: set[int] = set()
    descendant_seed: dict[int, bool] = {}

    def mark(node: SceneObject) -> bool:
        below = False
        for child in node.children:
            below |= mark(child)
        descendant_seed[id(node)] = below
        seed = is_seed(node)
        if seed:
            seeds.add(id(node))
        return below or seed

    for root in scene.objects:
        mark(root)

    bones: set[int] = set()

    def choose(node: SceneObject, seed_above: bool) -> None:
        if id(node) in seeds or (seed_above and descendant_seed[id(node)]
                                 and node.camera is None and node.light is None):
            bones.add(id(node))
        for child in node.children:
            choose(child, seed_above or id(node) in seeds)

    for root in scene.objects:
        choose(root, False)
    if not bones:
        return result

    groups: dict[int, Group] = {}

    def collect(node: SceneObject, group: Group | None) -> None:
        if id(node) in bones:
            if group is None:
                key = id(node.parent) if node.parent is not None else 0
                group = groups.get(key)
                if group is None:
                    group = Group(name="", parent=node.parent)
                    groups[key] = group
                    result.groups.append(group)
                group.roots.append(node)
            group.nodes.append(node)
            result.group_of[id(node)] = group
            result.all_nodes.append(node)
            for child in node.children:
                collect(child, group if id(child) in bones else None)
        else:
            for child in node.children:
                collect(child, None)

    for root in scene.objects:
        collect(root, None)

    for group in result.groups:
        if len(group.roots) == 1:
            group.name = f"{group.roots[0].name} Armature"
        elif group.parent is not None:
            group.name = f"{group.parent.name} Armature"
        else:
            group.name = "Armature"
        _rest_geometry(result, group, conv)
    return result


def _orthonormal(matrix: np.ndarray) -> np.ndarray:
    out = matrix.copy()
    u, _s, vt = np.linalg.svd(matrix[:3, :3])
    rotation = u @ vt
    if np.linalg.det(rotation) < 0:
        u[:, -1] *= -1
        rotation = u @ vt
    out[:3, :3] = rotation
    out[3] = (0.0, 0.0, 0.0, 1.0)
    return out


def _rest_geometry(result: Plan, group: Group, conv: AxisConversion) -> None:
    for node in group.nodes:
        transform = node.base_pose or node.transform
        local = conv.transform(convert.transform_matrix(
            transform.position, transform.rotation, transform.scale, transform.rotation_order,
            transform.quaternion,
        ))
        parent = node.parent if node.parent is not None and id(node.parent) in result.group_of else None
        if parent is not None and result.group_of[id(parent)] is group:
            result.bone_parent[id(node)] = parent
            result.rest_arm[id(node)] = result.rest_arm[id(parent)] @ local
        else:
            result.rest_arm[id(node)] = local
    # Lengths: explicit joint length, else the projection of the farthest child onto the node's X axis.
    pending = []
    for node in group.nodes:
        frame = result.rest_arm[id(node)]
        axis = frame[:3, 0]
        norm = np.linalg.norm(axis)
        axis = axis / norm if norm > 1e-12 else np.array([1.0, 0.0, 0.0])
        length = 0.0
        if node.bone_length and node.bone_length > 0:
            length = node.bone_length * conv.scale * (norm if norm > 1e-12 else 1.0)
        else:
            best = 0.0
            for child in node.children:
                if id(child) not in result.rest_arm:
                    continue
                offset = result.rest_arm[id(child)][:3, 3] - frame[:3, 3]
                distance = np.linalg.norm(offset)
                if distance < 1e-9:
                    continue
                projection = float(np.dot(offset, axis))
                if projection > best and projection > 0.5 * distance:
                    best = projection
            length = best
        result.length[id(node)] = length
        if length <= 0:
            pending.append(node)
    known = [v for v in (result.length[id(n)] for n in group.nodes) if v > 0]
    default = 0.5 * float(np.median(known)) if known else 1.0 * conv.scale
    for node in pending:
        parent = result.bone_parent.get(id(node))
        parent_length = result.length.get(id(parent), 0.0) if parent is not None else 0.0
        result.length[id(node)] = max(0.5 * parent_length if parent_length > 0 else default, 1e-3 * conv.scale)
    for node in group.nodes:
        result.bone_rest[id(node)] = _orthonormal(result.rest_arm[id(node)] @ R_BONE)
    # Static pose: where the current transform differs from the base pose used as rest.
    for node in group.nodes:
        if node.base_pose is None:
            continue
        transform = node.transform
        local = conv.transform(convert.transform_matrix(
            transform.position, transform.rotation, transform.scale, transform.rotation_order,
            transform.quaternion,
        ))
        parent = result.bone_parent.get(id(node))
        pose = (result.bone_rest[id(parent)] @ R_BONE_INV @ local @ R_BONE) if parent is not None \
            else local @ R_BONE
        basis = np.linalg.inv(result.bone_rest[id(node)]) @ pose
        if not np.allclose(basis, np.eye(4), atol=1e-6):
            result.static_basis[id(node)] = basis
