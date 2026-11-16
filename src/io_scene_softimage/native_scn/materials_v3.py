# SPDX-License-Identifier: GPL-3.0-or-later
"""Materials of scenes saved by XSI 2.x - 4.x (no `TLShell/Shader Definitions` stream).

Reverse engineered from the Half-Life 2 source scenes (see docs/native-format.md, "XSI 3.x materials"):

* Materials (class 4c4f7500, the same class as in later versions) live in the model's own object stream.
  A polymsh's cluster container (60f7ada0) refers to its polygon clusters (600e50f1); a cluster refers to
  the material applied to it. The material refers to its surface shader.
* Shaders are objects too, identified by class:
  - 7e5bfb8c: the Softimage|3D-compatible "Material__N" shader (what 3ds Max imports produced): colour
    parameter objects 3515cc71 (ambient), 3515cc72 (diffuse), 3515cc73 (specular), and a texture input
    (38d56c24) leading to a texture node;
  - 67120bbe: Phong; colour parameter objects 64e4ffa0 (ambient), 64e99530 (diffuse), 64efb1f0 (specular);
  - 5b003a9d: Constant.
  Colour objects hold RGBA as parameters 8c80deda / 8c80dedb / 8c80dedc / 8c80ded9.
* Texture nodes (class cf28e1e4) carry the UV projection name (UTF-16) in their preamble and connect to an
  image node (7e5bfb8d, "SoftColor") whose input is a cross-stream reference `0e b0 ff ff ff ff <handle>`
  to an image clip in `TLShell/Clips`. The clip and its image source in `TLShell/Sources` share a name; the
  source holds the file path.
"""

from __future__ import annotations

import re
import struct
from collections import namedtuple
from dataclasses import dataclass, field

from . import records

CLS_MATERIAL = "4c4f7500-51ef-11d0-854e-00a02417d029"
CLS_CONTAINER = "60f7ada0-7e12-11d0-9960-00a0243f0e60"
CLS_POLY_CLUSTER = "600e50f1-52fc-11d2-a924-00a024190119"
CLS_TEXTURE_NODE = "cf28e1e4"
SHADERS = {
    "7e5bfb8c-455d-11d1-864b-00a0c91412d9": "si3d",
    "67120bbe-c98c-11d1-9723-00a0243e3672": "phong",
    "5b003a9d-868a-11d4-9330-00e0293d48c6": "constant",
}
COLOURS = {
    "3515cc71": "ambient", "3515cc72": "diffuse", "3515cc73": "specular",       # si3d shader
    "64e4ffa0": "ambient", "64e99530": "diffuse", "64efb1f0": "specular",       # phong
}
RGBA = ("8c80deda", "8c80dedb", "8c80dedc", "8c80ded9")
P_NAME = "2b91c031-677a-11d1-b0cf-00a024c79287"
_SOURCE_REF = re.compile(rb"[\x06\x0e]\xb0\xff\xff\xff\xff", re.S)
_TEXT = re.compile(rb"(?:[\x20-\x7e]\x00){3,}", re.S)
_EXTENSION = re.compile(r"\.[A-Za-z]{2,4}$")

ClusterMaterial = namedtuple("ClusterMaterial", "name polygons material")


@dataclass
class LegacyMaterial:
    key: str
    name: str
    shader: str = ""
    colours: dict[str, tuple[float, float, float, float]] = field(default_factory=dict)
    images: list[tuple[str, list[str]]] = field(default_factory=list)   # (clip name, [paths])
    projections: list[str] = field(default_factory=list)


def _colour(st, obj):
    values = {}
    for record in obj.plist:
        guid = st.guid(record.id) if record.id is not None else None
        if guid and guid[:8] in RGBA and isinstance(record.value, (int, float)):
            values[guid[:8]] = float(record.value)
    if len(values) < 3:
        return None
    return tuple(values.get(k, 1.0 if k == "8c80ded9" else 0.0) for k in RGBA)


class _Clips:
    """Image clips (TLShell/Clips) and sources (TLShell/Sources) of a 3.x scene.

    A clip object is followed by helper objects; one of them references the clip's image source with a
    cross-stream reference `06 b0 ff ff ff ff <source handle>`. Names come from the objects' string
    parameters (exact), falling back to UTF-16 runs."""

    def __init__(self, cf):
        self.clip_names: dict[int, str] = {}
        self.clip_source: dict[int, int] = {}
        self.source_paths: dict[int, list[str]] = {}
        self.source_names: dict[str, int] = {}
        if cf.exists("TLShell/Sources"):
            data = cf.read("TLShell/Sources")
            for obj in _objects(data):
                name = _name(obj) or next(iter(_strings(data, obj.start, obj.end)), None)
                paths = []
                for text in _strings(data, obj.start, obj.end):
                    if ("\\" in text or "/" in text) and "." in text:
                        # One stored copy runs on into a length byte that decodes as an extra character.
                        candidate = text if _EXTENSION.search(text) else text[:-1]
                        if _EXTENSION.search(candidate) and candidate not in paths:
                            paths.append(candidate)
                if paths:
                    self.source_paths[obj.handle] = paths
                    if name:
                        self.source_names.setdefault(name, obj.handle)
        if cf.exists("TLShell/Clips"):
            data = cf.read("TLShell/Clips")
            current = None
            for obj in _objects(data):
                name = _name(obj)
                if name is None:
                    texts = _strings(data, obj.body, min(obj.end, obj.body + 200))
                    name = texts[0] if texts else None
                if name:
                    current = obj.handle
                    self.clip_names[current] = name
                if current is None or current in self.clip_source:
                    continue
                for m in _SOURCE_REF.finditer(data, obj.body, obj.end):
                    try:
                        handle, _q = records.cnum(data, m.end())
                    except (records.Lost, IndexError):
                        continue
                    if handle in self.source_paths:
                        self.clip_source[current] = handle
                        break

    def paths(self, handle: int) -> tuple[str | None, list[str]]:
        name = self.clip_names.get(handle)
        if name is None:
            return None, []
        source = self.clip_source.get(handle)
        if source is None:
            source = self.source_names.get(name)
        if source is None:
            # Last resort: the longest source name that prefixes the clip name.
            best = max((n for n in self.source_names if name.startswith(n)), key=len, default=None)
            source = self.source_names.get(best) if best else None
        return name, list(self.source_paths.get(source, [])) if source is not None else []


def _name(obj) -> str | None:
    """First string parameter of an object (its name in Sources / Clips streams)."""
    for record in obj.plist:
        if isinstance(record.value, str) and record.value:
            return record.value
    return None


def _objects(data):
    if data[:4] != records.MAGIC:
        return []
    try:
        return records.ObjectStream(data).objects
    except records.Lost:
        return []


def _strings(data, lo, hi):
    return [m.group().decode("utf-16-le") for m in _TEXT.finditer(data, lo, hi)]


class Reader:
    def __init__(self, sc):
        self.sc = sc
        self.clips = _Clips(sc.cfb)
        self.materials: dict[str, LegacyMaterial] = {}

    def material(self, storage: str, st, obj) -> str:
        key = f"{storage.rsplit('/', 1)[-1]}:{obj.handle}"
        if key in self.materials:
            return key
        name = obj.params.get(st_name_id(st)) if st_name_id(st) is not None else None
        result = LegacyMaterial(key=key, name=name or "Material")
        self.materials[key] = result
        seen = set()
        queue = [(h, 0) for h, _c, _p in obj.refs]
        while queue:
            handle, depth = queue.pop(0)
            if handle in seen or depth > 6:
                continue
            seen.add(handle)
            node = st.by_handle.get(handle)
            if node is None:
                continue
            guid = st.guid(node.pbase) or ""
            if guid in SHADERS and not result.shader:
                result.shader = SHADERS[guid]
                shader_name = node.params.get(st_name_id(st))
                if isinstance(shader_name, str) and shader_name not in {"Phong", "Constant", "Material"}:
                    result.name = shader_name
            role = COLOURS.get(guid[:8])
            if role and role not in result.colours:
                colour = _colour(st, node)
                if colour is not None:
                    result.colours[role] = colour
            if guid.startswith(CLS_TEXTURE_NODE):
                for text in _strings(st.data, node.body, min(node.end, node.body + 400)):
                    if text.lower().startswith("texture_projection") or text.lower().startswith("uv"):
                        if text not in result.projections:
                            result.projections.append(text)
                        break
            for m in _SOURCE_REF.finditer(st.data, node.body, node.end):
                try:
                    clip, _q = records.cnum(st.data, m.end())
                except (records.Lost, IndexError):
                    continue
                name, paths = self.clips.paths(clip)
                if not paths or all(re.split(r"[\\/]", p)[-1].lower().startswith("noicon") for p in paths):
                    continue   # unset image input: Softimage's placeholder picture
                if name and all(name != n for n, _p in result.images):
                    result.images.append((name, paths))
            for h, _c, _p in node.refs:
                queue.append((h, depth + 1))
        return key


def st_name_id(st):
    cached = getattr(st, "_name_id", None)
    if cached is None:
        cached = next((i for i, g in st.guids.items() if g == P_NAME), -1)
        st._name_id = cached
    return cached if cached >= 0 else None


def attach(sc) -> bool:
    """Fill `material` / `cluster_materials` of 3.x polygon meshes. Returns False when the scene does not
    use the 3.x material layout (nothing found)."""
    from .materials import cluster_elements

    reader = Reader(sc)
    found = False
    storages = {id(st): storage for storage, st in sc.models.items()}
    for so in sc.objects:
        if so.kind != "polymsh" or so.primitive is None:
            continue
        body = getattr(so, "body", None)
        if body is None:
            continue
        st = body[0]
        storage = storages.get(id(st), "")
        clusters = []
        for handle, _c, _p in so.primitive.refs:
            container = st.by_handle.get(handle)
            if container is None or st.guid(container.pbase) != CLS_CONTAINER:
                continue
            for h2, _c2, _p2 in container.refs:
                cluster = st.by_handle.get(h2)
                if cluster is None or st.guid(cluster.pbase) != CLS_POLY_CLUSTER:
                    continue
                material = next(
                    (st.by_handle[h3] for h3, _c3, _p3 in cluster.refs
                     if h3 in st.by_handle and st.guid(st.by_handle[h3].pbase) == CLS_MATERIAL),
                    None,
                )
                if material is None:
                    continue
                elements = cluster_elements(st, cluster) or []
                name = cluster.params.get(st_name_id(st)) or "Cluster"
                clusters.append(ClusterMaterial(name, elements, reader.material(storage, st, material)))
        if clusters:
            found = True
            # The largest cluster's material stands in for the object's own material.
            main = max(clusters, key=lambda c: len(c.polygons))
            so.material = main.material
            so.cluster_materials = [c for c in clusters if c is not main]
    if found:
        sc.legacy_materials = reader.materials
    return found
