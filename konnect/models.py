"""Telemetry / Job / Info / NetworkInfo models.

Trimmed from prusa.connect.ht90.models: drop HT90-specific Tool flags
(ObXidian / HF nozzle kinds — Prusa-specific SKUs) and keep only the
fields Connect needs from a generic Klipper printer. The shape of each
model still matches what the SDK expects to receive in telemetry() /
event_cb(event=INFO) calls.
"""
from __future__ import annotations

from dataclasses import dataclass, fields
from typing import Any

from prusa.connect.printer.const import Event, Source

TEMP_DIFF_TH = 0.5  # Only consider a temp changed if it moves >0.5°C


@dataclass
class Tool:
    """Minimal tool metadata — just nozzle diameter + loaded material.

    HT90 additionally tracks hardened/high-flow nozzle variants because
    its save_variables hold a canonical "nozzle" string. Generic Klipper
    doesn't, so we stay conservative: if the user has a save_variables
    entry with a parseable diameter we pick it up; otherwise we leave it
    None and Connect just doesn't display that field.
    """
    nozzle_diameter: float | None = None
    material: str | None = None

    @classmethod
    def from_variables(
        cls,
        nozzle_str: str | None,
        material: str | None,
    ) -> "Tool":
        tool = cls(material=material)
        if nozzle_str:
            try:
                # Accept "0.4", "0.4 mm", "0.4 HF" — take the leading float.
                tool.nozzle_diameter = float(str(nozzle_str).split()[0])
            except (ValueError, IndexError):
                pass
        return tool

    def update(self, other: "Tool") -> bool:
        diff = 0
        for field in fields(self):
            oval = getattr(other, field.name)
            if oval is not None and getattr(self, field.name) != oval:
                setattr(self, field.name, oval)
                diff += 1
        return bool(diff)

    def data(self) -> dict[str, Any]:
        return {
            "nozzle_diameter": self.nozzle_diameter,
            "material": self.material,
        }


class Telemetry:
    """Printer telemetry, matching what Connect's /p/telemetry accepts."""
    fan_extruder: int | None = None
    fan_print: int | None = None
    fan_chamber: int | None = None
    temp_bed: float | None = None
    target_bed: float | None = None
    temp_nozzle: float | None = None
    target_nozzle: float | None = None
    temp_chamber: float | None = None
    target_chamber: float | None = None
    progress: float | None = None
    speed: int | None = None
    flow: int | None = None
    filament: float | None = None
    time_printing: int | None = None
    time_remaining: int | None = None
    material: str | None = None
    axis_x: float | None = None
    axis_y: float | None = None
    axis_z: float | None = None

    TEMP_VALUES = ("temp_bed", "temp_nozzle", "temp_chamber")
    OTHER_VALUES = (
        "fan_extruder", "fan_print", "target_bed", "target_nozzle",
        "progress", "filament", "time_printing", "time_remaining",
        "material", "target_chamber", "fan_chamber", "axis_x", "axis_y",
        "axis_z", "speed", "flow",
    )
    CHAMBER_VALUES = ("temp_chamber", "target_chamber", "fan_chamber")

    def update(self, other: "Telemetry") -> bool:
        """Copy non-None fields from `other`. Returns True if anything changed.

        Temperatures only count as "changed" if they moved more than
        TEMP_DIFF_TH — avoids flooding Connect with 0.01°C jitter.
        """
        diff = 0
        for item in self.TEMP_VALUES:
            oval = getattr(other, item)
            sval = getattr(self, item)
            if oval is not None and (sval is None or abs(sval - oval) > TEMP_DIFF_TH):
                setattr(self, item, oval)
                diff += 1
        for item in self.OTHER_VALUES:
            oval = getattr(other, item)
            if oval is not None and getattr(self, item) != oval:
                setattr(self, item, oval)
                diff += 1
        return bool(diff)

    def data(self) -> dict[str, Any]:
        """Serialize for Printer.telemetry()."""
        data: dict[str, Any] = {}
        root = tuple(
            v for v in (self.TEMP_VALUES + self.OTHER_VALUES)
            if v not in self.CHAMBER_VALUES
        )
        for item in root:
            data[item] = getattr(self, item)
        # Chamber values are nested under `chamber` if present.
        if any(getattr(self, v) is not None for v in self.CHAMBER_VALUES):
            data["chamber"] = {
                "temp": self.temp_chamber,
                "target_temp": self.target_chamber,
                "fan_rpm": self.fan_chamber,
            }
        return data


class Job:
    """Print job state (history_changed + start_cmd_id correlation)."""
    id: int | None = None
    path: str | None = None
    start_cmd_id: int | None = None
    m_timestamp: int | None = None
    size: int | None = None
    mbl: list[int] | None = None

    def reset(self, job: dict) -> None:
        metadata = job.get("metadata", {})
        # Moonraker job_id is a hex string; Connect wants int.
        job_id = job.get("job_id")
        self.id = int(job_id, 16) if job_id else None
        self.path = "/" + job.get("filename", "")
        self.size = metadata.get("size")
        self.m_timestamp = metadata.get("modified")

    def data(self, root: str = "") -> dict[str, Any]:
        return {
            "path": root + (self.path or ""),
            "start_cmd_id": self.start_cmd_id,
            "m_timestamp": self.m_timestamp,
            "size": self.size,
            "mbl": self.mbl,
        }


class Info:
    """INFO-event payload — optional extras beyond what the SDK sends."""
    tool: Tool

    def __init__(self):
        self.tool = Tool()

    def update(self, other: "Info") -> bool:
        return self.tool.update(other.tool)

    def data(self, source: Source = Source.FIRMWARE) -> dict[str, Any]:
        return {
            "source": source,
            "event": Event.INFO,
            "tools": {"1": self.tool.data()},
            "slots": 1,
        }
