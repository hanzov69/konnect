"""Moonraker WebSocket client (JSON-RPC).

Adapted from prusa.connect.ht90.moonraker: same request/response correlation
pattern, same reconnect loop. Exposed as a thread-safe callable; callers get
the raw `result` dict back (or None on timeout/disconnect).
"""
from __future__ import annotations

import json
import logging
from queue import Queue
from threading import Event, Lock

import websocket

from .util import InfinityThread

log = logging.getLogger(__name__)


class Response:
    result: dict | None = None

    def __init__(self):
        self.ev = Event()


class Moonraker:
    """Blocking JSON-RPC client with a push queue for notify_* events."""

    def __init__(
        self,
        host: str,
        port: int,
        event_queue: Queue,
        default_timeout: float = 2.0,
    ):
        self.host = host
        self.port = port
        self.queue = event_queue
        self.default_timeout = default_timeout
        self._server = f"ws://{host}:{port}/websocket"
        self._connected = False
        self._req_id = 0
        self._responses: dict[int, Response] = {}
        self._lock = Lock()
        self.ws = websocket.WebSocketApp(
            self._server,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._thread = InfinityThread(
            target=self.ws.run_forever,
            kwargs={"reconnect": 1},
            name="moonraker-ws",
        )

    @property
    def connected(self) -> bool:
        return self._connected

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._thread.stop()
        try:
            self.ws.close()
        except Exception:  # noqa: BLE001
            pass

    def wait(self) -> None:
        self._thread.join()

    def _on_open(self, *_):
        self._connected = True
        # Push a synthetic event so the actions loop can re-subscribe after
        # a reconnect without special-casing connected/disconnected in two
        # places.
        self.queue.put(("on_open", self, {}))

    def _on_close(self, *args):
        msg = args[2] if len(args) == 3 else (args[1] if len(args) > 1 else None)
        if msg:
            log.info("Moonraker WS closed: %s", msg)
        self._connected = False

    def _on_error(self, *_):
        log.exception("Moonraker WS error")

    def _on_message(self, *args):
        message = args[1] if len(args) == 2 else args[0]
        response = json.loads(message)

        # Response to a request we made
        if "id" in response and response["id"] in self._responses:
            self._responses[response["id"]].result = response.get("result", {})
            self._responses[response["id"]].ev.set()
            return

        # Server-pushed notification — route to the event queue
        method = response.get("method")
        params_list = response.get("params") or [{}]
        params = params_list[0] if params_list else {}
        self.queue.put((method, self, params))

    def _enqueue(self, method: str, params: dict | None) -> Response | None:
        if not self._connected:
            return None
        self._req_id += 1
        resp = Response()
        self._responses[self._req_id] = resp
        self.ws.send(json.dumps({
            "jsonrpc": "2.0",
            "method": method,
            "params": params or {},
            "id": self._req_id,
        }))
        return resp

    def __call__(
        self,
        method: str,
        params: dict | None = None,
        timeout: float | None = None,
    ) -> dict | None:
        """Blocking JSON-RPC call. Returns None on timeout/disconnect."""
        if timeout is None:
            timeout = self.default_timeout
        with self._lock:
            resp = self._enqueue(method, params)
            if resp is None:
                log.debug("moonraker call %s skipped — not connected", method)
                return None
            if not resp.ev.wait(timeout):
                log.warning("moonraker call %s timed out after %.1fs", method, timeout)
                self._responses.pop(self._req_id, None)
                return None
            return self._responses.pop(self._req_id).result
