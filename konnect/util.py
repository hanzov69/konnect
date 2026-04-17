"""Shared utilities."""
import logging
import uuid
from hashlib import sha256
from pathlib import Path
from threading import Event, Thread
from typing import Callable, Optional

log = logging.getLogger(__name__)


class InfinityThread(Thread):
    """Thread that runs target in a loop until stop() is called.

    Mirrors the pattern used by prusa.connect.ht90 so restart loops survive
    transient WebSocket/HTTP failures.
    """

    def __init__(self, target: Callable, kwargs=None, name: Optional[str] = None):
        super().__init__(name=name, daemon=True)
        self._target = target
        self._kwargs = kwargs or {}
        self._stop_evt = Event()

    def run(self):
        while not self._stop_evt.is_set():
            try:
                self._target(**self._kwargs)
            except Exception:  # noqa: BLE001
                log.exception("InfinityThread target %s raised", self._target)
            if self._stop_evt.wait(1):
                break

    def stop(self):
        self._stop_evt.set()


def make_fingerprint(sn: str, cpu_serial: str) -> str:
    """Fingerprint = sha256(sn + cpu_serial) iterated 100x, hex.

    Same algorithm as prusa.connect.ht90 so the behavior on Connect is
    identical — fingerprint stability across reboots is what lets Connect
    recognize an already-registered printer.
    """
    digest = sha256(sn.encode() + cpu_serial.encode())
    for _ in range(100):
        digest.update(digest.digest())
    return digest.hexdigest()


def default_serial_number() -> str:
    """Generate a stable default serial number from the primary MAC.

    Users are encouraged to set their own via the onboarding UI; this is
    only a fallback so headless first-boot works.
    """
    mac = uuid.getnode()
    return f"KLP-{mac:012X}"


def nested_get(container, *keys, default=None):
    """Null-safe nested dict access.

    Moonraker's `printer.objects.subscribe` response contains keys whose
    values are sometimes `None` (not missing, but present with null
    value — e.g. `save_variables` when no variables exist). A chain of
    ``d.get("a", {}).get("b")`` blows up on that case because
    ``None.get`` raises AttributeError. This helper treats any
    non-mapping intermediate as "empty" and returns default.

    >>> nested_get({"a": {"b": 1}}, "a", "b")
    1
    >>> nested_get({"a": None}, "a", "b")          # handles value=None
    >>> nested_get({}, "a", "b", default="x")
    'x'
    """
    cur = container
    for key in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(key)
    return cur if cur is not None else default


def read_cpu_serial() -> str:
    """Read the SoC serial from /proc/cpuinfo.

    Raspberry Pi exposes "Serial\t: ...". On other boards we fall back to
    the machine-id so fingerprints remain stable per host.
    """
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("serial"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    try:
        return Path("/etc/machine-id").read_text().strip()
    except OSError:
        return "0" * 16
