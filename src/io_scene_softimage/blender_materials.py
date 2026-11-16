# SPDX-License-Identifier: GPL-3.0-or-later
"""Blender materials from `model.Material` (dotXSI SI_Material / XSI_Material)."""

from __future__ import annotations

from typing import Callable

import bpy

from .images import ImageResolver
from .model import Material, Scene, TextureLayer

Report = Callable[[set[str], str], None]


def set_input(node, name: str, value) -> None:
    socket = node.inputs.get(name)
    if socket is None or value is None:
        return
    current = socket.default_value
    if isinstance(value, (tuple, list)) and hasattr(current, "__len__"):
        length = len(current)
        values = tuple(value) + (1.0,) * max(0, length - len(value))
        socket.default_value = values[:length]
    else:
        socket.default_value = value


def load_image(resolver: ImageResolver, stored: str, report: Report, owner: str,
               notes: list[str]) -> bpy.types.Image:
    """The image for a stored path; a placeholder image pointing at the stored path when missing."""
    found = resolver.resolve(stored)
    if found is not None:
        return bpy.data.images.load(str(found), check_existing=True)
    shown = resolver.display_path(stored)
    name = shown.replace("\\", "/").rsplit("/", 1)[-1] or "missing"
    image = bpy.data.images.get(name)
    if image is None or image.filepath != shown:
        image = bpy.data.images.new(name, 8, 8)
        image.source = "FILE"
        image.filepath = shown
    notes.append(f"image file missing: {shown}")
    report({"WARNING"}, f"Material {owner}: image file not found: {stored}")
    return image


class MaterialBuilder:
    """Creates one Blender material per scene material on first use."""

    def __init__(self, scene: Scene, resolver: ImageResolver, import_textures: bool, report: Report):
        self.scene = scene
        self.resolver = resolver
        self.import_textures = import_textures
        self.report = report
        self.made: dict[str, bpy.types.Material] = {}

    def get(self, name: str) -> bpy.types.Material:
        if name in self.made:
            return self.made[name]
        source = self.scene.materials.get(name)
        if source is None:
            short = name.rsplit(".", 1)[-1]
            source = self.scene.materials.get(short)
            if source is None:
                source = next(
                    (m for key, m in self.scene.materials.items() if key.rsplit(".", 1)[-1] == short),
                    None,
                )
        if source is None:
            material = bpy.data.materials.new(name.rsplit(".", 1)[-1] or "Material")
            material["softimage_missing_definition"] = name
            self.report({"WARNING"}, f"Material {name!r} is referenced by geometry but has no definition; "
                                     "created a placeholder.")
        else:
            material = self._build(source)
        self.made[name] = material
        return material

    # .................................................................. node tree
    def _build(self, source: Material) -> bpy.types.Material:
        material = bpy.data.materials.new(source.name.rsplit(".", 1)[-1] or source.name)
        material.use_nodes = True
        tree = material.node_tree
        nodes, links = tree.nodes, tree.links
        nodes.clear()
        output = nodes.new("ShaderNodeOutputMaterial")
        output.location = (500, 0)
        bsdf = nodes.new("ShaderNodeBsdfPrincipled")
        bsdf.location = (150, 0)
        links.new(bsdf.outputs["BSDF"], output.inputs["Surface"])
        notes: list[str] = []
        diffuse = tuple(source.diffuse)
        material.diffuse_color = diffuse
        set_input(bsdf, "Base Color", diffuse)
        set_input(bsdf, "Roughness", source.roughness)
        set_input(bsdf, "Metallic", source.metallic)
        set_input(bsdf, "Specular Tint", (*source.specular, 1.0))
        set_input(bsdf, "Specular IOR Level", source.specular_level)
        set_input(bsdf, "Alpha", source.alpha)
        if source.emission_strength:
            set_input(bsdf, "Emission Color", (*source.emission, 1.0))
            set_input(bsdf, "Emission Strength", source.emission_strength)
        color_socket = None
        if source.shading_model == "CONSTANT":
            # Constant shading ignores lights: render the colour as emission.
            set_input(bsdf, "Base Color", (0.0, 0.0, 0.0, 1.0))
            set_input(bsdf, "Specular IOR Level", 0.0)
            set_input(bsdf, "Emission Color", diffuse)
            set_input(bsdf, "Emission Strength", 1.0)
            color_socket = bsdf.inputs.get("Emission Color")
        else:
            color_socket = bsdf.inputs.get("Base Color")
        if source.shading_model in {"SHADOW", "VERTEX_COLOR"}:
            notes.append(f"shading model {source.shading_model.lower()} approximated")
        if source.shading_model == "VERTEX_COLOR":
            attribute = nodes.new("ShaderNodeVertexColor")
            attribute.location = (-300, 200)
            links.new(attribute.outputs["Color"], color_socket)
        if self.import_textures and source.textures:
            self._texture(material, source, source.textures[0], color_socket, bsdf, notes)
            if len(source.textures) > 1:
                notes.append(f"{len(source.textures) - 1} additional texture layer(s) not connected")
        if source.alpha < 1.0:
            material.surface_render_method = "BLENDED"
        material["softimage_shader"] = source.shader
        if source.ambient is not None:
            material["softimage_ambient"] = source.ambient
        if source.parameters.get("specular_power") is not None:
            material["softimage_specular_power"] = float(source.parameters["specular_power"])
        if notes:
            material["softimage_import_notes"] = "; ".join(notes)
        return material

    def _texture(self, material, source: Material, layer: TextureLayer, color_socket, bsdf,
                 notes: list[str]) -> None:
        tree = material.node_tree
        nodes, links = tree.nodes, tree.links
        filename = self.scene.images.get(layer.image, layer.image)
        texture = nodes.new("ShaderNodeTexImage")
        texture.location = (-350, 0)
        texture.label = filename.replace("\\", "/").rsplit("/", 1)[-1]
        texture.image = load_image(self.resolver, filename, self.report, material.name, notes)
        if any(layer.alternate):
            texture.extension = "MIRROR"
        elif layer.mapping in {"UV", "UV_WRAPPED"} or layer.repeat != (1.0, 1.0):
            texture.extension = "REPEAT"
        if layer.mapping not in {"UV", "UV_WRAPPED"}:
            notes.append(f"{layer.mapping.lower()} projection approximated with UVs")
        vector = None
        uv_name = layer.uv_set
        if uv_name:
            uv = nodes.new("ShaderNodeUVMap")
            uv.location = (-950, -100)
            uv.uv_map = uv_name
            vector = uv.outputs["UV"]
        transform = _uv_transform(layer)
        if transform is not None:
            mapping = nodes.new("ShaderNodeMapping")
            mapping.location = (-700, -100)
            mapping.vector_type = "POINT"
            scale, offset = transform
            mapping.inputs["Scale"].default_value = (scale[0], scale[1], 1.0)
            mapping.inputs["Location"].default_value = (offset[0], offset[1], 0.0)
            if vector is None:
                coords = nodes.new("ShaderNodeTexCoord")
                coords.location = (-950, -100)
                vector = coords.outputs["UV"]
            links.new(vector, mapping.inputs["Vector"])
            vector = mapping.outputs["Vector"]
        if vector is not None:
            links.new(vector, texture.inputs["Vector"])
        color = texture.outputs["Color"]
        blend = layer.blending if layer.blending is not None else 1.0
        diffuse_weight = layer.weights.get("diffuse", 1.0) if layer.weights else 1.0
        factor = max(0.0, min(1.0, blend * (diffuse_weight if diffuse_weight > 0 else 1.0)))
        if factor < 0.999:
            mix = nodes.new("ShaderNodeMix")
            mix.data_type = "RGBA"
            mix.location = (-100, 150)
            mix.inputs["Factor"].default_value = factor
            mix.inputs["A"].default_value = tuple(source.diffuse)
            links.new(color, mix.inputs["B"])
            color = mix.outputs["Result"]
        if color_socket is not None:
            links.new(color, color_socket)
        transparency = layer.weights.get("transparency", 0.0) if layer.weights else 0.0
        if transparency > 0 or source.alpha < 1.0:
            links.new(texture.outputs["Alpha"], bsdf.inputs["Alpha"])
            material.surface_render_method = "BLENDED"
        material["softimage_texture"] = filename


def _uv_transform(layer: TextureLayer):
    """(scale, offset) applied to UVs for SI_Texture2D repeat / scale / offset / crop, or None."""
    scale_u = layer.repeat[0] * (layer.scale[0] or 1.0)
    scale_v = layer.repeat[1] * (layer.scale[1] or 1.0)
    offset_u, offset_v = layer.offset
    if layer.crop is not None and layer.size and layer.size[0] > 0 and layer.size[1] > 0:
        umin, umax, vmin, vmax = layer.crop
        width, height = layer.size
        full = umin == 0 and vmin == 0 and umax in {width - 1, width, 0} and vmax in {height - 1, height, 0}
        if not full and umax > umin and vmax > vmin:
            crop_u = (umax - umin + 1) / width
            crop_v = (vmax - vmin + 1) / height
            offset_u = offset_u * crop_u + umin / width
            offset_v = offset_v * crop_v + vmin / height
            scale_u *= crop_u
            scale_v *= crop_v
    if abs(scale_u - 1) < 1e-9 and abs(scale_v - 1) < 1e-9 and abs(offset_u) < 1e-9 and abs(offset_v) < 1e-9:
        return None
    return (scale_u, scale_v), (offset_u, offset_v)
