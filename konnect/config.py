"""Configuration loading.

Settings live in two places:
  - `konnect.cfg` for static operator choices (printer type, ports, paths)
  - moonrakerdb namespace `konnect` for dynamic values mutated at runtime
    (serial number, Connect server/token, camera selection/token)

Rationale: printer_type changing requires a service restart anyway (it's
sent to Connect at registration time and caching would bite us), so it
belongs in the file. Tokens and camera selection change during normal
operation via the web UI / KlipperScreen and belong in the DB.
"""
from __future__ import annotations

import configparser
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from prusa.connect.printer.const import PrinterType

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATHS = [
    Path(os.environ.get("KONNECT_CONFIG", "")) if os.environ.get("KONNECT_CONFIG") else None,
    Path.home() / "printer_data" / "config" / "konnect.cfg",
    Path("/etc/konnect/konnect.cfg"),
]


# Default firmware string per printer_type. Connect's UIs gate some
# features on the reported firmware version (notably the upload button
# on modern dashboards is disabled for very old versions), so each
# type gets a plausible-looking version that unlocks its dashboard.
# The `+6969` suffix distinguishes konnect from real Prusa builds.
_FIRMWARE_BY_TYPE: dict[str, str] = {
    "HT90":    "1.3.19+6969",   # real HT90 releases look like 1.3.19
    "MK4S":    "6.4.19+6969",   # Buddy firmware scheme
    "COREONE": "6.4.19+6969",   # Buddy firmware scheme
    "MK4":     "6.4.19+6969",   # Buddy firmware scheme
    "XL":      "6.4.19+6969",   # Buddy firmware scheme (toolchanger)
}


@dataclass
class Config:
    # Which server dialect to speak:
    #   "cloud" — connect.prusa3d.com via the SDK's HTTPS long-polling
    #   "pcl"   — a self-hosted Connect-compatible server via the
    #             xBuddy WebSocket protocol (see konnect/pcl.py)
    backend: str = "cloud"
    # The custom server to register with (backend = pcl only). Required
    # for PCL because the onboarding SPA posts the cloud's address;
    # these override whatever the UI sends.
    pcl_host: str = ""
    pcl_port: int = 8080
    pcl_tls: bool = False
    # Whether server-sent raw G-code frames (type G/F) are forwarded to
    # Klipper via Moonraker's printer.gcode.script. Your own server on
    # your own LAN, so on by default; set false to refuse them.
    remote_gcode: bool = True
    # Debug: append every WebSocket frame (both directions, truncated)
    # to /tmp/konnect_ws.log. The protocol-level analogue of
    # KONNECT_HTTP_SPY.
    ws_spy: bool = False
    # HT90 is the default — the only type that gets the full workflow
    # (file listing + upload + Set Ready + chamber controls) in
    # Connect's UI. MK4S / COREONE are also supported but have a
    # known upload limitation. See konnect/web.py.
    # With backend = pcl the limitation disappears (uploads travel over
    # the WebSocket), and MK4S / COREONE are the recommended types.
    printer_type: PrinterType = PrinterType.HT90
    moonraker_host: str = "127.0.0.1"
    moonraker_port: int = 7125
    web_host: str = "0.0.0.0"  # noqa: S104 - matches HT90 upstream; bound behind nginx
    web_port: int = 7130
    local_gcode_path: str = str(Path.home() / "printer_data" / "gcodes")
    local_fs_name: str = "local"
    crowsnest_config: str = str(
        Path.home() / "printer_data" / "config" / "crowsnest.conf"
    )
    # How long to wait for a moonraker JSON-RPC response (seconds).
    moonraker_timeout: float = 2.0
    # Rotating file log, Klipper convention (lives next to klipper.log
    # / moonraker.log). Empty string disables the file handler — only
    # stdout/journald is used in that case.
    log_path: str = str(Path.home() / "printer_data" / "logs" / "konnect.log")
    # Override the firmware version string reported to Prusa Connect.
    # Empty (default) means "derive from printer_type" via
    # _FIRMWARE_BY_TYPE above. Set explicitly in konnect.cfg if you
    # need a specific string.
    firmware_version: str = ""

    @classmethod
    def load(cls, path: Path | str | None = None) -> "Config":
        cfg = cls()
        # argparse hands --config over as a str.
        candidates = [Path(path)] if path else DEFAULT_CONFIG_PATHS
        for candidate in candidates:
            if candidate and candidate.is_file():
                cfg._apply_file(candidate)
                log.info("Loaded config from %s", candidate)
                break
        else:
            log.info("No konnect.cfg found — using defaults")
        return cfg

    def _apply_file(self, path: Path) -> None:
        parser = configparser.ConfigParser()
        parser.read(path)
        if "konnect" not in parser:
            return
        section = parser["konnect"]
        backend = section.get("backend", self.backend).strip().lower()
        if backend in ("cloud", "pcl"):
            self.backend = backend
        else:
            log.error(
                "Invalid backend=%r in %s. Valid: cloud, pcl. "
                "Falling back to %s.", backend, path, self.backend,
            )
        self.pcl_host = section.get("pcl_host", self.pcl_host).strip()
        self.pcl_port = section.getint("pcl_port", self.pcl_port)
        self.pcl_tls = section.getboolean("pcl_tls", self.pcl_tls)
        self.remote_gcode = section.getboolean(
            "remote_gcode", self.remote_gcode,
        )
        self.ws_spy = section.getboolean("ws_spy", self.ws_spy)
        if self.backend == "pcl" and not self.pcl_host:
            log.warning(
                "backend = pcl but pcl_host is not set — registration "
                "will fail until pcl_host (and pcl_port / pcl_tls) are "
                "added to %s.", path,
            )
        if raw_type := section.get("printer_type"):
            try:
                self.printer_type = PrinterType[raw_type.strip().upper()]
            except KeyError:
                valid = ", ".join(t.name for t in PrinterType)
                log.error(
                    "Invalid printer_type=%r in %s. Valid: %s. Falling back to %s.",
                    raw_type, path, valid, self.printer_type.name,
                )
        self.moonraker_host = section.get("moonraker_host", self.moonraker_host)
        self.moonraker_port = section.getint("moonraker_port", self.moonraker_port)
        self.web_host = section.get("web_host", self.web_host)
        self.web_port = section.getint("web_port", self.web_port)
        self.local_gcode_path = _expand(
            section.get("local_gcode_path", self.local_gcode_path),
        )
        self.local_fs_name = section.get("local_fs_name", self.local_fs_name)
        self.crowsnest_config = _expand(
            section.get("crowsnest_config", self.crowsnest_config),
        )
        self.moonraker_timeout = section.getfloat(
            "moonraker_timeout", self.moonraker_timeout,
        )
        self.log_path = _expand(section.get("log_path", self.log_path))
        self.firmware_version = section.get(
            "firmware_version", self.firmware_version,
        ).strip()

    def effective_firmware(self) -> str:
        """Return the firmware string to report to Connect.

        If ``firmware_version`` is set in konnect.cfg, use it verbatim;
        otherwise fall back to the type-appropriate default. Returns an
        empty string only when we have neither — in which case the
        caller should fall back to Klippy's system_info value.
        """
        if self.firmware_version:
            return self.firmware_version
        return _FIRMWARE_BY_TYPE.get(self.printer_type.name, "")


def _expand(path: str) -> str:
    """Expand ~ and $VAR in a path — ConfigParser returns literal strings."""
    return os.path.expanduser(os.path.expandvars(path))
