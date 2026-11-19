"""KonnectPrinter — the SDK Printer subclass.

Mirrors prusa.connect.ht90.printer.Printer but replaces the TPC settings
service with moonrakerdb and camera-streamer with Crowsnest. Startup is
still async so the main thread can `printer.start()` and return quickly.
"""
from __future__ import annotations

import logging
from pathlib import Path
from threading import Event as ThreadEvent
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
        # NOT an InfinityThread: that pattern sleeps 1s between target
        # runs, which held every accepted command for up to ~1.2s before
        # it executed. A server that sends commands back to back (a
        # SEND_INFO immediately followed by a file listing) then hits
        # "Another command is running" purely from scheduling latency.
        self._cmd_stop = ThreadEvent()
        self.command_thread = Thread(
            target=self._command_loop, name="konnect-cmd", daemon=True,
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

        # Custom-server backend: swap the SDK's HTTPS long-polling
        # for the xBuddy WebSocket session, and register the handlers
        # that only make sense there (inline upload actually works,
        # SET_TOKEN rotation, raw G-code frames).
        self.pcl = None
        if config.backend == "pcl":
            from .pcl import PCLSession
            self.pcl = PCLSession(self)
            self._register_handler(
                "START_INLINE_DOWNLOAD", self._h_start_inline_download,
            )
            self._register_handler("SET_TOKEN", self._h_set_token)
            if config.remote_gcode:
                self._register_handler("GCODE", self._h_gcode)

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

    CLOUD_HOST = "connect.prusa3d.com"

    @property
    def backend(self) -> str:
        """The transport in use: "cloud" (SDK long-polling) or "pcl"
        (the xBuddy WebSocket dialect a self-hosted server speaks)."""
        return "pcl" if self.pcl is not None else "cloud"

    def set_server(self, hostname: str, tls: bool, port: int) -> None:
        """Point konnect at a server, switching transports if needed.

        The official cloud speaks the SDK's HTTPS long-polling; any
        custom address is assumed to be a self-hosted Connect-compatible
        server speaking the xBuddy WebSocket protocol. Only valid while
        unregistered — a token belongs to one server.
        """
        if self.token:
            raise RuntimeError("Unregister before changing the server")
        wanted = "cloud" if hostname == self.CLOUD_HOST else "pcl"
        if wanted != self.backend:
            # Swap the transport loop. Both loops exit promptly when
            # running_loop drops; everything else (handlers, queue,
            # command machinery, moonraker) is transport-agnostic.
            self.stop_loop()
            if self.connect_thread.is_alive():
                self.connect_thread.join(timeout=10)
            if wanted == "pcl":
                from .pcl import PCLSession
                self.pcl = PCLSession(self)
            else:
                self.pcl = None
            self.cfg.backend = wanted
        self.set_connection(self.connect_url(hostname, tls, port), "")
        self.update_connect_settings(hostname, tls, port)
        if not self.connect_thread.is_alive():
            self.connect_thread = Thread(
                target=self.loop, name="konnect-sdk-loop", daemon=True,
            )
            self.connect_thread.start()
        log.info("Server set to %s (%s transport)", self.server, self.backend)

    def register(self):
        """Register with the server.

        Cloud: the SDK's registration, which spells printer_type as the
        numeric device code ("3.1.0"). A self-hosted server's capability
        profiles use the model-name spelling ("XL") — its log notes the
        numeric form as a spelling "the profile file does not use", and
        its Specification panel shows the registered string verbatim —
        so the custom backend registers with the name. Firmware is
        whatever effective_firmware() resolved by registration time.
        """
        if self.backend != "pcl":
            return super().register()
        if not self.server:
            raise RuntimeError("Server is not set")
        from prusa.connect.printer import const as sdk_const
        from prusa.connect.printer.models import Register
        data = {
            "sn": self.sn,
            "fingerprint": self.fingerprint,
            "printer_type": self.type.name if self.type else None,
            "firmware": self.firmware,
        }
        res = self.conn.post(self.server + "/p/register",
                             headers=self.make_headers(),
                             json=data,
                             timeout=sdk_const.CONNECTION_TIMEOUT)
        if res.status_code != 200:
            raise RuntimeError(res.text)
        self.code = res.headers["Code"]
        self.queue.put(Register(self.code))
        return self.code

    def loop(self) -> None:
        """SDK HTTPS loop (cloud) or the PCL WebSocket session."""
        if self.pcl is None:
            return super().loop()
        # Maintain the same running flag the SDK's loop() does, so
        # running_loop / stop_loop() keep working. It is name-mangled
        # private in the SDK base class, hence the explicit prefix.
        self._Printer__running_loop = True
        try:
            self.pcl.loop()
        finally:
            self._Printer__running_loop = False
        return None

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

        self._apply_pinned_server()

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
        self.emit_info()
        # Trick: force a full telemetry on next tick.
        self.actions.last_telemetry = 0

        self._attach_camera_if_configured()

    def stop(self) -> None:
        if self.camera_controller:
            self.camera_controller.stop()
        self.download_mgr.stop_loop()
        self.inotify_thread.stop()
        self._cmd_stop.set()
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
            self.emit_info()
        except Exception:  # noqa: BLE001
            log.exception("failed to send post-registration INFO")
        if self.camera is not None and self.camera.camera_id:
            self.camera_controller.register_camera(self.camera.camera_id)

    def _apply_pinned_server(self) -> None:
        """Reconcile an operator-pinned PCL address with the stored one.

        konnect.cfg may pin the server (on the U1, konnectd seeds it from
        extended2.cfg). The pin is the default for a *fresh* registration.
        Once registered, the token belongs to the server that issued it,
        so the address stored at registration wins — a stale pin used to
        redirect a registered printer at a dead address on every restart
        (seen on a U1: registered with :8080 in the UI, pinned :8823).
        """
        if self.pcl is None or not self.cfg.pcl_host:
            return
        stored = (
            self.db.get("connect", "hostname"),
            bool(self.db.get("connect", "tls")),
            int(self.db.get("connect", "port") or 0),
        )
        wanted = (self.cfg.pcl_host, bool(self.cfg.pcl_tls), int(self.cfg.pcl_port or 0))
        if stored == wanted:
            return
        if self.db.get("connect", "token"):
            log.warning(
                "konnect.cfg pins %s:%s but this printer is registered with "
                "%s:%s — keeping the registered server (unregister to move)",
                wanted[0], wanted[2], stored[0], stored[2],
            )
            return
        self.update_connect_settings(*wanted)

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

    # Largest embedded thumbnail a FILE_INFO answer may carry. The
    # xBuddy wire caps one frame at 2^16 bytes; the rest of the answer
    # and JSON overhead need room too. 50000 fits a typical 300x300 PNG
    # (~48KB of base64) with headroom.
    PREVIEW_BYTES_BUDGET = 50000

    def emit_info(self, source: Source = Source.FIRMWARE) -> dict[str, Any]:
        """Emit a full, unsolicited INFO event (printer description).

        Not named send_info: that is the SDK's SEND_INFO command handler
        (``send_info(caller: Command)``), and shadowing it handed the
        Command object in as ``source`` — the event could not serialize
        and the PCL sender thread died, after which nothing left the
        printer (seen on a U1 as PCL's storage and tools never updating).
        """
        info = self.get_info()
        info["source"] = source
        self.event_cb(**info)
        return info

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
        # PCL's protocol doc shows INFO carrying printer_type as the
        # "X.Y.Z" string (the SDK sends type/version/subversion split).
        # Harmless to add; only done for PCL to keep the cloud payload
        # byte-identical to released konnect versions.
        if self.pcl is not None and self.type is not None:
            info["printer_type"] = str(self.type)
        # Nozzle + tools. Connect's modern dashboards gate several
        # UI affordances (upload, start print) on having a valid
        # `tools` object describing at least one slot, laid out the
        # Buddy way. One entry per detected extruder — a toolchanger
        # (the 4-head Snapmaker U1 advertising as a Prusa XL) gets all
        # of its heads in the dashboard's tool strip, each with its own
        # loaded material. Values come from the telemetry pipeline
        # (save_variables on generic Klipper, print_task_config on the
        # U1); "---" is Buddy's spelling of an empty slot.
        tool_count = 1
        tools: dict[str, Any] = {}
        active = 1
        if hasattr(self, "actions"):
            tool_count = max(1, len(self.actions.extruders))
            tools = self.actions.info.tools
            try:
                active = self.actions.extruders.index(
                    self.actions._active_extruder) + 1
            except ValueError:
                active = 1
        info["tools"] = {}
        for index in range(1, tool_count + 1):
            tool = tools.get(str(index))
            info["tools"][str(index)] = {
                "nozzle_diameter": (tool and tool.nozzle_diameter) or 0.4,
                "high_flow": False,
                "hardened": False,
                "material": (tool and tool.material) or "---",
            }
        # Top-level nozzle is the picked head's, as Buddy reports it.
        picked = info["tools"].get(str(active)) or info["tools"]["1"]
        info["nozzle_diameter"] = picked["nozzle_diameter"]
        if tool_count > 1:
            info["slots"] = tool_count
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

    @staticmethod
    def _normalize_device_path(path: str) -> str:
        """Normalize the path variants servers send for printer storage.

        Buddy firmware speaks /usb/…; a Connect server's own path
        grammar is storage-prefixed without a leading slash (usb/…),
        and a trailing slash may tag along on a directory. Map them all
        onto the /usb/… form every konnect handler expects.
        """
        if path and not path.startswith("/"):
            path = "/" + path
        if len(path) > 1:
            path = path.rstrip("/")
        return path

    def _modern_to_legacy(self, path: str) -> str:
        """Translate the Buddy mountpoint (/usb/…) to the legacy
        /<local_fs_name>/… prefix the SDK's Filesystem is attached under,
        so SDK file handlers (delete, create folder) work on paths a
        modern dashboard sends."""
        path = self._normalize_device_path(path)
        if path == self._MODERN_MOUNTPOINT or path.startswith(
                self._MODERN_MOUNTPOINT + "/"):
            return f"/{self.cfg.local_fs_name}" + \
                path[len(self._MODERN_MOUNTPOINT):]
        return path

    def _translate_path_kwarg(self, command: SDKCommand) -> SDKCommand:
        if command.kwargs and "path" in command.kwargs:
            command.kwargs["path"] = self._modern_to_legacy(
                command.kwargs["path"])
        return command

    def delete_file(self, caller: SDKCommand) -> dict[str, Any]:
        return super().delete_file(self._translate_path_kwarg(caller))

    def delete_folder(self, caller: SDKCommand) -> dict[str, Any]:
        return super().delete_folder(self._translate_path_kwarg(caller))

    def create_folder(self, caller: SDKCommand) -> dict[str, Any]:
        return super().create_folder(self._translate_path_kwarg(caller))

    def _h_start_inline_download(self, command: SDKCommand) -> dict[str, Any]:
        """PCL pushes a file over the WebSocket (T chunks). Cloud mode
        never registers this handler — see printer_types / pcl.py."""
        if self.pcl is None:
            raise RuntimeError("Inline download needs the PCL backend")
        return self.pcl.start_inline_download(
            command.kwargs or {}, command.command_id,
        )

    def _h_set_token(self, command: SDKCommand) -> dict[str, Any]:
        """PCL rotates the printer token with SET_TOKEN."""
        token = (command.kwargs or {}).get("token")
        if not token:
            raise RuntimeError("SET_TOKEN requires a token kwarg")
        self.token = token
        self.db.set("connect", "token", value=token)
        return {"source": Source.CONNECT}

    def _h_gcode(self, command: SDKCommand) -> dict[str, Any]:
        """Forward a raw G/F frame to Klipper. Prusa and Klipper G-code
        dialects mostly overlap for the M-codes a dashboard sends; a
        line Klipper refuses fails the command with Klipper's reason."""
        gcode = (command.kwargs or {}).get("gcode") or (
            command.args[0] if command.args else "")
        if not gcode.strip():
            raise RuntimeError("Empty G-code command")
        log.info("Server G-code command %d: %r",
                 command.command_id, gcode[:200])
        # 55s: just under the server's own 60s command deadline, so a
        # blocking script (heat-and-wait, homing) fails on our side
        # first with a real reason instead of silently overrunning.
        # The command slot is occupied for the duration — that matches
        # real Buddy firmware, which rejects everything else while a
        # G-code runs.
        result = self.moonraker(
            "printer.gcode.script", {"script": gcode}, timeout=55.0,
        )
        if result != "ok":
            raise RuntimeError(
                f"gcode_script did not complete within 55s or failed: "
                f"{result!r}")
        return {"source": Source.CONNECT}

    def _h_send_file_info(self, command: SDKCommand) -> dict[str, Any]:
        """Respond to Connect's SEND_FILE_INFO with a Buddy-style payload.

        For a directory path, returns ``children`` + metadata so
        Connect's file-browser widget can list contents. For a file
        path, returns file metadata. Accepts the Buddy mountpoint
        (``/usb``) and also the legacy ``/<local_fs_name>`` prefix so
        HT90 / i3 dashboards keep working — both map to the same
        backing directory.
        """
        requested = command.kwargs.get("path", self._MODERN_MOUNTPOINT)
        raw = self._normalize_device_path(requested)
        # Translate the legacy /<local_fs_name> prefix to /usb so the
        # same backing dir serves both protocols.
        legacy_prefix = f"/{self.cfg.local_fs_name}"
        if raw == legacy_prefix or raw.startswith(legacy_prefix + "/"):
            raw = self._MODERN_MOUNTPOINT + raw[len(legacy_prefix):]

        data = modern_fs.file_info(
            self.cfg.local_gcode_path,
            self._MODERN_MOUNTPOINT,
            raw,
            hidden=self.cfg.hidden_patterns,
        )
        if data is None:
            raise RuntimeError(f"File not found: {command.kwargs.get('path')}")
        # Echo the path exactly as the server spelled it — servers
        # correlate the answer with the path they asked for, and the
        # spellings differ (/usb/… from Buddy dashboards, usb/… from a
        # self-hosted server's storage-prefixed grammar).
        data["path"] = requested
        if data["type"] == "PRINT_FILE":
            self._attach_preview(data, raw)
        return {
            "source": Source.CONNECT,
            "event": Event.FILE_INFO,
            **data,
        }

    def _attach_preview(self, data: dict[str, Any], norm_path: str) -> None:
        """Add the file's embedded thumbnail to a FILE_INFO answer.

        Servers ask per-file after a listing (one file at a time, cached
        per file version), so the parse cost is paid once per file.
        Failure to parse is just an answer with no picture.
        """
        target = modern_fs._resolve_under(  # noqa: SLF001 - same package
            Path(self.cfg.local_gcode_path),
            self._MODERN_MOUNTPOINT,
            norm_path,
        )
        if target is None or target.name.startswith("."):
            return
        try:
            from gcode_metadata import get_metadata
            meta = get_metadata(str(target))
            data.update(meta.data)
            # Send the largest thumbnail that fits one wire frame.
            # gcode_metadata can surface only one of several embedded
            # sizes (seen: a 300x300 reported, the 48x48 beside it
            # dropped), so when nothing fits, the file itself is
            # scanned for all of its thumbnail blocks.
            candidates = dict(meta.thumbnails or {})
            if not any(len(thumb) <= self.PREVIEW_BYTES_BUDGET
                       for thumb in candidates.values()):
                candidates.update(modern_fs.extract_thumbnails(target))
            best = b""
            for thumb in candidates.values():
                if len(best) < len(thumb) <= self.PREVIEW_BYTES_BUDGET:
                    best = thumb
            if best:
                data["preview"] = best.decode()
        except Exception:  # noqa: BLE001
            log.debug("No readable metadata in %s", target, exc_info=True)

    # ---- command handlers ---------------------------------------------

    def _command_loop(self) -> None:
        """Run accepted commands as soon as they land.

        The SDK's Command.new_cmd_evt fires when a command is accepted;
        the wait timeout only bounds how fast shutdown is noticed."""
        while not self._cmd_stop.is_set():
            try:
                if self.command.new_cmd_evt.wait(0.2):
                    self.command()
            except Exception:  # noqa: BLE001
                log.exception("command loop raised")

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
        raw_path = self._normalize_device_path(command.kwargs.get("path", ""))
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
        if self.pcl is not None:
            # Camera push (/p/camera + snapshot POSTs) is a cloud
            # feature. PCL pulls streams itself: declare an mjpeg
            # camera resource on the server pointing at the snapshot
            # URL instead.
            log.info(
                "Skipping camera push registration (custom server). "
                "Declare a camera resource on the server with "
                "snapshot_url: %s", snapshot_url,
            )
            return
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
