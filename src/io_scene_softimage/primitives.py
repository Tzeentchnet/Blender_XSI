# SPDX-License-Identifier: GPL-3.0-or-later
"""Polygon versions of Softimage's live primitives (no bpy), in Softimage axes (Y up).

Used when a native scene stores a primitive as generator parameters instead of frozen geometry and the
exact Softimage point order is not known (see native_scn/rebuild.py for the primitives whose order is
reproduced exactly). Defaults are Softimage's own (radius 4, length 8, 8 subdivisions).
"""

from __future__ import annotations

import math


def generate(kind: str, params: dict) -> tuple[list[tuple[float, float, float]], list[list[int]]] | None:
    p = params or {}
    if kind == "cube":
        h = float(p.get("length", 8.0)) / 2
        points = [(x, y, z) for z in (-h, h) for y in (-h, h) for x in (-h, h)]
        faces = [[0, 2, 3, 1], [0, 1, 5, 4], [0, 4, 6, 2], [1, 3, 7, 5], [2, 6, 7, 3], [4, 5, 7, 6]]
        return points, faces
    if kind == "grid":
        u, v = max(1, int(p.get("subdivu", 8))), max(1, int(p.get("subdivv", 8)))
        lu, lv = float(p.get("ulength", 8.0)), float(p.get("vlength", 8.0))
        points = [(-lu / 2 + lu * i / u, 0.0, -lv / 2 + lv * j / v) for i in range(u + 1) for j in range(v + 1)]
        faces = [[i * (v + 1) + j, i * (v + 1) + j + 1, (i + 1) * (v + 1) + j + 1, (i + 1) * (v + 1) + j]
                 for i in range(u) for j in range(v)]
        return points, faces
    if kind == "sphere":
        return _revolve(float(p.get("radius", 4.0)), max(3, int(p.get("subdivu", 8))),
                        max(2, int(p.get("subdivv", 8))))
    if kind in {"cylinder", "cone"}:
        radius = float(p.get("radius", 4.0))
        height = float(p.get("height", 8.0))
        segments = max(3, int(p.get("subdivu", 8)))
        top = radius if kind == "cylinder" else 0.0
        return _tube(radius, top, height, segments)
    if kind == "disc":
        outer = float(p.get("outerradius", 4.0))
        inner = float(p.get("innerradius", 0.0))
        segments = max(3, int(p.get("subdivu", 8)))
        ring = [(outer * math.cos(2 * math.pi * k / segments), 0.0, -outer * math.sin(2 * math.pi * k / segments))
                for k in range(segments)]
        if inner > 0:
            hole = [(inner * x / outer, 0.0, inner * z / outer) for x, _y, z in ring]
            faces = [[k, (k + 1) % segments, segments + (k + 1) % segments, segments + k] for k in range(segments)]
            return ring + hole, faces
        return ring, [list(range(segments))]
    if kind == "torus":
        radius = float(p.get("radius", 4.0))
        section = float(p.get("sectionradius", 1.0))
        u, v = max(3, int(p.get("subdivu", 8))), max(3, int(p.get("subdivv", 8)))
        points = []
        for i in range(u):
            a = 2 * math.pi * i / u
            for j in range(v):
                b = 2 * math.pi * j / v
                r = radius + section * math.cos(b)
                points.append((r * math.cos(a), section * math.sin(b), -r * math.sin(a)))
        faces = [[i * v + j, i * v + (j + 1) % v, ((i + 1) % u) * v + (j + 1) % v, ((i + 1) % u) * v + j]
                 for i in range(u) for j in range(v)]
        return points, faces
    if kind in {"tetrahedron", "octahedron", "icosahedron", "dodecahedron"}:
        return _platonic(kind, float(p.get("radius", 4.0)))
    return None


def _revolve(radius, u, v):
    points = [(0.0, -radius, 0.0), (0.0, radius, 0.0)]
    for i in range(u):
        a = 2 * math.pi * i / u
        for j in range(1, v):
            t = math.pi * j / v
            rho = radius * math.sin(t)
            points.append((-rho * math.cos(a), -radius * math.cos(t), rho * math.sin(a)))
    rings = v - 1

    def at(i, j):
        return 2 + (i % u) * rings + j

    faces = []
    for i in range(u):
        faces.append([0, at(i + 1, 0), at(i, 0)])
        for j in range(rings - 1):
            faces.append([at(i, j), at(i + 1, j), at(i + 1, j + 1), at(i, j + 1)])
        faces.append([at(i, rings - 1), at(i + 1, rings - 1), 1])
    return points, faces


def _tube(bottom, top, height, segments):
    ring = [(math.cos(2 * math.pi * k / segments), -math.sin(2 * math.pi * k / segments))
            for k in range(segments)]
    points = [(bottom * c, -height / 2, bottom * s) for c, s in ring]
    bottom_cap = list(reversed(range(segments)))
    if top <= 0:  # cone: sides meet at the apex
        points.append((0.0, height / 2, 0.0))
        faces = [[k, (k + 1) % segments, segments] for k in range(segments)]
        return points, faces + [bottom_cap]
    points += [(top * c, height / 2, top * s) for c, s in ring]
    faces = [[k, (k + 1) % segments, segments + (k + 1) % segments, segments + k] for k in range(segments)]
    return points, faces + [bottom_cap, list(range(segments, 2 * segments))]


def _platonic(kind, radius):
    phi = (1 + 5 ** 0.5) / 2
    if kind == "tetrahedron":
        points = [(1, 1, 1), (1, -1, -1), (-1, 1, -1), (-1, -1, 1)]
        faces = [[0, 1, 2], [0, 3, 1], [0, 2, 3], [1, 3, 2]]
    elif kind == "octahedron":
        points = [(1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)]
        faces = [[0, 2, 4], [2, 1, 4], [1, 3, 4], [3, 0, 4], [2, 0, 5], [1, 2, 5], [3, 1, 5], [0, 3, 5]]
    elif kind == "icosahedron":
        points = [(-1, phi, 0), (1, phi, 0), (-1, -phi, 0), (1, -phi, 0), (0, -1, phi), (0, 1, phi),
                  (0, -1, -phi), (0, 1, -phi), (phi, 0, -1), (phi, 0, 1), (-phi, 0, -1), (-phi, 0, 1)]
        faces = [[0, 11, 5], [0, 5, 1], [0, 1, 7], [0, 7, 10], [0, 10, 11], [1, 5, 9], [5, 11, 4],
                 [11, 10, 2], [10, 7, 6], [7, 1, 8], [3, 9, 4], [3, 4, 2], [3, 2, 6], [3, 6, 8], [3, 8, 9],
                 [4, 9, 5], [2, 4, 11], [6, 2, 10], [8, 6, 7], [9, 8, 1]]
    else:
        a, b = 1 / phi, phi
        points = [(1, 1, 1), (1, 1, -1), (1, -1, 1), (1, -1, -1), (-1, 1, 1), (-1, 1, -1), (-1, -1, 1),
                  (-1, -1, -1), (0, a, b), (0, a, -b), (0, -a, b), (0, -a, -b), (a, b, 0), (a, -b, 0),
                  (-a, b, 0), (-a, -b, 0), (b, 0, a), (b, 0, -a), (-b, 0, a), (-b, 0, -a)]
        faces = [[0, 8, 10, 2, 16], [0, 16, 17, 1, 12], [0, 12, 14, 4, 8], [1, 17, 3, 11, 9],
                 [1, 9, 5, 14, 12], [2, 10, 6, 15, 13], [2, 13, 3, 17, 16], [3, 13, 15, 7, 11],
                 [4, 14, 5, 19, 18], [4, 18, 6, 10, 8], [5, 9, 11, 7, 19], [6, 18, 19, 7, 15]]
    scale = radius / math.sqrt(sum(c * c for c in points[0]))
    return [tuple(c * scale for c in point) for point in points], faces
