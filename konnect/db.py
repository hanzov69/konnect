"""moonrakerdb helper — persistent settings namespace.

HT90 talks to an external TPC service on :5000 that persists settings. We
use moonrakerdb instead (JSON-RPC `server.database.get_item` /
`server.database.post_item`) so konnect doesn't need its own DB or config
writer. Namespace is `konnect`.

Layout:
  konnect.serial_number          str  (user-editable or MAC-derived default)
  konnect.printer_name           str  (optional, sent in Connect info)
  konnect.printer_location       str  (optional)
  konnect.connect.hostname       str  (default: connect.prusa3d.com)
  konnect.connect.tls            bool (default: True)
  konnect.connect.port           int  (default: 443)
  konnect.connect.token          str
  konnect.camera.token           str
  konnect.camera.snapshot_url    str  (chosen stream's snapshot endpoint)
  konnect.camera.stream_name     str  (which crowsnest cam was picked)
"""
from __future__ import annotations

import logging
from threading import Event
from time import sleep
from typing import Any

from .moonraker import Moonraker

log = logging.getLogger(__name__)

NAMESPACE = "konnect"

DEFAULTS: dict[str, Any] = {
    "connect": {
        "hostname": "connect.prusa3d.com",
        "tls": True,
        "port": 443,
        "token": "",
    },
    "camera": {
        "token": "",
        "snapshot_url": "",
        "stream_name": "",
    },
    "serial_number": "",
    "printer_name": "",
    "printer_location": "",
}


class Database:
    """Wraps `server.database.*` JSON-RPC methods.

    Reads are cached in-memory so the hot telemetry path doesn't hit
    moonrakerdb each time the printer class peeks at the token. Writes
    go to both the cache and moonrakerdb; on startup we seed from
    moonrakerdb and fall through to DEFAULTS for missing keys.
    """

    def __init__(self, moonraker: Moonraker):
        self._mr = moonraker
        self._cache: dict[str, Any] = {}
        self._ready = Event()

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def wait_ready(self, timeout: float | None = None) -> bool:
        return self._ready.wait(timeout)

    def load(self) -> None:
        """Seed cache from moonrakerdb. Safe to call repeatedly."""
        # Retry until moonraker is up — we're started from the SDK thread
        # and can tolerate a slow boot.
        for _ in range(60):
            if self._mr.connected:
                break
            sleep(0.5)
        else:
            log.warning("moonraker not connected after 30s — using defaults")

        result = self._mr(
            "server.database.get_item",
            {"namespace": NAMESPACE},
            timeout=5.0,
        )
        stored = {}
        if result and "value" in result:
            stored = result["value"] or {}
        self._cache = _deep_merge(DEFAULTS, stored)
        self._ready.set()
        log.info(
            "konnect db loaded: serial=%s camera_configured=%s registered=%s",
            self._cache.get("serial_number") or "<unset>",
            bool(self._cache.get("camera", {}).get("snapshot_url")),
            bool(self._cache.get("connect", {}).get("token")),
        )

    def get(self, *path: str, default: Any = None) -> Any:
        cur: Any = self._cache
        for key in path:
            if not isinstance(cur, dict) or key not in cur:
                return default
            cur = cur[key]
        return cur

    def set(self, *path: str, value: Any) -> None:
        if not path:
            raise ValueError("set() requires at least one path segment")
        # Write through: cache first (so immediate reads see it) then
        # moonrakerdb. We always write the full namespace blob because
        # `server.database.post_item` addresses individual keys only at
        # top level and using dotted keys works but is less robust across
        # moonraker versions.
        cur = self._cache
        for key in path[:-1]:
            cur = cur.setdefault(key, {})
        cur[path[-1]] = value
        self._flush_top_level(path[0])

    def replace(self, top_key: str, value: Any) -> None:
        """Replace an entire top-level key (e.g. resetting `connect` dict)."""
        self._cache[top_key] = value
        self._flush_top_level(top_key)

    def _flush_top_level(self, top_key: str) -> None:
        result = self._mr(
            "server.database.post_item",
            {
                "namespace": NAMESPACE,
                "key": top_key,
                "value": self._cache.get(top_key),
            },
            timeout=5.0,
        )
        if result is None:
            log.error("Failed to persist konnect.%s to moonrakerdb", top_key)


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Return base overlaid with overlay, preserving nested dicts."""
    out = {}
    for key, value in base.items():
        if isinstance(value, dict) and isinstance(overlay.get(key), dict):
            out[key] = _deep_merge(value, overlay[key])
        elif key in overlay:
            out[key] = overlay[key]
        else:
            out[key] = value.copy() if isinstance(value, dict) else value
    for key, value in overlay.items():
        if key not in out:
            out[key] = value
    return out
