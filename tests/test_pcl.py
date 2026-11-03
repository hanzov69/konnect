"""Unit tests for the custom Connect-server WebSocket transport.

Runnable standalone (``python tests/test_pcl.py``) or under pytest.
Exercises the pure protocol pieces — frame parsing, command dispatch,
inline-transfer bookkeeping — against the real SDK Command / Transfer
objects, with no sockets and no Moonraker.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from queue import Queue
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _compat  # noqa: F401, E402 - inotify stub for non-Linux dev boxes

from konnect import printer_types

printer_types.install()
printer_types.install_commands()

import websocket  # noqa: E402
from prusa.connect.printer.command import Command  # noqa: E402
from prusa.connect.printer.const import Event, Source  # noqa: E402
from prusa.connect.printer.download import Transfer  # noqa: E402

from konnect.pcl import InlineTransfer, PCLSession, _ws_url  # noqa: E402


def make_printer(tmp: Path):
    """A stub with just the attributes PCLSession touches."""
    events = []

    def event_cb(event, source, timestamp=None, command_id=None, **kwargs):
        events.append((event, kwargs))

    printer = SimpleNamespace(
        transfer=Transfer(),
        queue=Queue(),
        running_loop=True,
        token="tok",
        fingerprint="fp",
        server="http://pcl.example:8823",
        event_cb=event_cb,
        moonraker=lambda *a, **k: "ok",
        cfg=SimpleNamespace(
            local_gcode_path=str(tmp),
            local_fs_name="local",
        ),
    )
    printer.command = Command(event_cb)
    printer.events = events
    return printer


def test_ws_url():
    assert _ws_url("http://pcl.example:8823") == "ws://pcl.example:8823/p/ws"
    assert _ws_url("https://pcl.example") == "wss://pcl.example/p/ws"


def test_json_command_frame():
    with tempfile.TemporaryDirectory() as tmp:
        printer = make_printer(Path(tmp))
        session = PCLSession(printer)
        body = json.dumps({"command": "SEND_INFO", "kwargs": {}})
        session._handle_frame(
            websocket.ABNF.OPCODE_TEXT, f"J0000002A{body}".encode(),
        )
        assert printer.command.command_id == 0x2A
        assert printer.command.command_name == "SEND_INFO"
        assert printer.command.state == Event.ACCEPTED
        assert printer.events[0][0] == Event.ACCEPTED


def test_gcode_frames():
    with tempfile.TemporaryDirectory() as tmp:
        printer = make_printer(Path(tmp))
        session = PCLSession(printer)
        session._handle_frame(websocket.ABNF.OPCODE_TEXT, b"F0000002BM112")
        assert printer.command.command_id == 0x2B
        assert printer.command.command_name == "GCODE"
        assert printer.command.force is True
        assert printer.command.kwargs == {"gcode": "M112"}


def test_unknown_chunk_is_ignored():
    with tempfile.TemporaryDirectory() as tmp:
        printer = make_printer(Path(tmp))
        session = PCLSession(printer)
        session._handle_frame(
            websocket.ABNF.OPCODE_BINARY, b"T00000315" + b"x" * 64,
        )  # no active transfer — must not raise


def test_inline_transfer_adapts_and_finishes():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        printer = make_printer(root)
        session = PCLSession(printer)
        # session.ws is None → request frames aren't actually sent;
        # bookkeeping still runs.
        payload = b"G28\n" * 700  # 2800 bytes
        retval = session.start_inline_download(
            {"path": "/usb/sub/job.gcode", "orig_size": len(payload)},
            command_id=7,
        )
        assert retval["event"] == Event.TRANSFER_INFO
        tid = printer.transfer.transfer_id
        assert session.transfer is not None
        first = session.transfer.request_payload()
        assert first["start"] == 0
        assert first["end"] == len(payload) - 1  # capped at file size

        # Server serves ~1 KiB per frame regardless of the ask.
        session._handle_frame(
            websocket.ABNF.OPCODE_BINARY,
            b"T" + f"{tid:08X}".encode() + payload[:1011],
        )
        assert session.transfer.chunk == 1024  # adapted (1 KiB floor)
        assert printer.transfer.transferred == 1011
        session._handle_frame(
            websocket.ABNF.OPCODE_BINARY,
            b"T" + f"{tid:08X}".encode() + payload[1011:2022],
        )
        session._handle_frame(
            websocket.ABNF.OPCODE_BINARY,
            b"T" + f"{tid:08X}".encode() + payload[2022:],
        )
        dest = root / "sub" / "job.gcode"
        assert dest.read_bytes() == payload
        assert session.transfer is None
        assert any(e[0] == Event.TRANSFER_FINISHED for e in printer.events)
        assert not printer.transfer.in_progress


def test_inline_transfer_rejects_escape():
    with tempfile.TemporaryDirectory() as tmp:
        printer = make_printer(Path(tmp))
        session = PCLSession(printer)
        try:
            session.start_inline_download(
                {"path": "/usb/../../evil.gcode", "orig_size": 10},
                command_id=8,
            )
        except RuntimeError:
            pass
        else:
            raise AssertionError("path escape was accepted")


def test_stop_transfer_aborts():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        printer = make_printer(root)
        session = PCLSession(printer)
        session.start_inline_download(
            {"path": "/usb/job.gcode", "orig_size": 4096}, command_id=9,
        )
        printer.transfer.stop()  # what the STOP_TRANSFER handler does
        assert session.transfer is None
        assert any(e[0] == Event.TRANSFER_STOPPED for e in printer.events)
        assert not (root / "job.gcode").exists()


def test_query_commands_send_no_accepted():
    """Real firmware answers query commands with their data event alone;
    a leading ACCEPTED is consumed by the server as a dataless answer
    ('the printer reported nothing about that path') and the data event
    is ignored. Observed live on PCL. Action commands keep ACCEPTED."""
    import threading
    import time as _time

    from prusa.connect.printer.const import Event as EV
    from prusa.connect.printer.const import Source
    from prusa.connect.printer.models import Event as LoopEvent

    class FakeWS:
        def __init__(self):
            self.sent = []

        def send(self, text):
            self.sent.append(text)

        def ping(self):
            pass

    with tempfile.TemporaryDirectory() as tmp:
        printer = make_printer(Path(tmp))
        session = PCLSession(printer)

        # _on_command marks query commands, not actions.
        body = json.dumps({"command": "SEND_FILE_INFO",
                           "kwargs": {"path": "/usb"}})
        session._handle_frame(
            websocket.ABNF.OPCODE_TEXT, f"J0000002A{body}".encode())
        assert 0x2A in session._quiet_accept
        printer.command.teardown()
        body = json.dumps({"command": "START_PRINT",
                           "kwargs": {"path": "/usb/a.gcode"}})
        session._handle_frame(
            websocket.ABNF.OPCODE_TEXT, f"J0000002B{body}".encode())
        assert 0x2B not in session._quiet_accept

        # The sender drops exactly the marked ACCEPTED.
        session.ws = FakeWS()
        printer.queue.put(LoopEvent(EV.ACCEPTED, Source.CONNECT,
                                    command_id=0x2A))
        printer.queue.put(LoopEvent(EV.ACCEPTED, Source.CONNECT,
                                    command_id=0x2B))
        printer.queue.put(LoopEvent(EV.FILE_INFO, Source.CONNECT,
                                    command_id=0x2A, path="/usb"))
        sender = threading.Thread(target=session._send_loop, daemon=True)
        sender.start()
        deadline = _time.time() + 3
        while not printer.queue.empty() and _time.time() < deadline:
            _time.sleep(0.05)
        session._sender_stop.set()
        sender.join(timeout=2)
        sent = [json.loads(s) for s in session.ws.sent]
        assert [(e["event"], e["command_id"]) for e in sent] == [
            ("ACCEPTED", 0x2B), ("FILE_INFO", 0x2A)]
        assert not session._quiet_accept


ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for fn in ALL:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(ALL)} tests passed")
