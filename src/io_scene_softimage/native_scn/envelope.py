# SPDX-License-Identifier: GPL-3.0-or-later
"""Envelopes (skinning) of native scenes.

Reverse engineered from the Half-Life 2 source scenes (XSI 3.0 - 3.51), see docs/native-format.md:

* The envelope operator (class 470cf890) refers to: the polymsh it deforms, the mesh's global kinematic
  state, the point cluster it applies to (e4dd8e40), the weight map (c0fbfe80), then one pair per deformer:
  the deformer's bind matrix object (12f18b50) followed by the deformer's global kinematic state.
* Bind matrix objects hold 16 compact doubles (records.cdouble): a 4x4 matrix in row-vector convention
  (translation in the last row), the deformer's global transform when the envelope was applied.
* The weight map holds, after a header (deformer count, per-deformer display colours, point count and a
  point-sized block), a run-length encoded deformer x point matrix of float32 weights in percent. Each run
  is a compact number v = (skip << 16) | count followed by `count` float32 values: skip `skip` cells
  (zero weight), then store `count` cells. Rows are deformers in operator order; columns are the cluster's
  points. Every point's weights sum to 100.
"""

from __future__ import annotations

import struct

import numpy as np

from .records import Lost, cdouble, cnum

CLS_ENVELOPE = "470cf890-d3aa-11d0-996c-00a0243f0e60"
CLS_WEIGHTS = "c0fbfe80-e7f4-11d0-996d-00a0243f0e60"
CLS_BIND = "12f18b50-d380-11d0-9225-08003629ea02"
CLS_POINT_CLUSTER = "e4dd8e40-1e1c-11d0-aa2e-00a0243e34c4"


def bind_matrix(d: bytes, obj) -> np.ndarray | None:
    """The 4x4 bind matrix (column-vector convention) of a 12f18b50 object, or None."""
    for start in range(obj.body, min(obj.end, obj.body + 128)):
        try:
            q = start
            values = []
            for _ in range(16):
                if d[q] < 0xF8:
                    raise Lost("not a compact double")
                value, q = cdouble(d, q)
                values.append(value)
        except (Lost, IndexError, struct.error):
            continue
        matrix = np.array(values).reshape(4, 4)
        if abs(matrix[3, 3] - 1.0) < 1e-6 and np.allclose(matrix[:3, 3], 0.0, atol=1e-6):
            return matrix.T
    return None


def decode_weights(d: bytes, obj, deformers: int, points: int) -> np.ndarray | None:
    """deformers x points weight matrix (percent), or None when the layout is not recognized."""
    cells = deformers * points
    candidates = []
    for width_bytes in _cnum_encodings(points):
        p = d.find(width_bytes, obj.body, obj.end)
        while p >= 0:
            after = p + len(width_bytes)
            candidates.extend((after + 4 * points, after))
            p = d.find(width_bytes, p + 1, obj.end)
    for start in candidates:
        matrix = _runs(d, start, obj.end, cells)
        if matrix is not None:
            return matrix.reshape(deformers, points)
    return None


def _runs(d: bytes, p: int, end: int, cells: int) -> np.ndarray | None:
    out = np.zeros(cells, dtype=np.float32)
    cell = 0
    try:
        while cell < cells:
            v, p = cnum(d, p)
            count, skip = v & 0xFFFF, v >> 16
            cell += skip
            if cell + count > cells or p + 4 * count > end:
                return None
            if count:
                out[cell:cell + count] = np.frombuffer(d, dtype="<f4", count=count, offset=p)
                p += 4 * count
                cell += count
    except (Lost, IndexError):
        return None
    if cell != cells or end - p > 64:
        return None
    if not np.isfinite(out).all() or out.min() < -1e-3 or out.max() > 100.0 + 1e-2:
        return None
    return out


def _cnum_encodings(v: int) -> list[bytes]:
    out = []
    if v < 0x10:
        out.append(bytes([0xF0 | v]))
    if v < 0x1000:
        out.append(bytes([0xE0 | (v & 15), v >> 4]))
    if v < 0x100000:
        out.append(bytes([0xD0 | (v & 15), (v >> 4) & 255, v >> 12]))
    out.append(bytes([0xC0 | (v & 15), (v >> 4) & 255, (v >> 12) & 255, (v >> 20) & 255]))
    return out


def _named_owner(obj):
    while obj is not None:
        name = obj.params.get(3)
        if isinstance(name, str) and name:
            return obj
        obj = obj.parent
    return None


def _bmsh_bind_matrices(st, primitive, deformer_count):
    """The target bind matrix and ordered deformer matrices of an early envelope operator."""
    for op in st.objects:
        if not any(handle == primitive.handle for handle, _cls, _pos in op.refs):
            continue
        matrices = []
        for handle, _cls, _pos in op.refs:
            ref = st.by_handle.get(handle)
            if ref is None:
                continue
            matrix = bind_matrix(st.data, ref)
            if matrix is not None:
                matrices.append((ref, matrix))
        target_index = next(
            (
                index for index, (obj, _matrix) in enumerate(matrices)
                if obj.parent is primitive.parent
            ),
            None,
        )
        if target_index is None:
            continue
        deformers = matrices[target_index + 1:target_index + 1 + deformer_count]
        if len(deformers) == deformer_count:
            return matrices[target_index][1], [matrix for _obj, matrix in deformers]
    return None, []


def _attach_bmsh(sc) -> None:
    from .materials import cluster_elements

    for target in sc.objects:
        if target.kind != "polymsh" or target.mesh is None or target.mesh.layout != "BMSH":
            continue
        body = getattr(target, "body", None)
        if body is None:
            continue
        st = body[0]
        group = next(
            (
                obj for obj in st.objects
                if isinstance(obj.params.get(3), str)
                and obj.params[3].casefold() == "for_envelope"
            ),
            None,
        )
        if group is None:
            continue
        deformers = []
        for handle, _cls, _pos in group.refs:
            obj = st.by_handle.get(handle)
            owner = _named_owner(obj) if obj is not None else None
            if owner is not None:
                deformers.append((owner.params[3], owner))
        clusters = {}
        prefix = "envelopselcls"
        for obj in st.objects:
            name = obj.params.get(3)
            if not isinstance(name, str) or not name.casefold().startswith(prefix):
                continue
            suffix = name[len(prefix):]
            if suffix and not suffix.isdigit():
                continue
            index = int(suffix) if suffix else 0
            elements = cluster_elements(st, obj)
            if elements:
                clusters[index] = elements
        if not deformers or set(clusters) != set(range(len(deformers))):
            continue
        mesh_bind, deformer_binds = _bmsh_bind_matrices(st, target.primitive, len(deformers))
        if mesh_bind is not None:
            target.mesh_bind_matrix = mesh_bind
        if len(deformer_binds) == len(deformers):
            scene_objects = {
                (id(so.body[0]), so.handle): so
                for so in sc.objects
                if getattr(so, "body", None) is not None
            }
            for (_name, owner), matrix in zip(deformers, deformer_binds):
                deformer = scene_objects.get((id(st), owner.handle))
                if deformer is not None and deformer.bind_matrix is None:
                    deformer.bind_matrix = matrix
        else:
            sc.warnings.append(
                f"{target.name}: legacy envelope bind matrices could not be decoded"
            )
        used = set()
        envelope = {}
        valid = True
        for index, (name, _owner) in enumerate(deformers):
            vertices = clusters[index]
            current = set(vertices)
            if (
                len(current) != len(vertices)
                or used.intersection(current)
                or any(vertex < 0 or vertex >= len(target.mesh.points) for vertex in vertices)
            ):
                valid = False
                break
            used.update(current)
            envelope[name] = (
                np.asarray(vertices, dtype=np.int64),
                np.ones(len(vertices), dtype=np.float32),
            )
        if valid:
            target.envelope = envelope


def attach(sc) -> None:
    """Fill `envelope` ({deformer name: (vertices, weights 0..1)}) on enveloped polymsh objects and
    `bind_matrix` (Softimage global 4x4) on their deformers."""
    from .materials import cluster_elements

    by_primitive = {}
    kine_owner = getattr(sc, "kine_global_owner", {})
    for so in sc.objects:
        if so.kind == "polymsh" and so.primitive is not None and so.mesh is not None:
            body = getattr(so, "body", None)
            if body is not None:
                by_primitive[(id(body[0]), so.primitive.handle)] = so
    for st in sc.models.values():
        for op in st.objects:
            if st.guid(op.pbase) != CLS_ENVELOPE or not op.refs:
                continue
            refs = [st.by_handle.get(h) for h, _c, _p in op.refs]
            target = next((by_primitive.get((id(st), r.handle)) for r in refs if r is not None
                           and (id(st), r.handle) in by_primitive), None)
            if target is None:
                continue
            weights_obj = next((r for r in refs if r is not None and st.guid(r.pbase) == CLS_WEIGHTS), None)
            cluster = next((r for r in refs if r is not None and st.guid(r.pbase) == CLS_POINT_CLUSTER), None)
            pairs = []
            for first, second in zip(refs, refs[1:]):
                if (first is not None and second is not None and st.guid(first.pbase) == CLS_BIND
                        and second.handle in kine_owner.get(id(st), {})):
                    pairs.append((first, kine_owner[id(st)][second.handle]))
            if weights_obj is None or not pairs:
                continue
            npts = len(target.mesh.points)
            columns = cluster_elements(st, cluster) if cluster is not None else None
            ncols = len(columns) if columns else npts
            matrix = decode_weights(st.data, weights_obj, len(pairs), ncols)
            if matrix is None and ncols != npts:
                columns, ncols = None, npts
                matrix = decode_weights(st.data, weights_obj, len(pairs), npts)
            if matrix is None:
                sc.warnings.append("%s: envelope weights could not be decoded" % target.name)
                continue
            vertex_of_column = np.asarray(columns if columns else range(ncols), dtype=np.int64)
            envelope = {}
            for row, (bind_obj, deformer) in enumerate(pairs):
                nonzero = np.flatnonzero(matrix[row] > 1e-6)
                if len(nonzero):
                    envelope[deformer.name] = (vertex_of_column[nonzero], matrix[row, nonzero] / 100.0)
                bind = bind_matrix(st.data, bind_obj)
                if bind is not None and getattr(deformer, "bind_matrix", None) is None:
                    deformer.bind_matrix = bind
            target.envelope = envelope
    _attach_bmsh(sc)
