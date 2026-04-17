"""Handler for Prusa's `START_ENCRYPTED_DOWNLOAD` command.

This is how Connect delivers file uploads to modern (Buddy-firmware)
printers: Connect AES-128-CTR encrypts the gcode, hands the printer a
(key, iv) pair, and the printer fetches the ciphertext from
``/f/<iv_hex>/raw`` on the Connect host and decrypts to local storage.

Not in the SDK's Command enum (the SDK only has URL/CONNECT variants);
konnect's enum extension in :mod:`konnect.printer_types` adds it. See
``src/transfers/decrypt.{hpp,cpp}`` and ``src/connect/planner.cpp`` in
Prusa-Firmware-Buddy for the canonical algorithm:
  * URL: ``/f/<iv_as_hex>/raw``
  * Cipher: AES-128-CTR, key and iv = 16 bytes each
  * Sender block-pads to 16 bytes; we truncate by ``orig_size`` at write
"""
from __future__ import annotations

import logging
import os
import tempfile
from pathlib import Path
from threading import Thread
from typing import TYPE_CHECKING, Any

import requests
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

if TYPE_CHECKING:
    from .printer import KonnectPrinter

log = logging.getLogger(__name__)

BLOCK_SIZE = 16
# Request ciphertext in 64 KB chunks to keep memory bounded.
CHUNK_BYTES = 64 * 1024
DOWNLOAD_TIMEOUT_SEC = 600


class EncryptedDownloadError(Exception):
    """Any failure during fetch + decrypt + write."""


def start(
    printer: "KonnectPrinter",
    kwargs: dict[str, Any],
    mountpoint: str,
    local_root: str | os.PathLike,
) -> None:
    """Kick off an async decrypt-and-save for the given command kwargs.

    Must not block the command-dispatcher thread — downloads can be
    minutes long. Raises immediately on bad arguments or unsafe paths;
    the fetch itself happens on a daemon thread.
    """
    path = kwargs.get("path")
    iv_hex = kwargs.get("iv")
    key_hex = kwargs.get("key")
    orig_size = kwargs.get("orig_size")
    if not all(isinstance(x, str) and x for x in (path, iv_hex, key_hex)):
        raise EncryptedDownloadError(
            "missing path / iv / key in START_ENCRYPTED_DOWNLOAD",
        )
    try:
        key = bytes.fromhex(key_hex)
        iv = bytes.fromhex(iv_hex)
    except ValueError as exc:
        raise EncryptedDownloadError(f"bad hex in key/iv: {exc}") from exc
    if len(key) != BLOCK_SIZE or len(iv) != BLOCK_SIZE:
        raise EncryptedDownloadError(
            f"key+iv must be {BLOCK_SIZE} bytes each, "
            f"got key={len(key)} iv={len(iv)}",
        )
    if not isinstance(orig_size, int) or orig_size < 0:
        raise EncryptedDownloadError("orig_size must be a non-negative int")

    # Map Connect's `/usb/foo.gcode` → local filesystem path
    if not path.startswith(mountpoint + "/"):
        raise EncryptedDownloadError(
            f"path must start with {mountpoint}/ — got {path!r}",
        )
    rel = path[len(mountpoint) + 1:]
    local_root_p = Path(local_root).resolve()
    dest = (local_root_p / rel).resolve()
    try:
        dest.relative_to(local_root_p)
    except ValueError as exc:
        raise EncryptedDownloadError(f"path escapes root: {path}") from exc
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Buddy's host_and_port logic (src/connect/planner.cpp): when the
    # configured Connect port is 443/TLS, the download flips to plain
    # HTTP on port 80 — the CDN backing `/f/<iv>/raw` is a different
    # server from the API and doesn't serve HTTPS here. We mirror
    # that behaviour, deriving the host from printer.server.
    server = printer.server or "https://connect.prusa3d.com"
    from urllib.parse import urlparse
    parsed = urlparse(server)
    host = parsed.hostname or "connect.prusa3d.com"
    url = f"http://{host}/f/{iv_hex}/raw"
    token = printer.token or ""
    fingerprint = printer.fingerprint or ""

    log.info(
        "START_ENCRYPTED_DOWNLOAD: %s → %s (%d B) from %s",
        path, dest, orig_size, url,
    )

    thread = Thread(
        target=_run,
        args=(url, token, fingerprint, key, iv, orig_size, dest),
        name="konnect-enc-download",
        daemon=True,
    )
    thread.start()


def _run(
    url: str,
    token: str,
    fingerprint: str,
    key: bytes,
    iv: bytes,
    orig_size: int,
    dest: Path,
) -> None:
    """Fetch ciphertext → decrypt streaming → atomic-rename into place."""
    # Buddy's transfer request shape (src/transfers/download.cpp:203):
    #   GET http://<host>/f/<iv>/raw HTTP/1.1
    #   Host: <host>
    #   Content-Encryption-Mode: AES-CTR
    #   Range: bytes=0-<orig_size-1>
    # No Token / Fingerprint — the iv-in-URL is the capability token.
    # Range is non-optional; Connect uses presence of Range +
    # Content-Encryption-Mode as the signal that this is a printer
    # fetching encrypted content rather than a browser hitting the
    # home page.
    _ = token, fingerprint  # unused intentionally
    headers = {
        "User-Agent": "Buddy/6.4.19+6969",
        "Content-Encryption-Mode": "AES-CTR",
        "Range": f"bytes=0-{orig_size - 1}" if orig_size > 0 else "bytes=0-",
    }
    decryptor = Cipher(algorithms.AES(key), modes.CTR(iv)).decryptor()
    tmp_path = dest.with_suffix(dest.suffix + ".part")
    written = 0
    try:
        with requests.get(
            url,
            headers=headers,
            stream=True,
            timeout=DOWNLOAD_TIMEOUT_SEC,
        ) as resp:
            resp.raise_for_status()
            with open(tmp_path, "wb") as out:
                for chunk in resp.iter_content(CHUNK_BYTES):
                    if not chunk:
                        continue
                    plain = decryptor.update(chunk)
                    # Truncate pad at declared original size.
                    remaining = orig_size - written
                    if remaining <= 0:
                        break
                    if len(plain) > remaining:
                        plain = plain[:remaining]
                    out.write(plain)
                    written += len(plain)
                tail = decryptor.finalize()
                if tail and written < orig_size:
                    take = min(len(tail), orig_size - written)
                    out.write(tail[:take])
                    written += take
        if written != orig_size:
            raise EncryptedDownloadError(
                f"size mismatch: got {written}, expected {orig_size}",
            )
        # Atomic rename so a partial download never looks complete.
        os.replace(tmp_path, dest)
        log.info(
            "encrypted download complete: %s (%d B)", dest, written,
        )
    except Exception:
        log.exception("encrypted download failed for %s", dest)
        try:
            tmp_path.unlink()
        except OSError:
            pass
