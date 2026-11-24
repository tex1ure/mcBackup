"""Windows file-lock detection and world change fingerprinting."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value

LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
LOCKFILE_EXCLUSIVE_LOCK = 0x00000002

ERROR_ACCESS_DENIED = 5
ERROR_SHARING_VIOLATION = 32
ERROR_LOCK_VIOLATION = 33


class OVERLAPPED(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_void_p),
        ("InternalHigh", ctypes.c_void_p),
        ("Offset", wintypes.DWORD),
        ("OffsetHigh", wintypes.DWORD),
        ("hEvent", wintypes.HANDLE),
    ]


kernel32.CreateFileW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
]
kernel32.CreateFileW.restype = wintypes.HANDLE

kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL

kernel32.LockFileEx.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(OVERLAPPED),
]
kernel32.LockFileEx.restype = wintypes.BOOL

kernel32.UnlockFileEx.argtypes = [
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.DWORD,
    ctypes.POINTER(OVERLAPPED),
]
kernel32.UnlockFileEx.restype = wintypes.BOOL


def is_world_locked(world_path: str | Path) -> bool:
    """Return True if Minecraft currently holds an active lock on session.lock."""
    lock_file = Path(world_path) / "session.lock"
    if not lock_file.is_file():
        return False

    handle = kernel32.CreateFileW(
        str(lock_file),
        GENERIC_READ | GENERIC_WRITE,
        0,  # exclusive share mode: fails if any process has the file open
        None,
        OPEN_EXISTING,
        FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == INVALID_HANDLE_VALUE:
        err = ctypes.get_last_error()
        if err in (ERROR_SHARING_VIOLATION, ERROR_LOCK_VIOLATION, ERROR_ACCESS_DENIED):
            return True
        return False

    try:
        overlapped = OVERLAPPED()
        locked = kernel32.LockFileEx(
            handle,
            LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
            0,
            1,
            0,
            ctypes.byref(overlapped),
        )
        if not locked:
            err = ctypes.get_last_error()
            if err in (ERROR_LOCK_VIOLATION, ERROR_SHARING_VIOLATION):
                return True
            return False
        kernel32.UnlockFileEx(handle, 0, 1, 0, ctypes.byref(overlapped))
        return False
    finally:
        kernel32.CloseHandle(handle)


def compute_world_fingerprint(world_path: str | Path) -> str:
    """Compute a fast metadata + level.dat fingerprint to detect world saves."""
    root = Path(world_path)
    if not root.is_dir():
        return ""

    hasher = hashlib.sha256()
    entries: list[tuple[str, int, int]] = []

    for dirpath, _, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        for name in filenames:
            if name == "session.lock" or name.endswith(".tmp"):
                continue
            full = Path(dirpath) / name
            try:
                st = full.stat()
            except OSError:
                continue
            rel = name if rel_dir == "." else f"{rel_dir}/{name}"
            entries.append((rel, st.st_size, st.st_mtime_ns))

    entries.sort()
    for rel, size, mtime_ns in entries:
        hasher.update(f"{rel}|{size}|{mtime_ns}\n".encode("utf-8", errors="replace"))

    level_dat = root / "level.dat"
    if level_dat.is_file():
        try:
            hasher.update(level_dat.read_bytes())
        except OSError:
            pass

    return hasher.hexdigest()
