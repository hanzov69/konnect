"""Extend the SDK's closed `PrinterType` enum with newer Prusa models.

The SDK's `prusa.connect.printer.const.PrinterType` (as of 0.8.1 /
master at 2025-04) only knows:
  I3MK25, I3MK25S, I3MK3, I3MK3S, SL1, SL1S, M1, HT90

Prusa's own Buddy firmware advertises additional models — the tuples
below come directly from their published source:
  github.com/prusa3d/Prusa-Firmware-Buddy/blob/master/include/common/
    printer_model_data.hpp

Connect's server-side whitelist accepts these tuples too; we just
need the enum in our process to carry the new members so SDK
internals (`.value` unpacking, `str()` for User-Agent-Printer, etc.)
work correctly when we pass them.

We can't subclass the enum (the SDK does isinstance() checks against
the original class), so we inject members into the existing
PrinterType using the private `_member_map_`/`_value2member_map_` and
bypass Enum's locked __setattr__ via `type.__setattr__`. This is
deliberately fragile — if the stdlib Enum internals change we'll need
to adapt — but it's the cleanest stdlib-only option.
"""
from __future__ import annotations

from prusa.connect.printer.const import PrinterType

# Source: Prusa-Firmware-Buddy/include/common/printer_model_data.hpp
# Format: (name, (type, version, subversion))
_EXTENSIONS: tuple[tuple[str, tuple[int, int, int]], ...] = (
    # MK3.x line — same hardware platform evolution
    ("MK3_5",    (1, 3, 5)),
    ("MK3_5S",   (1, 3, 6)),
    ("MK3_9",    (1, 3, 9)),
    ("MK3_9S",   (1, 3, 10)),
    # MK4 / MK4S — Buddy-platform i3
    ("MK4",      (1, 4, 0)),
    ("MK4S",     (1, 4, 1)),
    # Core One — Prusa's CoreXY with actively heated chamber
    ("COREONE",  (7, 1, 0)),
    # Additional models, unlikely but accepted server-side
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
    # Enum's own __setattr__ refuses member assignment, so go through
    # the base type to register the attribute for dot-access
    # (PrinterType.MK4S).
    type.__setattr__(PrinterType, name, member)


def install() -> None:
    """Add every extension member to PrinterType. Call once, early."""
    for name, value in _EXTENSIONS:
        _extend(name, value)


# Auto-install on import — the whole point of this module.
install()
