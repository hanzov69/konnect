"""Custom Connect-server transport — the xBuddy WebSocket dialect.

Self-hosted Connect-compatible servers mount printers through the same
protocol real Buddy printers speak:

  1. HTTP registration on ``/p/register`` with ``Code`` / ``Token``
     headers — identical to the cloud, so the SDK's ``register()`` and
     ``Register`` poll objects work unchanged.
  2. An authenticated WebSocket on ``/p/ws`` (subprotocol
     ``prusa-connect``, ``Fingerprint`` + ``Token`` headers). The printer
     sends plain-JSON text frames (telemetry, events, inline-transfer
     requests); the server sends 9-byte-prefixed frames
     ``[type][8 hex id]`` where type J=JSON command, G/F=G-code,
     D=debug text, T=binary file chunk.

This module replaces the SDK's HTTPS long-polling loop with that
WebSocket session. Everything else — the queue the rest of konnect
feeds, the Command accept/finish lifecycle, the handlers — is reused
verbatim, so the SDK stays in charge of *what* is said and this module
only changes *how it travels*.

The big win over the cloud transport: uploads from the dashboard work.
The server pushes files with START_INLINE_DOWNLOAD + T chunks (no encrypted
``/f/<iv>/raw`` fetch gated behind a session we can't hold), so konnect
can finally receive files for MK4S / Core One dashboards.

Protocol reference: the printer side of Prusa-Firmware-Buddy
(src/connect/ — connect.cpp, command.cpp, websocket.hpp).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from queue import Empty
from threading import Event as ThreadEvent
from threading import Lock, Thread
from time import monotonic, sleep, time
from typing import TYPE_CHECKING, Any

import websocket
from prusa.connect.printer import const
from prusa.connect.printer.models import (
    CameraRegister,
    Event,
    Register,
    Telemetry,
)

if TYPE_CHECKING:
    from .printer import KonnectPrinter

log = logging.getLogger(__name__)

# How long recv() blocks before we get a tick to run housekeeping
# (transfer retries, stop checks). Also the granularity of shutdown.
RECV_TIMEOUT = 1.0
# Send a WebSocket ping if we had nothing to say for this long. The
# server's default read_deadline is 60s; telemetry normally re-arms it every 5s,
# but if Moonraker is down nothing is queued and only this keeps the
# session alive.
IDLE_PING = 20.0
# Reconnect backoff ceiling.
BACKOFF_MAX = 30.0
# Bytes we ask for per inline-transfer request. The server may answer with a
# smaller chunk (its per-write ceiling defaults to ~1 KiB); we adapt to
# whatever size it actually serves so the exchange stays one request →
# one frame.
CHUNK_REQUEST = 65536
# Re-request the current range if no chunk arrived for this long.
CHUNK_STALL = 5.0
CHUNK_RETRIES = 5


def _ws_url(server: str) -> str:
    """http(s):// server base → ws(s):// /p/ws endpoint."""
    if server.startswith("https://"):
        return "wss://" + server[len("https://"):] + "/p/ws"
    if server.startswith("http://"):
        return "ws://" + server[len("http://"):] + "/p/ws"
    return server.rstrip("/") + "/p/ws"


class InlineTransfer:
    """One server→printer file push (START_INLINE_DOWNLOAD + T chunks).

    Strictly sequential: one outstanding range request, bytes appended
    in arrival order. The server may answer a request with fewer bytes than
    asked (its write ceiling); we advance by what actually arrived and
    re-request from the new offset, shrinking our ask to the observed
    serving size so steady state is one request per frame.
    """

    def __init__(self, dest: Path, size: int, hash_: str, team_id: int,
                 transfer_id: int, to_print: bool):
        self.dest = dest
        self.size = size
        self.hash = hash_
        self.team_id = team_id
        self.transfer_id = transfer_id
        self.to_print = to_print
        self.offset = 0
        self.chunk = CHUNK_REQUEST
        self.requested_end = -1
        self.last_frame = monotonic()
        self.retries = 0
        self.tmp = dest.with_name("." + dest.name + ".konnect-part")
        self.tmp.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self.tmp, "wb")  # noqa: SIM115 - closed in finish/abort

    def request_payload(self) -> dict[str, Any]:
        want = min(self.chunk, self.size - self.offset)
        self.requested_end = self.offset + want - 1
        return {
            "transfer": "inline",
            "hash": self.hash,
            "team_id": self.team_id,
            "transfer_id": self.transfer_id,
            "file_id": self.transfer_id,
            "chunk": want,
            "start": self.offset,
            "end": self.requested_end,
        }

    def write(self, data: bytes) -> None:
        self._fh.write(data)
        self.offset += len(data)
        self.last_frame = monotonic()
        self.retries = 0
        # Server serves less than we ask? Ask for what it serves.
        if self.offset <= self.requested_end and len(data) < self.chunk:
            self.chunk = max(len(data), 1024)

    @property
    def done(self) -> bool:
        return self.offset >= self.size

    @property
    def range_fulfilled(self) -> bool:
        return self.offset > self.requested_end

    def finish(self) -> None:
        self._fh.close()
        os.replace(self.tmp, self.dest)

    def abort(self) -> None:
        try:
            self._fh.close()
        finally:
            self.tmp.unlink(missing_ok=True)


class PCLSession:
    """WebSocket session loop — drop-in replacement for SDK Printer.loop.

    Runs on the same thread the SDK loop would (konnect-sdk-loop) and
    spawns one sender thread per connection. Registration items still
    travel over HTTP exactly as the SDK does it, including before any
    WebSocket exists.
    """

    # One application frame must stay under the wire's 2^16 ceiling.
    FRAME_BUDGET = 60000
    SPY_PATH = "/tmp/konnect_ws.log"

    def __init__(self, printer: "KonnectPrinter"):
        self.printer = printer
        self.ws: websocket.WebSocket | None = None
        self.transfer: InlineTransfer | None = None
        self._transfer_lock = Lock()
        self._sender_stop = ThreadEvent()
        self._last_sent = monotonic()
        self._spy_enabled = bool(getattr(printer.cfg, "ws_spy", False))
        # Command ids whose ACCEPTED event must not reach the wire —
        # see IDEMPOTENT_COMMANDS and the sender loop.
        self._quiet_accept: set[int] = set()
        # STOP_TRANSFER goes through the SDK Transfer object; hook its
        # callback so the chunk pump notices.
        printer.transfer.stopped_cb = self._on_transfer_stopped

    def _spy(self, direction: str, payload: bytes | str) -> None:
        """Append one frame to the wire log (ws_spy = true in
        konnect.cfg). Truncated — size is what matters for the frame
        ceiling, the head is enough to identify the frame."""
        if not self._spy_enabled:
            return
        if isinstance(payload, bytes):
            head = payload[:8000].decode("utf-8", errors="replace")
        else:
            head = payload[:8000]
        try:
            with open(self.SPY_PATH, "a", encoding="utf-8") as fh:
                fh.write(f"{time():.3f} {direction} {len(payload)}B {head}\n")
        except OSError:
            pass

    # ---- main loop -------------------------------------------------------

    def loop(self) -> None:
        printer = self.printer
        backoff = 1.0
        while printer.running_loop:
            if not printer.token:
                self._poll_queue_http()
                continue
            try:
                self._connect()
                backoff = 1.0
                self._session()
            except Exception as exc:  # noqa: BLE001
                if printer.running_loop:
                    log.warning("Server session ended: %r — reconnecting in %.0fs",
                                exc, backoff)
            finally:
                self._close_ws()
                self._abort_transfer(const.Event.TRANSFER_ABORTED,
                                     "connection lost")
            # Interruptible backoff.
            deadline = monotonic() + backoff
            while printer.running_loop and monotonic() < deadline:
                sleep(0.2)
            backoff = min(backoff * 2, BACKOFF_MAX)

    def _connect(self) -> None:
        printer = self.printer
        url = _ws_url(printer.server or "")
        self.ws = websocket.create_connection(
            url,
            header={
                "Fingerprint": printer.fingerprint or "",
                "Token": printer.token or "",
            },
            subprotocols=["prusa-connect"],
            timeout=RECV_TIMEOUT,
            enable_multithread=True,
        )
        log.info("Connect server WebSocket established: %s", url)

    def _close_ws(self) -> None:
        if self.ws is not None:
            try:
                self.ws.close()
            except Exception:  # noqa: BLE001
                pass
            self.ws = None

    def _session(self) -> None:
        self._sender_stop = ThreadEvent()
        sender = Thread(target=self._send_loop, name="konnect-pcl-send",
                        daemon=True)
        sender.start()
        try:
            while self.printer.running_loop and self.ws is not None:
                try:
                    opcode, frame = self.ws.recv_data()
                except websocket.WebSocketTimeoutException:
                    self._tick_transfer()
                    continue
                self._handle_frame(opcode, frame)
        finally:
            self._sender_stop.set()
            sender.join(timeout=5)

    # ---- outgoing --------------------------------------------------------

    def _send_loop(self) -> None:
        """Drain printer.queue onto the WebSocket.

        Telemetry and events become plain JSON text frames. Register
        objects keep their HTTP semantics (poll /p/register until the
        operator approves the code). Camera registration is a cloud
        feature — PCL pulls camera streams itself — so those are
        dropped with a note.
        """
        printer = self.printer
        while not self._sender_stop.is_set() and printer.running_loop:
            try:
                item = printer.queue.get(timeout=0.5)
            except Empty:
                if monotonic() - self._last_sent > IDLE_PING:
                    try:
                        self.ws.ping()  # type: ignore[union-attr]
                        self._last_sent = monotonic()
                    except Exception:  # noqa: BLE001
                        return  # socket is gone; recv loop will notice
                continue

            if isinstance(item, Telemetry):
                if not self._send_json(item.to_payload()):
                    return  # telemetry is periodic — fine to drop one
            elif isinstance(item, Event):
                # Query commands are answered by their data event alone
                # (INFO, FILE_INFO, …) — real firmware sends no ACCEPTED
                # for them, and the server takes the first event bearing
                # the command id as THE answer. An ACCEPTED would be
                # consumed as a dataless answer ("the printer reported
                # nothing about that path") and the data event ignored.
                if (item.event is const.Event.ACCEPTED
                        and item.command_id in self._quiet_accept):
                    self._quiet_accept.discard(item.command_id)
                    continue
                if not self._send_json(item.to_payload()):
                    printer.queue.put(item)  # events must not get lost
                    return
            elif isinstance(item, Register):
                self._register_poll(item)
            elif isinstance(item, CameraRegister):
                log.info("Dropping camera registration — the server pulls "
                         "streams directly; declare a camera resource "
                         "there instead.")
            else:
                log.warning("Unknown queue item: %s", item)

    def send_json(self, payload: dict[str, Any]) -> bool:
        """Public send used by the transfer pump (thread-safe via
        enable_multithread)."""
        return self._send_json(payload)

    def _send_json(self, payload: dict[str, Any]) -> bool:
        ws = self.ws
        if ws is None:
            return False
        text = json.dumps(payload)
        if len(text) > self.FRAME_BUDGET:
            log.warning(
                "Outbound frame is %d bytes — over the wire's 64KiB "
                "ceiling; the server may drop or choke on it (%s)",
                len(text), text[:120],
            )
        self._spy("OUT", text)
        try:
            ws.send(text)
            self._last_sent = monotonic()
            return True
        except Exception as exc:  # noqa: BLE001
            log.warning("Send failed: %r", exc)
            return False

    # ---- registration over HTTP (same semantics as SDK loop_step) --------

    def _poll_queue_http(self) -> None:
        printer = self.printer
        try:
            item = printer.queue.get(timeout=0.5)
        except Empty:
            return
        if isinstance(item, Register):
            self._register_poll(item)
        else:
            log.debug("No token yet — dropping queue item %s", item)

    def _register_poll(self, item: Register) -> None:
        printer = self.printer
        try:
            headers = printer.make_headers(item.timestamp)
            res = item.send(printer.conn, printer.server, headers)
        except Exception as exc:  # noqa: BLE001
            log.warning("Registration poll failed: %r", exc)
            sleep(1)
            printer.queue.put(item)
            return
        if res.status_code == 200:
            printer.token = res.headers["Token"]
            log.info("Registration approved — token received.")
            printer.register_handler(printer.token)
            printer.code = None
        elif res.status_code in (202, 204) and item.timeout > time():
            printer.queue.put(item)
            sleep(1)
        else:
            log.warning("Registration poll: unexpected %s %s",
                        res.status_code, res.text[:200])

    # ---- incoming --------------------------------------------------------

    def _handle_frame(self, opcode: int, frame: bytes) -> None:
        self._spy("IN ", frame)
        if opcode == websocket.ABNF.OPCODE_BINARY:
            if len(frame) < 9 or frame[0:1] != b"T":
                log.warning("Unparseable binary frame (%d bytes)", len(frame))
                return
            ident = int(frame[1:9], 16)
            self._on_chunk(ident, frame[9:])
            return

        if opcode != websocket.ABNF.OPCODE_TEXT:
            return
        text = frame.decode("utf-8", errors="replace")
        if len(text) < 9:
            log.warning("Short text frame: %r", text)
            return
        kind = text[0]
        try:
            ident = int(text[1:9], 16)
        except ValueError:
            log.warning("Bad frame id: %r", text[:16])
            return
        body = text[9:]

        if kind == "J":
            self._on_command(ident, body)
        elif kind in ("G", "F"):
            self._on_gcode(ident, body, force=kind == "F")
        elif kind == "D":
            log.debug("PCL debug: %s", body)
        else:
            log.warning("Unknown frame type %r", kind)

    # Query commands a server may retry with the same id (e.g. after its
    # own wait timed out). The SDK answers a duplicate id with a bare
    # FINISHED and no payload — "the printer reported nothing" on the
    # server — so these are simply re-run instead.
    IDEMPOTENT_COMMANDS = frozenset({
        "SEND_INFO", "SEND_FILE_INFO", "SEND_JOB_INFO",
        "SEND_STATE_INFO", "SEND_TRANSFER_INFO",
    })

    def _on_command(self, command_id: int, body: str) -> None:
        printer = self.printer
        try:
            data = json.loads(body)
            command_name = data.get("command", "")
            log.info("Server command %d: %s %s", command_id, command_name,
                     data.get("kwargs") or "")
            if (printer.command.state is None
                    and printer.command.command_id == command_id
                    and command_name in self.IDEMPOTENT_COMMANDS):
                printer.command.command_id = -1  # take the retry as new
            if printer.command.check_state(command_id, command_name):
                if command_name in self.IDEMPOTENT_COMMANDS:
                    # Marked before accept() queues the ACCEPTED event —
                    # the sender drops it; see _send_loop.
                    self._quiet_accept.add(command_id)
                printer.command.accept(command_id,
                                       command_name=command_name,
                                       args=data.get("args"),
                                       kwargs=data.get("kwargs"))
        except Exception as exc:  # noqa: BLE001
            log.exception("Failed to accept command %d", command_id)
            printer.event_cb(const.Event.REJECTED, const.Source.CONNECT,
                             command_id=command_id, reason=str(exc))

    def _on_gcode(self, command_id: int, gcode: str, force: bool) -> None:
        printer = self.printer
        command_name = const.Command.GCODE.value
        if printer.command.check_state(command_id, command_name):
            printer.command.accept(command_id, command_name, [gcode],
                                   {"gcode": gcode}, force=force)

    # ---- inline transfer -------------------------------------------------

    def start_inline_download(self, kwargs: dict[str, Any],
                              command_id: int) -> dict[str, Any]:
        """Begin a PCL→printer file push. Called from the command thread
        by the START_INLINE_DOWNLOAD handler; returns the TRANSFER_INFO
        kwargs for Command.finish."""
        printer = self.printer
        path = kwargs.get("path") or ""
        size = int(kwargs.get("orig_size") or 0)
        if not path or size <= 0:
            raise RuntimeError("START_INLINE_DOWNLOAD requires path and "
                               "orig_size")
        dest = self._resolve_dest(path)
        if dest is None:
            raise RuntimeError(f"Path outside storage: {path}")

        # The SDK Transfer object carries progress into telemetry for free.
        retval = printer.transfer.start(
            const.TransferType.FROM_CONNECT,
            path,
            to_print=bool(kwargs.get("printing", False)),
            to_select=bool(kwargs.get("selecting", False)),
            start_cmd_id=command_id,
            hash_=kwargs.get("hash"),
            team_id=kwargs.get("team_id"),
        )
        printer.transfer.size = size
        printer.transfer.start_ts = time()

        with self._transfer_lock:
            self.transfer = InlineTransfer(
                dest=dest,
                size=size,
                hash_=kwargs.get("hash") or "",
                team_id=int(kwargs.get("team_id") or 0),
                transfer_id=printer.transfer.transfer_id,
                to_print=bool(kwargs.get("printing", False)),
            )
            self.send_json(self.transfer.request_payload())
        log.info("Inline transfer %d started: %s (%d bytes)",
                 printer.transfer.transfer_id, path, size)
        return retval

    def _resolve_dest(self, req_path: str) -> Path | None:
        """Map /usb/… (modern) or /<local_fs_name>/… (legacy) onto the
        local gcodes directory. Accepts the server's unslashed
        storage-prefixed form (usb/…) too."""
        if req_path and not req_path.startswith("/"):
            req_path = "/" + req_path
        cfg = self.printer.cfg
        root = Path(cfg.local_gcode_path)
        legacy = f"/{cfg.local_fs_name}"
        if req_path == legacy or req_path.startswith(legacy + "/"):
            req_path = "/usb" + req_path[len(legacy):]
        # Deliberately does not require existence — this is a write path.
        if not req_path.startswith("/usb"):
            return None
        tail = req_path[len("/usb"):].lstrip("/")
        if not tail:
            return None
        target = (root / tail)
        try:
            target.resolve().relative_to(root.resolve())
        except (OSError, ValueError):
            return None
        return target

    def _on_chunk(self, ident: int, data: bytes) -> None:
        printer = self.printer
        with self._transfer_lock:
            transfer = self.transfer
            if transfer is None or ident != transfer.transfer_id:
                log.warning("T frame for unknown transfer %d (%d bytes)",
                            ident, len(data))
                return
            transfer.write(data)
            printer.transfer.transferred = transfer.offset
            if transfer.done:
                transfer.finish()
                self.transfer = None
            elif transfer.range_fulfilled:
                self.send_json(transfer.request_payload())
                return
            else:
                return  # more frames expected for the current range

        # Done — outside the lock: events + optional print start.
        log.info("Inline transfer %d finished: %s", ident, transfer.dest)
        printer.event_cb(const.Event.TRANSFER_FINISHED, const.Source.CONNECT,
                         transfer_id=ident,
                         destination=printer.transfer.path)
        to_print = transfer.to_print
        printer.transfer.type = const.TransferType.NO_TRANSFER
        if to_print:
            rel = str(transfer.dest.relative_to(
                Path(printer.cfg.local_gcode_path)))
            printer.moonraker("printer.print.start",
                              {"filename": rel.replace(os.sep, "/")})

    def _tick_transfer(self) -> None:
        """Housekeeping on recv timeout: nudge a stalled transfer."""
        with self._transfer_lock:
            transfer = self.transfer
            if transfer is None:
                return
            if monotonic() - transfer.last_frame < CHUNK_STALL:
                return
            transfer.retries += 1
            if transfer.retries > CHUNK_RETRIES:
                ident = transfer.transfer_id
                transfer.abort()
                self.transfer = None
            else:
                log.info("Transfer %d stalled — re-requesting from %d",
                         transfer.transfer_id, transfer.offset)
                transfer.last_frame = monotonic()
                self.send_json(transfer.request_payload())
                return
        self._emit_transfer_end(const.Event.TRANSFER_ABORTED, ident,
                                "no chunks from server")

    def _on_transfer_stopped(self) -> None:
        """SDK Transfer.stop() callback (STOP_TRANSFER command)."""
        self._abort_transfer(const.Event.TRANSFER_STOPPED, "stopped")

    def _abort_transfer(self, event: const.Event, reason: str) -> None:
        with self._transfer_lock:
            transfer = self.transfer
            if transfer is None:
                return
            ident = transfer.transfer_id
            transfer.abort()
            self.transfer = None
        self._emit_transfer_end(event, ident, reason)

    def _emit_transfer_end(self, event: const.Event, ident: int,
                           reason: str) -> None:
        printer = self.printer
        log.warning("Inline transfer %d ended: %s", ident, reason)
        printer.event_cb(event, const.Source.CONNECT, transfer_id=ident,
                         reason=reason)
        printer.transfer.type = const.TransferType.NO_TRANSFER
