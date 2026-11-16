# SPDX-License-Identifier: GPL-3.0-or-later
"""dotXSI (text) reader: converts dotXSI 3.0 - 6.0 templates into the `model` scene description.

Parsing is done by `dotxsi_parser` (structure only, lazy values). This module interprets templates:

* scene info (SI_FileInfo, SI_Scene, SI_CoordinateSystem, SI_Angle, SI_Ambience)
* materials (SI_Material + SI_Texture2D from 3.0, XSI_Material + XSI_Shader from 3.5+) and images
* models, nulls, IK roots/joints/effectors, transforms, base poses, visibility, custom parameter sets
* polygon meshes (SI_PolygonList, SI_TriangleList, XSI_PolygonList), shapes in ORDERED and INDEXED
  layout, clusters, shape animation (SI_ShapeAnimation + its SHPANIM F-curve)
* F-curves (SI_FCurve) anywhere in the file, including XSI_Mixer / XSI_Action clips
* envelopes (SI_EnvelopeList / SI_Envelope)
"""

from __future__ import annotations

from collections import defaultdict
import math
from pathlib import Path
import re
from typing import Iterable

import numpy as np

from .dotxsi_parser import (
    DotXSIError,
    Element,
    Header,
    QuotedStr,
    decode_body,
    header_from_line,
    iter_elements,
    read_header,
    split_text,
)
from .model import (
    Action,
    AnimationCurve,
    Camera,
    Light,
    Material,
    Mesh,
    Scene,
    SceneObject,
    ShapeKey,
    TextureLayer,
    Transform,
)

__all__ = ["DotXSIError", "Header", "load", "loads", "read_header"]


# ---------------------------------------------------------------------- entry points
def load(path) -> Scene:
    source = Path(path)
    data = source.read_bytes()
    first_break = data.find(b"\n")
    header = header_from_line((data if first_break < 0 else data[:first_break]).rstrip(b"\r\n"))
    if first_break < 0:
        raise DotXSIError("The dotXSI file contains a header but no body.")
    warnings: list[str] = []
    text = decode_body(data[first_break + 1:], warnings)
    return _Converter(source, header, warnings).run(iter_elements(text, line_offset=1))


def loads(text: str, source_path: str = "<memory>") -> Scene:
    header, body = split_text(text)
    return _Converter(Path(source_path), header, []).run(iter_elements(body, line_offset=1))


# ---------------------------------------------------------------------- names
_PREFIXES = ("MDL-", "MSH-", "SHP-", "SRT-", "BASEPOSE-", "CAM-", "LGT-", "CRV-", "NUL-")


def clean_name(name: str) -> str:
    value = str(name).strip().strip('"')
    upper = value.upper()
    for prefix in _PREFIXES:
        if upper.startswith(prefix):
            return value[len(prefix):]
    return value


def short_name(name: str) -> str:
    return clean_name(name).rsplit(".", 1)[-1]


def same_name(left: str, right: str) -> bool:
    a, b = clean_name(left).casefold(), clean_name(right).casefold()
    return a == b or a.rsplit(".", 1)[-1] == b.rsplit(".", 1)[-1]


# ---------------------------------------------------------------------- channels
_COMPONENT = {"X": 0, "Y": 1, "Z": 2}
_CHANNEL_ALIASES = {
    "POS": "location", "POSITION": "location", "TRANS": "location", "TRANSLATION": "location",
    "TRANSL": "location", "LOC": "location",
    "ROT": "rotation", "ROTATION": "rotation", "ORI": "rotation",
    "SCL": "scale", "SCALE": "scale", "SCALING": "scale", "SCA": "scale",
}
_VISIBILITY = {"NODEVIS", "VISIBILITY", "VIEWVIS", "RENDVIS", "VIS"}


def resolve_channel(channel: str, component: int | None = None) -> tuple[str | None, int]:
    """(property, component) for a Softimage parameter name, or (None, 0)."""
    raw = channel.strip()
    lower = raw.lower()
    if "shpanim" in lower:
        return "shape_animation", 0
    # Scripting names: "Bip01.kine.local.posx", "kine.global.rotz", "posx"
    tail = lower.rsplit(".", 1)[-1]
    match = re.fullmatch(r"(pos|rot|scl|ori)([xyz])", tail)
    if match:
        prop = {"pos": "location", "rot": "rotation", "ori": "rotation", "scl": "scale"}[match.group(1)]
        return prop, "xyz".index(match.group(2))
    if tail in {"viewvis", "rendvis", "visibility"}:
        return "visibility", 0
    upper = raw.upper().replace(" ", "")
    if upper in _VISIBILITY:
        return "visibility", 0
    # dotXSI 3.0 names: XPOS, YROT, ZSCL; POSITION / ROTATION / SCALING (3 curves); TRANSLATION-X
    match = re.fullmatch(r"([XYZ])(POS|ROT|SCL|SCA|TRANS)", upper)
    if match:
        return _CHANNEL_ALIASES[match.group(2)], _COMPONENT[match.group(1)]
    parts = [part for part in re.split(r"[-_.]", upper) if part]
    if parts and parts[0] in _CHANNEL_ALIASES:
        prop = _CHANNEL_ALIASES[parts[0]]
        if len(parts) >= 2 and parts[-1] in _COMPONENT:
            return prop, _COMPONENT[parts[-1]]
        if component is not None:
            return prop, component
    match = re.fullmatch(r"(POS|ROT|SCL)([XYZ])", upper)
    if match:
        return _CHANNEL_ALIASES[match.group(1)], _COMPONENT[match.group(2)]
    return None, 0


# ---------------------------------------------------------------------- converter
class _Converter:
    def __init__(self, source: Path, header: Header, warnings: list[str]):
        self.scene = Scene(
            source_path=str(source),
            format_version=header.version,
            encoding=header.encoding,
            float_bits=header.float_bits,
            warnings=warnings,
        )
        self.envelopes: list[Element] = []
        self.curves: list[AnimationCurve] = []
        self.actions: dict[str, Action] = {}
        self.shape_animations: list[tuple[SceneObject, Element]] = []
        self.unknown: dict[str, int] = defaultdict(int)

    # .................................................................. driver
    def run(self, elements: Iterable[Element]) -> Scene:
        scene = self.scene
        for element in elements:
            template = element.template
            if template == "SI_FileInfo":
                labels = ("project", "user", "created", "application")
                scene.metadata.update(zip(labels, element.values))
            elif template == "SI_Scene":
                self._read_scene(element)
            elif template == "SI_CoordinateSystem":
                if len(element.tokens) >= 6:
                    scene.coordinate_system = tuple(
                        int(v) for v in element.ints(0, 6, "coordinate system")
                    )
            elif template == "SI_Angle":
                if element.tokens:
                    scene.angles_in_radians = element.integer(0, "angle unit") == 1
            elif template == "SI_Ambience":
                if len(element.tokens) >= 3:
                    scene.metadata["ambience"] = tuple(element.floats(0, 3, "ambience").tolist())
            elif template == "SI_MaterialLibrary":
                self._read_material_library(element)
            elif template in {"XSI_ImageLibrary", "SI_ImageLibrary"}:
                self._read_image_library(element)
            elif template in {"SI_Model", "XSI_Model"}:
                scene.objects.append(self._read_model(element))
            elif template in {"SI_Light", "XSI_Light"}:
                scene.objects.append(self._light_object(element))
            elif template in {"SI_Camera", "XSI_Camera"}:
                scene.objects.append(self._camera_object(element))
            elif template in {"SI_EnvelopeList", "XSI_EnvelopeList"}:
                self.envelopes.extend(element.children_of("SI_Envelope", "XSI_Envelope"))
            elif template in {"SI_Envelope", "XSI_Envelope"}:
                self.envelopes.append(element)
            elif template == "SI_FCurve":
                self._collect_curve(element, "")
            elif template in {"XSI_Mixer", "XSI_Action"}:
                self._collect_mixer(element)
            else:
                self.unknown[template] += 1
        scene.link_parents()
        self._apply_inherited_materials()
        self._assign_curves()
        self._assign_shape_animation()
        self._assign_envelopes()
        scene.actions = [action for action in self.actions.values() if action.curves]
        for template, count in sorted(self.unknown.items()):
            scene.warnings.append(f"Skipped {count} unsupported {template} template(s).")
        return scene

    def _read_scene(self, element: Element) -> None:
        self.scene.name = clean_name(element.name) or self.scene.name
        values = element.values
        if values and isinstance(values[0], str) and values[0].upper() in {"FRAMES", "SECONDS"}:
            self.scene.metadata["timing"] = values[0].upper()
            values = values[1:]
        numbers = [v for v in values if isinstance(v, (int, float))]
        if len(numbers) >= 3:
            self.scene.frame_start, self.scene.frame_end, self.scene.frame_rate = (
                float(numbers[0]), float(numbers[1]), float(numbers[2])
            )
            if self.scene.metadata.get("timing") == "SECONDS" and self.scene.frame_rate > 0:
                self.scene.frame_start *= self.scene.frame_rate
                self.scene.frame_end *= self.scene.frame_rate

    def _deg(self, value: float) -> float:
        return math.degrees(value) if self.scene.angles_in_radians else value

    # .................................................................. materials
    def _read_material_library(self, library: Element) -> None:
        for element in library.children:
            if element.template == "SI_Material":
                material = self._read_legacy_material(element)
            elif element.template == "XSI_Material":
                material = self._read_xsi_material(element)
            else:
                self.unknown[element.template] += 1
                continue
            self.scene.materials[material.name] = material

    def _read_legacy_material(self, element: Element) -> Material:
        """dotXSI 3.0 SI_Material: face color RGBA, specular power, specular RGB, emissive RGB,
        shading model, ambient RGB; SI_Texture2D children."""
        tokens = element.tokens
        name = element.name or "Material"
        if len(tokens) < 11:
            self.scene.warnings.append(f"SI_Material {name!r} has too few values; using defaults.")
            return Material(name=name, shader="SI_Material")
        diffuse = tuple(np.clip(element.floats(0, 4, "material color"), 0.0, 1.0).tolist())
        power = element.number(4, "specular power")
        specular = tuple(element.floats(5, 3, "specular color").tolist())
        emissive = tuple(element.floats(8, 3, "emissive color").tolist())
        model_index = element.integer(11, "shading model") if len(tokens) > 11 else 2
        ambient = (
            tuple(element.floats(12, 3, "ambient color").tolist()) if len(tokens) >= 15 else None
        )
        shading = {0: "CONSTANT", 1: "LAMBERT", 2: "PHONG", 3: "BLINN", 4: "SHADOW",
                   5: "VERTEX_COLOR"}.get(model_index, "PHONG")
        material = Material(
            name=name,
            shader=f"SI_Material ({shading.lower()})",
            diffuse=diffuse,  # type: ignore[arg-type]
            specular=specular,  # type: ignore[arg-type]
            specular_level=0.0 if shading in {"CONSTANT", "LAMBERT"} else min(1.0, 0.5 * max(specular)),
            roughness=_roughness_from_power(power),
            alpha=diffuse[3],
            emission=emissive,  # type: ignore[arg-type]
            emission_strength=1.0 if max(emissive) > 0 else 0.0,
            ambient=ambient,  # type: ignore[arg-type]
            shading_model=shading,
            parameters={"specular_power": power},
        )
        for texture in element.children_of("SI_Texture2D"):
            layer = self._read_texture2d(texture)
            if layer is not None:
                material.textures.append(layer)
        if material.textures:
            material.image = material.textures[0].image
        return material

    def _read_texture2d(self, element: Element) -> TextureLayer | None:
        """SI_Texture2D: image, mapping type, width, height, crop (4), UV swap, U/V repeat, U/V alternate,
        U/V scale, U/V offset, projection matrix (16), blending type, blending, ambient, diffuse,
        specular, transparency, reflectivity, roughness."""
        tokens = element.tokens
        if not tokens:
            return None
        image = str(tokens[0])
        layer = TextureLayer(image=image)
        if len(tokens) < 17:
            return layer
        values = element.values
        mapping = {0: "XY", 1: "XZ", 2: "YZ", 3: "UV", 4: "UV_WRAPPED", 5: "CYLINDRICAL",
                   6: "SPHERICAL", 7: "REFLECTION"}
        layer.mapping = mapping.get(int(_number(values[1], 3)), "UV")
        layer.size = (int(_number(values[2], 0)), int(_number(values[3], 0)))
        layer.crop = tuple(int(_number(v, 0)) for v in values[4:8])  # type: ignore[assignment]
        layer.swap_uv = bool(_number(values[8], 0))
        layer.repeat = (_number(values[9], 1), _number(values[10], 1))
        layer.alternate = (bool(_number(values[11], 0)), bool(_number(values[12], 0)))
        layer.scale = (_number(values[13], 1), _number(values[14], 1))
        layer.offset = (_number(values[15], 0), _number(values[16], 0))
        if len(values) >= 41:
            layer.blending_mode = int(_number(values[33], 1))
            layer.blending = _number(values[34], 1)
            layer.weights = dict(zip(
                ("ambient", "diffuse", "specular", "transparency", "reflectivity", "roughness"),
                (_number(v, 0) for v in values[35:41]),
            ))
        return layer

    def _read_xsi_material(self, element: Element) -> Material:
        shaders = element.children_of("XSI_Shader")
        surface = next(
            (
                shader for shader in shaders
                if not any(word in (shader.name + " " + _progid(shader)).lower()
                           for word in ("image", "texture", "bump", "txt2d", "projection"))
            ),
            shaders[0] if shaders else None,
        )
        params = _shader_parameters(surface)
        progid = _progid(surface) if surface else ""
        diffuse = _color(params, "diffuse", (0.8, 0.8, 0.8, 1.0))
        if "constant" in progid.lower():
            diffuse = _color(params, "color", diffuse)
        specular = _color(params, "specular", (0.5, 0.5, 0.5, 1.0))[:3]
        shiny = _number(params.get("shiny"), None)
        roughness = (
            math.sqrt(2.0 / (shiny + 2.0)) if shiny is not None and shiny > -2.0
            else _number(params.get("roughness"), 0.5)
        )
        transparency = _color(params, "transparency", (0.0, 0.0, 0.0, 0.0))
        alpha = max(0.0, min(1.0, 1.0 - max(transparency[:3])))
        emission = _color(params, "incandescence", (0.0, 0.0, 0.0, 1.0))[:3]
        material = Material(
            name=element.name or "Material",
            shader=progid or (surface.name if surface else ""),
            diffuse=diffuse,
            specular=specular,  # type: ignore[arg-type]
            roughness=max(0.0, min(1.0, float(roughness))),
            metallic=max(0.0, min(1.0, _number(params.get("metallic"), 0.0))),
            alpha=alpha,
            emission=emission,  # type: ignore[arg-type]
            emission_strength=_number(params.get("inc_inten"), 0.0),
            shading_model="CONSTANT" if "constant" in progid.lower() else "PHONG",
            parameters=params,
        )
        for shader in shaders:
            image_params = _shader_parameters(shader)
            candidate = image_params.get("tex") or image_params.get("Texture")
            if isinstance(candidate, str) and candidate:
                layer = TextureLayer(image=candidate)
                uv = image_params.get("tspace_id")
                if isinstance(uv, str) and uv:
                    layer.uv_set = uv
                material.textures.append(layer)
        if material.textures:
            material.image = material.textures[0].image
        return material

    def _read_image_library(self, library: Element) -> None:
        for image in library.children_of("XSI_Image", "SI_Image"):
            filename = next(
                (
                    value for value in image.values
                    if isinstance(value, str) and value and value.upper() not in _PARAM_TYPES
                ),
                None,
            )
            if filename:
                self.scene.images[image.name] = filename

    # .................................................................. hierarchy
    def _read_model(self, element: Element) -> SceneObject:
        obj = SceneObject(name=clean_name(element.name) or "Model", source_template=element.template)
        light_element = None
        for child in element.children:
            template = child.template
            if template == "SI_Transform":
                transform = self._read_transform(child)
                if "BASEPOSE" in child.name.upper():
                    obj.base_pose = transform
                else:
                    obj.transform = transform
            elif template == "XSI_Transform":
                obj.transform = self._read_transform(child)
            elif template == "XSI_BasePose":
                obj.base_pose = self._read_transform(child)
            elif template == "SI_Visibility" and child.tokens:
                obj.visible = bool(child.integer(0, "visibility"))
            elif template == "SI_GlobalMaterial" and child.tokens:
                obj.material = str(child.tokens[0])
                propagation = str(child.tokens[1]).upper() if len(child.tokens) > 1 else "NODE"
                obj.properties["softimage_material_propagation"] = propagation
                if propagation == "INHERITED":
                    obj.material = None
            elif template in {"SI_Mesh", "XSI_Mesh"}:
                obj.kind = "MESH"
                obj.mesh = self._read_mesh(child, obj)
            elif template in {"SI_Camera", "XSI_Camera"}:
                obj.kind = "CAMERA"
                obj.camera = self._read_camera(child)
            elif template in {"SI_Light", "XSI_Light"}:
                obj.kind = "LIGHT"
                obj.light = self._read_light(child)
                light_element = child
            elif template in {"SI_IK_Root", "SI_IK_Joint", "SI_IK_Effector"}:
                obj.kind = "BONE"
                obj.bone_role = {"SI_IK_Root": "IK_ROOT", "SI_IK_Joint": "IK_JOINT",
                                 "SI_IK_Effector": "IK_EFFECTOR"}[template]
                if template == "SI_IK_Joint" and len(child.tokens) > 1:
                    obj.bone_length = child.number(1, "bone length")
                if template == "SI_IK_Root" and child.tokens:
                    obj.properties["softimage_ik_chain"] = ", ".join(
                        str(v) for v in child.values[1:]
                    )
            elif template in {"SI_Model", "XSI_Model"}:
                obj.children.append(self._read_model(child))
            elif template == "SI_Null":
                if child.name:
                    obj.name = clean_name(child.name)
            elif template == "SI_FCurve":
                self._collect_curve(child, "", owner=obj)
            elif template in {"XSI_Mixer", "XSI_Action"}:
                self._collect_mixer(child)
            elif template == "XSI_CustomPSet":
                self._read_custom_pset(child, obj)
            elif template == "SI_Cluster":
                self._read_cluster(child, obj)
            elif template in {"SI_Instance", "XSI_Instance"}:
                obj.properties["softimage_instance_of"] = str(child.tokens[0]) if child.tokens else ""
            else:
                self.unknown[template] += 1
        if light_element is not None and obj.transform == Transform():
            self._light_orientation(light_element, obj)
        return obj

    def _read_transform(self, element: Element) -> Transform:
        tokens = element.tokens
        if element.template == "XSI_Transform":
            if len(tokens) < 10:
                raise DotXSIError(
                    f"XSI_Transform at line {element.line} has {len(tokens)} values; expected at least 10."
                )
            values = element.values
            return Transform(
                position=_vec3(element, 0, "position"),
                rotation=tuple(self._deg(v) for v in _vec3(element, 3, "rotation")),  # type: ignore[arg-type]
                scale=_vec3(element, 7, "scale"),
                rotation_order=_rotation_order(values[6]),
            )
        if element.template == "XSI_BasePose":
            if len(tokens) < 9:
                raise DotXSIError(
                    f"XSI_BasePose at line {element.line} has {len(tokens)} values; expected 9."
                )
            return Transform(
                position=_vec3(element, 0, "position"),
                rotation=tuple(self._deg(v) for v in _vec3(element, 3, "rotation")),  # type: ignore[arg-type]
                scale=_vec3(element, 6, "scale"),
            )
        if len(tokens) < 9:
            raise DotXSIError(
                f"SI_Transform at line {element.line} has {len(tokens)} values; expected 9."
            )
        return Transform(
            scale=_vec3(element, 0, "scale"),
            rotation=tuple(self._deg(v) for v in _vec3(element, 3, "rotation")),  # type: ignore[arg-type]
            position=_vec3(element, 6, "position"),
        )

    def _read_custom_pset(self, element: Element, obj: SceneObject) -> None:
        values = element.values
        if len(values) < 2:
            return
        count = int(_number(values[1], 0))
        cursor = 2
        prefix = short_name(element.name) or "CustomPSet"
        for _ in range(count):
            if cursor + 3 > len(values):
                break
            name, _kind, value = values[cursor:cursor + 3]
            obj.properties[f"{prefix}.{name}"] = value if isinstance(value, (int, float, str)) else str(value)
            cursor += 3

    def _read_cluster(self, element: Element, obj: SceneObject) -> None:
        tokens = element.tokens
        if len(tokens) < 4:
            return
        count = element.integer(3, "cluster element count")
        indices = element.ints(4, count, "cluster element")
        name = clean_name(element.name) or "Cluster"
        kind = "POINT"
        lowered = name.lower()
        if "poly" in lowered:
            kind = "POLYGON"
        elif "edge" in lowered:
            kind = "EDGE"
        pending = obj.properties.setdefault("_clusters", {})
        pending[name] = (kind, indices)  # type: ignore[index]
        if obj.mesh is not None:
            obj.mesh.clusters[name] = (kind, indices)

    def _apply_inherited_materials(self) -> None:
        for root in self.scene.objects:
            self._propagate_material(root, None)
        for obj in self.scene.walk():
            clusters = obj.properties.pop("_clusters", None)
            if clusters and obj.mesh is not None:
                obj.mesh.clusters.update(clusters)

    def _propagate_material(self, obj: SceneObject, inherited: str | None) -> None:
        propagation = obj.properties.get("softimage_material_propagation")
        if obj.material is None and inherited is not None:
            obj.material = inherited
        branch = obj.material if propagation == "BRANCH" else inherited
        for child in obj.children:
            self._propagate_material(child, branch)

    # .................................................................. meshes
    def _read_mesh(self, element: Element, owner: SceneObject) -> Mesh:
        layers: dict[str, np.ndarray] = {}
        uv_names: dict[str, str] = {}
        shape = element.child("SI_Shape")
        if shape is not None:
            layers, uv_names = self._read_shape(shape)
        else:
            xsi_shape = element.child("XSI_Shape")
            if xsi_shape is not None:
                layers = self._read_xsi_shape(xsi_shape)
        positions = layers.get("POSITION")
        mesh = Mesh(points=positions if positions is not None else np.zeros((0, 3)))
        builder = _MeshBuilder(self, mesh, layers, uv_names)
        for child in element.children:
            template = child.template
            if template in {"SI_PolygonList", "XSI_PolygonList"}:
                builder.add_polygon_list(child)
            elif template == "SI_TriangleList":
                builder.add_triangle_list(child)
            elif template in {"SI_TriStrip", "SI_TriangleStripList", "XSI_TriangleStripList"}:
                builder.add_triangle_strips(child)
            elif template == "SI_ShapeAnimation":
                self.shape_animations.append((owner, child))
            elif template == "SI_Cluster":
                self._read_cluster(child, owner)
            elif template not in {"SI_Shape", "XSI_Shape"}:
                self.unknown[template] += 1
        builder.finish()
        if not len(mesh.points):
            self.scene.warnings.append(f"{clean_name(element.name) or 'Mesh'}: no POSITION array was decoded.")
        return mesh

    def _read_shape(self, element: Element) -> tuple[dict[str, np.ndarray], dict[str, str]]:
        """SI_Shape: <array count>, ORDERED|INDEXED, then per array: <count>, <semantic>,
        [<UV projection name> for TEX_COORD_UV], values (INDEXED: each element starts with its index)."""
        tokens = element.tokens
        if len(tokens) < 2:
            return {}, {}
        count = element.integer(0, "shape array count")
        indexed = str(tokens[1]).upper() == "INDEXED"
        cursor = 2
        layers: dict[str, np.ndarray] = {}
        uv_names: dict[str, str] = {}
        for _ in range(count):
            if cursor + 1 >= len(tokens):
                raise DotXSIError(f"SI_Shape at line {element.line} ends before all {count} arrays.")
            entries = element.integer(cursor, "shape entry count")
            semantic = str(tokens[cursor + 1])
            cursor += 2
            upper = semantic.upper()
            if upper.startswith("TEX_COORD") and cursor < len(tokens) and not _is_number(tokens[cursor]):
                uv_names[upper] = str(tokens[cursor])
                cursor += 1
            arity = _semantic_arity(semantic)
            width = arity + (1 if indexed else 0)
            flat = element.floats(cursor, entries * width, f"{semantic} array")
            cursor += entries * width
            table = flat.reshape(entries, width) if entries else flat.reshape(0, width)
            if indexed:
                index = table[:, 0].astype(np.int64)
                size = int(index.max()) + 1 if entries else 0
                data = np.zeros((size, arity), dtype=np.float64)
                data[index] = table[:, 1:]
            else:
                data = table
            layers[upper] = data
        return layers, uv_names

    def _read_xsi_shape(self, element: Element) -> dict[str, np.ndarray]:
        layers: dict[str, np.ndarray] = {}
        for vertex_list in element.children_of("XSI_VertexList", "XSI_SubComponentAttributeList"):
            tokens = vertex_list.tokens
            if len(tokens) < 2 or _is_number(tokens[0]):
                continue
            semantics = str(tokens[0]).split("|")
            count = vertex_list.integer(1, "vertex count")
            cursor = 2
            for semantic in semantics:
                arity = _semantic_arity(semantic)
                required = count * arity
                if cursor + required > len(tokens):
                    break
                layers[semantic.upper()] = vertex_list.floats(cursor, required, semantic).reshape(count, arity)
                cursor += required
        return layers

    # .................................................................. cameras / lights
    def _camera_object(self, element: Element) -> SceneObject:
        return SceneObject(
            name=clean_name(element.name) or "Camera",
            kind="CAMERA",
            camera=self._read_camera(element),
            source_template=element.template,
        )

    def _read_camera(self, element: Element) -> Camera:
        tokens = element.tokens
        if element.template == "XSI_Camera":
            if len(tokens) < 9:
                raise DotXSIError(
                    f"XSI_Camera at line {element.line} has {len(tokens)} values; expected at least 9."
                )
            return Camera(
                aspect=element.number(1, "camera aspect"),
                field_of_view=element.number(2, "camera field of view"),
                fov_type="VERTICAL" if element.integer(3, "camera fov type") == 0 else "HORIZONTAL",
                projection="ORTHO" if element.integer(4, "camera projection") == 0 else "PERSP",
                ortho_height=element.number(5, "camera ortho height"),
                near=element.number(7, "camera near plane"),
                far=element.number(8, "camera far plane"),
            )
        if len(tokens) < 10:
            raise DotXSIError(f"SI_Camera at line {element.line} has {len(tokens)} values; expected 10.")
        return Camera(
            position=_vec3(element, 0, "camera position"),
            target=_vec3(element, 3, "camera interest"),
            roll=self._deg(element.number(6, "camera roll")),
            field_of_view=element.number(7, "camera field of view"),
            fov_type="VERTICAL",
            near=element.number(8, "camera near plane"),
            far=element.number(9, "camera far plane"),
        )

    def _light_object(self, element: Element) -> SceneObject:
        obj = SceneObject(
            name=clean_name(element.name) or "Light",
            kind="LIGHT",
            light=self._read_light(element),
            source_template=element.template,
        )
        self._light_orientation(element, obj)
        return obj

    def _light_orientation(self, element: Element, obj: SceneObject) -> None:
        """Infinite (type 3) and directional lights store an orientation instead of an interest."""
        light = obj.light
        if light is None or element.template != "SI_Light":
            return
        tokens = element.tokens
        if light.kind == "SUN" and len(tokens) == 10:
            orientation = _vec3(element, 7, "light orientation")
            light.target = None
            obj.transform = Transform(
                position=light.position or (0.0, 0.0, 0.0),
                rotation=tuple(self._deg(v) for v in orientation),  # type: ignore[arg-type]
            )
            light.position = None
            obj.properties["softimage_light_type"] = element.integer(0, "light type")

    def _read_light(self, element: Element) -> Light:
        tokens = element.tokens
        if element.template == "XSI_Light":
            # XSI_Light: type, color RGB, position, interest, cone, spread (5.0+); fall through.
            pass
        if len(tokens) < 7:
            raise DotXSIError(
                f"SI_Light at line {element.line} has {len(tokens)} values; expected at least 7."
            )
        type_index = element.integer(0, "light type")
        light = Light(
            kind={0: "POINT", 1: "SUN", 2: "SPOT", 3: "SUN"}.get(type_index, "POINT"),
            color=_vec3(element, 1, "light color"),
            position=_vec3(element, 4, "light position"),
        )
        if len(tokens) >= 10:
            light.target = _vec3(element, 7, "light interest")
        if len(tokens) >= 11:
            light.cone_angle = element.number(10, "light cone")
        if len(tokens) >= 12:
            light.spread_angle = element.number(11, "light spread")
        info = element.child("SI_LightInfo")
        if info is not None and len(info.tokens) >= 9:
            light.intensity = info.number(8, "light intensity")
        return light

    # .................................................................. animation
    def _collect_mixer(self, element: Element) -> None:
        if element.template == "XSI_Action":
            name = element.name or "Action"
            action = self.actions.setdefault(name, Action(name=name))
            values = element.values
            if len(values) >= 2:
                start, end = _number(values[0], None), _number(values[1], None)
                rate = self.scene.frame_rate or 30.0
                # Action ranges are written in seconds.
                if start is not None and end is not None:
                    action.start = start * rate if action.start is None else min(action.start, start * rate)
                    action.end = end * rate if action.end is None else max(action.end, end * rate)
            for curve in element.descendants("SI_FCurve"):
                self._collect_curve(curve, name)
            return
        for child in element.children:
            if child.template in {"XSI_Action", "XSI_Mixer"}:
                self._collect_mixer(child)
            elif child.template == "SI_FCurve":
                self._collect_curve(child, element.name or "Mixer")
            else:
                self.unknown[child.template] += 1

    def _collect_curve(self, element: Element, action: str, owner: SceneObject | None = None) -> None:
        for curve in self._read_fcurve(element):
            curve.action = action
            if owner is not None and (not curve.target or same_name(curve.target, owner.name)):
                curve.target = owner.name
            self.curves.append(curve)
            if action:
                self.actions.setdefault(action, Action(name=action)).curves.append(curve)

    def _read_fcurve(self, element: Element) -> list[AnimationCurve]:
        """SI_FCurve: object, parameter, interpolation, number of curves, values per key (frame excluded),
        number of keys, keys. CUBIC keys: value, in (dx, dy), out (dx, dy) relative to the key; HERMITE
        keys: value, in slope, out slope."""
        tokens = element.tokens
        if len(tokens) < 6:
            raise DotXSIError(
                f"SI_FCurve at line {element.line} has {len(tokens)} values; expected at least 6."
            )
        target = clean_name(str(tokens[0]))
        channel = str(tokens[1])
        interpolation = str(tokens[2]).upper()
        curve_count = max(1, element.integer(3, "curve count"))
        per_curve = max(1, element.integer(4, "values per key"))
        key_count = element.integer(5, "key count")
        available = len(tokens) - 6
        per_key = 1 + curve_count * per_curve
        if available < key_count * per_key:
            # Some writers count the frame among the key values.
            if curve_count == 1 and per_curve >= 2 and available >= key_count * per_curve:
                per_key = per_curve
                per_curve = per_curve - 1
            else:
                raise DotXSIError(
                    f"SI_FCurve at line {element.line} declares {key_count} keys of {per_key} values, "
                    "but its key array is truncated."
                )
        table = element.floats(6, key_count * per_key, "animation key").reshape(key_count, per_key)
        frames = table[:, 0].copy()
        curves = []
        for component in range(curve_count):
            base = 1 + component * per_curve
            values = table[:, base].copy()
            prop, index = resolve_channel(channel, component if curve_count > 1 else None)
            curve = AnimationCurve(
                target=target, channel=channel, interpolation=interpolation,
                frames=frames, values=values, prop=prop, index=index,
            )
            if interpolation == "CUBIC" and per_curve >= 5:
                curve.handles_left = np.column_stack(
                    (frames + table[:, base + 1], values + table[:, base + 2])
                )
                curve.handles_right = np.column_stack(
                    (frames + table[:, base + 3], values + table[:, base + 4])
                )
            elif interpolation == "HERMITE" and per_curve >= 3:
                curve.handles_left, curve.handles_right = _hermite_handles(
                    frames, values, table[:, base + 1], table[:, base + 2]
                )
            if prop == "rotation" and self.scene.angles_in_radians:
                _scale_curve(curve, 180.0 / math.pi)
            curves.append(curve)
        return curves

    def _assign_curves(self) -> None:
        by_name: dict[str, SceneObject] = {}
        by_short: dict[str, SceneObject] = {}
        for obj in self.scene.walk():
            by_name.setdefault(obj.name.casefold(), obj)
            by_short.setdefault(obj.name.rsplit(".", 1)[-1].casefold(), obj)
        missing: dict[str, int] = defaultdict(int)
        unsupported: dict[str, int] = defaultdict(int)
        for curve in self.curves:
            key = clean_name(curve.target).casefold()
            target = by_name.get(key) or by_short.get(key.rsplit(".", 1)[-1])
            if target is None:
                missing[curve.target] += 1
                continue
            if curve.prop is None:
                unsupported[curve.channel.rsplit(".", 1)[-1]] += 1
            target.animation.append(curve)
        for name, count in missing.items():
            self.scene.warnings.append(
                f"Animation target {name!r} was not found; skipped {count} F-curve(s)."
            )
        for channel, count in unsupported.items():
            self.scene.warnings.append(f"Skipped {count} F-curve(s) on unsupported parameter {channel!r}.")

    def _assign_shape_animation(self) -> None:
        for owner, element in self.shape_animations:
            mesh = owner.mesh
            if mesh is None:
                continue
            tokens = element.tokens
            mesh.shape_interpolation = str(tokens[0]).upper() if tokens else "LINEAR"
            for index, shape in enumerate(element.children_of("SI_Shape")):
                layers, _ = self._read_shape(shape)
                positions = layers.get("POSITION")
                if positions is None:
                    continue
                if positions.shape != mesh.points.shape:
                    padded = mesh.points.copy()
                    count = min(len(padded), len(positions))
                    padded[:count] = positions[:count]
                    positions = padded
                suffix = re.search(r"-(\d+)$", shape.name)
                number = int(suffix.group(1)) if suffix else index
                mesh.shape_keys.append(ShapeKey(name=f"Frame {number}", points=positions, frame=None))
            driver = next((c for c in owner.animation if c.prop == "shape_animation"), None)
            if driver is None:
                start = self.scene.frame_start
                for number, key in enumerate(mesh.shape_keys):
                    key.frame = start + number
            else:
                owner.properties["softimage_shape_animation_curve"] = driver.channel

    def _assign_envelopes(self) -> None:
        if not self.envelopes:
            return
        objects = list(self.scene.walk())
        parsed = []
        largest = 0.0
        for envelope in self.envelopes:
            tokens = envelope.tokens
            if len(tokens) < 3:
                self.scene.warnings.append(
                    f"SI_Envelope at line {envelope.line} has fewer than three header values."
                )
                continue
            target_name = clean_name(str(tokens[0]) or envelope.name)
            deformer = clean_name(str(tokens[1]))
            count = envelope.integer(2, "envelope weight count")
            table = envelope.floats(3, count * 2, "envelope weights").reshape(count, 2)
            largest = max(largest, float(table[:, 1].max()) if count else 0.0)
            parsed.append((target_name, deformer, table))
        percent = largest > 1.0 + 1e-6
        deformers: set[str] = set()
        for target_name, deformer, table in parsed:
            target = next(
                (obj for obj in objects if obj.mesh is not None and same_name(obj.name, target_name)),
                None,
            )
            if target is None:
                self.scene.warnings.append(f"Envelope target {target_name!r} was not found.")
                continue
            vertices = table[:, 0].astype(np.int64)
            weights = table[:, 1] / (100.0 if percent else 1.0)
            valid = (vertices >= 0) & (vertices < len(target.mesh.points))
            old = target.mesh.envelope_weights.get(deformer)
            vertices, weights = vertices[valid], np.clip(weights[valid], 0.0, 1.0)
            if old is not None:
                vertices = np.concatenate((old[0], vertices))
                weights = np.concatenate((old[1], weights))
            target.mesh.envelope_weights[deformer] = (vertices, weights)
            deformers.add(deformer.casefold())
        for obj in objects:
            if obj.name.casefold() in deformers and not obj.bone_role:
                obj.bone_role = "DEFORMER"


# ---------------------------------------------------------------------- mesh assembly
_LAYER_ARITY = {"POSITION": 3, "NORMAL": 3, "TANGENT": 3, "BINORMAL": 3, "COLOR": 4}


def _semantic_arity(semantic: str) -> int:
    upper = semantic.upper()
    if upper.startswith("TEX_COORD") or upper.startswith("UV"):
        return 2
    for key, arity in _LAYER_ARITY.items():
        if upper.startswith(key):
            return arity
    return 1


class _MeshBuilder:
    def __init__(self, converter: _Converter, mesh: Mesh, layers: dict[str, np.ndarray],
                 uv_names: dict[str, str]):
        self.converter = converter
        self.mesh = mesh
        self.layers = layers
        self.uv_names = uv_names
        self.sizes: list[np.ndarray] = []
        self.corners: list[np.ndarray] = []
        self.materials: list[np.ndarray] = []
        self.attributes: dict[str, list[np.ndarray | None]] = defaultdict(list)
        self.corner_total = 0
        self.material_slots: dict[str, int] = {}

    def _material_slot(self, name: str) -> int:
        if name not in self.material_slots:
            self.material_slots[name] = len(self.material_slots)
            self.mesh.material_names.append(name)
        return self.material_slots[name]

    def _valid_polygons(self, element: Element, sizes: np.ndarray, corners: np.ndarray,
                        what: str) -> np.ndarray | None:
        """Corner mask of polygons whose points all exist, or None when every polygon is valid.
        Polygons referencing missing points (seen in some exporter output) are dropped with a warning."""
        count = len(self.mesh.points)
        bad_corner = (corners < 0) | (corners >= count)
        if not bad_corner.any():
            return None
        polygon_of_corner = np.repeat(np.arange(len(sizes)), sizes)
        bad_polygon = np.zeros(len(sizes), dtype=bool)
        bad_polygon[polygon_of_corner[bad_corner]] = True
        self.converter.scene.warnings.append(
            f"{what} at line {element.line}: dropped {int(bad_polygon.sum())} polygon(s) that reference "
            f"points outside 0..{max(count - 1, 0)}."
        )
        return ~bad_polygon[polygon_of_corner]

    def _append(self, element: Element, sizes: np.ndarray, corners: np.ndarray, material: str,
                information: str, cursor: int, what: str) -> None:
        corner_count = len(corners)
        keep = self._valid_polygons(element, sizes, corners, what)
        if keep is not None:
            polygon_starts = np.concatenate(([0], np.cumsum(sizes)[:-1]))
            sizes = sizes[keep[polygon_starts]]
            corners = corners[keep]
        self.sizes.append(sizes.astype(np.int32))
        self.corners.append(corners.astype(np.int32))
        self.materials.append(np.full(len(sizes), self._material_slot(material), dtype=np.int32))
        tokens = element.tokens
        seen = set()
        for semantic in filter(None, information.split("|")):
            upper = semantic.upper()
            if cursor + corner_count > len(tokens):
                self.converter.scene.warnings.append(
                    f"{element.name or element.template}: missing {semantic} corner indices."
                )
                break
            indices = element.ints(cursor, corner_count, f"{semantic} index")
            cursor += corner_count
            if keep is not None:
                indices = indices[keep]
            source = self.layers.get(upper)
            if source is None or (len(indices) and (indices.min() < 0 or indices.max() >= len(source))):
                self.converter.scene.warnings.append(
                    f"{element.name or element.template}: {semantic} indices could not be resolved."
                )
                continue
            key = self._attribute_key(upper)
            if key is None:
                continue
            seen.add(key)
            self._pad(key)
            self.attributes[key].append(source[indices])
        for key in self.attributes:
            if key not in seen:
                self._pad(key)
                self.attributes[key].append(None)
        self.corner_total += len(corners)

    def _attribute_key(self, upper: str) -> str | None:
        if upper.startswith("NORMAL"):
            return "NORMAL"
        if upper.startswith("TEX_COORD") or upper.startswith("UV"):
            return upper
        if upper.startswith("COLOR"):
            return upper
        return None

    def _pad(self, key: str) -> None:
        # Attribute first seen on a later list: earlier lists had none.
        missing = len(self.sizes) - 1 - len(self.attributes[key])
        if missing > 0:
            self.attributes[key].extend([None] * missing)

    def add_polygon_list(self, element: Element) -> None:
        tokens = element.tokens
        if len(tokens) < 4:
            raise DotXSIError(
                f"{element.template} at line {element.line} has fewer than four header values."
            )
        if element.template == "XSI_PolygonList":
            information, material = str(tokens[0]), str(tokens[1])
            corner_count = element.integer(2, "polygon node count")
            polygon_count = element.integer(3, "polygon count")
        else:
            polygon_count = element.integer(0, "polygon count")
            information, material = str(tokens[1]), str(tokens[2])
            corner_count = element.integer(3, "polygon node count")
        cursor = 4
        if cursor + polygon_count + corner_count > len(tokens):
            raise DotXSIError(f"Polygon list at line {element.line} is truncated.")
        sizes = element.ints(cursor, polygon_count, "polygon size")
        cursor += polygon_count
        if int(sizes.sum()) != corner_count:
            raise DotXSIError(
                f"Polygon list at line {element.line} declares {corner_count} nodes, "
                f"but polygon sizes sum to {int(sizes.sum())}."
            )
        corners = element.ints(cursor, corner_count, "point index")
        cursor += corner_count
        self._append(element, sizes, corners, material, information, cursor, "Polygon list")

    def add_triangle_list(self, element: Element) -> None:
        tokens = element.tokens
        if len(tokens) < 3:
            raise DotXSIError(f"SI_TriangleList at line {element.line} has fewer than three header values.")
        triangle_count = element.integer(0, "triangle count")
        information, material = str(tokens[1]), str(tokens[2])
        corner_count = triangle_count * 3
        if 3 + corner_count > len(tokens):
            raise DotXSIError(f"Triangle list at line {element.line} is truncated.")
        corners = element.ints(3, corner_count, "point index")
        self._append(element, np.full(triangle_count, 3, np.int32), corners, material, information,
                     3 + corner_count, "Triangle list")

    def add_triangle_strips(self, element: Element) -> None:
        """SI_TriStrip / strip lists: <vertex count>, <info>, <material>, point indices, attributes."""
        tokens = element.tokens
        if len(tokens) < 3:
            return
        count = element.integer(0, "strip vertex count")
        information, material = str(tokens[1]), str(tokens[2])
        strip = element.ints(3, count, "strip point index")
        if count < 3 or (len(strip) and (strip.min() < 0 or strip.max() >= len(self.mesh.points))):
            self.converter.scene.warnings.append(
                f"Triangle strip at line {element.line} was skipped (invalid point indices)."
            )
            return
        a, b, c = strip[:-2], strip[1:-1], strip[2:]
        odd = (np.arange(count - 2) % 2) == 1
        first = np.where(odd, b, a)
        second = np.where(odd, a, b)
        corners = np.column_stack((first, second, c)).reshape(-1)
        expand = np.column_stack((np.where(odd, np.arange(1, count - 1), np.arange(count - 2)),
                                  np.where(odd, np.arange(count - 2), np.arange(1, count - 1)),
                                  np.arange(2, count))).reshape(-1)
        self.sizes.append(np.full(count - 2, 3, np.int32))
        self.corners.append(corners.astype(np.int32))
        self.materials.append(np.full(count - 2, self._material_slot(material), np.int32))
        cursor = 3 + count
        seen = set()
        for semantic in filter(None, information.split("|")):
            if cursor + count > len(tokens):
                break
            indices = element.ints(cursor, count, f"{semantic} index")[expand]
            cursor += count
            source = self.layers.get(semantic.upper())
            key = self._attribute_key(semantic.upper())
            if source is None or key is None or (len(indices) and indices.max() >= len(source)):
                continue
            seen.add(key)
            self._pad(key)
            self.attributes[key].append(source[indices])
        for key in self.attributes:
            if key not in seen:
                self._pad(key)
                self.attributes[key].append(None)
        self.corner_total += len(corners)

    def finish(self) -> None:
        mesh = self.mesh
        if not self.sizes:
            return
        mesh.polygon_sizes = np.concatenate(self.sizes)
        mesh.corner_vertices = np.concatenate(self.corners)
        mesh.face_material_index = np.concatenate(self.materials)
        for key, parts in self.attributes.items():
            parts = parts + [None] * (len(self.sizes) - len(parts))
            arity = next(p.shape[1] for p in parts if p is not None)
            filled = []
            for part, corners in zip(parts, self.corners):
                if part is None:
                    fill = np.zeros((len(corners), arity))
                    if key.startswith("COLOR"):
                        fill[:] = 1.0
                    filled.append(fill)
                else:
                    filled.append(part)
            data = np.concatenate(filled)
            if key == "NORMAL":
                mesh.corner_normals = data
            elif key.startswith("COLOR"):
                if data.shape[1] < 4:
                    data = np.column_stack((data, np.ones((len(data), 4 - data.shape[1]))))
                mesh.color_layers[key if key != "COLOR" else "COLOR"] = data
            else:
                projection = self.uv_names.get(key)
                name = re.sub(r"^TEX_COORD_?", "", key, flags=re.IGNORECASE)
                if name.upper() == "UV" and projection:
                    name = projection
                mesh.uv_layers[name or "UVMap"] = data


# ---------------------------------------------------------------------- helpers
_PARAM_TYPES = {
    "BOOLEAN", "BYTE", "INTEGER", "LONG", "FLOAT", "DOUBLE", "STRING", "COLOR", "VECTOR", "IMAGE",
}


def _progid(shader: Element | None) -> str:
    if shader is None:
        return ""
    first = shader.tokens[0] if shader.tokens else ""
    return str(first) if isinstance(first, QuotedStr) and "." in first else ""


def _shader_parameters(shader: Element | None) -> dict[str, object]:
    if shader is None:
        return {}
    values = shader.values
    params: dict[str, object] = {}
    for index in range(len(values) - 2):
        name, middle, last = values[index:index + 3]
        if not isinstance(name, str):
            continue
        middle_type = isinstance(middle, str) and middle.upper() in _PARAM_TYPES
        last_type = isinstance(last, str) and last.upper() in _PARAM_TYPES
        if middle_type:
            params.setdefault(name, last)
        elif last_type:
            params.setdefault(name, middle)
    return params


def _color(params: dict[str, object], prefix: str, default: tuple[float, float, float, float]):
    return tuple(
        float(_number(params.get(f"{prefix}.{channel}"), fallback))
        for channel, fallback in zip(("red", "green", "blue", "alpha"), default)
    )


def _number(value: object, default):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if isinstance(value, str) and not isinstance(value, QuotedStr):
        try:
            return float(value)
        except ValueError:
            return default
    return default


def _is_number(token: str) -> bool:
    if isinstance(token, QuotedStr):
        return False
    try:
        float(token)
        return True
    except ValueError:
        return False


def _roughness_from_power(power: float) -> float:
    if power is None or power <= -2.0:
        return 0.5
    return float(max(0.0, min(1.0, math.sqrt(2.0 / (power + 2.0)))))


def _vec3(element: Element, start: int, what: str) -> tuple[float, float, float]:
    if start + 3 > len(element.tokens):
        raise DotXSIError(f"{what} in {element.template} at line {element.line} is truncated.")
    return tuple(element.floats(start, 3, what).tolist())  # type: ignore[return-value]


def _rotation_order(value: object) -> str:
    orders = ("XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX")
    if isinstance(value, str):
        normalized = value.upper().replace("-", "")
        if normalized in orders:
            return normalized
    if isinstance(value, (int, float)):
        return orders[int(value) % 6]
    return "XYZ"


def _hermite_handles(frames, values, slope_in, slope_out):
    count = len(frames)
    gap_prev = np.empty(count)
    gap_next = np.empty(count)
    if count > 1:
        gaps = np.diff(frames)
        gap_prev[1:] = gaps
        gap_prev[0] = gaps[0]
        gap_next[:-1] = gaps
        gap_next[-1] = gaps[-1]
    else:
        gap_prev[:] = gap_next[:] = 1.0
    left = np.column_stack((frames - gap_prev / 3.0, values - slope_in * gap_prev / 3.0))
    right = np.column_stack((frames + gap_next / 3.0, values + slope_out * gap_next / 3.0))
    return left, right


def _scale_curve(curve: AnimationCurve, factor: float) -> None:
    curve.values = curve.values * factor
    for name in ("handles_left", "handles_right"):
        handles = getattr(curve, name)
        if handles is not None:
            handles = handles.copy()
            handles[:, 1] *= factor
            setattr(curve, name, handles)
