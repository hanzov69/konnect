"""Buddy-style file protocol for Prusa Connect.

Real MK4/MK4S/Core One printers (Buddy firmware) use a different
filesystem schema in Connect than the legacy SDK one that HT90 /
i3 types use:

1. INFO events carry a `storages` array advertising mount points
   (``/usb``, etc.) rather than a pre-populated `files` tree.
2. Connect enumerates each mountpoint on demand by sending
   ``SEND_FILE_INFO`` commands with ``kwargs.path``; the printer
   responds with a FILE_INFO event containing ``children`` + per-file
   metadata.

This module implements the printer side of that protocol against a
single local directory. Combined with enum extensions in
:mod:`konnect.printer_types`, it lets konnect masquerade as an MK4S /
Core One / etc. and have Connect's modern dashboards actually render
files + accept uploads.

Schema references:
  * github.com/prusa3d/Prusa-Firmware-Buddy/blob/master/
      src/connect/render.cpp         (`storages` + FILE_INFO layout)
  * github.com/prusa3d/Prusa-Firmware-Buddy/blob/master/
      src/common/filename_type.cpp   (file_type_by_ext)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

# Match Buddy's filename_type.cpp `file_type_by_ext`.
_PRINTABLE_EXTS: tuple[str, ...] = (
    ".g", ".gc", ".gco", ".gcode", ".bgcode", ".bgc",
)
_FIRMWARE_EXTS: tuple[str, ...] = (".bbf",)


def file_type_by_ext(name: str) -> str:
    """Classify a filename into Connect's file-type enum.

    >>> file_type_by_ext("foo.gcode")
    'PRINT_FILE'
    >>> file_type_by_ext("Foo.BGCODE")
    'PRINT_FILE'
    >>> file_type_by_ext("update.bbf")
    'FIRMWARE'
    >>> file_type_by_ext("README.md")
    'FILE'
    """
    lo = name.lower()
    if lo.endswith(_PRINTABLE_EXTS):
        return "PRINT_FILE"
    if lo.endswith(_FIRMWARE_EXTS):
        return "FIRMWARE"
    return "FILE"


def build_storages(
    mountpoint: str,
    root: str | os.PathLike,
) -> list[dict[str, Any]]:
    """Build the ``storages`` array for an INFO event payload.

    Connect uses this to discover mount points; it then issues
    SEND_FILE_INFO commands against the mountpoint paths to list
    contents. Buddy advertises USB regardless of whether it's
    actually a USB stick — so do we.
    """
    try:
        st = os.statvfs(root)
        free = st.f_bavail * st.f_frsize
    except OSError:
        free = 0
    return [
        {
            "mountpoint": mountpoint,
            "type": "USB",
            "read_only": False,
            "free_space": free,
            "is_sfn": True,
        },
    ]


def _resolve_under(
    root: Path,
    mountpoint: str,
    req_path: str,
) -> Path | None:
    """Map a Connect-supplied ``/usb/sub/foo.gcode`` path to a real
    filesystem path under ``root``.

    Returns ``None`` if the request escapes via ``..`` or symlinks,
    or if the mountpoint prefix doesn't match.
    """
    if not req_path.startswith(mountpoint):
        return None
    tail = req_path[len(mountpoint):].lstrip("/")
    try:
        target = (root / tail).resolve()
        target.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return target


def file_info(
    root: str | os.PathLike,
    mountpoint: str,
    req_path: str,
) -> dict[str, Any] | None:
    """Build FILE_INFO response data for ``SEND_FILE_INFO path=<req_path>``.

    Returns a dict suitable for merging into an event payload
    (``{"source":..., "event": Event.FILE_INFO, **file_info_data}``),
    or ``None`` if the path doesn't resolve to anything readable —
    the caller should raise an error in that case so Connect receives
    a proper rejection rather than a silent miss.
    """
    root_p = Path(root)
    norm = req_path.rstrip("/") or mountpoint

    # The root of the storage has no "tail" — Connect asks for the
    # mountpoint verbatim, e.g. req_path == "/usb".
    if norm == mountpoint:
        target = root_p
    else:
        target = _resolve_under(root_p, mountpoint, req_path)
    if target is None or not target.exists():
        return None

    try:
        st = target.stat()
    except OSError:
        return None

    if target.is_dir():
        children: list[dict[str, Any]] = []
        try:
            entries = sorted(target.iterdir())
        except OSError:
            entries = []
        for ent in entries:
            # Hide dot-files, matching Buddy's DirRenderer.
            if ent.name.startswith("."):
                continue
            try:
                cst = ent.stat()
            except OSError:
                continue
            is_dir = ent.is_dir()
            children.append({
                "name": ent.name,
                "display_name": ent.name,
                "size": 0 if is_dir else cst.st_size,
                "m_timestamp": int(cst.st_mtime),
                "read_only": False,
                "type": "FOLDER" if is_dir else file_type_by_ext(ent.name),
            })
        return {
            "children": children,
            "file_count": len(children),
            "size": 0,
            "m_timestamp": int(st.st_mtime),
            "read_only": False,
            "display_name": target.name or mountpoint.lstrip("/") or "storage",
            "type": "FOLDER",
            "path": norm,
        }

    # Plain file.
    return {
        "size": st.st_size,
        "m_timestamp": int(st.st_mtime),
        "read_only": False,
        "display_name": target.name,
        "type": file_type_by_ext(target.name),
        "path": req_path,
    }
