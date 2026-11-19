"""Unit tests for toolchanger (Snapmaker U1) telemetry.

Runnable standalone (``python tests/test_u1.py``) or under pytest.
Drives konnect.actions with a stubbed Moonraker: four extruders, Buddy
``slot`` telemetry, active-head tracking.
"""
from __future__ import annotations

import sys
from pathlib import Path
from queue import Queue

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import _compat  # noqa: F401, E402 - inotify stub for non-Linux dev boxes

from prusa.connect.printer.const import State  # noqa: E402

from konnect.actions import Actions  # noqa: E402


class StubPrinter:
    def __init__(self):
        self.state = State.IDLE
        self.job_id = None
        self.telemetry_calls = []
        self.events = []
        self.moonraker = None

    def set_state(self, state, source, **kwargs):
        self.state = state

    def telemetry(self, **kwargs):
        self.telemetry_calls.append(kwargs)

    def event_cb(self, *args, **kwargs):
        self.events.append((args, kwargs))

    def emit_info(self, source=None):
        # Mirror KonnectPrinter.emit_info: a full INFO built from the
        # per-head tool state, recorded as an event.
        tools = {
            key: {"material": tool.material or "---",
                  "nozzle_diameter": tool.nozzle_diameter or 0.4}
            for key, tool in self.actions.info.tools.items()
        }
        info = {"event": "INFO", "tools": tools, "slots": len(tools)}
        self.events.append(((), info))
        return info


class StubMoonraker:
    connected = True

    def __init__(self, objects, status):
        self.objects = objects
        self.status = status
        self.queue = Queue()

    def __call__(self, method, params=None, timeout=None):
        if method == "printer.objects.list":
            return {"objects": self.objects}
        if method in ("printer.objects.subscribe", "printer.objects.query"):
            return {"status": self.status}
        if method == "server.history.list":
            return {"jobs": []}
        return {}


U1_OBJECTS = [
    "gcode_move", "toolhead", "extruder", "extruder1", "extruder2",
    "extruder3", "heater_bed", "print_stats", "virtual_sdcard",
]

# print_task_config as the paxx12 2.0.0 firmware reports it: parallel
# arrays indexed by physical head. Head 2 (extruder1) is empty.
U1_FILAMENT = {
    "filament_type": ["PLA", "NONE", "PLA", "PETG"],
    "filament_exist": [True, False, True, True],
    "filament_vendor": ["Snapmaker", "NONE", "Generic", "Generic"],
    "filament_sub_type": ["Basic", "NONE", "Basic", ""],
}

U1_STATUS = {
    "toolhead": {"position": [1, 2, 3], "extruder": "extruder2"},
    "print_task_config": U1_FILAMENT,
    "extruder": {"temperature": 25.04, "target": 0},
    "extruder1": {"temperature": 26.17, "target": 0},
    "extruder2": {"temperature": 215.43, "target": 220},
    "extruder3": {"temperature": 27.0, "target": 0},
    "heater_bed": {"temperature": 60.0, "target": 60},
    "print_stats": {"state": "printing", "print_duration": 10},
}


def make_actions():
    printer = StubPrinter()
    actions = Actions(printer)
    printer.actions = actions
    return printer, actions


def test_detects_four_extruders():
    printer, actions = make_actions()
    moonraker = StubMoonraker(U1_OBJECTS, U1_STATUS)
    actions.subscribe(moonraker, {})
    assert actions.extruders == [
        "extruder", "extruder1", "extruder2", "extruder3",
    ]
    assert "extruder3" in actions.subscribe_objects
    assert "extruder" in actions.subscribe_objects["toolhead"]


def test_slot_telemetry_and_active_head():
    printer, actions = make_actions()
    moonraker = StubMoonraker(U1_OBJECTS, U1_STATUS)
    actions.subscribe(moonraker, {})

    assert printer.state == State.PRINTING
    sent = printer.telemetry_calls[-1]
    assert sent["temp_nozzle"] == 215.4  # picked head (extruder2), rounded
    assert sent["target_nozzle"] == 220
    slot = sent["slot"]
    assert slot["active"] == 3
    assert slot["1"]["temp"] == 25.0
    assert slot["4"]["temp"] == 27.0


def test_partial_update_keeps_other_heads():
    printer, actions = make_actions()
    moonraker = StubMoonraker(U1_OBJECTS, U1_STATUS)
    actions.subscribe(moonraker, {})
    printer.telemetry_calls.clear()

    # A diff update: only one head's temperature moved, picker changed.
    actions.send_telemetry(moonraker, {
        "toolhead": {"extruder": "extruder"},
        "extruder": {"temperature": 199.9},
    })
    sent = printer.telemetry_calls[-1]
    assert sent["slot"]["active"] == 1
    assert sent["slot"]["1"]["temp"] == 199.9
    assert sent["slot"]["3"]["temp"] == 215.4  # retained from before
    assert sent["temp_nozzle"] == 199.9  # followed the picker


def _info_events(printer):
    return [kw for _a, kw in printer.events if kw.get("event") == "INFO"]


def test_per_head_material_from_print_task_config():
    """Observed on both U1s: every tool reported material '---' because
    konnect only knew the save_variables.loaded_filament convention. The
    U1 publishes per-head filament on print_task_config."""
    printer, actions = make_actions()
    moonraker = StubMoonraker(U1_OBJECTS, U1_STATUS)
    actions.subscribe(moonraker, {})

    sent = printer.telemetry_calls[-1]
    slot = sent["slot"]
    assert slot["1"]["material"] == "PLA"
    assert slot["2"]["material"] == "---"  # filament_exist false
    assert slot["3"]["material"] == "PLA"
    assert slot["4"]["material"] == "PETG"
    assert slot["2"]["temp"] == 26.2  # temps still ride along
    # Top-level material follows the picked head (extruder2 -> slot 3).
    assert sent["material"] == "PLA"

    infos = _info_events(printer)
    assert infos, "a material change must re-announce the tools"
    tools = infos[-1]["tools"]
    assert infos[-1]["slots"] == 4
    assert [tools[k]["material"] for k in "1234"] == [
        "PLA", "---", "PLA", "PETG",
    ]


def test_filament_change_and_swap_update_material():
    printer, actions = make_actions()
    moonraker = StubMoonraker(U1_OBJECTS, U1_STATUS)
    actions.subscribe(moonraker, {})
    printer.telemetry_calls.clear()
    printer.events.clear()

    # A spool is loaded into head 2: Klipper sends the changed arrays
    # only (status updates are diffs; filament_exist may lag type).
    actions.send_telemetry(moonraker, {
        "print_task_config": {
            "filament_type": ["PLA", "ABS", "PLA", "PETG"],
        },
    })
    # Type known but filament_exist still false -> stays empty.
    assert printer.telemetry_calls == [] or \
        printer.telemetry_calls[-1].get("slot", {}).get("2", {}).get(
            "material", "---") == "---"
    actions.send_telemetry(moonraker, {
        "print_task_config": {
            "filament_exist": [True, True, True, True],
        },
    })
    sent = printer.telemetry_calls[-1]
    assert sent["slot"]["2"]["material"] == "ABS"
    assert sent["slot"]["4"]["material"] == "PETG"  # retained
    assert _info_events(printer)[-1]["tools"]["2"]["material"] == "ABS"

    # Picking another head moves the top-level material with it.
    printer.telemetry_calls.clear()
    actions.send_telemetry(moonraker, {"toolhead": {"extruder": "extruder3"}})
    sent = printer.telemetry_calls[-1]
    assert sent["material"] == "PETG"
    assert sent["slot"]["active"] == 4

    # Unloading head 4 reports the empty-slot spelling.
    actions.send_telemetry(moonraker, {
        "print_task_config": {
            "filament_type": ["PLA", "ABS", "PLA", "NONE"],
            "filament_exist": [True, True, True, False],
        },
    })
    sent = printer.telemetry_calls[-1]
    assert sent["slot"]["4"]["material"] == "---"
    assert sent["material"] == "---"
    assert _info_events(printer)[-1]["tools"]["4"]["material"] == "---"


def test_single_tool_material_from_save_variables():
    """Generic Klipper keeps the HT90 convention: one loaded_filament
    entry in save_variables feeds material and the single tool."""
    printer, actions = make_actions()
    moonraker = StubMoonraker(
        ["toolhead", "extruder", "heater_bed", "print_stats",
         "save_variables"],
        {
            "extruder": {"temperature": 24.9, "target": 0},
            "print_stats": {"state": "standby"},
            "save_variables": {"variables": {
                "loaded_filament": "PETG", "nozzle": "0.6",
            }},
        },
    )
    actions.subscribe(moonraker, {})
    sent = printer.telemetry_calls[-1]
    assert sent["material"] == "PETG"
    assert sent.get("slot") is None
    # The INFO follows a tick later (material is read from the
    # previous telemetry pass on single-tool printers, as before).
    actions.send_telemetry(moonraker, {})
    tools = _info_events(printer)[-1]["tools"]
    assert list(tools) == ["1"]
    assert tools["1"]["material"] == "PETG"
    assert tools["1"]["nozzle_diameter"] == 0.6


def test_single_tool_has_no_slot():
    printer, actions = make_actions()
    moonraker = StubMoonraker(
        ["toolhead", "extruder", "heater_bed", "print_stats"],
        {
            "extruder": {"temperature": 24.9, "target": 0},
            "print_stats": {"state": "standby"},
        },
    )
    actions.subscribe(moonraker, {})
    assert actions.extruders == ["extruder"]
    sent = printer.telemetry_calls[-1]
    assert "slot" not in sent or sent.get("slot") is None
    assert sent["temp_nozzle"] == 24.9


def test_moonraker_webcam_discovery():
    """The U1 (paxx12 firmware) registers its camera in Moonraker as
    [webcam case] with a relative snapshot URL — discovery must surface
    it rewritten onto the host the browser used."""
    from konnect.camera import discover_moonraker_webcams, externalize_url

    def moonraker(method, params=None, timeout=None):
        assert method == "server.webcams.list"
        return {"webcams": [
            {"name": "case", "service": "webrtc-camerastreamer",
             "snapshot_url": "/webcam/snapshot.jpg",
             "stream_url": "/webcam/webrtc", "enabled": True},
            {"name": "disabled-cam", "snapshot_url": "/x.jpg",
             "enabled": False},
            {"name": "absolute", "service": "ustreamer",
             "snapshot_url": "http://127.0.0.1:8080/?action=snapshot"},
        ]}

    cams = discover_moonraker_webcams(moonraker, "192.168.95.249:7130")
    assert [c.name for c in cams] == ["case", "absolute"]
    assert cams[0].snapshot_url == \
        "http://192.168.95.249/webcam/snapshot.jpg"
    assert cams[1].snapshot_url == \
        "http://192.168.95.249:8080/?action=snapshot"
    # Non-loopback absolute URLs are left alone.
    assert externalize_url("http://cam.lan/snap", "p.lan") == \
        "http://cam.lan/snap"


def test_storage_and_path_shapes():
    """PCL's path grammar is storage-prefixed without a leading slash
    (usb/models) while Buddy speaks /usb/models — konnect must take
    both, and the storages array must carry Buddy's path/name/available
    fields (observed: PCL's Files page only showed a bare USB entry)."""
    import tempfile

    from konnect import modern_fs
    from konnect.printer import KonnectPrinter

    norm = KonnectPrinter._normalize_device_path
    assert norm("usb") == "/usb"
    assert norm("usb/models") == "/usb/models"
    assert norm("/usb/") == "/usb"
    assert norm("/usb/models") == "/usb/models"
    assert norm("") == ""

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "a.gcode").write_text("G28\n")
        storages = modern_fs.build_storages("/usb", tmp)
        entry = storages[0]
        assert entry["path"] == "/usb/"
        assert entry["name"] == "usb"
        assert entry["available"] is True
        assert entry["mountpoint"] == "/usb"

        listing = modern_fs.file_info(tmp, "/usb", "/usb")
        assert listing["type"] == "FOLDER"
        assert [c["name"] for c in listing["children"]] == ["a.gcode"]


def test_file_info_echoes_requested_path():
    """The server correlates a FILE_INFO answer with the path it asked
    for, in its own spelling — observed on PCL as 'the printer reported
    nothing about that path: usb' when konnect echoed '/usb'."""
    import tempfile

    from prusa.connect.printer.command import Command as SDKCommand
    from konnect.config import Config
    from konnect.printer import KonnectPrinter

    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "a.gcode").write_text("G28\n")
        cfg = Config()
        cfg.local_gcode_path = tmp
        cfg.log_path = ""
        printer = KonnectPrinter(cfg)
        for asked in ("usb", "/usb", "usb/", "/usb/"):
            cmd = SDKCommand(lambda *a, **k: None)
            cmd.kwargs = {"path": asked}
            data = printer._h_send_file_info(cmd)
            assert data["path"] == asked, (asked, data["path"])
            assert [c["name"] for c in data["children"]] == ["a.gcode"]
        # A file answer carries its metadata without blowing up on a
        # thumbnail-less file.
        cmd = SDKCommand(lambda *a, **k: None)
        cmd.kwargs = {"path": "usb/a.gcode"}
        data = printer._h_send_file_info(cmd)
        assert data["path"] == "usb/a.gcode"
        assert "preview" not in data or data["preview"]
        printer.stop_loop()


def test_symlinked_dirs_are_listed():
    """Klipper tooling symlinks dirs like shaper_calibrate into gcodes;
    lexical confinement must follow them while still refusing wire-side
    traversal (seen on the U1 as 'reported nothing about that path')."""
    import tempfile

    from konnect import modern_fs

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "gcodes"
        outside = Path(tmp) / "outside"
        root.mkdir()
        outside.mkdir()
        (outside / "data.csv").write_text("x\n")
        try:
            (root / "shaper_calibrate").symlink_to(
                outside, target_is_directory=True)
        except OSError:
            return  # no symlink privilege on this dev box — covered on CI/Linux
        listing = modern_fs.file_info(root, "/usb", "/usb/shaper_calibrate")
        assert listing is not None and listing["type"] == "FOLDER"
        assert [c["name"] for c in listing["children"]] == ["data.csv"]
        # Wire-side traversal is still refused.
        assert modern_fs.file_info(root, "/usb", "/usb/../outside") is None


def test_idempotent_retry_reruns():
    """A server retrying a finished query command's id must get a fresh
    FILE_INFO, not the SDK's bare FINISHED resend."""
    import tempfile
    from types import SimpleNamespace

    from prusa.connect.printer.command import Command as SDKCommand
    from prusa.connect.printer.download import Transfer

    with tempfile.TemporaryDirectory() as tmp:
        printer = SimpleNamespace(
            transfer=Transfer(),
            queue=Queue(),
            running_loop=True,
            event_cb=lambda *a, **k: None,
            cfg=SimpleNamespace(local_gcode_path=tmp, local_fs_name="local"),
        )
        printer.command = SDKCommand(lambda *a, **k: None)
        from konnect.pcl import PCLSession
        session = PCLSession(printer)
        body = '{"command": "SEND_STATE_INFO", "kwargs": {}}'
        session._on_command(0x10, body)
        assert printer.command.state is not None
        printer.command.finish(source=None)  # simulate executed
        assert printer.command.state is None
        session._on_command(0x10, body)  # same id, retried
        assert printer.command.state is not None  # re-accepted, will re-run


def test_pcl_registration_spells_model_name():
    """A self-hosted server's profiles use the model-name spelling; the
    numeric code registered as model '3.1.0' in its Specification panel
    and logged as 'a model spelling the profile file does not use'."""
    import tempfile
    from types import SimpleNamespace

    from konnect import printer_types
    printer_types.install()
    printer_types.install_commands()

    from konnect.config import Config
    from konnect.printer import KonnectPrinter
    from prusa.connect.printer.const import PrinterType

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.backend = "pcl"
        cfg.pcl_host = "pcl.lan"
        cfg.local_gcode_path = tmp
        cfg.log_path = ""
        cfg.printer_type = PrinterType.XL
        printer = KonnectPrinter(cfg)
        printer.sn = "SN1"
        printer.fingerprint = "f" * 50
        printer.firmware = cfg.effective_firmware()
        printer.set_connection("http://pcl.lan:8823", "")
        posts = []

        def fake_post(url, headers=None, json=None, timeout=None):
            posts.append((url, json))
            return SimpleNamespace(status_code=200, headers={"Code": "AB12"})

        printer.conn = SimpleNamespace(post=fake_post)
        code = printer.register()
        assert code == "AB12"
        url, payload = posts[0]
        assert url.endswith("/p/register")
        assert payload["printer_type"] == "XL"
        assert payload["firmware"] == "6.4.19+6969"
        printer.stop_loop()


def test_extract_thumbnails_fallback():
    """gcode_metadata surfaced only a 300x300 too big for one frame
    while the 48x48 beside it was dropped (seen on a Snapmaker-sliced
    file) — the fallback scanner must read every embedded block."""
    import base64
    import tempfile

    from konnect import modern_fs

    small = base64.b64encode(b"small-png").decode()
    big = base64.b64encode(b"big-png-" * 64).decode()
    gcode = (
        "; generated by SomeSlicer\n"
        f"; thumbnail begin 48x48 {len(small)}\n"
        f"; {small}\n"
        "; thumbnail end\n"
        f"; thumbnail_QOI begin 32x32 8\n; AAAAAAAA\n; thumbnail_QOI end\n"
        f"; thumbnail begin 300x300 {len(big)}\n"
        + "".join(f"; {big[i:i + 60]}\n" for i in range(0, len(big), 60))
        + "; thumbnail end\n"
        "G28\nG1 X10\n"
    )
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "part.gcode"
        path.write_text(gcode)
        thumbs = modern_fs.extract_thumbnails(path)
        assert sorted(thumbs) == ["300x300", "48x48"]  # QOI skipped
        assert thumbs["48x48"] == small.encode()
        assert thumbs["300x300"] == big.encode()


def test_pinned_server_never_overrides_a_registration():
    """Seen on a U1: registered via the UI with PCL at :8080, while
    konnect.cfg (seeded from extended2.cfg) pinned a stale :8823. Every
    restart rewrote the stored server to the pin and konnect dialed a
    dead address. A pin is only the default for a fresh registration."""
    import tempfile

    from konnect.config import Config
    from konnect.printer import KonnectPrinter

    class FakeDB:
        def __init__(self, data):
            self.data = data

        def get(self, *keys, default=None):
            node = self.data
            for key in keys:
                if not isinstance(node, dict) or key not in node:
                    return default
                node = node[key]
            return node

        def set(self, *keys, value):
            node = self.data
            for key in keys[:-1]:
                node = node.setdefault(key, {})
            node[keys[-1]] = value

    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.backend = "pcl"
        cfg.pcl_host = "192.168.95.23"
        cfg.pcl_port = 8823
        cfg.pcl_tls = False
        cfg.local_gcode_path = tmp
        cfg.log_path = ""
        printer = KonnectPrinter(cfg)
        try:
            # Registered elsewhere: the pin must leave the stored server alone.
            printer.db = FakeDB({"connect": {
                "hostname": "192.168.95.111", "tls": False, "port": 8080,
                "token": "TOKEN",
            }})
            printer._apply_pinned_server()
            assert printer.db.get("connect", "hostname") == "192.168.95.111"
            assert printer.db.get("connect", "port") == 8080
            # Not registered: the pin is the default.
            printer.db = FakeDB({"connect": {
                "hostname": "connect.prusa3d.com", "tls": True, "port": 443,
                "token": "",
            }})
            printer._apply_pinned_server()
            assert printer.db.get("connect", "hostname") == "192.168.95.23"
            assert printer.db.get("connect", "port") == 8823
            assert printer.db.get("connect", "tls") is False
        finally:
            printer.stop_loop()


def test_sdk_send_info_handler_is_not_shadowed():
    """The SDK answers SEND_INFO through Printer.send_info(caller) and
    konnect once shadowed it with a method of another signature: the
    Command object became the event's source and the PCL sender died
    (seen on a U1 as PCL never getting storages or tools)."""
    import inspect
    import tempfile

    from prusa.connect.printer import Printer as SDKPrinter
    from prusa.connect.printer.command import Command as SDKCommand
    from konnect.config import Config
    from konnect.printer import KonnectPrinter

    assert KonnectPrinter.send_info is SDKPrinter.send_info
    assert list(inspect.signature(KonnectPrinter.send_info).parameters) == [
        "self", "caller",
    ]
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config()
        cfg.local_gcode_path = tmp
        cfg.log_path = ""
        printer = KonnectPrinter(cfg)
        try:
            cmd = SDKCommand(lambda *a, **k: None)
            answer = printer.send_info(cmd)
            assert "tools" in answer and "storages" in answer
            # The unsolicited full INFO has its own name and a real source.
            emitted = printer.emit_info()
            assert hasattr(emitted["source"], "value")
        finally:
            printer.stop_loop()


def test_pcl_sender_survives_an_unrenderable_item():
    """A queue item whose payload cannot be rendered is dropped with a
    log line; the next item still goes out."""
    import threading
    import time
    from types import SimpleNamespace

    from prusa.connect.printer.const import Event as EventType, State
    from prusa.connect.printer.models import Event, Telemetry
    from konnect.pcl import PCLSession

    from prusa.connect.printer.download import Transfer

    sent = []
    printer = SimpleNamespace(
        queue=Queue(), running_loop=True, token="t", transfer=Transfer(),
        event_cb=lambda *a, **k: None,
        cfg=SimpleNamespace(local_gcode_path=".", local_fs_name="local"),
    )
    session = PCLSession(printer)
    session.ws = SimpleNamespace(send=lambda text: sent.append(text),
                                 ping=lambda: None)
    printer.queue.put(Event(EventType.INFO, "not-a-source"))  # no .value
    printer.queue.put(Telemetry(State.IDLE, temp_bed=20.0))
    session._sender_stop.clear()
    th = threading.Thread(target=session._send_loop, daemon=True)
    th.start()
    for _ in range(60):
        if sent:
            break
        time.sleep(0.05)
    printer.running_loop = False
    th.join(timeout=3)
    assert sent and '"temp_bed": 20.0' in sent[0]


def test_hidden_paths_are_kept_from_listings():
    """Klipper tooling drops shaper_calibrate / calibration_data into
    gcodes; they mean nothing on a Connect dashboard. Hidden patterns
    leave listings and answer as missing when asked for directly."""
    import tempfile

    from konnect import modern_fs
    from konnect.config import Config

    cfg = Config()
    assert cfg.hidden_patterns == ["shaper_calibrate", "calibration_data"]
    cfg.hidden_paths = " shaper_calibrate/,, calibration_data, logs/*.csv "
    assert cfg.hidden_patterns == [
        "shaper_calibrate", "calibration_data", "logs/*.csv",
    ]

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "part.gcode").write_text("G28\n")
        (root / "shaper_calibrate").mkdir()
        (root / "shaper_calibrate" / "calibration_data_x.csv").write_text("x")
        (root / "calibration_data").mkdir()
        (root / "logs").mkdir()
        (root / "logs" / "a.csv").write_text("x")
        (root / "logs" / "keep.gcode").write_text("G28\n")
        hidden = cfg.hidden_patterns

        listing = modern_fs.file_info(root, "/usb", "/usb", hidden=hidden)
        assert [c["name"] for c in listing["children"]] == ["logs", "part.gcode"]
        # A hidden folder, and anything in it, is "not there" when asked.
        assert modern_fs.file_info(
            root, "/usb", "/usb/shaper_calibrate", hidden=hidden) is None
        assert modern_fs.file_info(
            root, "/usb", "/usb/shaper_calibrate/calibration_data_x.csv",
            hidden=hidden) is None
        # A nested pattern hides just what it names.
        logs = modern_fs.file_info(root, "/usb", "/usb/logs", hidden=hidden)
        assert [c["name"] for c in logs["children"]] == ["keep.gcode"]
        # No patterns: everything is listed, as before.
        listing = modern_fs.file_info(root, "/usb", "/usb")
        assert [c["name"] for c in listing["children"]] == [
            "calibration_data", "logs", "part.gcode", "shaper_calibrate",
        ]


def _u1_printer(tmp, printer_type="U1", backend="pcl"):
    from konnect import printer_types
    printer_types.install()
    from konnect.config import Config
    from konnect.printer import KonnectPrinter
    from prusa.connect.printer.const import PrinterType

    cfg = Config()
    cfg.backend = backend
    cfg.pcl_host = "pcl.lan"
    cfg.local_gcode_path = tmp
    cfg.log_path = ""
    cfg.printer_type = getattr(PrinterType, printer_type)
    printer = KonnectPrinter(cfg)
    printer.sn = "SN1"
    printer.fingerprint = "f" * 50
    return printer


def test_native_u1_profile_registers_by_name_with_real_firmware():
    """The native profile stops pretending: the server learns the model
    'U1' and the machine's own firmware (Klipper's software_version),
    not a Buddy-looking version."""
    import tempfile
    from types import SimpleNamespace

    with tempfile.TemporaryDirectory() as tmp:
        printer = _u1_printer(tmp)
        try:
            calls = []

            def moonraker(method, params=None, timeout=None):
                calls.append(method)
                if method == "printer.info":
                    return {"software_version": "2.0.0-paxx12-24"}
                return {}
            printer.moonraker = moonraker
            assert printer.cfg.effective_firmware() == ""  # no Buddy default
            assert printer._resolve_firmware({}) == "2.0.0-paxx12-24"
            printer.firmware = printer._resolve_firmware({})
            # An operator override still wins.
            printer.cfg.firmware_version = "9.9.9"
            assert printer._resolve_firmware({}) == "9.9.9"
            printer.cfg.firmware_version = ""
            # Klipper unreachable: fall back to the OS version.
            printer.moonraker = lambda *a, **k: None
            assert printer._resolve_firmware(
                {"system_info": {"distribution": {"version": "12"}}}) == "12"

            printer.set_connection("http://pcl.lan:8080", "")
            posts = []

            def fake_post(url, headers=None, json=None, timeout=None):
                posts.append(json)
                return SimpleNamespace(status_code=200, headers={"Code": "AB12"})
            printer.conn = SimpleNamespace(post=fake_post)
            printer.register()
            assert posts[0]["printer_type"] == "U1"
            assert posts[0]["firmware"] == "2.0.0-paxx12-24"
        finally:
            printer.stop_loop()


def test_native_u1_profile_reports_tree_shaped_tools():
    """Heads counted from 0, tool_count, the loaded material as a package
    instance with its colour, null for an empty head. The XL profile
    keeps Buddy's numbered shape."""
    import tempfile

    from konnect.models import Tool

    with tempfile.TemporaryDirectory() as tmp:
        printer = _u1_printer(tmp)
        try:
            printer.moonraker = lambda *a, **k: {}
            printer.actions.extruders = [
                "extruder", "extruder1", "extruder2", "extruder3"]
            printer.actions._active_extruder = "extruder2"
            printer.actions.info.tools = {
                "1": Tool(0.4, "PLA", "#E72F1D"),
                "2": Tool(None, "---", None),
                "3": Tool(0.4, "PETG", None),
                "4": Tool(0.4, "PLA", "#6F4C2F"),
            }
            assert printer.tree_tools
            info = printer.get_info()
            assert info["tool_count"] == 4
            assert "slots" not in info
            assert sorted(info["tools"]) == ["0", "1", "2", "3"]
            head = info["tools"]["0"]
            assert head["type"] == "FFF_PRINT_HEAD"
            assert head["features"] == {
                "nozzle_diameter": 0.4, "nozzle_hardened": False,
                "nozzle_high_flow": False,
            }
            assert head["feeder"] is None
            mat = head["material_package_instance"]["package"]["material"]
            assert mat["abbreviation"] == "PLA"
            assert mat["primary_color"] == {"color_rgba": "#E72F1D"}
            assert info["tools"]["1"]["material_package_instance"] is None
            assert "primary_color" not in \
                info["tools"]["2"]["material_package_instance"]["package"]["material"]
            assert info["printer_type"] == "U1"
        finally:
            printer.stop_loop()

        xl = _u1_printer(tmp, "XL")
        try:
            xl.moonraker = lambda *a, **k: {}
            xl.actions.extruders = ["extruder", "extruder1"]
            assert not xl.tree_tools
            info = xl.get_info()
            assert "tool_count" not in info
            assert sorted(info["tools"]) == ["1", "2"]
            assert info["slots"] == 2
            assert info["printer_type"] == "3.1.0"
        finally:
            xl.stop_loop()


def test_native_u1_profile_tree_telemetry_and_colour():
    """Telemetry carries the tree `tools` block instead of `slot`, and
    the spool colour is read from print_task_config's RRGGBBAA."""
    printer, actions = make_actions()
    printer.tree_tools = True
    status = dict(U1_STATUS)
    status["print_task_config"] = dict(U1_FILAMENT, filament_color_rgba=[
        "E72F1DFF", "FFFFFFFF", "F72323FF", "6F4C2FFF"])
    moonraker = StubMoonraker(U1_OBJECTS, status)
    actions.subscribe(moonraker, {})
    sent = printer.telemetry_calls[-1]
    assert sent.get("slot") is None
    tools = sent["tools"]
    assert sorted(tools) == ["0", "1", "2", "3"]
    assert tools["2"] == {
        "type": "FFF_PRINT_HEAD", "active": True,
        "nozzle_temp": 215.4, "nozzle_target_temp": 220,
    }
    assert tools["0"]["active"] is False
    assert tools["0"]["nozzle_target_temp"] == 0
    # Colours land on the tools, none for the empty head.
    assert actions.info.tools["1"].color == "#E72F1D"
    assert actions.info.tools["2"].color is None
    assert actions.info.tools["4"].color == "#6F4C2F"
    assert actions.snapmaker is False  # stub object list lacks print_task_config
    moonraker.objects = U1_OBJECTS + ["print_task_config"]
    actions._detect_extruders(moonraker)
    assert actions.snapmaker is True


def test_set_type_persists_and_is_locked_once_registered():
    """The onboarding page's choice takes effect at once and survives a
    restart: written as a line edit so comments in konnect.cfg stay."""
    import tempfile

    from konnect.config import Config
    from prusa.connect.printer.const import PrinterType

    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "konnect.cfg"
        cfg_path.write_text(
            "# my notes\n[konnect]\nbackend = pcl\n"
            "printer_type: XL   # old\npcl_host = pcl.lan\n", encoding="utf-8")
        cfg = Config.load(str(cfg_path))
        assert cfg.path == cfg_path
        assert cfg.write_printer_type("U1") == cfg_path
        text = cfg_path.read_text(encoding="utf-8")
        assert "# my notes" in text and "pcl_host = pcl.lan" in text
        assert "printer_type = U1\n" in text and "XL" not in text

        # No printer_type line yet: inserted under [konnect].
        cfg_path.write_text("[konnect]\nbackend = pcl\n", encoding="utf-8")
        cfg.write_printer_type("U1")
        assert cfg_path.read_text(encoding="utf-8").startswith(
            "[konnect]\nprinter_type = U1\nbackend = pcl\n")

        printer = _u1_printer(tmp, "XL")
        try:
            printer.cfg.path = cfg_path
            printer.moonraker = lambda *a, **k: {}
            printer.set_type(PrinterType.U1)
            assert printer.type is PrinterType.U1
            assert printer.cfg.printer_type is PrinterType.U1
            printer.token = "TOKEN"
            try:
                printer.set_type(PrinterType.XL)
            except RuntimeError:
                pass
            else:
                raise AssertionError("a registered printer must not change type")
        finally:
            printer.stop_loop()


def test_config_load_accepts_str_path():
    """`konnect --config <path>` hands a str to Config.load (found on
    the U1: it crashed with 'str' has no attribute 'is_file')."""
    import tempfile

    from konnect.config import Config

    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "konnect.cfg"
        cfg_path.write_text(
            "[konnect]\nbackend = pcl\npcl_host = pcl.lan\n"
            "pcl_port = 8823\n", encoding="utf-8",
        )
        cfg = Config.load(str(cfg_path))
        assert cfg.backend == "pcl"
        assert cfg.pcl_host == "pcl.lan"
        assert cfg.pcl_port == 8823


ALL = [v for k, v in sorted(globals().items()) if k.startswith("test_")]

if __name__ == "__main__":
    for fn in ALL:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"{len(ALL)} tests passed")
