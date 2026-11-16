# SPDX-License-Identifier: GPL-3.0-or-later
"""Reader for text dotXSI 3.x files with partial 5.0/6.0 template support."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
import re
from typing import Iterable, Iterator, Sequence

from .model import (
    AnimationCurve,
    Camera,
    Light,
    Material,
    Mesh,
    Scene,
    SceneObject,
    Transform,
)


class DotXSIError(ValueError):
    """Raised when a dotXSI file cannot be decoded safely."""


@dataclass(frozen=True)
class Header:
    version: str
    encoding: str
    float_bits: int
    raw: str


@dataclass(frozen=True)
class Token:
    kind: str
    value: object
    line: int
    column: int


@dataclass
class Element:
    template: str
    name: str = ""
    values: list[object] = field(default_factory=list)
    children: list["Element"] = field(default_factory=list)
    line: int = 0

    def child(self, *templates: str) -> "Element | None":
        wanted = set(templates)
        return next((item for item in self.children if item.template in wanted), None)

    def children_of(self, *templates: str) -> list["Element"]:
        wanted = set(templates)
        return [item for item in self.children if item.template in wanted]

    def descendants(self, *templates: str) -> Iterator["Element"]:
        wanted = set(templates)
        for child in self.children:
            if child.template in wanted:
                yield child
            yield from child.descendants(*templates)


_HEADER = re.compile(
    rb"^(?:\xef\xbb\xbf)?xsi\s+([0-9]{4})(txt|bin)\s+([0-9]{4})\s*$",
    re.IGNORECASE,
)
_TOKEN = re.compile(
    r"""
    (?P<SPACE>\s+)
  | (?P<COMMENT>//[^\r\n]*|\#[^\r\n]*|/\*.*?\*/)
  | (?P<STRING>"(?:\\.|[^"\\])*")
  | (?P<LBRACE>\{)
  | (?P<RBRACE>\})
  | (?P<SEP>[,;])
  | (?P<ATOM>[^\s{},;"]+)
    """,
    re.VERBOSE | re.DOTALL,
)
_INTEGER = re.compile(r"^[+-]?\d+$")
_NUMBER = re.compile(
    r"^[+-]?(?:\d+\.\d*|\.\d+|\d+)(?:[eEdD][+-]?\d+)?$"
)


def read_header(path: str | Path) -> Header:
    with open(path, "rb") as stream:
        first = stream.readline(256).rstrip(b"\r\n")
    match = _HEADER.match(first)
    if not match:
        if first.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise DotXSIError("This is a native Softimage scene; import it as .scn or .emdl.")
        shown = first.decode("ascii", "replace")
        raise DotXSIError(f"Not a recognized dotXSI header: {shown!r}")
    encoding = match.group(2).decode("ascii").lower()
    if encoding == "bin":
        raise DotXSIError(
            "Binary dotXSI is not supported. Re-export the file from Softimage as ASCII text."
        )
    bits = int(match.group(3))
    return Header(
        version=match.group(1).decode("ascii"),
        encoding=encoding,
        float_bits=bits,
        raw=first.decode("ascii", "replace"),
    )


def load(path: str | Path) -> Scene:
    source = Path(path)
    data = source.read_bytes()
    first_break = data.find(b"\n")
    if first_break < 0:
        read_header(source)
        raise DotXSIError("The dotXSI file contains a header but no body.")
    header = read_header(source)
    body = data[first_break + 1 :]
    warnings: list[str] = []
    try:
        text = body.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = body.decode("cp1252")
        warnings.append("The file is not UTF-8; decoded it as Windows-1252.")
    elements = _Parser(_tokens(text, line_offset=1)).iter_elements()
    return _convert(source, header, elements, warnings)


def loads(text: str, source_path: str = "<memory>") -> Scene:
    first, separator, body = text.lstrip("\ufeff").partition("\n")
    match = re.fullmatch(
        r"xsi\s+([0-9]{4})(txt|bin)\s+([0-9]{4})\s*",
        first.rstrip("\r"),
        re.IGNORECASE,
    )
    if not match:
        raise DotXSIError(f"Not a recognized dotXSI header: {first!r}")
    if not separator:
        raise DotXSIError("The dotXSI text contains a header but no body.")
    if match.group(2).lower() == "bin":
        raise DotXSIError(
            "Binary dotXSI is not supported. Re-export the file from Softimage as ASCII text."
        )
    header = Header(match.group(1), "txt", int(match.group(3)), first)
    return _convert(
        Path(source_path),
        header,
        _Parser(_tokens(body, line_offset=1)).iter_elements(),
        [],
    )


def _decode_string(raw: str) -> str:
    body = raw[1:-1]
    output: list[str] = []
    index = 0
    escapes = {'"': '"', "\\": "\\", "n": "\n", "r": "\r", "t": "\t"}
    while index < len(body):
        char = body[index]
        if char == "\\" and index + 1 < len(body):
            following = body[index + 1]
            if following in escapes:
                output.append(escapes[following])
            else:
                output.extend(("\\", following))
            index += 2
            continue
        output.append(char)
        index += 1
    return "".join(output)


def _atom(raw: str) -> object:
    if _INTEGER.fullmatch(raw):
        return int(raw)
    if _NUMBER.fullmatch(raw):
        return float(raw.replace("D", "E").replace("d", "e"))
    return raw


def _tokens(text: str, line_offset: int = 0) -> Iterator[Token]:
    position = 0
    line = 1 + line_offset
    column = 1
    for match in _TOKEN.finditer(text):
        if match.start() != position:
            fragment = text[position : match.start()]
            raise DotXSIError(
                f"Unexpected text at line {line}, column {column}: {fragment[:32]!r}"
            )
        kind = match.lastgroup or ""
        raw = match.group()
        token_line, token_column = line, column
        newlines = raw.count("\n")
        if newlines:
            line += newlines
            column = len(raw) - raw.rfind("\n")
        else:
            column += len(raw)
        position = match.end()
        if kind in {"SPACE", "COMMENT"}:
            continue
        value: object
        if kind == "STRING":
            value = _decode_string(raw)
        elif kind == "ATOM":
            value = _atom(raw)
        else:
            value = raw
        yield Token(kind, value, token_line, token_column)
    if position != len(text):
        raise DotXSIError(
            f"Unexpected text at line {line}, column {column}: {text[position:position + 32]!r}"
        )
    yield Token("EOF", "", line, column)


class _Parser:
    def __init__(self, tokens: Iterator[Token]):
        self._tokens = tokens
        self._lookahead: list[Token] = []

    def _peek(self, offset: int = 0) -> Token:
        while len(self._lookahead) <= offset:
            self._lookahead.append(next(self._tokens))
        return self._lookahead[offset]

    def _take(self) -> Token:
        token = self._peek()
        self._lookahead.pop(0)
        return token

    def parse(self) -> list[Element]:
        return list(self.iter_elements())

    def iter_elements(self) -> Iterator[Element]:
        while self._peek().kind != "EOF":
            if self._peek().kind == "SEP":
                self._take()
                continue
            if not self._element_start():
                token = self._peek()
                raise DotXSIError(
                    f"Expected a template at line {token.line}, column {token.column}; "
                    f"found {token.value!r}."
                )
            yield self._element()

    def _element_start(self) -> bool:
        if self._peek().kind != "ATOM":
            return False
        if self._peek(1).kind == "LBRACE":
            return True
        return (
            self._peek(1).kind in {"ATOM", "STRING"}
            and self._peek(2).kind == "LBRACE"
        )

    def _element(self) -> Element:
        template = self._take()
        name = ""
        if self._peek().kind != "LBRACE":
            name = str(self._take().value)
        opening = self._take()
        if opening.kind != "LBRACE":
            raise DotXSIError(
                f"Expected '{{' after {template.value!r} at line {template.line}."
            )
        element = Element(str(template.value), name, line=template.line)
        while True:
            token = self._peek()
            if token.kind == "EOF":
                raise DotXSIError(
                    f"Unclosed {element.template} block starting at line {element.line}."
                )
            if token.kind == "RBRACE":
                self._take()
                return element
            if token.kind == "SEP":
                self._take()
                continue
            if self._element_start():
                element.children.append(self._element())
                continue
            if token.kind in {"ATOM", "STRING"}:
                element.values.append(self._take().value)
                continue
            raise DotXSIError(
                f"Unexpected {token.value!r} at line {token.line}, column {token.column}."
            )


def _convert(
    source: Path,
    header: Header,
    elements: Iterable[Element],
    warnings: list[str],
) -> Scene:
    scene = Scene(
        source_path=str(source),
        format_version=header.version,
        encoding=header.encoding,
        float_bits=header.float_bits,
        warnings=warnings,
    )
    envelopes: list[Element] = []
    unassigned_curves: list[AnimationCurve] = []
    for element in elements:
        template = element.template
        if template == "SI_FileInfo":
            labels = ("project", "user", "created", "application")
            scene.metadata.update(zip(labels, element.values))
        elif template == "SI_Scene":
            scene.name = _clean_name(element.name) or scene.name
            values = element.values
            if values and isinstance(values[0], str) and values[0].upper() == "FRAMES":
                values = values[1:]
            if len(values) >= 3:
                scene.frame_start = _float(values[0], element, "scene start")
                scene.frame_end = _float(values[1], element, "scene end")
                scene.frame_rate = _float(values[2], element, "scene frame rate")
        elif template == "SI_CoordinateSystem":
            if len(element.values) >= 6:
                scene.coordinate_system = tuple(
                    _int(value, element, "coordinate system")
                    for value in element.values[:6]
                )
        elif template == "SI_MaterialLibrary":
            _read_material_library(scene, element)
        elif template == "XSI_ImageLibrary":
            _read_image_library(scene, element)
        elif template in {"SI_Model", "XSI_Model"}:
            obj, curves = _read_model(scene, element)
            scene.objects.append(obj)
            unassigned_curves.extend(curves)
        elif template in {"SI_Light", "XSI_Light"}:
            scene.objects.append(_read_light_object(element))
        elif template in {"SI_Camera", "XSI_Camera"}:
            scene.objects.append(_read_camera_object(element))
        elif template == "SI_EnvelopeList":
            envelopes.extend(element.children_of("SI_Envelope"))
        elif template == "SI_Envelope":
            envelopes.append(element)
        elif template == "SI_FCurve":
            unassigned_curves.append(_read_fcurve(element))
    _assign_curves(scene, unassigned_curves)
    _assign_envelopes(scene, envelopes)
    return scene


def _read_material_library(scene: Scene, library: Element) -> None:
    for element in library.children_of("XSI_Material", "SI_Material"):
        material = _read_material(element)
        scene.materials[material.name] = material


def _read_material(element: Element) -> Material:
    shader = next(
        (
            child
            for child in element.children
            if child.template == "XSI_Shader"
            and not any(word in child.name.lower() for word in ("image", "texture", "bump"))
        ),
        element.child("XSI_Shader"),
    )
    params = _shader_parameters(shader) if shader else {}
    diffuse = _color(params, "diffuse", (0.8, 0.8, 0.8, 1.0))
    if shader and "constant" in " ".join(map(str, shader.values[:1])).lower():
        diffuse = _color(params, "color", diffuse)
    specular = _color(params, "specular", (0.5, 0.5, 0.5, 1.0))[:3]
    shiny = _number(params.get("shiny"))
    roughness = (
        math.sqrt(2.0 / (shiny + 2.0))
        if shiny is not None and shiny > -2.0
        else _number(params.get("roughness"), 0.5)
    )
    transparency = _color(params, "transparency", (0.0, 0.0, 0.0, 0.0))
    alpha = max(0.0, min(1.0, 1.0 - max(transparency)))
    emission = _color(params, "incandescence", (0.0, 0.0, 0.0, 1.0))[:3]
    emission_strength = _number(params.get("inc_inten"), 0.0)
    image = None
    for image_shader in element.children_of("XSI_Shader"):
        image_params = _shader_parameters(image_shader)
        candidate = image_params.get("tex")
        if isinstance(candidate, str) and candidate:
            image = candidate
            break
    return Material(
        name=element.name or "Material",
        shader=shader.name if shader else "",
        diffuse=diffuse,
        specular=specular,
        roughness=max(0.0, min(1.0, float(roughness))),
        metallic=max(0.0, min(1.0, _number(params.get("metallic"), 0.0))),
        alpha=alpha,
        emission=emission,
        emission_strength=float(emission_strength),
        image=image,
        parameters=params,
    )


_PARAM_TYPES = {
    "BOOLEAN",
    "BYTE",
    "INTEGER",
    "LONG",
    "FLOAT",
    "DOUBLE",
    "STRING",
    "COLOR",
    "VECTOR",
    "IMAGE",
}


def _shader_parameters(shader: Element | None) -> dict[str, object]:
    if shader is None:
        return {}
    values = shader.values
    params: dict[str, object] = {}
    for index in range(len(values) - 2):
        name, middle, last = values[index : index + 3]
        if not isinstance(name, str):
            continue
        middle_type = isinstance(middle, str) and middle.upper() in _PARAM_TYPES
        last_type = isinstance(last, str) and last.upper() in _PARAM_TYPES
        if middle_type:
            params.setdefault(name, last)
        elif last_type:
            params.setdefault(name, middle)
    return params


def _read_image_library(scene: Scene, library: Element) -> None:
    for image in library.children_of("XSI_Image", "SI_Image"):
        filename = next(
            (
                value
                for value in image.values
                if isinstance(value, str)
                and value
                and not value.upper() in _PARAM_TYPES
            ),
            None,
        )
        if filename:
            scene.images[image.name] = filename


def _read_model(
    scene: Scene, element: Element
) -> tuple[SceneObject, list[AnimationCurve]]:
    obj = SceneObject(
        name=_clean_name(element.name) or "Model",
        source_template=element.template,
    )
    curves: list[AnimationCurve] = []
    for child in element.children:
        template = child.template
        if template == "SI_Transform":
            transform = _read_transform(child)
            if "BASEPOSE" in child.name.upper():
                obj.base_pose = transform
            else:
                obj.transform = transform
        elif template == "XSI_Transform":
            obj.transform = _read_transform(child)
        elif template == "XSI_BasePose":
            obj.base_pose = _read_transform(child)
        elif template == "SI_Visibility" and child.values:
            obj.visible = bool(_int(child.values[0], child, "visibility"))
        elif template == "SI_GlobalMaterial" and child.values:
            obj.material = str(child.values[0])
        elif template in {"SI_Mesh", "XSI_Mesh"}:
            obj.kind = "MESH"
            obj.mesh = _read_mesh(scene, child)
        elif template in {"SI_Camera", "XSI_Camera"}:
            obj.kind = "CAMERA"
            obj.camera = _read_camera(child)
            if child.name:
                obj.name = _clean_name(child.name)
        elif template in {"SI_Light", "XSI_Light"}:
            obj.kind = "LIGHT"
            obj.light = _read_light(child)
            if child.name:
                obj.name = _clean_name(child.name)
        elif template in {"SI_IK_Root", "SI_IK_Joint", "SI_IK_Effector"}:
            obj.kind = "BONE"
            if template == "SI_IK_Joint" and len(child.values) > 1:
                obj.bone_length = _float(child.values[1], child, "bone length")
        elif template in {"SI_Model", "XSI_Model"}:
            nested, nested_curves = _read_model(scene, child)
            obj.children.append(nested)
            curves.extend(nested_curves)
        elif template == "SI_FCurve":
            curve = _read_fcurve(child)
            if not curve.target or _same_name(curve.target, obj.name):
                obj.animation.append(curve)
            else:
                curves.append(curve)
    for fcurve in element.descendants("SI_FCurve"):
        if fcurve in element.children:
            continue
        curve = _read_fcurve(fcurve)
        if not curve.target or _same_name(curve.target, obj.name):
            obj.animation.append(curve)
        else:
            curves.append(curve)
    if obj.kind == "EMPTY":
        null = element.child("SI_Null")
        if null and null.name:
            obj.name = _clean_name(null.name)
    return obj, curves


def _read_transform(element: Element) -> Transform:
    values = element.values
    if element.template == "XSI_Transform":
        if len(values) < 10:
            raise DotXSIError(
                f"XSI_Transform at line {element.line} has {len(values)} values; expected at least 10."
            )
        order = _rotation_order(values[6])
        return Transform(
            position=_vec3(values, 0, element, "position"),
            rotation=_vec3(values, 3, element, "rotation"),
            scale=_vec3(values, 7, element, "scale"),
            rotation_order=order,
        )
    if element.template == "XSI_BasePose":
        if len(values) < 9:
            raise DotXSIError(
                f"XSI_BasePose at line {element.line} has {len(values)} values; expected 9."
            )
        return Transform(
            position=_vec3(values, 0, element, "position"),
            rotation=_vec3(values, 3, element, "rotation"),
            scale=_vec3(values, 6, element, "scale"),
        )
    if len(values) < 9:
        raise DotXSIError(
            f"SI_Transform at line {element.line} has {len(values)} values; expected 9."
        )
    return Transform(
        scale=_vec3(values, 0, element, "scale"),
        rotation=_vec3(values, 3, element, "rotation"),
        position=_vec3(values, 6, element, "position"),
    )


def _rotation_order(value: object) -> str:
    if isinstance(value, str):
        normalized = value.upper().replace("-", "")
        if normalized in {"XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX"}:
            return normalized
    if isinstance(value, (int, float)):
        return ("XYZ", "XZY", "YXZ", "YZX", "ZXY", "ZYX")[int(value) % 6]
    return "XYZ"


def _read_mesh(scene: Scene, element: Element) -> Mesh:
    shape = element.child("SI_Shape")
    layers = _read_shape(shape) if shape else {}
    if not shape:
        xsi_shape = element.child("XSI_Shape")
        if xsi_shape:
            layers.update(_read_xsi_shape(xsi_shape))
    positions = layers.get("POSITION", [])
    mesh = Mesh(points=[_as_vec3(value) for value in positions])
    polygon_lists = element.children_of("SI_PolygonList", "XSI_PolygonList")
    for polygon_list in polygon_lists:
        _append_polygon_list(scene, mesh, layers, polygon_list)
    for triangle_list in element.children_of("SI_TriangleList"):
        _append_triangle_list(scene, mesh, layers, triangle_list)
    if not mesh.points:
        scene.warnings.append(
            f"{_clean_name(element.name) or 'Mesh'}: no POSITION array was decoded."
        )
    return mesh


_LAYER_ARITY = {
    "POSITION": 3,
    "NORMAL": 3,
    "TANGENT": 3,
    "BINORMAL": 3,
    "COLOR": 4,
}


def _semantic_arity(semantic: str) -> int:
    upper = semantic.upper()
    if upper.startswith("TEX_COORD") or upper.startswith("UV"):
        return 2
    for key, arity in _LAYER_ARITY.items():
        if upper.startswith(key):
            return arity
    return 1


def _read_shape(element: Element) -> dict[str, list[tuple[float, ...]]]:
    values = element.values
    if len(values) < 2:
        return {}
    count = _int(values[0], element, "shape array count")
    cursor = 2
    layers: dict[str, list[tuple[float, ...]]] = {}
    for _ in range(count):
        if cursor + 1 >= len(values):
            raise DotXSIError(
                f"SI_Shape at line {element.line} ends before all {count} arrays."
            )
        entries = _int(values[cursor], element, "shape entry count")
        semantic = str(values[cursor + 1])
        cursor += 2
        if (
            semantic.upper().startswith("TEX_COORD")
            and cursor < len(values)
            and isinstance(values[cursor], str)
        ):
            cursor += 1
        arity = _semantic_arity(semantic)
        required = entries * arity
        if cursor + required > len(values):
            raise DotXSIError(
                f"{semantic} array at line {element.line} needs {required} scalars; "
                f"only {len(values) - cursor} remain."
            )
        flat = [
            _float(value, element, f"{semantic} value")
            for value in values[cursor : cursor + required]
        ]
        cursor += required
        layers[semantic.upper()] = [
            tuple(flat[index : index + arity])
            for index in range(0, len(flat), arity)
        ]
    return layers


def _read_xsi_shape(element: Element) -> dict[str, list[tuple[float, ...]]]:
    layers: dict[str, list[tuple[float, ...]]] = {}
    for vertex_list in element.children_of("XSI_VertexList"):
        values = vertex_list.values
        if len(values) < 2 or not isinstance(values[0], str):
            continue
        semantics = values[0].split("|")
        count = _int(values[1], vertex_list, "vertex count")
        cursor = 2
        for semantic in semantics:
            arity = _semantic_arity(semantic)
            required = count * arity
            if cursor + required > len(values):
                break
            flat = [
                _float(value, vertex_list, semantic)
                for value in values[cursor : cursor + required]
            ]
            layers[semantic.upper()] = [
                tuple(flat[index : index + arity])
                for index in range(0, len(flat), arity)
            ]
            cursor += required
    return layers


def _append_polygon_list(
    scene: Scene,
    mesh: Mesh,
    layers: dict[str, list[tuple[float, ...]]],
    element: Element,
) -> None:
    values = element.values
    if element.template == "XSI_PolygonList":
        if len(values) < 4:
            raise DotXSIError(
                f"XSI_PolygonList at line {element.line} has fewer than four header values."
            )
        information = str(values[0])
        material = str(values[1])
        corners = _int(values[2], element, "polygon node count")
        polygon_count = _int(values[3], element, "polygon count")
        cursor = 4
    else:
        if len(values) < 4:
            raise DotXSIError(
                f"SI_PolygonList at line {element.line} has fewer than four header values."
            )
        polygon_count = _int(values[0], element, "polygon count")
        information = str(values[1])
        material = str(values[2])
        corners = _int(values[3], element, "polygon node count")
        cursor = 4
    if cursor + polygon_count + corners > len(values):
        raise DotXSIError(
            f"Polygon list at line {element.line} is truncated."
        )
    sizes = [
        _int(value, element, "polygon size")
        for value in values[cursor : cursor + polygon_count]
    ]
    cursor += polygon_count
    if sum(sizes) != corners:
        raise DotXSIError(
            f"Polygon list at line {element.line} declares {corners} nodes, "
            f"but polygon sizes sum to {sum(sizes)}."
        )
    point_indices = [
        _int(value, element, "point index")
        for value in values[cursor : cursor + corners]
    ]
    cursor += corners
    start = 0
    for size in sizes:
        polygon = point_indices[start : start + size]
        if any(index < 0 or index >= len(mesh.points) for index in polygon):
            raise DotXSIError(
                f"Polygon list at line {element.line} references a point outside "
                f"0..{max(len(mesh.points) - 1, 0)}."
            )
        mesh.polygons.append(polygon)
        mesh.face_materials.append(material)
        start += size
    _append_corner_data(scene, mesh, layers, element, information, cursor, corners)


def _append_triangle_list(
    scene: Scene,
    mesh: Mesh,
    layers: dict[str, list[tuple[float, ...]]],
    element: Element,
) -> None:
    values = element.values
    if len(values) < 3:
        raise DotXSIError(
            f"SI_TriangleList at line {element.line} has fewer than three header values."
        )
    triangle_count = _int(values[0], element, "triangle count")
    information = str(values[1])
    material = str(values[2])
    corners = triangle_count * 3
    cursor = 3
    if cursor + corners > len(values):
        raise DotXSIError(
            f"Triangle list at line {element.line} is truncated."
        )
    point_indices = [
        _int(value, element, "point index")
        for value in values[cursor : cursor + corners]
    ]
    cursor += corners
    if any(index < 0 or index >= len(mesh.points) for index in point_indices):
        raise DotXSIError(
            f"Triangle list at line {element.line} references a point outside "
            f"0..{max(len(mesh.points) - 1, 0)}."
        )
    for start in range(0, corners, 3):
        mesh.polygons.append(point_indices[start : start + 3])
        mesh.face_materials.append(material)
    _append_corner_data(scene, mesh, layers, element, information, cursor, corners)


def _append_corner_data(
    scene: Scene,
    mesh: Mesh,
    layers: dict[str, list[tuple[float, ...]]],
    element: Element,
    information: str,
    cursor: int,
    corners: int,
) -> None:
    values = element.values
    for semantic in filter(None, information.split("|")):
        if cursor + corners > len(values):
            scene.warnings.append(
                f"{element.name or element.template}: missing {semantic} corner indices."
            )
            break
        indices = [
            _int(value, element, f"{semantic} index")
            for value in values[cursor : cursor + corners]
        ]
        cursor += corners
        source = layers.get(semantic.upper())
        if not source or any(index < 0 or index >= len(source) for index in indices):
            scene.warnings.append(
                f"{element.name or element.template}: {semantic} indices could not be resolved."
            )
            continue
        corners_data = [source[index] for index in indices]
        upper = semantic.upper()
        if upper.startswith("NORMAL"):
            mesh.corner_normals.extend(_as_vec3(value) for value in corners_data)
        elif upper.startswith("TEX_COORD") or upper.startswith("UV"):
            name = re.sub(r"^TEX_COORD_?", "", semantic, flags=re.IGNORECASE) or "UVMap"
            mesh.uv_layers.setdefault(name, []).extend(_as_vec2(value) for value in corners_data)
        elif upper.startswith("COLOR"):
            mesh.color_layers.setdefault(semantic, []).extend(
                _as_vec4(value) for value in corners_data
            )


def _read_camera_object(element: Element) -> SceneObject:
    return SceneObject(
        name=_clean_name(element.name) or "Camera",
        kind="CAMERA",
        camera=_read_camera(element),
        source_template=element.template,
    )


def _read_camera(element: Element) -> Camera:
    values = element.values
    if element.template == "XSI_Camera":
        if len(values) < 9:
            raise DotXSIError(
                f"XSI_Camera at line {element.line} has {len(values)} values; expected at least 9."
            )
        return Camera(
            aspect=_float(values[1], element, "camera aspect"),
            field_of_view=_float(values[2], element, "camera field of view"),
            projection="ORTHO" if _int(values[4], element, "camera projection") == 0 else "PERSP",
            ortho_height=_float(values[5], element, "camera ortho height"),
            near=_float(values[7], element, "camera near plane"),
            far=_float(values[8], element, "camera far plane"),
        )
    if len(values) < 10:
        raise DotXSIError(
            f"SI_Camera at line {element.line} has {len(values)} values; expected 10."
        )
    return Camera(
        position=_vec3(values, 0, element, "camera position"),
        target=_vec3(values, 3, element, "camera interest"),
        roll=_float(values[6], element, "camera roll"),
        field_of_view=_float(values[7], element, "camera field of view"),
        near=_float(values[8], element, "camera near plane"),
        far=_float(values[9], element, "camera far plane"),
    )


def _read_light_object(element: Element) -> SceneObject:
    return SceneObject(
        name=_clean_name(element.name) or "Light",
        kind="LIGHT",
        light=_read_light(element),
        source_template=element.template,
    )


def _read_light(element: Element) -> Light:
    values = element.values
    if len(values) < 7:
        raise DotXSIError(
            f"SI_Light at line {element.line} has {len(values)} values; expected at least 7."
        )
    type_index = _int(values[0], element, "light type")
    kinds = {0: "POINT", 1: "SUN", 2: "SPOT", 3: "SUN"}
    light = Light(
        kind=kinds.get(type_index, "POINT"),
        color=_vec3(values, 1, element, "light color"),
        position=_vec3(values, 4, element, "light position"),
    )
    if len(values) >= 10:
        light.target = _vec3(values, 7, element, "light interest")
    if len(values) >= 11:
        light.cone_angle = _float(values[10], element, "light cone")
    if len(values) >= 12:
        light.spread_angle = _float(values[11], element, "light spread")
    info = element.child("SI_LightInfo")
    if info and len(info.values) >= 9:
        light.intensity = _float(info.values[8], info, "light intensity")
    return light


def _read_fcurve(element: Element) -> AnimationCurve:
    values = element.values
    if len(values) < 6:
        raise DotXSIError(
            f"SI_FCurve at line {element.line} has {len(values)} values; expected at least 6."
        )
    target = _clean_name(str(values[0]))
    channel = str(values[1])
    interpolation = str(values[2])
    values_per_key = max(2, _int(values[4], element, "values per key"))
    key_count = _int(values[5], element, "key count")
    required = key_count * values_per_key
    if len(values) < 6 + required:
        raise DotXSIError(
            f"SI_FCurve at line {element.line} declares {key_count} keys of "
            f"{values_per_key} values, but its key array is truncated."
        )
    flat = [
        _float(value, element, "animation key")
        for value in values[6 : 6 + required]
    ]
    keys = [
        tuple(flat[index : index + values_per_key])
        for index in range(0, len(flat), values_per_key)
    ]
    return AnimationCurve(target, channel, interpolation, keys)


def _assign_curves(scene: Scene, curves: list[AnimationCurve]) -> None:
    objects = list(scene.walk())
    for curve in curves:
        target = next(
            (obj for obj in objects if _same_name(obj.name, curve.target)),
            None,
        )
        if target is None:
            scene.warnings.append(
                f"Animation target {curve.target!r} was not found; skipped {curve.channel}."
            )
        else:
            target.animation.append(curve)


def _assign_envelopes(scene: Scene, envelopes: list[Element]) -> None:
    objects = list(scene.walk())
    for envelope in envelopes:
        values = envelope.values
        if len(values) < 3:
            scene.warnings.append(
                f"SI_Envelope at line {envelope.line} has fewer than three header values."
            )
            continue
        target_name = _clean_name(str(values[0]) or envelope.name)
        deformer = _clean_name(str(values[1]))
        count = _int(values[2], envelope, "envelope weight count")
        if len(values) < 3 + count * 2:
            raise DotXSIError(
                f"SI_Envelope at line {envelope.line} declares {count} weights, "
                "but its weight array is truncated."
            )
        target = next(
            (
                obj
                for obj in objects
                if obj.mesh is not None and _same_name(obj.name, target_name)
            ),
            None,
        )
        if target is None:
            scene.warnings.append(
                f"Envelope target {target_name!r} was not found."
            )
            continue
        weights: list[tuple[int, float]] = []
        for index in range(count):
            vertex = _int(values[3 + index * 2], envelope, "envelope vertex")
            weight = _float(values[4 + index * 2], envelope, "envelope weight")
            if weight > 1.0:
                weight /= 100.0
            if 0 <= vertex < len(target.mesh.points):
                weights.append((vertex, max(0.0, min(1.0, weight))))
        target.mesh.envelope_weights.setdefault(deformer, []).extend(weights)


def _color(
    params: dict[str, object],
    prefix: str,
    default: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    values = []
    for channel, fallback in zip(("red", "green", "blue", "alpha"), default):
        values.append(_number(params.get(f"{prefix}.{channel}"), fallback))
    return tuple(float(value) for value in values)  # type: ignore[return-value]


def _number(value: object, default: float | None = None) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return default


def _float(value: object, element: Element, field_name: str) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        result = float(value)
        if math.isfinite(result):
            return result
    raise DotXSIError(
        f"{field_name} in {element.template} at line {element.line} is not a finite number: {value!r}."
    )


def _int(value: object, element: Element, field_name: str) -> int:
    result = _float(value, element, field_name)
    if not result.is_integer():
        raise DotXSIError(
            f"{field_name} in {element.template} at line {element.line} is not an integer: {value!r}."
        )
    return int(result)


def _vec3(
    values: Sequence[object],
    start: int,
    element: Element,
    field_name: str,
) -> tuple[float, float, float]:
    if start + 3 > len(values):
        raise DotXSIError(
            f"{field_name} in {element.template} at line {element.line} is truncated."
        )
    return tuple(
        _float(value, element, field_name) for value in values[start : start + 3]
    )  # type: ignore[return-value]


def _as_vec2(value: Sequence[float]) -> tuple[float, float]:
    return (float(value[0]), float(value[1]))


def _as_vec3(value: Sequence[float]) -> tuple[float, float, float]:
    return (float(value[0]), float(value[1]), float(value[2]))


def _as_vec4(value: Sequence[float]) -> tuple[float, float, float, float]:
    padded = tuple(value) + (1.0, 1.0, 1.0, 1.0)
    return tuple(float(item) for item in padded[:4])  # type: ignore[return-value]


def _clean_name(name: str) -> str:
    value = name.strip().strip('"')
    prefixes = (
        "MDL-",
        "MSH-",
        "SHP-",
        "SRT-",
        "BASEPOSE-",
        "CAM-",
        "LGT-",
    )
    for prefix in prefixes:
        if value.upper().startswith(prefix):
            return value[len(prefix) :]
    return value


def _same_name(left: str, right: str) -> bool:
    a, b = _clean_name(left), _clean_name(right)
    return a.casefold() == b.casefold() or a.rsplit(".", 1)[-1].casefold() == b.rsplit(".", 1)[-1].casefold()
