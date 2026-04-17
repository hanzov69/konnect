"""Extend the SDK's closed `PrinterType` enum with newer Prusa models.

Currently inert — the only supported printer type is HT90, which is
already in the stock SDK (`I3MK25`, `I3MK25S`, `I3MK3`, `I3MK3S`,
`SL1`, `SL1S`, `M1`, `HT90`). Connect's dashboard UIs for non-HT90
types either lack file-browser support via the legacy SDK `files`
tree or don't accept SET_PRINTER_READY commands, so exposing MK4S /
Core One / etc. wouldn't gain anything functionally.

This module is retained with the machinery below so the decision is
reversible: if Prusa publishes a spec for the Buddy-firmware file
protocol (or Connect starts honoring legacy `files` for MK4-family
types) we can call `install()` from `__main__.py` to re-enable the
extensions.

Mechanism: the SDK does isinstance() checks against `PrinterType`, so
subclassing doesn't work. We inject members into the existing enum
via the private `_member_map_` / `_value2member_map_` and bypass
`Enum.__setattr__` using `type.__setattr__`. Tuples come from
github.com/prusa3d/Prusa-Firmware-Buddy/blob/master/include/common/
  printer_model_data.hpp
"""
from __future__ import annotations

from prusa.connect.printer.const import PrinterType

# (name, (type, version, subversion)) — NOT installed by default.
# To re-enable, call install() from __main__ before any SDK use.
_EXTENSIONS: tuple[tuple[str, tuple[int, int, int]], ...] = (
    ("MK3_5",    (1, 3, 5)),
    ("MK3_5S",   (1, 3, 6)),
    ("MK3_9",    (1, 3, 9)),
    ("MK3_9S",   (1, 3, 10)),
    ("MK4",      (1, 4, 0)),
    ("MK4S",     (1, 4, 1)),
    ("COREONE",  (7, 1, 0)),
    ("MINI",     (2, 1, 0)),
    ("XL",       (3, 1, 0)),
    ("IX",       (4, 1, 0)),
)


def _extend(name: str, value: tuple[int, int, int]) -> None:
    """Inject a single member into PrinterType. Idempotent."""
    if name in PrinterType._member_map_:
        return
    member = object.__new__(PrinterType)
    member._name_ = name  # noqa: SLF001
    member._value_ = value  # noqa: SLF001
    PrinterType._member_map_[name] = member
    PrinterType._value2member_map_[value] = member
    PrinterType._member_names_.append(name)
    type.__setattr__(PrinterType, name, member)


def install() -> None:
    """Add every extension member to PrinterType."""
    for name, value in _EXTENSIONS:
        _extend(name, value)


# No auto-install. Call install() explicitly if you want MK4S/COREONE.
