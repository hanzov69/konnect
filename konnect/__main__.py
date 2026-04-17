"""CLI entry: `python -m konnect` / `konnect`."""
from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from argparse import ArgumentParser
from contextlib import suppress
from pathlib import Path
from threading import Event

from prusa.connect.printer import __version__ as sdk_version
from prusa.connect.printer import const

from . import __version__
from .config import Config
from .printer import KonnectPrinter
from .web import run_in_thread

LOG_FORMAT = (
    "%(asctime)s %(levelname)s {%(module)s.%(funcName)s():%(lineno)d} "
    "[%(threadName)s]: %(message)s"
)
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def _setup_logging(debug: bool, log_path: Path | None) -> None:
    """Attach a stdout handler (→ journald) and an optional rotating
    file handler at the configured path (klipper convention).

    Stdout is always attached so `journalctl -u konnect` keeps working
    for systemd-style debugging. The file handler lets users tail
    ~/printer_data/logs/konnect.log alongside klipper.log / moonraker.log.
    """
    root = logging.root
    root.setLevel(logging.DEBUG if debug else logging.INFO)

    fmt = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    stdout = logging.StreamHandler(sys.stdout)
    stdout.setFormatter(fmt)
    root.addHandler(stdout)

    if log_path is not None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            # 10 MB × 3 rollovers — plenty for konnect's modest logging,
            # matches the per-file cap Moonraker uses by default.
            file_handler = logging.handlers.RotatingFileHandler(
                log_path,
                maxBytes=10 * 1024 * 1024,
                backupCount=3,
            )
            file_handler.setFormatter(fmt)
            root.addHandler(file_handler)
        except OSError as exc:
            # Don't let a log-file problem keep the service from starting —
            # stdout/journald will still capture everything.
            logging.getLogger(__name__).warning(
                "Could not open log file %s: %s (falling back to journald only)",
                log_path, exc,
            )

    if debug:
        logging.getLogger("connect-printer").setLevel(logging.DEBUG)


def main() -> int:
    parser = ArgumentParser(
        prog="konnect",
        description="Prusa Connect bridge for generic Klipper/Moonraker printers.",
    )
    parser.add_argument("-d", "--debug", action="store_true")
    parser.add_argument("--version", action="store_true")
    parser.add_argument("--config", type=str, default=None)
    args = parser.parse_args()

    if args.version:
        print(f"konnect: {__version__}")
        print(f"prusa-connect-sdk: {sdk_version}")
        return 0

    # Load config FIRST (uses print() / stderr for its own tracing) so
    # we know where to put logs, then wire logging.
    cfg = Config.load(args.config)
    log_path = Path(cfg.log_path) if cfg.log_path else None
    _setup_logging(debug=args.debug, log_path=log_path)

    log = logging.getLogger(__name__)
    log.info(
        "konnect %s starting (sdk %s, pid %s, log → %s)",
        __version__, sdk_version, os.getpid(), log_path or "stdout only",
    )

    # Shorter SDK HTTP timeout — registration polling feels laggy with default.
    const.CONNECTION_TIMEOUT = 5

    printer = KonnectPrinter(cfg)
    printer.start()
    run_in_thread(printer, debug=args.debug)

    event = Event()
    with suppress(KeyboardInterrupt):
        event.wait()

    printer.stop()
    printer.wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
