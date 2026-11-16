# SPDX-License-Identifier: GPL-3.0-or-later
"""Create Blender data from the Blender-independent scene description (`model.Scene`).

Both importers (dotXSI and native .scn/.emdl) produce a `model.Scene`; this module turns it into
objects, meshes, cameras, lights, materials, armatures and actions.

Coordinates: points convert as A p and transforms as A M A^-1 (see `convert.AxisConversion`). Cameras
and lights look down -Z in both applications, so their own frame gets an extra P on the right, and
their children a P^-1 on the left.

Skeletons: nodes chosen by `skeleton.plan` become bones of one armature per skeleton. A bone's rest
matrix is the node's rest frame (in armature space) rotated so that the bone's Y axis follows the node's
X axis (Softimage chains point down X): B = normalize(N @ R). Pose: for a bone whose parent bone is p,
basis = B^-1 @ B_p @ R^-1 @ L(t) @ R (root bones: B^-1 @ L(t) @ R), where L(t) is the node's converted
local matrix. Objects parented to a bone use matrix_parent_inverse = T(0, -length, 0) @ R^-1 so their
basis stays the node-local matrix.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from pathlib import Path
from typing import Callable

import bpy
import numpy as np
from mathutils import Matrix

from . import blender_anim, blender_mesh, convert, skeleton
from .blender_materials import MaterialBuilder
from .convert import AxisConversion
from .images import ImageResolver
from .model import AnimationCurve, Scene, SceneObject, Transform

Report = Callable[[set[str], str], None]
LIGHT_POWER = {"POINT": 1000.0, "SPOT": 2000.0, "SUN": 3.0, "AREA": 1000.0}
R_BONE = np.array([[0.0, 1.0, 0.0, 0.0], [-1.0, 0.0, 0.0, 0.0], [0.0, 0.0, 1.0, 0.0],
                   [0.0, 0.0, 0.0, 1.0]])   # Rz(-90): bone Y follows node X
R_BONE_INV = R_BONE.T
FIXED_KINDS = {"CAMERA", "LIGHT"}


@dataclass
class ImportOptions:
    import_materials: bool = True
    import_textures: bool = True
    search_textures: bool = True
    import_animation: bool = True
    import_shape_animation: bool = True
    use_collection: bool = True
    skeleton_mode: str = "AUTO"          # AUTO, ALL, NONE
    up_axis: str = "Y"
    global_scale: float = 1.0
    set_scene_frames: bool = True


def to_matrix(array: np.ndarray) -> Matrix:
    return Matrix(np.asarray(array, dtype=np.float64).tolist())


def local_matrix(transform: Transform) -> np.ndarray:
    return convert.transform_matrix(
        transform.position, transform.rotation, transform.scale, transform.rotation_order,
        transform.quaternion,
    )


# Kept for backwards compatibility with earlier versions of this module.
def convert_point(point):
    x, y, z = point
    return (x, -z, y)


class SceneBuilder:
    def __init__(self, context, scene: Scene, options: ImportOptions, report: Report,
                 material_lookup: Callable[[str], bpy.types.Material] | None = None):
        self.context = context
        self.scene = scene
        self.options = options
        self.report = report
        self.conv = AxisConversion.from_up_axis(options.up_axis, options.global_scale)
        self.P = self.conv.rotation4
        self.P_inv = self.P.T
        self.objects: list[bpy.types.Object] = []
        self.made: dict[int, bpy.types.Object] = {}          # id(node) -> object carrying its data
        self.world: dict[int, np.ndarray] = {}               # id(node) -> Blender world (rest) matrix
        self.material_lookup = material_lookup
        self.materials: MaterialBuilder | None = None
        self.plan: skeleton.Plan | None = None
        self.armatures: dict[int, bpy.types.Object] = {}     # id(group) -> armature object
        self.armature_groups: dict[int, skeleton.Group] = {}  # armature object pointer -> group
        self.bone_names: dict[int, str] = {}                 # id(node) -> Blender bone name
        self.bone_children: list[tuple[bpy.types.Object, SceneObject, np.ndarray]] = []
        self.shape_key_names: dict[int, list[str | None]] = {}

    # .................................................................. entry
    def build(self) -> list[bpy.types.Object]:
        context = self.context
        if context.object is not None and context.object.mode != "OBJECT":
            bpy.ops.object.mode_set(mode="OBJECT")
        self.collection = context.collection
        if self.options.use_collection:
            name = Path(self.scene.source_path).stem or self.scene.name
            self.collection = bpy.data.collections.new(name)
            context.collection.children.link(self.collection)
        for obj in context.selected_objects:
            obj.select_set(False)
        if self.options.import_materials and self.material_lookup is None:
            resolver = ImageResolver(self.scene.source_path, self.options.search_textures)
            self.materials = MaterialBuilder(self.scene, resolver, self.options.import_textures, self.report)
        self.scene.link_parents()
        self.plan = skeleton.plan(self.scene, self.options.skeleton_mode, self.conv)
        for root in self.scene.objects:
            self._create(root, None, np.eye(4))
        for group in self.plan.groups:
            self._build_armature(group)
        self._parent_to_bones()
        self._deformers()
        if self.options.import_animation:
            self._animate()
        for obj in self.objects:
            if not obj.hide_get():
                obj.select_set(True)
        if self.objects:
            context.view_layer.objects.active = self.objects[0]
        return self.objects

    # .................................................................. objects
    def _local(self, node: SceneObject, transform: Transform | None = None) -> np.ndarray:
        """Converted local matrix including camera / light frame fixes."""
        matrix = self.conv.transform(local_matrix(transform or node.transform))
        parent = node.parent
        if parent is not None and parent.kind in FIXED_KINDS:
            matrix = self.P_inv @ matrix
        if node.kind in FIXED_KINDS:
            matrix = matrix @ self.P
        return matrix

    def _create(self, node: SceneObject, parent_obj: bpy.types.Object | None, parent_world: np.ndarray) -> None:
        plan = self.plan
        assert plan is not None
        group = plan.group_of.get(id(node))
        if group is not None:
            arm = self._armature_object(group, parent_obj, parent_world)
            world = self.world[id(group)] @ plan.rest_arm[id(node)]
            self.world[id(node)] = world
            if node.mesh is not None or node.camera is not None or node.light is not None:
                # The bone's own geometry (bone display boxes, deformer meshes) rides on the bone.
                obj = self._make_object(node)
                if obj.type == "MESH" and not obj.data.uv_layers and not node.mesh.envelope_weights:
                    # Bone display geometry (3ds Max bone boxes, helper shapes): keep, but out of renders.
                    obj.display_type = "WIRE"
                    obj.hide_render = True
                    obj["softimage_bone_display"] = True
                basis = self.P.copy() if node.kind in FIXED_KINDS else np.eye(4)
                obj.matrix_basis = to_matrix(basis)
                self.bone_children.append((obj, node, basis))
                self.made[id(node)] = obj
            for child in node.children:
                if id(child) in plan.group_of:
                    self._create(child, arm, parent_world)
                else:
                    self._create_bone_child(child, node)
            return
        obj = self._make_object(node)
        self.made[id(node)] = obj
        if parent_obj is not None:
            obj.parent = parent_obj
        basis = self._place(node, obj, parent_world)
        world = parent_world @ basis
        self.world[id(node)] = world
        for child in node.children:
            self._create(child, obj, world)

    def _create_bone_child(self, node: SceneObject, bone_node: SceneObject) -> None:
        """A non-bone node below a bone: an object parented to that bone."""
        obj = self._make_object(node)
        self.made[id(node)] = obj
        parent_world = self.world[id(bone_node)]
        basis = self._place(node, obj, parent_world)
        self.bone_children.append((obj, bone_node, basis))
        world = parent_world @ basis
        self.world[id(node)] = world
        for child in node.children:
            self._create(child, obj, world)

    def _basis_matrix(self, node: SceneObject, parent_world: np.ndarray) -> np.ndarray:
        look = self._look_at(node)
        if look is not None:
            return np.linalg.inv(parent_world) @ look
        return self._local(node)

    def _place(self, node: SceneObject, obj: bpy.types.Object, parent_world: np.ndarray) -> np.ndarray:
        basis = self._basis_matrix(node, parent_world)
        self._apply_basis(obj, node, basis)
        return basis

    def _apply_basis(self, obj: bpy.types.Object, node: SceneObject, basis: np.ndarray) -> None:
        simple = self._channel_compatible(node)
        if simple:
            transform = node.transform
            obj.location = self.conv.points(np.array([transform.position]))[0]
            if transform.quaternion is not None:
                obj.rotation_mode = "QUATERNION"
                w, x, y, z = transform.quaternion
                vector = self.conv.directions(np.array([[x, y, z]]))[0]
                obj.rotation_quaternion = (w, *vector)
            else:
                obj.rotation_mode = self.conv.euler_order(transform.rotation_order)
                rotation = [0.0, 0.0, 0.0]
                for source_axis, angle in enumerate(transform.rotation):
                    axis, sign = self.conv.axis_map()[source_axis]
                    rotation[axis] = math.radians(angle) * sign
                obj.rotation_euler = rotation
            scale = [1.0, 1.0, 1.0]
            for source_axis, value in enumerate(transform.scale):
                scale[self.conv.axis_map()[source_axis][0]] = value
            obj.scale = scale
        else:
            obj.rotation_mode = "QUATERNION"
            obj.matrix_basis = to_matrix(basis)

    def _channel_compatible(self, node: SceneObject) -> bool:
        """Whether the node's transform maps component-wise to Blender loc / rot / scale."""
        if node.kind in FIXED_KINDS or self._look_at(node) is not None:
            return False
        parent = node.parent
        return parent is None or parent.kind not in FIXED_KINDS

    def _look_at(self, node: SceneObject) -> np.ndarray | None:
        """Blender world matrix for cameras / lights given by position and interest."""
        settings = node.camera if node.camera is not None else node.light
        if settings is None or settings.position is None or settings.target is None:
            return None
        parent_world = self.world.get(id(node.parent), np.eye(4)) if node.parent else np.eye(4)
        position = parent_world @ np.append(self.conv.points(np.array([settings.position]))[0], 1.0)
        target = parent_world @ np.append(self.conv.points(np.array([settings.target]))[0], 1.0)
        direction = target[:3] - position[:3]
        if np.linalg.norm(direction) < 1e-9:
            direction = np.array([0.0, 0.0, -1.0])
        forward = direction / np.linalg.norm(direction)
        up = np.array([0.0, 0.0, 1.0])
        if abs(np.dot(forward, up)) > 0.999:
            up = np.array([0.0, 1.0, 0.0])
        right = np.cross(forward, up)
        right /= np.linalg.norm(right)
        true_up = np.cross(right, forward)
        rotation = np.column_stack((right, true_up, -forward))
        roll = getattr(settings, "roll", 0.0)
        if roll:
            rotation = rotation @ convert.euler_matrices(np.array([[0.0, 0.0, roll]]))[0]
        out = np.eye(4)
        out[:3, :3] = rotation
        out[:3, 3] = position[:3]
        return out

    def _make_object(self, node: SceneObject) -> bpy.types.Object:
        data = None
        if node.mesh is not None and node.kind in {"MESH", "BONE", "EMPTY", "MODEL"} and len(node.mesh.points):
            data = self._make_mesh(node)
        elif node.camera is not None:
            data = self._make_camera(node)
        elif node.light is not None:
            data = self._make_light(node)
        obj = bpy.data.objects.new(node.name, data)
        self.collection.objects.link(obj)
        self.objects.append(obj)
        if data is None:
            obj.empty_display_size = max(0.05, 0.5 * self.conv.scale)
            if node.kind == "BONE":
                obj.empty_display_type = "SINGLE_ARROW"
                obj.empty_display_size = (node.bone_length or 1.0) * self.conv.scale
        if node.mesh is not None and data is not None:
            mesh = node.mesh
            if mesh.envelope_weights:
                blender_mesh.add_vertex_groups(obj, mesh.envelope_weights)
            for cluster_name, (kind, indices) in mesh.clusters.items():
                if kind == "POINT":
                    blender_mesh.add_vertex_groups(
                        obj, {cluster_name: (indices, np.ones(len(indices)))}
                    )
            if mesh.shape_keys:
                self.shape_key_names[id(node)] = blender_mesh.add_shape_keys(obj, mesh, self.conv)
        if not node.visible:
            obj.hide_set(True)
        if not node.visible or not node.render_visible:
            obj.hide_render = True
        obj["softimage_template"] = node.source_template
        for key, value in node.properties.items():
            if key.startswith("_"):
                continue
            try:
                obj[key[:63]] = value
            except (TypeError, ValueError, OverflowError):
                obj[key[:63]] = str(value)
        return obj

    def _make_mesh(self, node: SceneObject) -> bpy.types.Mesh:
        source = node.mesh
        assert source is not None
        mesh, _kept = blender_mesh.build_mesh(node.name, source, self.conv, self.report)
        names = list(source.material_names)
        if not names and node.material:
            names = [node.material]
        if self.options.import_materials:
            for name in names:
                material = self._material(name)
                if material is not None:
                    mesh.materials.append(material)
        return mesh

    def _material(self, name: str):
        if self.material_lookup is not None:
            return self.material_lookup(name)
        if self.materials is not None:
            return self.materials.get(name)
        return None

    def _make_camera(self, node: SceneObject) -> bpy.types.Camera:
        settings = node.camera
        assert settings is not None
        camera = bpy.data.cameras.new(node.name)
        camera.type = settings.projection
        camera.clip_start = max(1e-6, settings.near * self.conv.scale)
        camera.clip_end = max(camera.clip_start, settings.far * self.conv.scale)
        if settings.projection == "ORTHO":
            camera.ortho_scale = settings.ortho_height * self.conv.scale
        elif settings.field_of_view:
            fov = math.radians(max(0.01, min(179.0, settings.field_of_view)))
            if settings.fov_type == "VERTICAL":
                camera.sensor_fit = "VERTICAL"
                camera.angle_y = fov
            else:
                camera.sensor_fit = "HORIZONTAL"
                camera.angle_x = fov
        if settings.aspect:
            camera["softimage_aspect"] = settings.aspect
        return camera

    def _make_light(self, node: SceneObject) -> bpy.types.Light:
        settings = node.light
        assert settings is not None
        light = bpy.data.lights.new(node.name, settings.kind)
        light.color = settings.color
        light.energy = LIGHT_POWER.get(settings.kind, 1000.0) * settings.intensity
        if hasattr(light, "use_shadow"):
            light.use_shadow = settings.shadows
        if settings.kind == "SPOT":
            light.spot_size = math.radians(max(1.0, min(180.0, settings.cone_angle)))
            if settings.cone_angle:
                light.spot_blend = max(0.0, min(1.0, settings.spread_angle / settings.cone_angle))
        return light

    # .................................................................. armatures
    def _armature_object(self, group: skeleton.Group, parent_obj, parent_world: np.ndarray) -> bpy.types.Object:
        arm = self.armatures.get(id(group))
        if arm is not None:
            return arm
        data = bpy.data.armatures.new(group.name)
        data.display_type = "STICK" if len(group.nodes) > 60 else "OCTAHEDRAL"
        arm = bpy.data.objects.new(group.name, data)
        self.collection.objects.link(arm)
        self.objects.append(arm)
        if parent_obj is not None:
            arm.parent = parent_obj
        arm.show_in_front = True
        self.armatures[id(group)] = arm
        self.armature_groups[arm.as_pointer()] = group
        self.world[id(group)] = parent_world
        return arm

    def _build_armature(self, group: skeleton.Group) -> None:
        plan = self.plan
        assert plan is not None
        arm = self.armatures.get(id(group))
        if arm is None:
            return
        view_layer = self.context.view_layer
        view_layer.objects.active = arm
        arm.select_set(True)
        bpy.ops.object.mode_set(mode="EDIT")
        edit_bones = arm.data.edit_bones
        for node in group.nodes:
            bone = edit_bones.new(node.name[:63])
            length = plan.length[id(node)]
            bone.head = (0.0, 0.0, 0.0)
            bone.tail = (0.0, length, 0.0)
            bone.matrix = to_matrix(plan.bone_rest[id(node)])
            bone.use_deform = True
            self.bone_names[id(node)] = bone.name
        for node in group.nodes:
            parent = plan.bone_parent.get(id(node))
            if parent is not None:
                edit_bones[self.bone_names[id(node)]].parent = edit_bones[self.bone_names[id(parent)]]
        bpy.ops.object.mode_set(mode="OBJECT")
        arm.select_set(False)
        for node in group.nodes:
            bone = arm.data.bones.get(self.bone_names[id(node)])
            if bone is not None and node.bone_role:
                bone["softimage_role"] = node.bone_role
            pose_bone = arm.pose.bones.get(self.bone_names[id(node)])
            if pose_bone is not None:
                pose_bone.rotation_mode = "QUATERNION"
                static = plan.static_basis.get(id(node))
                if static is not None:
                    loc, quat, scale = convert.decompose(static[None])
                    pose_bone.location = loc[0]
                    pose_bone.rotation_quaternion = quat[0]
                    pose_bone.scale = scale[0]

    def _parent_to_bones(self) -> None:
        plan = self.plan
        assert plan is not None
        for obj, bone_node, basis in self.bone_children:
            group = plan.group_of[id(bone_node)]
            arm = self.armatures[id(group)]
            obj.parent = arm
            obj.parent_type = "BONE"
            obj.parent_bone = self.bone_names[id(bone_node)]
            offset = np.eye(4)
            offset[1, 3] = -plan.length[id(bone_node)]
            obj.matrix_parent_inverse = to_matrix(offset @ R_BONE_INV)

    def _deformers(self) -> None:
        """Armature modifiers for enveloped meshes whose deformers became bones."""
        if not self.armatures:
            return
        bone_owner: dict[str, bpy.types.Object] = {}
        plan = self.plan
        assert plan is not None
        for group in plan.groups:
            arm = self.armatures.get(id(group))
            for node in group.nodes:
                bone_owner.setdefault(node.name.casefold(), arm)
                bone_owner.setdefault(node.name.rsplit(".", 1)[-1].casefold(), arm)
        for node in self.scene.walk():
            if node.mesh is None or not node.mesh.envelope_weights:
                continue
            obj = self.made.get(id(node))
            if obj is None or obj.type != "MESH":
                continue
            arms = {bone_owner.get(name.casefold()) for name in node.mesh.envelope_weights}
            arms.discard(None)
            for arm in arms:
                modifier = obj.modifiers.new("Softimage Envelope", "ARMATURE")
                modifier.object = arm
            if len(arms) == 1 and obj.parent_type == "BONE":
                # An enveloped mesh must not also follow a bone (Softimage envelopes ignore the motion of
                # the mesh's parents): keep its rest placement, parented to the armature object.
                arm = next(iter(arms))
                arm_world = self.world[id(self.armature_groups[arm.as_pointer()])]
                obj.parent = arm
                obj.parent_type = "OBJECT"
                obj.matrix_parent_inverse = Matrix.Identity(4)
                obj.matrix_basis = to_matrix(np.linalg.inv(arm_world) @ self.world[id(node)])
            # Vertex group names must match Blender bone names (which may have been truncated).
            by_name = {n.name.casefold(): n for n in plan.all_nodes}
            for name in node.mesh.envelope_weights:
                target = by_name.get(name.casefold())
                if target is not None and id(target) in self.bone_names:
                    group = obj.vertex_groups.get(name[:63])
                    if group is not None:
                        group.name = self.bone_names[id(target)]

    # .................................................................. animation
    def _animate(self) -> None:
        scene = self.scene
        stem = Path(scene.source_path).stem or scene.name
        names = sorted({a.name for a in scene.actions})
        action_name = stem if len(names) != 1 else f"{stem}|{names[0]}"
        writer = blender_anim.ActionWriter(action_name)
        plan = self.plan
        assert plan is not None
        for node in scene.walk():
            curves = [c for c in node.animation if c.prop is not None and len(c.frames)]
            if not curves:
                continue
            if id(node) in plan.group_of:
                continue    # bones are sampled per armature below
            obj = self.made.get(id(node))
            if obj is None:
                continue
            self._animate_object(writer, node, obj, curves)
        for group in plan.groups:
            self._animate_armature(writer, group)
        if self.options.import_shape_animation:
            for node in scene.walk():
                if node.mesh is not None and node.mesh.shape_keys and id(node) in self.made:
                    self._animate_shapes(writer, node, self.made[id(node)])
        starts = [a.start for a in scene.actions if a.start is not None]
        ends = [a.end for a in scene.actions if a.end is not None]
        action = writer.finish(
            min(starts) if starts else scene.frame_start, max(ends) if ends else scene.frame_end
        )
        if action is not None:
            action["softimage_clips"] = ", ".join(names)
            if self.options.set_scene_frames:
                self._set_scene_frames()

    def _set_scene_frames(self) -> None:
        scene, target = self.scene, self.context.scene
        target.frame_start = int(math.floor(scene.frame_start))
        target.frame_end = max(target.frame_start, int(math.ceil(scene.frame_end)))
        if scene.frame_rate > 0:
            fps = round(scene.frame_rate)
            target.render.fps = max(1, fps)
            target.render.fps_base = fps / scene.frame_rate if fps else 1.0

    def _animate_object(self, writer, node: SceneObject, obj, curves: list[AnimationCurve]) -> None:
        transform_curves = [c for c in curves if c.prop in {"location", "rotation", "scale"}]
        if transform_curves:
            if self._channel_compatible(node) and node.transform.quaternion is None:
                for curve in transform_curves:
                    self._write_remapped(writer, obj, curve)
            else:
                self._write_sampled_object(writer, node, obj, transform_curves)
        for curve in curves:
            if curve.prop == "visibility":
                hidden = (np.asarray(curve.values) < 0.5).astype(np.float64)
                for path in ("hide_viewport", "hide_render"):
                    writer.add_curve(obj, path, 0, curve.frames, hidden, "CONSTANT", group=obj.name)

    def _write_remapped(self, writer, obj, curve: AnimationCurve) -> None:
        axis, factor = self.conv.channel(curve.prop, curve.index)
        if curve.prop == "rotation":
            factor *= math.pi / 180.0
            path = "rotation_euler"
        else:
            path = curve.prop
        values = np.asarray(curve.values) * factor
        left = right = None
        if curve.handles_left is not None and curve.handles_right is not None:
            left = np.column_stack((curve.handles_left[:, 0], curve.handles_left[:, 1] * factor))
            right = np.column_stack((curve.handles_right[:, 0], curve.handles_right[:, 1] * factor))
        writer.add_curve(obj, path, axis, curve.frames, values, curve.interpolation, left, right,
                         group=obj.name)

    def _sample_frames(self, curves: list[AnimationCurve]) -> np.ndarray:
        frames = np.unique(np.concatenate([np.asarray(c.frames) for c in curves]))
        smooth = any(c.handles_left is not None or c.interpolation.upper() in {"CUBIC", "HERMITE", "BEZIER"}
                     for c in curves)
        if smooth and len(frames) > 1:
            dense = np.arange(math.floor(frames[0]), math.ceil(frames[-1]) + 1, 1.0)
            frames = np.unique(np.concatenate((frames, dense)))
        return frames

    def _sample_local(self, node: SceneObject, curves: list[AnimationCurve], frames: np.ndarray) -> np.ndarray:
        """Node-local Softimage matrices (F, 4, 4) at the given frames."""
        transform = node.transform
        count = len(frames)
        channels = {
            "location": np.tile(np.asarray(transform.position, dtype=np.float64), (count, 1)),
            "rotation": np.tile(np.asarray(transform.rotation, dtype=np.float64), (count, 1)),
            "scale": np.tile(np.asarray(transform.scale, dtype=np.float64), (count, 1)),
        }
        rotation_animated = False
        for curve in curves:
            if curve.prop in channels and 0 <= curve.index < 3:
                channels[curve.prop][:, curve.index] = curve.evaluate(frames)
                rotation_animated |= curve.prop == "rotation"
        if transform.quaternion is not None and not rotation_animated:
            rotation = np.broadcast_to(convert.quaternion_matrices(np.asarray(transform.quaternion)[None]),
                                       (count, 3, 3))
        else:
            rotation = convert.euler_matrices(channels["rotation"], transform.rotation_order)
        return convert.compose(channels["location"], rotation, channels["scale"])

    def _write_sampled_object(self, writer, node: SceneObject, obj, curves) -> None:
        frames = self._sample_frames(curves)
        local = self.conv.transform(self._sample_local(node, curves, frames))
        parent = node.parent
        if parent is not None and parent.kind in FIXED_KINDS:
            local = self.P_inv @ local
        if node.kind in FIXED_KINDS:
            local = local @ self.P
        obj.rotation_mode = "QUATERNION"
        locations, quaternions, scales = convert.decompose(local)
        mode = "CONSTANT" if all("CONSTANT" in c.interpolation.upper() for c in curves) else "LINEAR"
        for index in range(3):
            writer.add_curve(obj, "location", index, frames, locations[:, index], mode, group=obj.name)
            writer.add_curve(obj, "scale", index, frames, scales[:, index], mode, group=obj.name)
        for index in range(4):
            writer.add_curve(obj, "rotation_quaternion", index, frames, quaternions[:, index], mode,
                             group=obj.name)

    def _animate_armature(self, writer, group: skeleton.Group) -> None:
        plan = self.plan
        assert plan is not None
        arm = self.armatures.get(id(group))
        if arm is None:
            return
        animated = [
            (node, [c for c in node.animation if c.prop in {"location", "rotation", "scale"} and len(c.frames)])
            for node in group.nodes
        ]
        animated = [(node, curves) for node, curves in animated if curves]
        if not animated:
            return
        frames = self._sample_frames([c for _, curves in animated for c in curves])
        mode = "CONSTANT" if all(
            "CONSTANT" in c.interpolation.upper() for _, curves in animated for c in curves
        ) else "LINEAR"
        for node, curves in animated:
            local = self.conv.transform(self._sample_local(node, curves, frames))
            bone_rest = plan.bone_rest[id(node)]
            parent = plan.bone_parent.get(id(node))
            if parent is not None:
                pose = plan.bone_rest[id(parent)] @ R_BONE_INV @ local @ R_BONE
            else:
                pose = local @ R_BONE
            basis = np.linalg.inv(bone_rest) @ pose
            locations, quaternions, scales = convert.decompose(basis)
            name = self.bone_names[id(node)]
            prefix = f'pose.bones["{bpy.utils.escape_identifier(name)}"]'
            for index in range(3):
                if not blender_anim.drop_constant_identity(locations[:, index], 0.0, 1e-5 * self.conv.scale):
                    writer.add_curve(arm, f"{prefix}.location", index, frames, locations[:, index], mode,
                                     group=name)
                if not blender_anim.drop_constant_identity(scales[:, index], 1.0):
                    writer.add_curve(arm, f"{prefix}.scale", index, frames, scales[:, index], mode,
                                     group=name)
            identity = (1.0, 0.0, 0.0, 0.0)
            if not all(blender_anim.drop_constant_identity(quaternions[:, i], identity[i]) for i in range(4)):
                for index in range(4):
                    writer.add_curve(arm, f"{prefix}.rotation_quaternion", index, frames,
                                     quaternions[:, index], mode, group=name)

    def _animate_shapes(self, writer, node: SceneObject, obj) -> None:
        mesh = node.mesh
        names = self.shape_key_names.get(id(node)) or []
        key = obj.data.shape_keys
        if key is None or not names:
            return
        driver = next((c for c in node.animation if c.prop == "shape_animation"), None)
        constant = "CONSTANT" in mesh.shape_interpolation
        if driver is not None:
            frames = np.asarray(driver.frames, dtype=np.float64)
            values = np.asarray(driver.values, dtype=np.float64)
            numbers = []
            for shape in mesh.shape_keys:
                digits = "".join(ch for ch in shape.name if ch.isdigit())
                numbers.append(float(digits) if digits else None)
        else:
            order = [s.frame for s in mesh.shape_keys]
            frames = np.asarray([f if f is not None else i for i, f in enumerate(order)], dtype=np.float64)
            values = np.arange(len(frames), dtype=np.float64)
            numbers = list(values)
        for number, name in zip(numbers, names):
            if name is None or number is None:
                continue
            distance = np.abs(values - number)
            near = distance <= 1.0 + 1e-9
            if not near.any():
                continue
            weights = np.clip(1.0 - distance[near], 0.0, 1.0)
            if constant:
                weights = (distance[near] < 0.5).astype(np.float64)
            path = f'key_blocks["{bpy.utils.escape_identifier(name)}"].value'
            writer.add_curve(key, path, 0, frames[near], weights, "CONSTANT" if constant else "LINEAR")


def build(context, scene: Scene, *, report: Report, options: ImportOptions | None = None,
          material_lookup=None, **legacy) -> list[bpy.types.Object]:
    """Build Blender data for a parsed scene. Keyword arguments of earlier versions
    (import_materials=..., use_collection=...) are accepted and mapped onto ImportOptions."""
    options = options or ImportOptions()
    for key, value in legacy.items():
        if hasattr(options, key):
            setattr(options, key, value)
    return SceneBuilder(context, scene, options, report, material_lookup).build()
