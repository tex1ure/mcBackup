"""Discover Minecraft world saves across launchers and parse world metadata."""

from __future__ import annotations

import gzip
import os
import struct
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable

from lock_detect import is_world_locked


@dataclass
class DiscoveredWorld:
    path: str
    folder_name: str
    level_name: str
    source_label: str
    last_modified: float
    size_bytes: int
    is_locked: bool

    @property
    def display_name(self) -> str:
        if self.level_name and self.level_name != self.folder_name:
            return f"{self.level_name} ({self.folder_name})"
        return self.folder_name

    @property
    def last_modified_str(self) -> str:
        if self.last_modified <= 0:
            return "Unknown"
        return datetime.fromtimestamp(self.last_modified).strftime("%Y-%m-%d %H:%M")

    @property
    def size_mb_str(self) -> str:
        mb = self.size_bytes / (1024 * 1024)
        if mb < 1:
            return f"{self.size_bytes / 1024:.0f} KB"
        return f"{mb:.1f} MB"


def _read_nbt_level_name(level_dat_path: Path) -> str:
    """Extract Data.LevelName from a gzipped Java Edition level.dat file."""
    if not level_dat_path.is_file():
        return ""
    try:
        with gzip.open(level_dat_path, "rb") as fh:
            data = fh.read(256 * 1024)
    except OSError:
        return ""

    idx = 0
    length = len(data)

    def read_u8() -> int:
        nonlocal idx
        if idx >= length:
            raise EOFError
        val = data[idx]
        idx += 1
        return val

    def read_i16() -> int:
        nonlocal idx
        if idx + 2 > length:
            raise EOFError
        val = struct.unpack_from(">h", data, idx)[0]
        idx += 2
        return val

    def read_u16() -> int:
        nonlocal idx
        if idx + 2 > length:
            raise EOFError
        val = struct.unpack_from(">H", data, idx)[0]
        idx += 2
        return val

    def read_i32() -> int:
        nonlocal idx
        if idx + 4 > length:
            raise EOFError
        val = struct.unpack_from(">i", data, idx)[0]
        idx += 4
        return val

    def read_str() -> str:
        nonlocal idx
        n = read_u16()
        if idx + n > length:
            raise EOFError
        s = data[idx : idx + n].decode("utf-8", errors="replace")
        idx += n
        return s

    fixed_sizes = {1: 1, 2: 2, 3: 4, 4: 8, 5: 4, 6: 8}

    def skip_payload(tag_type: int) -> None:
        nonlocal idx
        if tag_type in fixed_sizes:
            idx += fixed_sizes[tag_type]
        elif tag_type == 7:  # Byte Array
            idx += read_i32()
        elif tag_type == 8:  # String
            idx += read_u16()
        elif tag_type == 9:  # List
            elem_type = read_u8()
            count = read_i32()
            for _ in range(max(0, count)):
                skip_payload(elem_type)
        elif tag_type == 10:  # Compound
            while True:
                t = read_u8()
                if t == 0:
                    break
                _ = read_str()
                skip_payload(t)
        elif tag_type == 11:  # Int Array
            idx += read_i32() * 4
        elif tag_type == 12:  # Long Array
            idx += read_i32() * 8
        else:
            raise ValueError(f"Unknown NBT tag {tag_type}")

    try:
        root_type = read_u8()
        if root_type != 10:
            return ""
        _ = read_str()
        while True:
            t = read_u8()
            if t == 0:
                break
            name = read_str()
            if t == 10 and name == "Data":
                while True:
                    inner_t = read_u8()
                    if inner_t == 0:
                        break
                    inner_name = read_str()
                    if inner_t == 8 and inner_name == "LevelName":
                        return read_str()
                    skip_payload(inner_t)
                return ""
            skip_payload(t)
    except (EOFError, ValueError, struct.error):
        return ""
    return ""


def _dir_size_and_mtime(world_path: Path) -> tuple[int, float]:
    total = 0
    latest = 0.0
    level_dat = world_path / "level.dat"
    try:
        latest = level_dat.stat().st_mtime
    except OSError:
        pass

    for dirpath, _, filenames in os.walk(world_path):
        for fname in filenames:
            try:
                st = (Path(dirpath) / fname).stat()
                total += st.st_size
                if st.st_mtime > latest:
                    latest = st.st_mtime
            except OSError:
                continue
    return total, latest


def is_valid_world_dir(path: Path) -> bool:
    return path.is_dir() and ((path / "level.dat").is_file() or (path / "levelname.txt").is_file())


def inspect_world(world_path: str | Path, source_label: str = "Custom") -> DiscoveredWorld | None:
    p = Path(world_path).resolve()
    if not is_valid_world_dir(p):
        return None
    level_name = _read_nbt_level_name(p / "level.dat")
    if not level_name and (p / "levelname.txt").is_file():
        try:
            level_name = (p / "levelname.txt").read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            level_name = ""
    size_bytes, mtime = _dir_size_and_mtime(p)
    return DiscoveredWorld(
        path=str(p),
        folder_name=p.name,
        level_name=level_name or p.name,
        source_label=source_label,
        last_modified=mtime,
        size_bytes=size_bytes,
        is_locked=is_world_locked(p),
    )


def _candidate_saves_roots(extra_roots: Iterable[str] = ()) -> list[tuple[str, Path]]:
    roots: list[tuple[str, Path]] = []
    appdata = os.environ.get("APPDATA", "")
    localappdata = os.environ.get("LOCALAPPDATA", "")
    userprofile = os.environ.get("USERPROFILE", "")

    if appdata:
        roots.append(("Vanilla (.minecraft)", Path(appdata) / ".minecraft" / "saves"))
        for launcher in ("PolyMC", "PrismLauncher", "MultiMC"):
            inst_dir = Path(appdata) / launcher / "instances"
            if inst_dir.is_dir():
                for inst in inst_dir.iterdir():
                    if not inst.is_dir():
                        continue
                    for sub in (".minecraft", "minecraft"):
                        s = inst / sub / "saves"
                        if s.is_dir():
                            roots.append((f"{launcher} ({inst.name})", s))

        modrinth = Path(appdata) / "ModrinthApp" / "profiles"
        if modrinth.is_dir():
            for prof in modrinth.iterdir():
                s = prof / "saves"
                if s.is_dir():
                    roots.append((f"Modrinth ({prof.name})", s))

    if userprofile:
        cf = Path(userprofile) / "curseforge" / "minecraft" / "Instances"
        if cf.is_dir():
            for inst in cf.iterdir():
                s = inst / "saves"
                if s.is_dir():
                    roots.append((f"CurseForge ({inst.name})", s))

    if localappdata:
        bedrock = (
            Path(localappdata)
            / "Packages"
            / "Microsoft.MinecraftUWP_8wekyb3d8bbwe"
            / "LocalState"
            / "games"
            / "com.mojang"
            / "minecraftWorlds"
        )
        if bedrock.is_dir():
            roots.append(("Minecraft Bedrock", bedrock))

    # Check common secondary drive Minecraft locations (like A:\minecraft\game\saves)
    for drive in ("A:", "B:", "C:", "D:", "E:"):
        for rel in ("minecraft/game/saves", "minecraft/saves", "Minecraft/saves", "PolyMC/instances"):
            cand = Path(f"{drive}/{rel}")
            if not cand.is_dir():
                continue
            if cand.name == "instances":
                for inst in cand.iterdir():
                    for sub in (".minecraft", "minecraft"):
                        s = inst / sub / "saves"
                        if s.is_dir():
                            roots.append((f"PolyMC ({inst.name})", s))
            else:
                roots.append((f"{drive} Minecraft", cand))

    for extra in extra_roots:
        ep = Path(extra)
        if ep.is_dir():
            roots.append(("Custom Folder", ep))

    return roots


def discover_worlds(
    selected_paths: Iterable[str] = (),
    extra_roots: Iterable[str] = (),
) -> list[DiscoveredWorld]:
    """Find all Minecraft worlds from known launchers plus explicitly selected paths."""
    seen: set[str] = set()
    found: list[DiscoveredWorld] = []

    for label, saves_root in _candidate_saves_roots(extra_roots):
        if not saves_root.is_dir():
            continue
        for child in saves_root.iterdir():
            if not child.is_dir():
                continue
            norm = str(child.resolve()).lower()
            if norm in seen:
                continue
            info = inspect_world(child, source_label=label)
            if info:
                seen.add(norm)
                found.append(info)

    for raw_path in selected_paths:
        p = Path(raw_path)
        norm = str(p.resolve()).lower() if p.exists() else str(p).lower()
        if norm in seen:
            continue
        info = inspect_world(p, source_label="Selected")
        if info:
            seen.add(norm)
            found.append(info)

    found.sort(key=lambda w: w.last_modified, reverse=True)
    return found
