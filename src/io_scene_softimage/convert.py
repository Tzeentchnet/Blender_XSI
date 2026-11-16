# SPDX-License-Identifier: GPL-3.0-or-later
"""Coordinate conversion and batched transform math (NumPy only, no bpy).

Softimage is right-handed with Y up; Blender is right-handed with Z up. The conversion is a
similarity A = s * P where P is a signed axis permutation (a +90 degree rotation about X for Y-up
sources, identity for Z-up sources) and s an optional uniform scale. Points convert as A p,
transforms as A M A^-1, which keeps every transform local to its parent.

All matrix helpers work on stacks of matrices (shape (F, 4, 4)) so that animation is converted for all
frames at once.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np


@dataclass(frozen=True)
class AxisConversion:
    """P maps source axes to Blender axes: blender = P @ source."""

    matrix: np.ndarray                 # 3x3 signed permutation
    scale: float = 1.0

    @classmethod
    def from_up_axis(cls, up: str = "Y", scale: float = 1.0) -> "AxisConversion":
        if up.upper() == "Z":
            matrix = np.eye(3)
        else:
            # +90 degrees about X: (x, y, z) -> (x, -z, y)
            matrix = np.array([[1.0, 0.0, 0.0], [0.0, 0.0, -1.0], [0.0, 1.0, 0.0]])
        return cls(matrix, float(scale))

    @property
    def matrix4(self) -> np.ndarray:
        out = np.eye(4)
        out[:3, :3] = self.matrix * self.scale
        return out

    @property
    def matrix4_inv(self) -> np.ndarray:
        out = np.eye(4)
        out[:3, :3] = self.matrix.T / self.scale
        return out

    @property
    def rotation4(self) -> np.ndarray:
        out = np.eye(4)
        out[:3, :3] = self.matrix
        return out

    def points(self, points: np.ndarray) -> np.ndarray:
        return (np.asarray(points, dtype=np.float64) @ self.matrix.T) * self.scale

    def directions(self, vectors: np.ndarray) -> np.ndarray:
        return np.asarray(vectors, dtype=np.float64) @ self.matrix.T

    def transform(self, matrices: np.ndarray) -> np.ndarray:
        """A M A^-1 for one matrix or a stack."""
        return self.matrix4 @ matrices @ self.matrix4_inv

    def axis_map(self) -> list[tuple[int, float]]:
        """For each source axis j: (Blender axis k, sign) with P e_j = sign * e_k."""
        out = []
        for j in range(3):
            column = self.matrix[:, j]
            k = int(np.argmax(np.abs(column)))
            out.append((k, float(np.sign(column[k]))))
        return out

    def euler_order(self, order: str) -> str:
        """Blender Euler order equivalent to a source order under the axis permutation."""
        mapping = self.axis_map()
        return "".join("XYZ"[mapping["XYZ".index(axis)][0]] for axis in order.upper())

    def channel(self, prop: str, index: int) -> tuple[int, float]:
        """Destination component and factor for a location / rotation / scale channel."""
        k, sign = self.axis_map()[index]
        if prop == "location":
            return k, sign * self.scale
        if prop == "rotation":
            return k, sign
        return k, 1.0


# ---------------------------------------------------------------------- batched math
def euler_matrices(angles_deg: np.ndarray, order: str = "XYZ") -> np.ndarray:
    """Rotation matrices (F, 3, 3) for Euler angles (F, 3) in degrees. Order 'XYZ' applies X first
    (matrix Rz @ Ry @ Rx), the convention shared by Softimage and Blender."""
    angles = np.radians(np.atleast_2d(np.asarray(angles_deg, dtype=np.float64)))
    count = len(angles)
    cos, sin = np.cos(angles), np.sin(angles)
    mats = {}
    for axis in range(3):
        m = np.zeros((count, 3, 3))
        c, s = cos[:, axis], sin[:, axis]
        a, b = [(1, 2), (2, 0), (0, 1)][axis]
        m[:, axis, axis] = 1.0
        m[:, a, a] = c
        m[:, a, b] = -s
        m[:, b, a] = s
        m[:, b, b] = c
        mats["XYZ"[axis]] = m
    result = np.broadcast_to(np.eye(3), (count, 3, 3)).copy()
    for axis in order.upper():
        result = mats[axis] @ result
    return result


def quaternion_matrices(quats: np.ndarray) -> np.ndarray:
    """(F, 3, 3) from (F, 4) quaternions (w, x, y, z); normalized first."""
    q = np.atleast_2d(np.asarray(quats, dtype=np.float64))
    q = q / np.linalg.norm(q, axis=1, keepdims=True).clip(1e-12)
    w, x, y, z = q.T
    return np.stack([
        np.stack([1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)], axis=-1),
        np.stack([2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)], axis=-1),
        np.stack([2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)], axis=-1),
    ], axis=1)


def compose(locations: np.ndarray, rotations: np.ndarray, scales: np.ndarray) -> np.ndarray:
    """(F, 4, 4) = T @ R @ S from (F, 3), (F, 3, 3), (F, 3)."""
    locations = np.atleast_2d(locations)
    count = len(locations)
    out = np.zeros((count, 4, 4))
    out[:, :3, :3] = rotations * np.asarray(scales)[:, None, :]
    out[:, :3, 3] = locations
    out[:, 3, 3] = 1.0
    return out


def decompose(matrices: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(locations (F, 3), quaternions (F, 4) w-first, scales (F, 3)) of affine matrices without shear."""
    matrices = np.asarray(matrices, dtype=np.float64)
    locations = matrices[:, :3, 3].copy()
    basis = matrices[:, :3, :3]
    scales = np.linalg.norm(basis, axis=1)
    det = np.linalg.det(basis)
    scales[:, 0] *= np.where(det < 0, -1.0, 1.0)
    rotation = basis / np.where(np.abs(scales) < 1e-12, 1.0, scales)[:, None, :]
    return locations, matrix_quaternions(rotation), scales


def matrix_quaternions(rot: np.ndarray) -> np.ndarray:
    """(F, 4) quaternions (w, x, y, z) of rotation matrices (F, 3, 3), sign made continuous."""
    rot = np.asarray(rot, dtype=np.float64)
    m00, m01, m02 = rot[:, 0, 0], rot[:, 0, 1], rot[:, 0, 2]
    m10, m11, m12 = rot[:, 1, 0], rot[:, 1, 1], rot[:, 1, 2]
    m20, m21, m22 = rot[:, 2, 0], rot[:, 2, 1], rot[:, 2, 2]
    trace = m00 + m11 + m22
    q = np.empty((len(rot), 4))
    case0 = trace > 0
    case1 = ~case0 & (m00 >= m11) & (m00 >= m22)
    case2 = ~case0 & ~case1 & (m11 >= m22)
    case3 = ~case0 & ~case1 & ~case2
    s = np.sqrt(np.maximum(trace[case0] + 1.0, 1e-12)) * 2
    q[case0] = np.stack([0.25 * s, (m21 - m12)[case0] / s, (m02 - m20)[case0] / s,
                         (m10 - m01)[case0] / s], axis=-1)
    s = np.sqrt(np.maximum(1.0 + m00[case1] - m11[case1] - m22[case1], 1e-12)) * 2
    q[case1] = np.stack([(m21 - m12)[case1] / s, 0.25 * s, (m01 + m10)[case1] / s,
                         (m02 + m20)[case1] / s], axis=-1)
    s = np.sqrt(np.maximum(1.0 + m11[case2] - m00[case2] - m22[case2], 1e-12)) * 2
    q[case2] = np.stack([(m02 - m20)[case2] / s, (m01 + m10)[case2] / s, 0.25 * s,
                         (m12 + m21)[case2] / s], axis=-1)
    s = np.sqrt(np.maximum(1.0 + m22[case3] - m00[case3] - m11[case3], 1e-12)) * 2
    q[case3] = np.stack([(m10 - m01)[case3] / s, (m02 + m20)[case3] / s, (m12 + m21)[case3] / s,
                         0.25 * s], axis=-1)
    q /= np.linalg.norm(q, axis=1, keepdims=True).clip(1e-12)
    return make_continuous(q)


def make_continuous(quats: np.ndarray) -> np.ndarray:
    """Flip quaternion signs so consecutive samples stay in the same hemisphere."""
    if len(quats) < 2:
        return quats
    dots = np.einsum("ij,ij->i", quats[1:], quats[:-1])
    flips = np.cumprod(np.where(dots < 0, -1.0, 1.0))
    out = quats.copy()
    out[1:] *= flips[:, None]
    return out


def matrix_eulers(rot: np.ndarray, order: str = "XYZ") -> np.ndarray:
    """Euler angles in radians (F, 3) of rotation matrices for a Blender order, unwrapped over time."""
    rot = np.asarray(rot, dtype=np.float64)
    order = order.upper()
    i, j, k = ("XYZ".index(order[0]), "XYZ".index(order[1]), "XYZ".index(order[2]))
    parity = (j - i) % 3 == 1
    # Generic Tait-Bryan extraction for R = Rk @ Rj @ Ri (first axis applied first).
    sign = 1.0 if parity else -1.0
    sy = np.clip(-sign * rot[:, k, i], -1.0, 1.0)
    angle_j = np.arcsin(sy)
    angle_i = np.arctan2(sign * rot[:, k, j], rot[:, k, k])
    angle_k = np.arctan2(sign * rot[:, j, i], rot[:, i, i])
    out = np.empty((len(rot), 3))
    out[:, i], out[:, j], out[:, k] = angle_i, angle_j, angle_k
    return np.unwrap(out, axis=0) if len(out) > 1 else out


def transform_matrix(position, rotation_deg, scale, order: str = "XYZ", quaternion=None) -> np.ndarray:
    """Single 4x4 local matrix of a Softimage SRT (scale, then rotation, then translation)."""
    if quaternion is not None and np.linalg.norm(quaternion) > 1e-9:
        rot = quaternion_matrices(np.asarray(quaternion)[None])
    else:
        rot = euler_matrices(np.asarray(rotation_deg)[None], order)
    return compose(np.asarray(position)[None], rot, np.asarray(scale)[None])[0]


def degrees_to_radians(values: np.ndarray) -> np.ndarray:
    return np.asarray(values) * (math.pi / 180.0)
