# SPDX-License-Identifier: GPL-3.0-or-later
"""Animation of native scenes: F-curves connected to kinematic parameters.

Reverse engineered from the Half-Life 2 source scenes (XSI 3.0 - 3.51), see docs/native-format.md:

* A parameter that is driven stores, after its object's parameter records, a connection table: entries
  `01 00 <parameter id u16> <flags u8> <x u8> ...`; flags 07 = connected input, 03 = not connected (the same
  convention as shader connections). The object's references that follow the table belong to the
  connected entries, in table order.
* F-curve objects (class e2a86051-f669-11d1-8d60-080036f3cc02) hold, after a short header ending in a
  `UC1010` chunk: f64 default value, f64 minimum, f64 maximum (usually -DBL_MAX / DBL_MAX), u32 curve type,
  u32 key count, then per key 7 f64 and a u32: time (seconds), value, value again, left handle
  (time, value), right handle (time, value), interpolation (1 constant, 2 linear, 3 cubic). Handles are
  absolute positions.
* Expressions (class 12723eb2-7ddb-11d0-a57f-00a0c91412de) carry their text in a string parameter
  (`ValveBiped.HandControlPosL.kine.local.roty = 0.0`); they are reported, not evaluated.
"""

from __future__ import annotations

import re
import struct
from dataclasses import dataclass

CLS_FCURVE = "e2a86051-f669-11d1-8d60-080036f3cc02"
CLS_EXPRESSION = "12723eb2-7ddb-11d0-a57f-00a0c91412de"
P_EXPRESSION_TEXT = "333afeca-a7bd-11d0-a583-00a0c91412de"
_CLAMP = b"\xff\xff\xff\xff\xff\xff\xef\xff\xff\xff\xff\xff\xff\xff\xef\x7f"
_ENTRY = re.compile(rb"\x01\x00(..)([\x03\x07])[\x00\x01]", re.S)
KEY_INTERPOLATION = {1: "CONSTANT", 2: "LINEAR", 3: "CUBIC"}


@dataclass
class NativeCurve:
    default: float
    curve_type: int
    times: list[float]          # seconds
    values: list[float]
    left: list[tuple[float, float]]
    right: list[tuple[float, float]]
    interpolation: list[str]


def read_fcurve(d: bytes, obj) -> NativeCurve | None:
    """Decode an F-curve object, or None when its layout is not recognized."""
    p = d.find(_CLAMP, obj.start, obj.end)
    if p < 8:
        return _read_clampless(d, obj)
    return _keys(d, obj, p - 8)


def _keys(d: bytes, obj, at: int) -> NativeCurve | None:
    try:
        default, _low, _high = struct.unpack_from("<3d", d, at)
        curve_type, count = struct.unpack_from("<II", d, at + 24)
    except struct.error:
        return None
    q = at + 32
    if count > 1_000_000 or q + count * 60 > obj.end:
        return None
    curve = NativeCurve(default, curve_type, [], [], [], [], [])
    for _ in range(count):
        time, value, _value2, lt, lv, rt, rv, mode = struct.unpack_from("<7dI", d, q)
        q += 60
        curve.times.append(time)
        curve.values.append(value)
        curve.left.append((lt, lv))
        curve.right.append((rt, rv))
        curve.interpolation.append(KEY_INTERPOLATION.get(mode, "CUBIC"))
    return curve


def _read_clampless(d: bytes, obj) -> NativeCurve | None:
    """Curves with user clamps: find the header by checking that the declared keys end the object."""
    tag = d.find(b"UC1010", obj.start, obj.end)
    if tag < 0:
        return None
    for at in range(tag + 6, min(obj.end - 32, tag + 120)):
        try:
            low, high = struct.unpack_from("<2d", d, at + 8)
            count = struct.unpack_from("<I", d, at + 28)[0]
        except struct.error:
            return None
        if not (low <= high) or count > 100_000 or at + 32 + count * 60 > obj.end:
            continue
        curve = _keys(d, obj, at)
        if curve is None or not curve.times:
            continue
        finite = all(abs(v) < 1e12 for v in curve.times + curve.values)
        ordered = all(b >= a for a, b in zip(curve.times, curve.times[1:]))
        if finite and ordered:
            return curve
    return None


def connections(st, obj) -> list[tuple[int, object]]:
    """[(parameter id, connected object)] for the driven parameters of `obj`."""
    if not obj.plist or not obj.refs:
        return []
    d = st.data
    table_start = obj.plist[-1].pos
    first_ref = min(pos for _h, _c, pos in obj.refs)
    entries = [
        (struct.unpack_from("<H", m.group(1))[0], m.group(2)[0])
        for m in _ENTRY.finditer(d, table_start, first_ref)
    ]
    connected = [param for param, flags in entries if flags == 0x07]
    refs = sorted(obj.refs, key=lambda r: r[2])
    if len(connected) != len(refs):
        return []
    out = []
    for param, (handle, _cls, _pos) in zip(connected, refs):
        target = st.by_handle.get(handle)
        if target is not None:
            out.append((param, target))
    return out


def expression_text(st, obj) -> str | None:
    for record in obj.plist:
        if record.id is not None and st.guid(record.id) == P_EXPRESSION_TEXT and isinstance(record.value, str):
            return record.value
    return None
