# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alex Doss
"""UV sets (texture projections) of frozen polygon meshes in a Softimage .scn.

Found from corpus cases mat_uv / mat_uv2 and checked against Softimage's per-corner UVs
(tools/sicapture/softimage/dump_materials_truth.py). See docs/research/scn-format.md, "UVs".

* The polymsh refers to its cluster container (class 60f7ada0), which refers to the sample cluster
  (`Texture_Coordinates_AUTO`, class 600e50f2) and to each texture projection property (class 6630d811), named
  like the UV set (`Texture_Projection`). In older files the sample cluster refers to the properties.
* In the property: the distinct UV values as raw f32 (u, v) pairs, `<marker> <u32 size> fc <pairs>`, and an index
  per polygon node, `<marker> <u32 size> <cnum nodes> <width u8> 00 fc <nodes x width bytes>` (`fa` in place of
  `fc` in some files, same layout). The number of pairs
  is the largest index + 1.
"""
import re
import struct
from collections import namedtuple

from . import records

CLS_CONTAINER = "60f7ada0-7e12-11d0-9960-00a0243f0e60"
CLS_UVPROP = "6630d811-0699-11d1-b723-00aa0068bf56"
CLS_SAMPLES = "600e50f2-52fc-11d2-a924-00a024190119"   # sample cluster (Texture_Coordinates_AUTO)

UVSet = namedtuple("UVSet", "name corners covered")
# corners: (u, v) per polygon corner, in Mesh.polygons order; covered: per corner, False where the
# projection has no value for that node (XSI 3.x writes 0xffff..., e.g. after merging meshes).
MISSING = (0.0, 0.0)


def _values(d, lo, hi):
    """Candidate UV value blocks in [lo, hi): sized blocks `<marker> <u32 size> <cnum count> <count x (u, v) f32>`
    with finite values. Yields (pairs, end of the pairs)."""
    p = lo
    while p + 6 < hi:
        try:
            size = struct.unpack_from("<I", d, p + 1)[0]
            if 0 < size <= hi - p - 5:
                count, q = records.cnum(d, p + 5)
                if count and q + 8 * count <= p + 5 + size:
                    flat = struct.unpack_from("<%df" % (2 * count), d, q)
                    if all(abs(x) < 1e6 for x in flat):   # also rejects NaN
                        yield [(flat[2 * i], flat[2 * i + 1]) for i in range(count)], q + 8 * count
        except (records.Lost, IndexError, struct.error):
            pass
        p += 1


def _indices(d, lo, hi, nnodes, count):
    """Node -> value index: `<marker> <u32 size> <cnum nodes> 01 00 <cnum count> <nodes entries>` (2015) or
    `<marker> <u32 size> <cnum nodes> <cnum count> <nodes entries>` (XSI 3.x), entries of 1, 2 or 4 bytes as
    the value count needs. Entries >= count (all bits set) mean "no value"."""
    from .mesh import _cnum_variants
    w, fmt = (1, "B") if count < 0x100 else (2, "H") if count < 0x10000 else (4, "I")
    # Jump to occurrences of the node count (in any compact width) instead of testing every byte: large
    # scenes have thousands of candidate value blocks, and a byte-by-byte scan after each was quadratic.
    candidates = sorted({
        m.start() - 5
        for encoding in _cnum_variants(nnodes)
        for m in re.finditer(re.escape(encoding), d[lo:hi])
        if m.start() >= 5
    })
    for offset in candidates:
        p = lo + offset
        try:
            size = struct.unpack_from("<I", d, p + 1)[0]
            end = p + 5 + size
            if end > hi:
                continue
            n, q = records.cnum(d, p + 5)
            if n != nnodes:
                continue
            starts = []
            if q + 1 < hi and d[q + 1] == 0:
                starts.append(q + 2)
            starts.append(q)
            for start in starts:
                rng, r = records.cnum(d, start)
                for width, form in ((w, fmt), (1, "B"), (2, "H"), (4, "I")):
                    if rng == count and r + n * width == end:
                        return list(struct.unpack_from("<%d%s" % (n, form), d, r))
        except (records.Lost, IndexError, struct.error):
            continue
    return None


def read(st, polymsh, mesh):
    """UV sets of a frozen mesh: [UVSet], in the order Softimage lists the projections."""
    nnodes = sum(len(p) for p in mesh.polygons)
    nodes = getattr(mesh, "nodes", None)
    if nodes is None:                            # nodes in polygon order (2015 scenes)
        nodes, n = [], 0
        for poly in mesh.polygons:
            nodes.append(list(range(n, n + len(poly))))
            n += len(poly)
    out = []
    for h, _c, _p in polymsh.refs:
        cont = st.by_handle.get(h)
        if cont is None or st.guid(cont.pbase) != CLS_CONTAINER:
            continue
        props, seen = [], set()
        owner = {}                               # UV property handle -> its sample cluster
        for h2, _c2, _p2 in cont.refs:
            ref = st.by_handle.get(h2)
            if ref is not None and st.guid(ref.pbase) == CLS_SAMPLES:
                for h3, _c3, _p3 in ref.refs:
                    owner.setdefault(h3, ref)
        for h2, _c2, _p2 in cont.refs:       # on the container (2015), or on the sample cluster (older files)
            ref = st.by_handle.get(h2)
            if ref is None:
                continue
            g = st.guid(ref.pbase)
            cands = [ref] if g == CLS_UVPROP else (
                [st.by_handle.get(h3) for h3, _c3, _p3 in ref.refs] if g == CLS_SAMPLES else [])
            for c in cands:
                if c is not None and st.guid(c.pbase) == CLS_UVPROP and c.handle not in seen:
                    seen.add(c.handle)
                    props.append(c)
        for prop in sorted(props, key=lambda o: o.handle):     # creation order, as Softimage lists them
            name = next((v for v in prop.params.values() if isinstance(v, str)), "UV")
            for vals, at in _values(st.data, prop.body, prop.end):
                idx = _indices(st.data, at, prop.end, nnodes, len(vals))
                if idx is None and len(vals) == nnodes:
                    idx = range(nnodes)               # one value per node, no index array (older files)
                if idx is None:
                    continue
                idx = _by_node(st, owner.get(prop.handle), idx, nnodes)
                count = len(vals)
                order = [n for poly in nodes for n in poly]
                if max(order, default=0) >= len(idx):
                    continue
                covered = [idx[n] < count for n in order]
                if not any(covered):
                    continue
                corners = [vals[idx[n]] if ok else MISSING for n, ok in zip(order, covered)]
                out.append(UVSet(name, corners, covered))
                break
    return out


def _by_node(st, cluster, idx, nnodes):
    """Index entries follow the element order of the sample cluster that owns the projection (XSI 3.x
    clusters list the nodes in their own order). Returns the indices re-ordered by node."""
    if cluster is None:
        return idx
    from .materials import cluster_elements
    elements = cluster_elements(st, cluster)
    if not elements or len(elements) != len(idx) or max(elements) >= nnodes:
        return idx
    if all(e == k for k, e in enumerate(elements)):
        return idx
    by_node = [0xFFFFFFFF] * nnodes
    for k, node in enumerate(elements):
        by_node[node] = idx[k]
    return by_node


def merged(sets):
    """One UV set combining partial projections (first projection that covers a corner wins), or None
    when every set already covers all corners."""
    if len(sets) < 2 or all(all(s.covered) for s in sets):
        return None
    corners = list(sets[0].corners)
    covered = list(sets[0].covered)
    for uv in sets[1:]:
        for i, ok in enumerate(uv.covered):
            if ok and not covered[i]:
                corners[i] = uv.corners[i]
                covered[i] = True
    return UVSet("UVMap", corners, covered)
