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
from . import encrypted_download, modern_fs
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
        # Buddy-style per-path enumeration. Overrides the SDK default
        # which returns a legacy flat-files response MK4/Core One
        # dashboards ignore.
        self._register_handler("SEND_FILE_INFO", self._h_send_file_info)
        # Buddy-style encrypted-upload. The enum extension for this
        # command lives in konnect.printer_types; if that hasn't run
        # the handler simply skips registration (no regression vs
        # older konnect versions).
        self._register_handler(
            "START_ENCRYPTED_DOWNLOAD",
            self._h_start_encrypted_download,
        )

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

        # Firmware string reported to Connect. Operator override in
        # konnect.cfg wins; otherwise use the type-appropriate default
        # (see _FIRMWARE_BY_TYPE in config.py). Falls back to Klippy's
        # OS distro version only when neither is available.
        self.firmware = self.cfg.effective_firmware() or nested_get(
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

    # ---- Buddy-style file protocol shim --------------------------------
    #
    # For modern Prusa types (MK4, MK4S, Core One, etc.) Connect's
    # dashboard ignores the legacy `files` tree in INFO and instead
    # reads a `storages` array + issues per-path SEND_FILE_INFO
    # commands. We augment the SDK's INFO payload with storages and
    # override SEND_FILE_INFO to speak the new protocol. For legacy
    # types (HT90 etc.) the extra fields are harmless — Connect
    # ignores what its dashboard doesn't need.

    # The mountpoint we advertise. Matches Buddy firmware
    # (/usb is what real MK4-family printers expose).
    _MODERN_MOUNTPOINT = "/usb"

    def get_info(self) -> dict[str, Any]:
        info = super().get_info()
        info["storages"] = modern_fs.build_storages(
            self._MODERN_MOUNTPOINT, self.cfg.local_gcode_path,
        )
        # Buddy-style fields that MK4/Core One dashboards inspect
        # before they decide the printer is usable (enables the
        # upload button, queue controls, etc.). Keeping them set
        # unconditionally — legacy HT90 dashboards ignore them.
        info["sn"] = self.sn or ""
        info["appendix"] = False
        info["transfer_paused"] = False
        # Nozzle + tools. Connect's modern dashboards gate several
        # UI affordances (upload, start print) on having a valid
        # `tools` object describing at least one slot. We emit a
        # single slot matching Buddy's MK4S layout. Values come
        # from moonraker's save_variables if available, else
        # reasonable defaults.
        material = self.actions.info.tool.material if (
            hasattr(self, "actions") and self.actions.info.tool.material
        ) else "---"
        nozzle_d = self.actions.info.tool.nozzle_diameter if (
            hasattr(self, "actions") and self.actions.info.tool.nozzle_diameter
        ) else 0.4
        info["nozzle_diameter"] = nozzle_d
        info["tools"] = {
            "1": {
                "nozzle_diameter": nozzle_d,
                "high_flow": False,
                "hardened": False,
                "material": material,
            },
        }
        return info

    def _h_start_encrypted_download(self, _command: SDKCommand) -> dict[str, Any]:
        """Refuse Connect's encrypted-upload command.

        We have the machinery to fetch + AES-CTR decrypt (see
        :mod:`konnect.encrypted_download`), but Connect only serves the
        ``/f/<iv>/raw`` ciphertext to clients that hold the real
        Buddy-firmware WebSocket session on ``/p/ws``. The SDK uses
        HTTPS long-polling, so we can't complete the transfer.
        Declining cleanly (instead of ACCEPTED-then-hung) tells
        Connect's UI to surface an error quickly rather than retrying
        the same iv forever.

        If you want uploads to work: register as ``HT90`` which uses
        the legacy ``START_CONNECT_DOWNLOAD`` protocol, which the SDK's
        DownloadMgr handles end-to-end over HTTPS.
        """
        raise RuntimeError(
            "START_ENCRYPTED_DOWNLOAD requires the Buddy WebSocket "
            "protocol (/p/ws) which konnect's long-polling SDK does "
            "not implement. Use printer_type = HT90 for uploads."
        )

    def _h_send_file_info(self, command: SDKCommand) -> dict[str, Any]:
        """Respond to Connect's SEND_FILE_INFO with a Buddy-style payload.

        For a directory path, returns ``children`` + metadata so
        Connect's file-browser widget can list contents. For a file
        path, returns file metadata. Accepts the Buddy mountpoint
        (``/usb``) and also the legacy ``/<local_fs_name>`` prefix so
        HT90 / i3 dashboards keep working — both map to the same
        backing directory.
        """
        raw = command.kwargs.get("path", self._MODERN_MOUNTPOINT)
        # Translate the legacy /<local_fs_name> prefix to /usb so the
        # same backing dir serves both protocols.
        legacy_prefix = f"/{self.cfg.local_fs_name}"
        if raw == legacy_prefix or raw.startswith(legacy_prefix + "/"):
            raw = self._MODERN_MOUNTPOINT + raw[len(legacy_prefix):]

        data = modern_fs.file_info(
            self.cfg.local_gcode_path,
            self._MODERN_MOUNTPOINT,
            raw,
        )
        if data is None:
            raise RuntimeError(f"File not found: {command.kwargs.get('path')}")
        return {
            "source": Source.CONNECT,
            "event": Event.FILE_INFO,
            **data,
        }

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
        # Connect sends either the legacy `/<local_fs_name>/foo.gcode`
        # (HT90 / i3 dashboards) or the Buddy-style
        # `/<modern_mountpoint>/foo.gcode` (MK4 / Core One). Accept
        # both and strip to the moonraker-relative filename.
        raw_path = command.kwargs.get("path", "")
        filename = None
        for prefix in (
            f"/{self.cfg.local_fs_name}/",
            self._MODERN_MOUNTPOINT + "/",
        ):
            if raw_path.startswith(prefix):
                filename = raw_path[len(prefix):]
                break
        if filename is None:
            raise RuntimeError(
                f"Path must start with /{self.cfg.local_fs_name}/ or "
                f"{self._MODERN_MOUNTPOINT}/ — got {raw_path!r}"
            )
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
