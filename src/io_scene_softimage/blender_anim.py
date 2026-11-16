# SPDX-License-Identifier: GPL-3.0-or-later
"""Writing animation into Blender 5.x layered (slotted) actions with bulk keyframe insertion."""

from __future__ import annotations

import bpy
import numpy as np

INTERPOLATION = {"CONSTANT": 0, "LINEAR": 1, "BEZIER": 2}
HANDLE_FREE = 0
HANDLE_AUTO_CLAMPED = 3


class ActionWriter:
    """One action holding a slot per animated datablock (objects, armatures, shape-key blocks)."""

    def __init__(self, name: str):
        self.action = bpy.data.actions.new(name)
        self.layer = self.action.layers.new("Softimage")
        self.strip = self.layer.strips.new(type="KEYFRAME")
        self._bags: dict[int, object] = {}
        self._groups: dict[tuple[int, str], object] = {}
        self.used = False

    def _channelbag(self, datablock):
        key = datablock.as_pointer()
        bag = self._bags.get(key)
        if bag is None:
            slot = self.action.slots.new(id_type=datablock.id_type, name=datablock.name)
            bag = self.strip.channelbag(slot, ensure=True)
            animation_data = datablock.animation_data_create()
            animation_data.action = self.action
            animation_data.action_slot = slot
            self._bags[key] = bag
        return bag

    def add_curve(
        self,
        datablock,
        data_path: str,
        index: int,
        frames: np.ndarray,
        values: np.ndarray,
        interpolation: str = "LINEAR",
        handles_left: np.ndarray | None = None,
        handles_right: np.ndarray | None = None,
        group: str | None = None,
    ):
        frames = np.asarray(frames, dtype=np.float64)
        values = np.asarray(values, dtype=np.float64)
        if not len(frames):
            return None
        bag = self._channelbag(datablock)
        curve = bag.fcurves.find(data_path, index=index)
        if curve is None:
            curve = bag.fcurves.new(data_path, index=index)
        if group:
            key = (datablock.as_pointer(), group)
            channel_group = self._groups.get(key)
            if channel_group is None:
                channel_group = bag.groups.new(group[:63])
                self._groups[key] = channel_group
            curve.group = channel_group
        count = len(frames)
        points = curve.keyframe_points
        points.add(count)
        points.foreach_set("co", np.column_stack((frames, values)).astype(np.float32).ravel())
        mode = interpolation.upper()
        bezier = handles_left is not None and handles_right is not None
        code = INTERPOLATION["BEZIER"] if bezier else INTERPOLATION.get(
            "CONSTANT" if "CONSTANT" in mode else "LINEAR", 1
        )
        points.foreach_set("interpolation", np.full(count, code, dtype=np.int32))
        if bezier:
            points.foreach_set("handle_left_type", np.full(count, HANDLE_FREE, dtype=np.int32))
            points.foreach_set("handle_right_type", np.full(count, HANDLE_FREE, dtype=np.int32))
            points.foreach_set("handle_left", np.asarray(handles_left, dtype=np.float32).ravel())
            points.foreach_set("handle_right", np.asarray(handles_right, dtype=np.float32).ravel())
        curve.update()
        self.used = True
        return curve

    def finish(self, frame_start: float | None, frame_end: float | None) -> bpy.types.Action | None:
        if not self.used:
            bpy.data.actions.remove(self.action)
            return None
        if frame_start is not None and frame_end is not None and frame_end > frame_start:
            self.action.use_frame_range = True
            self.action.frame_start = frame_start
            self.action.frame_end = frame_end
        return self.action


def drop_constant_identity(values: np.ndarray, identity: float, tolerance: float = 1e-6) -> bool:
    """True when a sampled channel never leaves its identity value (no curve needed)."""
    return bool(np.all(np.abs(np.asarray(values) - identity) <= tolerance))
