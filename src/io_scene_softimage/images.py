# SPDX-License-Identifier: GPL-3.0-or-later
"""Finding the image files that Softimage scenes refer to (no bpy).

Scenes usually store absolute paths from the machine that wrote them (for example
`U:\\hl2\\hl2\\materialsrc\\Models\\Alyx\\alyxboot.tga`) or `[Project Path]` relative paths. The resolver
tries, in order:

1. the stored path itself (after `[Project Path]` substitution);
2. "re-rooting": every trailing part of the stored path (`materialsrc/Models/Alyx/alyxboot.tga`,
   `Models/Alyx/alyxboot.tga`, ...) below the scene folder, its Softimage project, its Pictures folder and
   every ancestor folder of the scene;
3. the same with other common image extensions (a `.tga` that was converted to `.png`, ...);
4. optionally, a basename index of image files under the nearest ancestor that looks like a content root
   (contains `materialsrc`, `Pictures`, `textures` ...), ranked by how many trailing folder names match.
"""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
import re

IMAGE_EXTENSIONS = (".tga", ".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".psd", ".dds",
                    ".exr", ".hdr", ".pic", ".sgi", ".rgb", ".webp")
CONTENT_FOLDERS = {"materialsrc", "pictures", "textures", "materials", "maps", "images"}
MAX_INDEX_FILES = 400_000


def split_stored_path(stored: str) -> list[str]:
    """Path components of a stored (often Windows) path, without drive or root."""
    cleaned = re.sub(r"(?i)\[project path\][\\/]*", "", stored.strip().strip('"'))
    parts = [p for p in PureWindowsPath(cleaned.replace("/", "\\")).parts]
    if parts and (parts[0].endswith(":\\") or parts[0].endswith(":") or parts[0] in {"\\", "\\\\"}
                  or parts[0].startswith("\\\\")):
        parts = parts[1:]
    return [p for p in parts if p not in {"", ".", "\\"}]


def project_folder(scene_path: str | Path) -> Path:
    """The Softimage project of a scene: the folder above its `Scenes` folder (or the scene folder)."""
    directory = Path(scene_path).resolve().parent
    for candidate in (directory, *directory.parents):
        if candidate.name.casefold() == "scenes":
            return candidate.parent
    return directory


class ImageResolver:
    def __init__(self, scene_path: str | Path, search: bool = True, max_levels: int = 6):
        self.scene_path = Path(scene_path).resolve()
        self.search = search
        self.project = project_folder(self.scene_path)
        directory = self.scene_path.parent
        ancestors = [directory, *directory.parents][:max_levels + 1]
        roots: list[Path] = [directory, self.project, self.project / "Pictures", directory / "Pictures"]
        roots.extend(ancestors[1:])
        self.roots = list(dict.fromkeys(roots))
        self._cache: dict[str, Path | None] = {}
        self._index: dict[str, list[Path]] | None = None
        self.max_levels = max_levels

    # .................................................................. public
    def resolve(self, stored: str) -> Path | None:
        key = stored.casefold()
        if key not in self._cache:
            self._cache[key] = self._resolve(stored)
        return self._cache[key]

    def display_path(self, stored: str) -> str:
        """A path to show (and to store on a placeholder image) when the file is missing."""
        substituted = re.sub(r"(?i)\[project path\]", str(self.project).replace("\\", "\\\\"), stored)
        return substituted

    # .................................................................. strategies
    def _resolve(self, stored: str) -> Path | None:
        if not stored:
            return None
        substituted = re.sub(r"(?i)\[project path\][\\/]*", "", stored)
        direct = Path(substituted)
        if direct.is_absolute() and direct.is_file():
            return direct
        if re.match(r"(?i)\[project path\]", stored) and (self.project / substituted).is_file():
            return (self.project / substituted).resolve()
        parts = split_stored_path(stored)
        if not parts:
            return None
        found = self._reroot(parts)
        if found is not None:
            return found
        stem, ext = os.path.splitext(parts[-1])
        for alternative in IMAGE_EXTENSIONS:
            if alternative == ext.casefold():
                continue
            found = self._reroot(parts[:-1] + [stem + alternative])
            if found is not None:
                return found
        if self.search:
            return self._search(parts)
        return None

    def _reroot(self, parts: list[str]) -> Path | None:
        for length in range(len(parts), 0, -1):
            tail = Path(*parts[-length:])
            for root in self.roots:
                candidate = root / tail
                if candidate.is_file():
                    return candidate.resolve()
        return None

    def _search(self, parts: list[str]) -> Path | None:
        index = self._build_index()
        name = parts[-1].casefold()
        stem = os.path.splitext(name)[0]
        candidates = index.get(name) or []
        if not candidates:
            for ext in IMAGE_EXTENSIONS:
                candidates = index.get(stem + ext) or []
                if candidates:
                    break
        if not candidates:
            return None
        wanted = [p.casefold() for p in parts[:-1]]

        def score(path: Path) -> tuple[int, int]:
            folders = [p.casefold() for p in path.parent.parts]
            matched = 0
            for a, b in zip(reversed(wanted), reversed(folders)):
                if a != b:
                    break
                matched += 1
            common = len(set(wanted) & set(folders))
            return (matched, common, -len(path.parts))  # type: ignore[return-value]

        return max(candidates, key=score).resolve()

    def _build_index(self) -> dict[str, list[Path]]:
        if self._index is not None:
            return self._index
        index: dict[str, list[Path]] = {}
        root = self._content_root()
        total = 0
        stack = [root]
        while stack and total < MAX_INDEX_FILES:
            current = stack.pop()
            try:
                with os.scandir(current) as entries:
                    for entry in entries:
                        try:
                            if entry.is_dir(follow_symlinks=False):
                                stack.append(Path(entry.path))
                            elif entry.name.casefold().endswith(IMAGE_EXTENSIONS):
                                index.setdefault(entry.name.casefold(), []).append(Path(entry.path))
                                total += 1
                        except OSError:
                            continue
            except OSError:
                continue
        self._index = index
        return index

    def _content_root(self) -> Path:
        """Nearest ancestor (within max_levels) that contains a typical content folder."""
        directory = self.scene_path.parent
        for level, candidate in enumerate((directory, *directory.parents)):
            if level > self.max_levels:
                break
            try:
                names = {entry.name.casefold() for entry in os.scandir(candidate) if entry.is_dir()}
            except OSError:
                continue
            if names & CONTENT_FOLDERS:
                return candidate
        return self.project
