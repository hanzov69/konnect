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


@dataclass
class Config:
    # HT90 is the only officially-supported type in the UI right now
    # (see konnect/web.py SUPPORTED_PRINTER_TYPES for rationale).
    # The stock SDK already ships with HT90 — no enum extension needed.
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
    # Empty = use Klippy's reported OS distro version. Default is
    # "1.3.19+6969" which matches the HT90 firmware numbering
    # convention (real HT90 firmware releases look like "1.3.19") with
    # a distinguishing suffix so Connect's Firmware field shows a
    # plausible HT90-style string.
    firmware_version: str = "1.3.19+6969"

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        cfg = cls()
        candidates = [path] if path else DEFAULT_CONFIG_PATHS
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


def _expand(path: str) -> str:
    """Expand ~ and $VAR in a path — ConfigParser returns literal strings."""
    return os.path.expanduser(os.path.expandvars(path))
