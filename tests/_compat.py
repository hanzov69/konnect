"""Test-only shim: the Prusa SDK imports inotify_simple at package
import time, which is Linux-only. The tests never watch a filesystem,
so on Windows/macOS dev boxes a stub with the right names is enough.
"""
from __future__ import annotations

import os
import sys
import types

if not hasattr(os, "statvfs"):  # Unix-only; stub free-space probing
    os.statvfs = lambda _p: types.SimpleNamespace(
        f_bavail=0, f_frsize=0, f_blocks=0, f_bsize=0,
    )

try:
    import inotify_simple  # noqa: F401
except ImportError:

    class _INotify:
        def __init__(self, *args, **kwargs):
            pass

        def add_watch(self, *args, **kwargs):
            return 0

        def rm_watch(self, *args, **kwargs):
            pass

        def read(self, *args, **kwargs):
            return []

        def fileno(self):
            return -1

    class _Flags:
        def __getattr__(self, _name):
            return 0

        @staticmethod
        def from_mask(_mask):
            return []

    _stub = types.ModuleType("inotify_simple")
    _stub.INotify = _INotify
    _stub.flags = _Flags()
    _stub.masks = _Flags()
    sys.modules["inotify_simple"] = _stub
