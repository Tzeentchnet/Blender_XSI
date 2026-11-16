# SPDX-License-Identifier: GPL-3.0-or-later
"""Create Blender data from the Blender-independent dotXSI scene model."""

from __future__ import annotations

from math import radians
import os
from pathlib import Path
from typing import Callable

import bpy
from mathutils import Euler, Matrix, Vector

from .model import AnimationCurve, Material, Scene, SceneObject, Transform


Report = Callable[[set[str], str], None]
C = Matrix.Rotation(radians(90.0), 4, "X")
C_INV = C.inverted()
LIGHT_POWER = {"POINT": 1000.0, "SPOT": 2000.0, "SUN": 3.0, "AREA": 1000.0}


def convert_point(point: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = point
    return (x, -z, y)


def local_matrix(transform: Transform) -> Matrix:
    rotation = Euler(
        tuple(radians(value) for value in transform.rotation),
        transform.rotation_order,
    )
    return Matrix.LocRotScale(
        Vector(transform.position),
        rotation.to_quaternion(),
        Vector(transform.scale),
    )


def blender_matrix(transform: Transform) -> Matrix:
    return C @ local_matrix(transform) @ C_INV


class ImageResolver:
    def __init__(self, source_path: str, search: bool):
        self.source_path = Path(source_path).resolve()
        self.search = search
        self._cache: dict[str, Path | None] = {}
        self._search_roots = self._roots()

    def _roots(self) -> list[Path]:
        source_dir = self.source_path.parent
        roots = [source_dir, source_dir / "Pictures", source_dir.parent / "Pictures"]
        cursor = source_dir
        while cursor.parent != cursor:
            if cursor.name.casefold() == "scenes":
                roots.extend((cursor.parent, cursor.parent / "Pictures"))
                break
            cursor = cursor.parent
        unique: list[Path] = []
        for root in roots:
            if root not in unique:
                unique.append(root)
        return unique

    def resolve(self, filename: str) -> Path | None:
        normalized = filename.replace("[Project Path]", "", 1).replace("\\", os.sep)
        requested = Path(normalized)
        key = requested.name.casefold()
        if key in self._cache:
            return self._cache[key]
        candidates = [requested] if requested.is_absolute() else []
        candidates.extend(root / requested for root in self._search_roots)
        candidates.extend(root / requested.name for root in self._search_roots)
        for candidate in candidates:
            if candidate.is_file():
                resolved = candidate.resolve()
                self._cache[key] = resolved
                return resolved
        if self.search:
            for root in self._search_roots:
                found = self._find_bounded(root, key)
                if found:
                    self._cache[key] = found
                    return found
        self._cache[key] = None
        return None

    @staticmethod
    def _find_bounded(root: Path, basename: str, max_depth: int = 4) -> Path | None:
        if not root.is_dir():
            return None
        root_depth = len(root.parts)
        for current, directories, files in os.walk(root):
            depth = len(Path(current).parts) - root_depth
            if depth >= max_depth:
                directories[:] = []
            match = next((name for name in files if name.casefold() == basename), None)
            if match:
                return (Path(current) / match).resolve()
        return None


def build(
    context,
    scene: Scene,
    *,
    import_materials: bool,
    import_textures: bool,
    search_textures: bool,
    import_animation: bool,
    use_collection: bool,
    report: Report,
) -> list[bpy.types.Object]:
    collection = context.collection
    if use_collection:
        collection = bpy.data.collections.new(Path(scene.source_path).stem or scene.name)
        context.collection.children.link(collection)
    for obj in context.selected_objects:
        obj.select_set(False)
    resolver = ImageResolver(scene.source_path, search_textures)
    materials = (
        _build_materials(scene, resolver, import_textures, report)
        if import_materials
        else {}
    )
    made: dict[int, bpy.types.Object] = {}
    objects: list[bpy.types.Object] = []
    absolute: list[tuple[bpy.types.Object, SceneObject]] = []

    def walk(source: SceneObject, parent: bpy.types.Object | None) -> None:
        obj = _make_object(source, scene, materials, collection, report)
        made[id(source)] = obj
        objects.append(obj)
        if parent:
            obj.parent = parent
        obj.rotation_mode = source.transform.rotation_order
        obj.matrix_basis = blender_matrix(source.transform)
        if not source.visible:
            obj.hide_viewport = True
            obj.hide_render = True
        obj["softimage_template"] = source.source_template
        if (
            source.camera
            and source.camera.position is not None
            and source.camera.target is not None
        ) or (
            source.light
            and source.light.position is not None
            and source.light.target is not None
        ):
            absolute.append((obj, source))
        for child in source.children:
            walk(child, obj)

    for root in scene.objects:
        walk(root, None)
    for obj, source in absolute:
        if source.camera:
            obj.matrix_world = _look_at(
                source.camera.position,
                source.camera.target,
                source.camera.roll,
            )
        elif source.light:
            obj.matrix_world = _look_at(source.light.position, source.light.target, 0.0)
    if import_animation:
        current_frame = context.scene.frame_current
        for source in scene.walk():
            if source.animation:
                _animate_object(made[id(source)], source, report)
        context.scene.frame_start = int(scene.frame_start)
        context.scene.frame_end = int(scene.frame_end)
        if scene.frame_rate > 0:
            context.scene.render.fps = round(scene.frame_rate)
            context.scene.render.fps_base = (
                round(scene.frame_rate) / scene.frame_rate
                if not scene.frame_rate.is_integer()
                else 1.0
            )
        context.scene.frame_set(current_frame)
    for obj in objects:
        obj.select_set(True)
    if objects:
        context.view_layer.objects.active = objects[0]
    return objects


def _make_object(
    source: SceneObject,
    scene: Scene,
    materials: dict[str, bpy.types.Material],
    collection: bpy.types.Collection,
    report: Report,
) -> bpy.types.Object:
    if source.kind == "MESH" and source.mesh is not None:
        data = _make_mesh(source, scene, materials, report)
    elif source.kind == "CAMERA" and source.camera is not None:
        data = _make_camera(source)
    elif source.kind == "LIGHT" and source.light is not None:
        data = _make_light(source)
    else:
        data = None
    obj = bpy.data.objects.new(source.name, data)
    collection.objects.link(obj)
    if source.mesh is not None:
        for group_name, weights in source.mesh.envelope_weights.items():
            group = obj.vertex_groups.new(name=group_name)
            for vertex, weight in weights:
                group.add([vertex], weight, "REPLACE")
    if source.kind == "BONE":
        obj.empty_display_type = "OCTAHEDRAL"
        obj.empty_display_size = source.bone_length or 1.0
        obj["softimage_bone"] = True
    return obj


def _make_mesh(
    source: SceneObject,
    scene: Scene,
    materials: dict[str, bpy.types.Material],
    report: Report,
) -> bpy.types.Mesh:
    source_mesh = source.mesh
    assert source_mesh is not None
    mesh = bpy.data.meshes.new(source.name)
    mesh.from_pydata(
        [convert_point(point) for point in source_mesh.points],
        [],
        source_mesh.polygons,
    )
    mesh.update()
    loop_count = len(mesh.loops)
    if source_mesh.corner_normals:
        if len(source_mesh.corner_normals) == loop_count:
            normals = [convert_point(normal) for normal in source_mesh.corner_normals]
            mesh.normals_split_custom_set(normals)
        else:
            report(
                {"WARNING"},
                f"{source.name}: skipped {len(source_mesh.corner_normals)} corner normals "
                f"for {loop_count} mesh loops.",
            )
    for name, values in source_mesh.uv_layers.items():
        if len(values) != loop_count:
            report(
                {"WARNING"},
                f"{source.name}: skipped UV set {name!r}; it has {len(values)} values "
                f"for {loop_count} mesh loops.",
            )
            continue
        layer = mesh.uv_layers.new(name=name or "UVMap")
        layer.data.foreach_set("uv", [component for uv in values for component in uv])
    for name, values in source_mesh.color_layers.items():
        if len(values) != loop_count:
            report(
                {"WARNING"},
                f"{source.name}: skipped color set {name!r}; it has {len(values)} values "
                f"for {loop_count} mesh loops.",
            )
            continue
        layer = mesh.color_attributes.new(
            name=name or "Color",
            type="FLOAT_COLOR",
            domain="CORNER",
        )
        layer.data.foreach_set(
            "color", [component for color in values for component in color]
        )
    material_names = list(dict.fromkeys(source_mesh.face_materials))
    if not material_names and source.material:
        material_names = [source.material]
    slots: dict[str, int] = {}
    for material_name in material_names:
        material = _ensure_material(materials, material_name, report)
        mesh.materials.append(material)
        slots[material_name] = len(mesh.materials) - 1
    for polygon, material_name in zip(mesh.polygons, source_mesh.face_materials):
        if material_name in slots:
            polygon.material_index = slots[material_name]
        else:
            material = _ensure_material(materials, material_name, report)
            mesh.materials.append(material)
            slots[material_name] = len(mesh.materials) - 1
            polygon.material_index = slots[material_name]
    return mesh


def _make_camera(source: SceneObject) -> bpy.types.Camera:
    settings = source.camera
    assert settings is not None
    camera = bpy.data.cameras.new(source.name)
    camera.type = settings.projection
    camera.clip_start = max(1e-6, settings.near)
    camera.clip_end = max(camera.clip_start, settings.far)
    if settings.projection == "ORTHO":
        camera.ortho_scale = settings.ortho_height
    else:
        camera.sensor_fit = "HORIZONTAL"
        camera.angle_x = radians(settings.field_of_view)
    if settings.aspect:
        camera["softimage_aspect"] = settings.aspect
    return camera


def _make_light(source: SceneObject) -> bpy.types.Light:
    settings = source.light
    assert settings is not None
    light = bpy.data.lights.new(source.name, settings.kind)
    light.color = settings.color
    light.energy = LIGHT_POWER.get(settings.kind, 1000.0) * settings.intensity
    if settings.kind == "SPOT":
        light.spot_size = radians(settings.cone_angle)
        if settings.cone_angle:
            light.spot_blend = max(
                0.0, min(1.0, settings.spread_angle / settings.cone_angle)
            )
    return light


def _look_at(
    position: tuple[float, float, float],
    target: tuple[float, float, float],
    roll: float,
) -> Matrix:
    location = Vector(convert_point(position))
    direction = Vector(convert_point(target)) - location
    if direction.length_squared < 1e-12:
        direction = Vector((0.0, 0.0, -1.0))
    rotation = direction.to_track_quat("-Z", "Y").to_matrix().to_4x4()
    if roll:
        rotation = rotation @ Matrix.Rotation(radians(roll), 4, "Z")
    return Matrix.Translation(location) @ rotation


def _find_material(
    materials: dict[str, bpy.types.Material], name: str
) -> bpy.types.Material | None:
    return materials.get(name) or materials.get(name.rsplit(".", 1)[-1])


def _ensure_material(
    materials: dict[str, bpy.types.Material],
    name: str,
    report: Report,
) -> bpy.types.Material:
    material = _find_material(materials, name)
    if material is not None:
        return material
    short_name = name.rsplit(".", 1)[-1] or "Material"
    material = bpy.data.materials.new(short_name)
    material["softimage_missing_definition"] = name
    materials[name] = material
    materials.setdefault(short_name, material)
    report(
        {"WARNING"},
        f"Material {name!r} is referenced by geometry but has no definition; "
        "created a placeholder.",
    )
    return material


def _build_materials(
    scene: Scene,
    resolver: ImageResolver,
    import_textures: bool,
    report: Report,
) -> dict[str, bpy.types.Material]:
    made: dict[str, bpy.types.Material] = {}
    for source in scene.materials.values():
        material = _make_material(
            source,
            scene,
            resolver,
            import_textures,
            report,
        )
        made[source.name] = material
        made.setdefault(source.name.rsplit(".", 1)[-1], material)
    return made


def _make_material(
    source: Material,
    scene: Scene,
    resolver: ImageResolver,
    import_textures: bool,
    report: Report,
) -> bpy.types.Material:
    material = bpy.data.materials.new(source.name.rsplit(".", 1)[-1] or source.name)
    material.use_nodes = True
    material.diffuse_color = source.diffuse
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    bsdf = nodes.get("Principled BSDF")
    if bsdf is None:
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
    _set_input(bsdf, "Base Color", source.diffuse)
    _set_input(bsdf, "Roughness", source.roughness)
    _set_input(bsdf, "Metallic", source.metallic)
    _set_input(bsdf, "Specular Tint", (*source.specular, 1.0))
    _set_input(bsdf, "Alpha", source.alpha)
    if source.emission_strength:
        _set_input(bsdf, "Emission Color", (*source.emission, 1.0))
        _set_input(bsdf, "Emission Strength", source.emission_strength)
    material["softimage_shader"] = source.shader
    if import_textures and source.image:
        filename = scene.images.get(source.image, source.image)
        found = resolver.resolve(filename)
        texture = nodes.new("ShaderNodeTexImage")
        texture.label = filename
        if found:
            texture.image = bpy.data.images.load(str(found), check_existing=True)
            links.new(texture.outputs["Color"], bsdf.inputs["Base Color"])
            if source.alpha < 1.0 and texture.outputs.get("Alpha"):
                links.new(texture.outputs["Alpha"], bsdf.inputs["Alpha"])
        else:
            material["softimage_missing_image"] = filename
            report(
                {"WARNING"},
                f"Material {material.name}: image file not found: {filename}",
            )
    return material


def _set_input(node, name: str, value) -> None:
    socket = node.inputs.get(name)
    if socket is None:
        return
    if isinstance(value, tuple):
        current = socket.default_value
        length = len(current) if hasattr(current, "__len__") else len(value)
        socket.default_value = (value + (1.0,) * length)[:length]
    else:
        socket.default_value = value


def _animate_object(
    obj: bpy.types.Object,
    source: SceneObject,
    report: Report,
) -> None:
    channels = {
        mapping: curve
        for curve in source.animation
        if (mapping := _channel_mapping(curve.channel)) is not None and curve.keys
    }
    if not channels:
        report(
            {"WARNING"},
            f"{source.name}: no supported transform channels were found in its F-curves.",
        )
        return
    frames = sorted({key[0] for curve in channels.values() for key in curve.keys})
    samples: list[tuple[float, tuple[Vector, Euler, Vector]]] = []
    for frame in frames:
        position = list(source.transform.position)
        rotation = list(source.transform.rotation)
        scale = list(source.transform.scale)
        destinations = {"location": position, "rotation_euler": rotation, "scale": scale}
        for (data_path, index), curve in channels.items():
            destinations[data_path][index] = _sample_curve(curve, frame)
        transform = Transform(
            position=tuple(position),
            rotation=tuple(rotation),
            scale=tuple(scale),
            rotation_order=source.transform.rotation_order,
        )
        matrix = blender_matrix(transform)
        location, quaternion, converted_scale = matrix.decompose()
        converted_rotation = quaternion.to_euler(source.transform.rotation_order)
        samples.append((frame, (location, converted_rotation, converted_scale)))
    action = bpy.data.actions.new(f"{source.name} Action")
    animation_data = obj.animation_data_create()
    interpolation = (
        "CONSTANT"
        if all("CONSTANT" in curve.interpolation.upper() for curve in channels.values())
        else "LINEAR"
    )
    if hasattr(action, "slots"):
        slot = action.slots.new(obj.id_type, obj.name)
        layer = action.layers.new("Softimage")
        strip = layer.strips.new(type="KEYFRAME")
        channel_bag = strip.channelbag(slot, ensure=True)
        for data_path, value_index in (
            ("location", 0),
            ("rotation_euler", 1),
            ("scale", 2),
        ):
            for component in range(3):
                fcurve = channel_bag.fcurves.new(
                    data_path=data_path,
                    index=component,
                )
                fcurve.keyframe_points.add(len(samples))
                for point, (frame, values) in zip(fcurve.keyframe_points, samples):
                    point.co = (frame, values[value_index][component])
                    point.interpolation = interpolation
                fcurve.update()
        animation_data.action = action
        animation_data.action_slot = slot
    else:
        for data_path, value_index in (
            ("location", 0),
            ("rotation_euler", 1),
            ("scale", 2),
        ):
            for component in range(3):
                fcurve = action.fcurves.new(data_path=data_path, index=component)
                fcurve.keyframe_points.add(len(samples))
                for point, (frame, values) in zip(fcurve.keyframe_points, samples):
                    point.co = (frame, values[value_index][component])
                    point.interpolation = interpolation
                fcurve.update()
        animation_data.action = action


def _channel_mapping(channel: str) -> tuple[str, int] | None:
    normalized = reformat = (
        channel.upper()
        .replace("_", "-")
        .replace(".", "-")
        .replace(" ", "-")
    )
    aliases = {
        "POS": "TRANSLATION",
        "POSITION": "TRANSLATION",
        "TRANS": "TRANSLATION",
        "ROT": "ROTATION",
        "SCL": "SCALING",
        "SCALE": "SCALING",
    }
    parts = [part for part in reformat.split("-") if part]
    if len(parts) < 2:
        for prefix in ("POS", "ROT", "SCL"):
            if normalized.startswith(prefix) and normalized[-1:] in "XYZ":
                parts = [prefix, normalized[-1]]
                break
    if len(parts) < 2 or parts[-1] not in {"X", "Y", "Z"}:
        return None
    kind = aliases.get(parts[0], parts[0])
    data_path = {
        "TRANSLATION": "location",
        "ROTATION": "rotation_euler",
        "SCALING": "scale",
    }.get(kind)
    if data_path is None:
        return None
    return data_path, "XYZ".index(parts[-1])


def _sample_curve(curve: AnimationCurve, frame: float) -> float:
    keys = sorted(curve.keys, key=lambda item: item[0])
    if frame <= keys[0][0]:
        return keys[0][1]
    if frame >= keys[-1][0]:
        return keys[-1][1]
    for left, right in zip(keys, keys[1:]):
        if left[0] <= frame <= right[0]:
            if "CONSTANT" in curve.interpolation.upper() or right[0] == left[0]:
                return left[1]
            factor = (frame - left[0]) / (right[0] - left[0])
            return left[1] + (right[1] - left[1]) * factor
    return keys[-1][1]
