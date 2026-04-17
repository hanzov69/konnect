"""KonnectPrinter — the SDK Printer subclass.

Mirrors prusa.connect.ht90.printer.Printer but replaces the TPC settings
service with moonrakerdb and camera-streamer with Crowsnest. Startup is
still async so the main thread can `printer.start()` and return quickly.
"""
from __future__ import annotations

import logging
from threading import Thread
from time import sleep
from typing import Any

from prusa.connect.printer import Printer as SDKPrinter
from prusa.connect.printer.camera import Camera
from prusa.connect.printer.command import Command as SDKCommand
from prusa.connect.printer.const import (
    Command,
    Event,
    Source,
    State,
    TriggerScheme,
)

from .actions import Actions
from .camera import CrowsnestDriver
from .config import Config
from .db import Database
from .moonraker import Moonraker
from .util import (
    InfinityThread,
    default_serial_number,
    make_fingerprint,
    nested_get,
)

log = logging.getLogger(__name__)


class KonnectPrinter(SDKPrinter):
    """Prusa Connect bridge for a generic Klipper/Moonraker setup."""
    # pylint: disable=too-many-instance-attributes

    def __init__(self, config: Config):
        self.cfg = config
        self.actions = Actions(self)
        self.moonraker = Moonraker(
            host=config.moonraker_host,
            port=config.moonraker_port,
            event_queue=self.actions.queue,
            default_timeout=config.moonraker_timeout,
        )
        self.db = Database(self.moonraker)
        self.camera_driver: CrowsnestDriver | None = None
        self.camera: Camera | None = None

        super().__init__(
            type_=config.printer_type,
            mmu_supported=False,
        )
        # SDK invokes register_handler(token) once registration completes.
        self.register_handler = self._on_registered

        # Background threads; we manage their lifecycle explicitly.
        self.connect_thread = Thread(
            target=self.loop, name="konnect-sdk-loop", daemon=True,
        )
        self.camera_thread = Thread(
            target=self.camera_controller.snapshot_loop,
            name="konnect-camera",
            daemon=True,
        )
        self.inotify_thread = InfinityThread(
            target=self.inotify_handler,
            kwargs={"timeout": 0.2},
            name="konnect-inotify",
        )
        self.command_thread = InfinityThread(
            target=self._command_tick, name="konnect-cmd",
        )
        self.download_thread = Thread(
            target=self.download_mgr.loop,
            name="konnect-download",
            daemon=True,
        )

        # Expose the local gcode directory to Connect's file browser.
        self.attach(config.local_gcode_path, config.local_fs_name)

        # Command handlers — each delegates to a moonraker RPC. Guarded
        # against SDK version drift: the PyPI 0.8.1 release of
        # prusa.connect.sdk.printer lacks some commands that the HT90
        # firmware's vendored copy has (notably SET_VALUE, used for
        # chamber target temp). If the running SDK doesn't define a
        # command, we just skip the handler — Connect will reply with
        # "command not supported" when the user tries to use it, but
        # the service still starts.
        self._register_handler("SEND_JOB_INFO", self._h_job_info)
        self._register_handler("PAUSE_PRINT", self._h_pause)
        self._register_handler("RESUME_PRINT", self._h_resume)
        self._register_handler("STOP_PRINT", self._h_stop)
        self._register_handler("START_PRINT", self._h_start)
        self._register_handler("SET_PRINTER_READY", self._h_set_ready)
        self._register_handler("SET_VALUE", self._h_set_value)

    def _register_handler(self, command_name: str, handler) -> None:
        """Attach a Connect command handler if the SDK knows the command."""
        cmd = getattr(Command, command_name, None)
        if cmd is None:
            log.warning(
                "SDK does not define Command.%s — skipping handler. "
                "Update prusa.connect.sdk.printer if you need this command "
                "(printer_type=HT90 in particular benefits from SET_VALUE).",
                command_name,
            )
            return
        self.set_handler(cmd, handler)

    # ---- lifecycle -----------------------------------------------------

    def start(self) -> None:
        """Kick off background startup (non-blocking)."""
        Thread(target=self._start, name="konnect-start", daemon=True).start()

    def _start(self) -> None:
        self.actions.start()
        self.moonraker.start()
        self.connect_thread.start()
        self.camera_thread.start()

        # Wait for moonrakerdb to be reachable and seed our settings cache.
        self.db.load()

        # Serial number: user-supplied via UI, else MAC-derived default.
        sn = self.db.get("serial_number") or default_serial_number()
        if not self.db.get("serial_number"):
            self.db.set("serial_number", value=sn)
        self.sn = sn

        # Wait for klippy to provide its system info so we can set firmware
        # version + fingerprint (SDK requires both before registration).
        machine_info = None
        while not machine_info:
            if not self.running_loop:
                return
            if not self.moonraker.connected:
                sleep(0.2)
                continue
            machine_info = self.moonraker("machine.system_info")

        # Prefer an operator-provided firmware string from konnect.cfg;
        # fall back to whatever Moonraker/Klippy reports for OS distro
        # version. Connect shows this verbatim in the printer info
        # panel, so it's useful to override if you want to match a
        # particular firmware naming scheme (e.g. "3.14.1-6969" to
        # mirror Prusa's versioning for the printer type being
        # advertised).
        self.firmware = self.cfg.firmware_version or nested_get(
            machine_info, "system_info", "distribution", "version",
            default="klipper",
        )
        cpu_serial = nested_get(
            machine_info, "system_info", "cpu_info", "serial_number",
            default="",
        )
        self.fingerprint = make_fingerprint(self.sn, cpu_serial)

        # Apply stored Connect connection, if registered.
        server_url = self._current_server_url()
        token = self.db.get("connect", "token") or ""
        self.set_connection(server_url, token)

        # Kick off the remaining worker threads.
        self.command_thread.start()
        self.inotify_thread.start()
        self.download_thread.start()

        # Send initial INFO so Connect has metadata as soon as we're online.
        info = self.get_info()
        info["source"] = Source.FIRMWARE
        self.event_cb(**info)
        # Trick: force a full telemetry on next tick.
        self.actions.last_telemetry = 0

        self._attach_camera_if_configured()

    def stop(self) -> None:
        if self.camera_controller:
            self.camera_controller.stop()
        self.download_mgr.stop_loop()
        self.inotify_thread.stop()
        self.command_thread.stop()
        self.stop_loop()
        self.queue.put(None)  # unblock SDK loop
        self.moonraker.stop()
        self.actions.stop()

    def wait(self) -> None:
        self.camera_thread.join(timeout=5)
        self.download_thread.join(timeout=5)
        self.inotify_thread.join(timeout=5)
        self.command_thread.join(timeout=5)
        self.moonraker.wait()
        self.connect_thread.join(timeout=5)
        self.actions.wait()

    # ---- registration callback -----------------------------------------

    def _on_registered(self, token: str | None) -> None:
        """Called by SDK when register()→poll loop resolves.

        token=None means we're resetting. On successful registration we
        also push a fresh INFO event — Connect expects printer metadata
        right after accepting the new token, and without it subsequent
        telemetry POSTs get rejected (503 SERVICE_UNAVAILABLE) because
        the server has the printer in an "unpopulated" state.
        """
        self.db.set("connect", "token", value=token or "")
        if not token:
            return
        # Re-emit INFO so Connect has current metadata for this token.
        try:
            info = self.get_info()
            info["source"] = Source.FIRMWARE
            self.event_cb(**info)
        except Exception:  # noqa: BLE001
            log.exception("failed to send post-registration INFO")
        if self.camera is not None and self.camera.camera_id:
            self.camera_controller.register_camera(self.camera.camera_id)

    def _current_server_url(self) -> str:
        hostname = self.db.get("connect", "hostname", default="connect.prusa3d.com")
        tls = self.db.get("connect", "tls", default=True)
        port = int(self.db.get("connect", "port", default=443) or 0)
        return SDKPrinter.connect_url(hostname, tls, port)

    def update_connect_settings(
        self, hostname: str, tls: bool, port: int,
    ) -> None:
        self.db.set("connect", "hostname", value=hostname)
        self.db.set("connect", "tls", value=tls)
        self.db.set("connect", "port", value=port)

    # ---- command handlers ---------------------------------------------

    def _command_tick(self) -> None:
        # The SDK's Command.new_cmd_evt is fired when Connect pushes a job.
        # InfinityThread wraps this in a loop; if we miss the 200ms edge
        # we just retry — no harm.
        if self.command.new_cmd_evt.wait(0.2):
            self.command()

    def _h_job_info(self, command: SDKCommand) -> dict[str, Any]:
        requested = int(command.kwargs.get("job_id", 0))
        if self.job_id != requested:
            raise RuntimeError(f"Unknown job id {requested}")
        return {
            "source": Source.CONNECT,
            "event": Event.JOB_INFO,
            **self.actions.job.data("/" + self.cfg.local_fs_name),
        }

    def _h_pause(self, _):
        if self.state != State.PRINTING:
            raise RuntimeError("Not printing")
        self.moonraker("printer.print.pause")
        return {"source": Source.CONNECT}

    def _h_resume(self, _):
        if self.state != State.PAUSED:
            raise RuntimeError("Not paused")
        self.moonraker("printer.print.resume")
        return {"source": Source.CONNECT}

    def _h_stop(self, _):
        if self.state not in (State.PRINTING, State.PAUSED):
            raise RuntimeError("Nothing to stop")
        self.moonraker("printer.print.cancel")
        return {"source": Source.CONNECT}

    def _h_start(self, command: SDKCommand):
        if self.state not in (State.IDLE, State.READY):
            raise RuntimeError("Can't print now")
        if not command.kwargs:
            raise RuntimeError("Nothing to print")
        # Connect sends path like "/local/foo.gcode"; strip the leading
        # "/local/" to get the moonraker-relative filename.
        raw_path = command.kwargs.get("path", "")
        prefix = f"/{self.cfg.local_fs_name}/"
        if not raw_path.startswith(prefix):
            raise RuntimeError(f"Path must start with {prefix}")
        filename = raw_path[len(prefix):]
        if not filename:
            raise RuntimeError("Empty filename")
        self.actions.job.start_cmd_id = command.command_id
        result = self.moonraker("printer.print.start", {"filename": filename})
        return {"source": Source.CONNECT, "reason": result}

    def _h_set_ready(self, _):
        self.job_id = None
        self.set_state(State.READY, Source.CONNECT, ready=True)
        return {"source": Source.CONNECT}

    def _h_set_value(self, command: SDKCommand):
        if not command.kwargs:
            raise RuntimeError("No arguments supplied")
        key, value = next(iter(command.kwargs.items()))
        if key == "chamber_target_temp":
            script = (
                f"SET_HEATER_TEMPERATURE HEATER=heater_chamber TARGET={value}"
            )
            result = self.moonraker("printer.gcode.script", {"script": script})
            if result == "ok":
                return {"source": Source.CONNECT, "reason": result}
            raise RuntimeError(f"gcode_script failed: {result}")
        raise RuntimeError(f"Unrecognized argument {key}")

    # ---- camera helpers ------------------------------------------------

    def _attach_camera_if_configured(self) -> None:
        """Register the currently-selected Crowsnest stream with Connect.

        If the user hasn't picked a stream yet (first boot / fresh setup),
        we simply skip — they'll call set_camera() from the web UI.
        """
        snapshot_url = self.db.get("camera", "snapshot_url") or ""
        if not snapshot_url:
            log.info("No camera snapshot_url configured — skipping register")
            return
        self._register_camera(snapshot_url)

    def _register_camera(self, snapshot_url: str) -> None:
        camera_id = "camera-" + self.fingerprint[:26]
        camera_token = self.db.get("camera", "token") or ""
        self.camera_driver = CrowsnestDriver(
            camera_id,
            {
                "name": f"{self.type.name} Camera",
                "driver": "crowsnest",
                "token": camera_token,
                "snapshot_url": snapshot_url,
            },
            self._camera_store_cb,
        )
        self.camera_driver.connect()
        self.camera = Camera(self.camera_driver)
        self.camera.trigger_scheme = TriggerScheme.THIRTY_SEC
        self.camera_controller.add_camera(self.camera)

        # If token is absent or the camera hasn't registered with Connect
        # yet (e.g. first time), ask the camera controller to do so. This
        # is a no-op if already registered.
        if not self.camera.is_registered or not camera_token:
            self.camera_controller.register_camera(camera_id)

    def _camera_store_cb(self, _camera_id: str) -> None:
        """Persist the camera token that the SDK hands us after register."""
        if self.camera is not None:
            self.db.set("camera", "token", value=self.camera.token or "")

    def set_camera(self, snapshot_url: str, stream_name: str = "") -> None:
        """Called by the web UI when the user picks a stream."""
        self.unregister_camera()
        self.db.set("camera", "snapshot_url", value=snapshot_url)
        self.db.set("camera", "stream_name", value=stream_name)
        # Fresh selection resets the token — Connect issues a new one.
        self.db.set("camera", "token", value="")
        self._register_camera(snapshot_url)

    def unregister_camera(self) -> None:
        """Tear down any currently-attached camera + clear its token."""
        if self.camera is not None and self.camera.is_registered:
            self.camera.set_token(None)
        self.db.set("camera", "token", value="")
        self.camera = None
        self.camera_driver = None
