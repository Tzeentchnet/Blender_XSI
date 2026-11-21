# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Alex Doss
"""Polygon mesh geometry from a polymsh primitive object.

After the polymsh parameter records comes a geometry blob  01 0d <u32> 0e <u32>  that starts with four
compact ints (points, edges + 1, polygons, polygon nodes), followed by arrays:

* integer arrays   f0 f1 <range> <width> f1 <n> <width> <n>  then n elements of `width` bytes.
  The first and last three elements are framing; the values sit in between. `range` is the value
  bound (point count for vertex indices, node count for polygon starts).
* double arrays    f0 f1 <count> <element size> <count>  then raw little-endian doubles
  (points: element size 24 = x, y, z).

Only frozen geometry is stored like this; it is what Softimage keeps for every polygon mesh.
"""
import collections
import itertools
import math
import re
import struct

from .records import Lost, cdouble, cnum


class MeshError(Exception):
    pass


class Mesh:
    __slots__ = ("points", "polygons", "counts", "edges_ok", "nodes", "layout")

    def __init__(self, points, polygons, counts, edges_ok=None, nodes=None, layout="2015"):
        self.points = points        # [(x, y, z), ...]
        self.polygons = polygons    # [[i0, i1, ...], ...]
        self.counts = counts        # header counts: points, edges+1, polygons, nodes
        # True when the polygons give exactly the stored edge count (a cheap topology self-check);
        # False flags geometry that is probably damaged; None = not checked.
        self.edges_ok = edges_ok
        # Polygon node indices per polygon when nodes are not numbered in polygon order (XSI 3.x).
        self.nodes = nodes
        self.layout = layout

    def __repr__(self):
        return "<Mesh %d points, %d polygons>" % (len(self.points), len(self.polygons))


_ARR = re.compile(rb"\xf0\xf1", re.S)


_BLOB = re.compile(rb"\x01[\x02-\x3f].{4}[\x02-\x3f]", re.S)
_BMSH_TAG = b"HSMB0010"
_MSHI_TAG = b"IHSM2010"


def _blob(d, obj):
    """Find '01 <A> <u32 n+5> <B> <u32 n>' inside the object; return (start, end) of the inner payload.
    A and B are block markers, numbered per stream (0d/0e in most 2015 scenes, 07/08 and others in older
    ones), so the blob is recognised by its nesting: the outer block holds exactly the inner one."""
    p = obj.body
    while True:
        m = _BLOB.search(d, p, obj.end)
        if m is None:
            raise MeshError("no geometry blob")
        p = m.start()
        s1 = struct.unpack_from("<I", d, p + 2)[0]
        if p + 11 <= obj.end and p + 6 + s1 <= obj.end:
            s2 = struct.unpack_from("<I", d, p + 7)[0]
            if s1 == s2 + 5 and s2 > 4:
                return p + 11, p + 11 + s2
        p += 1


def _int_array(d, p, end):
    """Integer array at p (pointing at f0 f1). Returns (range, values, next) or None."""
    try:
        rng, q = cnum(d, p + 2)
        width, q = cnum(d, q)
        if width not in (1, 2, 4) or d[q] != 0xF1:
            return None
        n, q = cnum(d, q + 1)
        w2, q = cnum(d, q)
        n2, q = cnum(d, q)
    except (Lost, IndexError):
        return None
    if w2 != width or n2 != n or n < 6 or q + n * width > end:
        return None
    fmt = {1: "B", 2: "H", 4: "I"}[width]
    vals = list(struct.unpack_from("<%d%s" % (n, fmt), d, q)[3:-3])
    q += n * width
    # Every integer array is followed by an override table  f0 f1 <count> fc <count>  of 12-byte entries
    # (u32 value, u32 extra, u32 position): the slot at `position` holds an index into this table, the
    # real value is in the entry. Empty in most scenes; scenes saved by older versions use it, e.g. for
    # a vertex shared by many polygons. (`extra` is not understood; it counts upwards from 15.)
    # An empty table is just  f0 f1 f0 fc  (no second count).
    if d[q:q + 2] == b"\xf0\xf1":
        try:
            cnt, r = cnum(d, q + 2)
            es, r = cnum(d, r)
            cnt2 = 0
            if cnt:
                cnt2, r = cnum(d, r)
        except (Lost, IndexError):
            cnt = es = cnt2 = None
        if es == 12 and cnt == cnt2 and r + 12 * cnt <= end:
            for i in range(cnt):
                v, _extra, pos = struct.unpack_from("<3I", d, r + 12 * i)
                if pos < len(vals):
                    vals[pos] = v
            q = r + 12 * cnt
    return rng, vals, q


def _double_array(d, p, end, elem):
    """Double array 'f0 f1 <count> <elem> <count>' at p. Returns (count, flat doubles, next) or None."""
    try:
        cnt, q = cnum(d, p + 2)
        es, q = cnum(d, q)
        cnt2, q = cnum(d, q)
    except (Lost, IndexError):
        return None
    if es != elem or cnt2 != cnt or q + cnt * es > end:
        return None
    k = es // 8
    return cnt, struct.unpack_from("<%dd" % (cnt * k), d, q), q + cnt * es


def read_polymsh(d, obj):
    try:
        lo, hi = _blob(d, obj)
    except MeshError as blob_error:
        try:
            return read_polymsh_bmsh(d, obj)
        except MeshError as bmsh_error:
            raise MeshError("%s; BMSH layout: %s" % (blob_error, bmsh_error))
    try:
        npts, q = cnum(d, lo)
        _nedges1, q = cnum(d, q)
        npolys, q = cnum(d, q)
        nnodes, q = cnum(d, q)
    except (Lost, IndexError) as e:
        raise MeshError("bad geometry header: %s" % e)
    indices = starts = points = None
    edge_polys = []   # edge -> polygon arrays (two: one per side), range = polygons, length = edges + 1
    edge_sides = []   # for each of them: the edge's index inside that polygon (from the packed array before it)
    edges_end = starts_at = starts_end = None
    packed = None
    p = lo
    while True:
        m = _ARR.search(d, p, hi)
        if m is None:
            break
        p = m.start()
        if points is None and indices is not None:
            arr = _double_array(d, p, hi, 24)
            if arr and arr[0] == npts:
                flat = arr[1]
                points = [tuple(flat[i:i + 3]) for i in range(0, len(flat), 3)]
                p = arr[2]
                continue
        pk = _nibble_array(d, p, hi, npolys) if indices is not None and len(edge_polys) < 2 else None
        if pk is not None:
            packed, p = pk
            continue
        arr = _int_array(d, p, hi)
        if not arr:
            p += 2
            continue
        start, (rng, vals, p) = p, arr
        if indices is None and rng == npts and len(vals) == nnodes:
            indices = vals
        elif rng == npolys and len(vals) == _nedges1 and len(edge_polys) < 2:
            edge_polys.append(vals)
            edge_sides.append(packed)
            edges_end = p
        elif starts is None and rng == nnodes and len(vals) == npolys:
            starts, starts_at, starts_end = vals, start, p
        if indices is not None and starts is not None and points is not None:
            break
    if indices is None or points is None:
        try:
            return read_polymsh_v3(d, lo, hi)
        except MeshError as legacy_error:
            raise MeshError("arrays not found (points=%s indices=%s); XSI 3.x layout: %s" % (
                points is not None, indices is not None, legacy_error))
    sizes = _polygon_sizes(edge_polys, npolys, nnodes)
    if sizes is None:
        if npolys and nnodes % npolys == 0:          # uniform polygons
            sizes = [nnodes // npolys] * npolys
        else:
            raise MeshError("polygon sizes not found")
    if starts is None:                                # no start array: the polygons follow each other
        starts = [0] + list(itertools.accumulate(sizes))[:-1]
    flags = None
    if edges_end is not None and starts_at is not None:
        flags = _polygon_flags(d, edges_end, starts_at, npolys, nnodes)
    chains = {}
    if flags is not None and starts_end is not None:
        chained = {i for i, f in enumerate(flags) if not f & 8}
        if chained:
            chains = _polygon_chains(d, starts_end, hi, nnodes, chained, sizes)
    polygons = []
    for i in range(npolys):
        if flags is not None and not flags[i] & 8:
            slots = chains.get(i)
            if slots is None:
                raise MeshError("node chain of polygon %d not found" % i)
            polygons.append([indices[s] for s in slots])
        else:
            polygons.append(indices[starts[i]:starts[i] + sizes[i]])
    ok = _edges_agree(polygons, edge_polys, edge_sides, npolys)
    if ok is None and _nedges1:
        ok = _edge_count(polygons, npts) == _nedges1 - 1
    return Mesh(points, polygons, (npts, _nedges1, npolys, nnodes), ok)


# ---------------------------------------------------------------- XSI 1.x BMSH layout -----------------
# Models saved by early Softimage XSI releases keep frozen geometry in a BMSH0010 / CMSH2010 chunk.
# The four topology tables use compact signed as well as unsigned integers. A polygon node stores its
# vertex, an oriented edge id, and a reverse node id; the edge and polygon tables that follow the point
# array make the topology self-validating.

def _legacy_num(d, p, end):
    """Early-stream compact integer, including the signed A/9/8/7 width tags."""
    if p >= end:
        raise Lost("compact int past end")
    high = d[p] >> 4
    if high in (0xF, 0xE, 0xD, 0xC):
        width, signed = 0xF - high, False
    elif high in (0xA, 0x9, 0x8, 0x7):
        width, signed = 0xA - high, True
    else:
        raise Lost("compact int tag %02x at %d" % (d[p], p))
    if p + width >= end:
        raise Lost("truncated compact int at %d" % p)
    value = d[p] & 0xF
    for i in range(width):
        value |= d[p + 1 + i] << (4 + 8 * i)
    bits = 4 + 8 * width
    if signed and value & (1 << (bits - 1)):
        value -= 1 << bits
    return value, p + 1 + width


def _legacy_nums(d, p, end, count):
    values = []
    for _ in range(count):
        value, p = _legacy_num(d, p, end)
        values.append(value)
    return values, p


def read_polymsh_bmsh(d, obj):
    """Decode an early BMSH0010 frozen polygon mesh."""
    tag = d.find(_BMSH_TAG, obj.body, obj.end)
    if tag < 0 or tag + 12 > obj.end:
        raise MeshError("no BMSH0010 chunk")
    bmsh_end = tag + 12 + struct.unpack_from("<I", d, tag + 8)[0]
    if bmsh_end > obj.end:
        raise MeshError("truncated BMSH0010 chunk")
    info = d.find(_MSHI_TAG, tag + 12, bmsh_end)
    if info < 0 or info + 12 > bmsh_end:
        raise MeshError("no MSHI2010 table")
    p = info + 12 + struct.unpack_from("<I", d, info + 8)[0]
    if p > bmsh_end:
        raise MeshError("truncated MSHI2010 table")

    try:
        (nnodes, node_capacity, _node_stride), p = _legacy_nums(d, p, bmsh_end, 3)
        if nnodes < 3 or node_capacity < nnodes or nnodes > (bmsh_end - p) // 3:
            raise MeshError("bad polygon-node header")
        node_vertex = [0] * nnodes
        node_edge = [0] * nnodes
        node_ids = [0] * nnodes
        for i in range(nnodes):
            (node_vertex[i], node_edge[i], node_ids[i]), p = _legacy_nums(d, p, bmsh_end, 3)
        if set(node_ids) != set(range(nnodes)):
            raise MeshError("polygon-node ids are not a permutation")

        (npts, point_capacity, _point_stride), p = _legacy_nums(d, p, bmsh_end, 3)
        if npts < 3 or point_capacity < npts or any(v < 0 or v >= npts for v in node_vertex):
            raise MeshError("bad point header or polygon-node vertex")
        flat = [0.0] * (3 * npts)
        for i in range(3 * npts):
            flat[i], p = cdouble(d, p)
        if not all(math.isfinite(value) for value in flat):
            raise MeshError("non-finite point coordinate")
        points = [tuple(flat[i:i + 3]) for i in range(0, len(flat), 3)]

        _point_metadata, p = _legacy_nums(d, p, bmsh_end, 12)
        (nedges1, edge_capacity, _edge_stride), p = _legacy_nums(d, p, bmsh_end, 3)
        expected_edges1 = max((abs(edge) for edge in node_edge), default=0) + 1
        if nedges1 != expected_edges1 or edge_capacity < nedges1:
            raise MeshError("bad edge header")
        edges = [None] * nedges1
        for i in range(nedges1):
            values, p = _legacy_nums(d, p, bmsh_end, 4)
            edges[i] = values

        (npolys, polygon_capacity, _polygon_stride), p = _legacy_nums(d, p, bmsh_end, 3)
        if npolys < 1 or polygon_capacity < npolys or npolys > nnodes // 3:
            raise MeshError("bad polygon header")
        node_polygons = []
        polygons = []
        total = 0
        for _ in range(npolys):
            (size, _flags1, _flags2), p = _legacy_nums(d, p, bmsh_end, 3)
            if size < 3 or total + size > nnodes:
                raise MeshError("bad polygon size")
            node_polygon, p = _legacy_nums(d, p, bmsh_end, size)
            if any(node < 0 or node >= nnodes for node in node_polygon):
                raise MeshError("polygon-node index out of range")
            node_polygons.append(node_polygon)
            polygons.append([node_vertex[node] for node in node_polygon])
            total += size
    except (Lost, IndexError, struct.error) as error:
        raise MeshError("truncated BMSH geometry: %s" % error)

    used_nodes = [node for polygon in node_polygons for node in polygon]
    if total != nnodes or set(used_nodes) != set(range(nnodes)):
        raise MeshError("polygons do not use every polygon node once")
    used_edges = {abs(edge) for edge in node_edge}
    if used_edges != set(range(1, nedges1)):
        raise MeshError("polygon nodes do not use every edge")
    for polygon, node_polygon in zip(polygons, node_polygons):
        for i, node in enumerate(node_polygon):
            edge_id = node_edge[node]
            edge = edges[abs(edge_id)]
            current, following = (edge[0], edge[1]) if edge_id > 0 else (edge[1], edge[0])
            if current != polygon[i] or following != polygon[(i + 1) % len(polygon)]:
                raise MeshError("polygon-node edge cycle does not close")
    return Mesh(points, polygons, (npts, nedges1, npolys, nnodes), True,
                nodes=node_polygons, layout="BMSH")


# ---------------------------------------------------------------- XSI 3.x layout ----------------------
# Scenes saved by XSI 2.x - 4.x serialize the geometry sequentially with compact numbers instead of the
# f0 f1 arrays (reverse engineered from the Half-Life 2 source scenes, XSI 3.0 - 3.51):
#
#   <npts> <nedges + 1> <npolys> <nnodes>          blob header (as in later versions)
#   ...                                            vertex / edge flag runs, crease lists (not decoded)
#   <npts> <nnodes> <x>                            node table header (preceded by `f1 <npts> <k>` and k
#                                                  (cnum, f32) pairs, apparently vertex creases)
#   nnodes x (<vertex cnum> <slot byte>)           node -> vertex; slot = the node's rank around its vertex
#   <npts> <capacity> <x>                          point header
#   3 * npts compact doubles                       point positions (records.cdouble)
#   <npolys> <nedges + 1> ...                      edge table: (polygon cnum, side byte) pairs, not decoded
#   <nnodes> <npolys> <k values>                   polygon list header; k varies (seen 2 - 36 values)
#   npolys x (<size> <size node cnums>)            polygons as node indices
#
# Compact numbers are not always written at their shortest width (3486 can be `de d9 00`), so values are
# searched in every width. The decode is validated by its own invariants: node vertices < npts, the point
# header follows the node table, and the polygon list uses each node exactly once.

def _cnum_variants(v):
    out = []
    if v < 0x10:
        out.append(bytes([0xF0 | v]))
    if v < 0x1000:
        out.append(bytes([0xE0 | (v & 15), v >> 4]))
    if v < 0x100000:
        out.append(bytes([0xD0 | (v & 15), (v >> 4) & 255, v >> 12]))
    out.append(bytes([0xC0 | (v & 15), (v >> 4) & 255, (v >> 12) & 255, (v >> 20) & 255]))
    return out


def _sequence_re(*values):
    return re.compile(b"|".join(
        re.escape(b"".join(combo))
        for combo in itertools.product(*[_cnum_variants(v) for v in values])
    ), re.S)


def read_polymsh_v3(d, lo, hi):
    from .records import cdouble
    try:
        npts, q = cnum(d, lo)
        nedges1, q = cnum(d, q)
        npolys, q = cnum(d, q)
        nnodes, q = cnum(d, q)
    except (Lost, IndexError) as e:
        raise MeshError("bad geometry header: %s" % e)
    if npts == 0 or npolys == 0 or nnodes == 0:
        raise MeshError("empty mesh")
    # node table
    found = None
    for m in _sequence_re(npts, nnodes).finditer(d, lo + 1, hi):
        q = m.end()
        try:
            _extra, q = cnum(d, q)
            vertex = [0] * nnodes
            for i in range(nnodes):
                v, q = cnum(d, q)
                if v >= npts:
                    raise MeshError("node vertex out of range")
                vertex[i] = v
                q += 1
            n2, q = cnum(d, q)
            if n2 != npts:
                continue
            found = (vertex, q)
            break
        except (Lost, IndexError, MeshError):
            continue
    if found is None:
        raise MeshError("node table not found")
    vertex, q = found
    try:
        _capacity, q = cnum(d, q)
        _x, q = cnum(d, q)
        flat = [0.0] * (3 * npts)
        for i in range(3 * npts):
            flat[i], q = cdouble(d, q)
    except (Lost, IndexError) as e:
        raise MeshError("points not decoded: %s" % e)
    points = [tuple(flat[i:i + 3]) for i in range(0, len(flat), 3)]
    points_end = q
    # polygon list
    for m in _sequence_re(nnodes, npolys).finditer(d, points_end, hi):
        for skip in range(0, 48):
            q = m.end()
            try:
                for _ in range(skip):
                    _v, q = cnum(d, q)
                polys = []
                total = 0
                for _ in range(npolys):
                    k, q = cnum(d, q)
                    if k < 3 or total + k > nnodes:
                        raise MeshError("bad polygon size")
                    poly = [0] * k
                    for j in range(k):
                        poly[j], q = cnum(d, q)
                    total += k
                    polys.append(poly)
            except (Lost, IndexError, MeshError):
                continue
            if total != nnodes:
                continue
            used = [v for poly in polys for v in poly]
            if max(used) >= nnodes or len(set(used)) != nnodes:
                continue
            polygons = [[vertex[n] for n in poly] for poly in polys]
            # The 3.x edge count is only a hint: mismatches are reported as "not checked".
            ok = True if nedges1 and _edge_count(polygons, npts) == nedges1 - 1 else None
            return Mesh(points, polygons, (npts, nedges1, npolys, nnodes), ok, nodes=polys, layout="3.x")
    raise MeshError("polygon list not decoded")


def _nibble_array(d, p, hi, rng_want):
    """f0 f1 <rng> f1 <n> f1 <n> <n bytes>, 4-bit values, low nibble first (the per-edge index-in-polygon
    arrays before each edge -> polygon array). Returns (values, next) or None."""
    try:
        rng, q = cnum(d, p + 2)
        if rng != rng_want or d[q] != 0xF1:
            return None
        n, q = cnum(d, q + 1)
        if d[q] != 0xF1:
            return None
        n2, q = cnum(d, q + 1)
    except (Lost, IndexError):
        return None
    if n2 != n or q + n > hi or d[q + n:q + n + 2] != b"\xf0\xf1":
        return None
    vals = []
    for b in d[q:q + n]:
        vals += (b & 15, b >> 4)
    return vals, q + n


def _edges_agree(polygons, edge_polys, edge_sides, npolys):
    """Self-check: every edge table entry names, on each side, a polygon and the edge's index inside it
    (edge k runs from corner k to k + 1). Where both sides are filled in, both must give the same pair of
    points. After topology edits one edge can be split over two entries with one side each; those are
    skipped. Returns True / False, or None when the arrays are not there."""
    if len(edge_polys) != 2 or None in edge_sides:
        return None
    (e0, e1), (k0, k1) = edge_polys, edge_sides
    for e in range(1, len(e0)):
        a, b = e0[e], e1[e]
        if a >= npolys or b >= npolys:
            continue
        pa, pb = polygons[a], polygons[b]
        i, j = k0[e], k1[e]
        if i >= len(pa) or j >= len(pb):
            return False
        if {pa[i], pa[(i + 1) % len(pa)]} != {pb[j], pb[(j + 1) % len(pb)]}:
            return False
    return True


def _cnum_bytes(v):
    """Encoding of a compact int (inverse of records.cnum)."""
    if v < 0x10:
        return bytes([0xF0 | v])
    if v < 0x1000:
        return bytes([0xE0 | (v & 15), v >> 4])
    if v < 0x100000:
        return bytes([0xD0 | (v & 15), (v >> 4) & 255, v >> 12])
    return bytes([0xC0 | (v & 15), (v >> 4) & 255, (v >> 12) & 255, v >> 20])


def _polygon_flags(d, lo, hi, npolys, nnodes):
    """Per-polygon 4-bit codes, between the edge arrays and the start array:
    f0 f1 <nnodes> <bits> f1 <nnodes> f1 <nbytes> f1 <nbytes> <bytes>, low nibble first.
    Bit 3 set: the polygon's nodes are one run from its stored start. Clear: they are listed in a node chain
    (Softimage leaves the node array as it is after topology edits: inverted, welded, collapsed, split ...
    polygons get chains). The low 3 bits are the node count (when < 8). <bits> 0 = no codes: all runs.
    Returns the codes, or None when the array is absent."""
    head = b"\xf0\xf1" + _cnum_bytes(nnodes)
    p = d.find(head, lo, hi)
    while p >= 0:
        try:
            bits, q = cnum(d, p + len(head))
            if d[q] == 0xF1:
                n, q = cnum(d, q + 1)
                if n == nnodes and d[q] == 0xF1:
                    nb, q = cnum(d, q + 1)
                    if d[q] == 0xF1:
                        nb2, q = cnum(d, q + 1)
                        if nb2 == nb and q + nb <= hi:
                            if bits == 0:
                                return [8] * npolys
                            codes = []
                            for b in d[q:q + nb]:
                                codes += (b & 15, b >> 4)
                            return codes[:npolys] if len(codes) >= npolys else None
        except (Lost, IndexError):
            pass
        p = d.find(head, p + 1, hi)
    return None


def _polygon_chains(d, lo, hi, nnodes, chained, sizes):
    """Node chains of the polygons whose nodes are not one run. After the start array, a list (f0 f1) of
    arrays, one per polygon size: f1 <nnodes> <w> f1 <n> <w> <n>, values (after 3 framing elements) = for
    each such polygon of that size its node slots in order, then the polygon's index. Only the first array
    follows the f0 f1, so the arrays are found by their own header. Returns {polygon: [slots]}."""
    head = b"\xf1" + _cnum_bytes(nnodes)
    out = {}
    p = d.find(head, lo, hi)
    while p >= 0 and len(out) < len(chained):
        q = p + len(head)
        try:
            w, q = cnum(d, q)
            if w in (1, 2, 4) and d[q] == 0xF1:
                n, q = cnum(d, q + 1)
                w2, q = cnum(d, q)
                n2, q = cnum(d, q)
                if w2 == w and n2 == n and q + n * w <= hi:
                    if n >= 6:
                        vals = list(struct.unpack_from("<%d%s" % (n, {1: "B", 2: "H", 4: "I"}[w]), d, q))[3:-3]
                        _split_chains(vals, chained, sizes, out)
                    p = d.find(head, q + n * w, hi)
                    continue
        except (Lost, IndexError):
            pass
        p = d.find(head, p + 1, hi)
    return out


def _split_chains(vals, chained, sizes, out):
    """vals = [k slots, polygon] repeated, all polygons of one size k."""
    for k in range(1, len(vals)):
        if len(vals) % (k + 1):
            continue
        groups = [vals[i:i + k + 1] for i in range(0, len(vals), k + 1)]
        if all(g[-1] in chained and sizes[g[-1]] == k for g in groups):
            for g in groups:
                out[g[-1]] = g[:-1]
            return


def _edge_count(polygons, _npts=None):
    """Number of edges the polygons need, counted the way the stored edge table does: one per vertex
    pair, but a non-manifold pair used by k > 2 polygon sides takes ceil(k / 2) edges.
    (Kept to C-level loops: meshes can have millions of nodes.)"""
    a, b = [], []
    for poly in polygons:
        a += poly
        b += poly[1:]
        b.append(poly[0])
    uses = collections.Counter(zip(map(min, a, b), map(max, a, b)))
    n = len(uses)
    if len(a) > 2 * n:   # some pair is used by more than two polygon sides
        n += sum((k - 1) // 2 for k in uses.values() if k > 2)
    return n


def _polygon_sizes(edge_polys, npolys, nnodes):
    """Polygon sizes from the two edge -> polygon arrays: a polygon has as many nodes as edges. Entry 0
    of both arrays is a placeholder; values >= npolys mean 'no polygon on this side' (open border).
    Returns None when the arrays are missing or do not add up to the node count."""
    if len(edge_polys) < 2:
        return None
    count = collections.Counter(edge_polys[0][1:])
    count.update(edge_polys[1][1:])
    sizes = [count.get(i, 0) for i in range(npolys)]
    if sum(sizes) != nnodes or min(sizes, default=3) < 3:
        return None
    return sizes
