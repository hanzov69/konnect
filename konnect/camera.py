"""Crowsnest-backed CameraDriver for Prusa Connect.

Crowsnest (https://github.com/mainsail-crew/crowsnest) is the standard
MainsailOS webcam stack. Each `[cam <name>]` section in crowsnest.conf
spawns a ustreamer or camera-streamer process on a port; snapshots are
available at `http://HOST:PORT/?action=snapshot` (ustreamer) or
`http://HOST:PORT/snapshot` (camera-streamer).

We discover available cams by parsing crowsnest.conf, and register the
one the user picked (stored in moonrakerdb as konnect.camera.snapshot_url).
"""
from __future__ import annotations

import configparser
import logging
import re
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Callable

import requests
from prusa.connect.printer.camera_driver import CameraDriver
from prusa.connect.printer.const import CapabilityType

log = logging.getLogger(__name__)

# Defaults per the Crowsnest docs. We list a few common ones so we can
# guess a working snapshot URL from port alone when the user only has
# the port number.
CROWSNEST_SNAPSHOT_PATHS = ("/?action=snapshot", "/snapshot")


@dataclass
class DiscoveredCamera:
    name: str
    port: int
    mode: str  # mjpg, ustreamer, camera-streamer, multi
    snapshot_url: str  # fully-qualified URL we'll POST to Connect

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "port": self.port,
            "mode": self.mode,
            "snapshot_url": self.snapshot_url,
        }


def discover_crowsnest_cameras(
    config_path: str | Path,
    host: str = "127.0.0.1",
) -> list[DiscoveredCamera]:
    """Parse crowsnest.conf and return its declared cams.

    Crowsnest's config uses ConfigParser-compatible INI with
    `[cam <name>]` sections. We extract (name, mode, port) and build a
    candidate snapshot URL. If a cam section is missing from the config
    file we skip it silently — the user can still enter a URL manually
    via the web UI.
    """
    path = Path(config_path)
    if not path.is_file():
        log.info("crowsnest.conf not found at %s", path)
        return []

    parser = configparser.ConfigParser(
        inline_comment_prefixes=("#", ";"),
        strict=False,
    )
    try:
        parser.read(path)
    except configparser.Error:
        log.exception("Failed to parse %s", path)
        return []

    cams = []
    for section in parser.sections():
        match = re.match(r"cam\s+(.+)$", section, re.IGNORECASE)
        if not match:
            continue
        name = match.group(1).strip()
        mode = parser.get(section, "mode", fallback="mjpg").strip().lower()
        try:
            port = parser.getint(section, "port")
        except (configparser.NoOptionError, ValueError):
            log.warning("crowsnest cam %s has no port — skipping", name)
            continue

        # ustreamer uses /?action=snapshot; camera-streamer uses /snapshot.
        snap_path = (
            "/snapshot" if mode == "camera-streamer" else "/?action=snapshot"
        )
        cams.append(DiscoveredCamera(
            name=name,
            port=port,
            mode=mode,
            snapshot_url=f"http://{host}:{port}{snap_path}",
        ))
    return cams


def discover_moonraker_webcams(
    moonraker,
    external_host: str | None = None,
) -> list[DiscoveredCamera]:
    """Webcams registered in Moonraker (``server.webcams.list``).

    The canonical registry on stacks without crowsnest.conf — e.g. the
    Snapmaker U1 extended firmware announces its v4l2-mpp camera as
    ``[webcam case]`` with ``snapshot_url: /webcam/snapshot.jpg``.
    Relative and loopback URLs are rewritten against external_host so
    the result is reachable from another machine (what a Prusa Connect
    Local server needs to pull snapshots).
    """
    try:
        result = moonraker("server.webcams.list", timeout=2.0)
    except Exception:  # noqa: BLE001
        log.exception("server.webcams.list failed")
        return []
    if not result:
        return []
    cams = []
    for cam in result.get("webcams", []):
        if cam.get("enabled") is False:
            continue
        snapshot = (cam.get("snapshot_url") or "").strip()
        if not snapshot:
            continue
        cams.append(DiscoveredCamera(
            name=cam.get("name") or "webcam",
            port=0,
            mode=cam.get("service") or "moonraker",
            snapshot_url=externalize_url(snapshot, external_host),
        ))
    return cams


def externalize_url(url: str, external_host: str | None) -> str:
    """Make a Moonraker-relative or loopback URL reachable from outside.

    ``/webcam/snapshot.jpg`` is relative to the printer's web root
    (nginx on port 80); ``http://127.0.0.1:8080/…`` is reachable only
    on the printer itself. Both are rewritten onto external_host (a
    Host header value; any port is dropped for the relative case and
    kept for the loopback case).

    >>> externalize_url("/webcam/snapshot.jpg", "printer.lan:7130")
    'http://printer.lan/webcam/snapshot.jpg'
    >>> externalize_url("http://127.0.0.1:8080/snap", "printer.lan")
    'http://printer.lan:8080/snap'
    >>> externalize_url("http://cam.lan/snap", "printer.lan")
    'http://cam.lan/snap'
    """
    if not external_host:
        return url
    hostname = external_host.rsplit(":", 1)[0] if not external_host.startswith(
        "[") else external_host.split("]")[0] + "]"
    if url.startswith("/"):
        return f"http://{hostname}{url}"
    return re.sub(
        r"//(127\.0\.0\.1|localhost)(?=[:/]|$)", f"//{hostname}", url,
    )


class CrowsnestDriver(CameraDriver):
    """SDK-compatible driver: downloads a JPEG from Crowsnest on demand."""
    name = "crowsnest"
    REQUEST_TIMEOUT = 4.0

    def __init__(
        self,
        camera_id: str,
        config: dict[str, str],
        store_cb: Callable[[str], None],
    ):
        # The SDK's CameraDriver.__init__ stashes config under .config
        # and exposes .config_key etc. We pass a no-op store_cb upstream
        # because we persist via moonrakerdb in the outer service.
        super().__init__(camera_id, config, lambda _: None)
        self.store_cb = store_cb
        self.snapshot_url: str = config.get("snapshot_url", "")

    def _connect(self) -> None:
        self._capabilities = {
            CapabilityType.TRIGGER_SCHEME,
            CapabilityType.IMAGING,
        }

    def take_a_photo(self) -> bytes | None:
        if not self.snapshot_url:
            log.error("snapshot_url not configured")
            return None
        try:
            res = requests.get(self.snapshot_url, timeout=self.REQUEST_TIMEOUT)
            if res.status_code == HTTPStatus.OK.value:
                return res.content
            log.warning(
                "snapshot %s returned %s", self.snapshot_url, res.status_code,
            )
        except requests.RequestException:
            log.exception("snapshot request failed for %s", self.snapshot_url)
        return None
