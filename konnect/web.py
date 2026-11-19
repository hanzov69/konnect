"""Flask web API + static onboarding UI.

Endpoints (kept wire-compatible with prusa.connect.ht90 where possible):

  GET    /connection             registration status + code/token
  POST   /connection             begin registration (accepts {connect:{…}})
  DELETE /connection             reset registration

  GET    /status                 simple state probe used by KlipperScreen
  POST   /set_printer_ready/<b>  toggle READY/IDLE

  GET    /printer                identity info (sn, printer_type, firmware)
  POST   /printer                update mutable identity (sn, name, location)

  GET    /cameras                list detected Crowsnest cams + currently selected
  POST   /camera                 pick a stream {snapshot_url, stream_name}
  DELETE /camera                 unregister

  GET    /printer-types          enum list + human-readable hints for onboarding

Additionally, static files under /static/ serve the onboarding SPA, and
`/` redirects there.
"""
from __future__ import annotations

import logging
from http import HTTPStatus
from pathlib import Path
from socket import gethostbyname
from threading import Thread
from urllib.error import HTTPError
from urllib.request import urlopen

from io import BytesIO

import qrcode
from flask import Flask, Response, jsonify, redirect, request, send_from_directory
from prusa.connect.printer.const import (
    PrinterType,
    RegistrationStatus,
    Source,
    State,
)

from . import printer_types
from .camera import (
    discover_crowsnest_cameras,
    discover_moonraker_webcams,
)
from .printer import KonnectPrinter

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Printer types konnect exposes, in UI order. HT90 is first because
# it's the only type that has the full workflow (file listing +
# upload + Set Ready + chamber controls) end-to-end with konnect's
# long-polling SDK. MK4S / COREONE render a more modern dashboard
# but Connect's upload UI requires Buddy's WebSocket protocol, so
# uploads from Connect don't work for those types.
#
# Any other SDK PrinterType enum member is still settable via
# `printer_type` in konnect.cfg — we just don't advertise them.
SUPPORTED_PRINTER_TYPES: tuple[str, ...] = ("HT90", "MK4S", "COREONE")

# The default selection in the onboarding wizard. HT90 is the
# recommended pick for the cloud; against a custom (self-hosted) server
# uploads travel over the WebSocket, so the modern types work
# end-to-end and MK4S is the recommendation (such servers profile the
# Buddy device models; HT90 may not be among them).
DEFAULT_PRINTER_TYPE: str = "HT90"
DEFAULT_PRINTER_TYPE_PCL: str = "MK4S"
# What the wizard pre-selects on Snapmaker firmware.
DEFAULT_PRINTER_TYPE_SNAPMAKER: str = "U1"

PRINTER_TYPE_HINTS_PCL: dict[str, dict[str, str]] = {
    "U1": {
        "label": "Snapmaker U1 (native)",
        "description": (
            "Registers as a U1, not a Prusa model: the server shows the "
            "four heads with the material and colour loaded in each, "
            "and the printer's real firmware version. Uploads, pause/"
            "resume/stop and per-head temperatures work. The server has "
            "no profile for it, so it shows a generic printer icon."
        ),
        "recommended_for": "Snapmaker U1 on a self-hosted server",
        "warning": "",
    },
    "MK4S": {
        "label": "Prusa MK4S (recommended)",
        "description": (
            "Modern dashboard, and on a self-hosted server everything "
            "works end-to-end: file listing, uploads from the server's "
            "UI (inline WebSocket transfer), Set Ready, print start."
        ),
        "recommended_for": "Default pick for a custom server",
        "warning": "",
    },
    "COREONE": {
        "label": "Prusa Core One (chamber controls)",
        "description": (
            "Same feature set as MK4S plus chamber temperature controls "
            "— konnect forwards chamber_target_temp to your "
            "[heater_chamber] if you have one."
        ),
        "recommended_for": "CoreXY with a heated chamber",
        "warning": "",
    },
    "XL": {
        "label": "Prusa XL (toolchanger emulation)",
        "description": (
            "Pretend to be a Prusa XL. konnect detects every Klipper "
            "extruder (extruder, extruder1, …) and reports per-head "
            "temperatures and materials the Buddy way. The server draws "
            "the XL's five heads, so a four-head machine shows a blank "
            "fifth."
        ),
        "recommended_for": "Multi-extruder Klipper machines other than the U1",
        "warning": "",
    },
    "HT90": {
        "label": "Prusa Pro HT90 (not recommended here)",
        "description": (
            "The legacy type konnect recommends against the cloud. A "
            "self-hosted server profiles capabilities per Buddy device "
            "model and may not carry an HT90 profile."
        ),
        "recommended_for": "Cloud deployments — pick MK4S here",
        "warning": (
            "⚠ The server may refuse or misprofile the HT90 type. "
            "MK4S / Core One / XL are the tested picks here."
        ),
    },
}

# Hints shown in the onboarding UI for each type we expose.
# `warnings` surfaces as a red-banner message next to the card.
PRINTER_TYPE_HINTS: dict[str, dict[str, str]] = {
    "HT90": {
        "label": "Prusa Pro HT90 (recommended)",
        "description": (
            "Industrial dashboard with chamber temperature controls. "
            "The ONLY type where everything works end-to-end with "
            "konnect: file listing, uploads from Connect's UI, "
            "Set Ready / Cancel Ready, and chamber temperature "
            "controls. Picks up any Klipper printer — chamber widgets "
            "stay idle if you don't have a [heater_chamber]."
        ),
        "recommended_for": "Default pick — works for any Klipper printer",
        "warning": "",
    },
    "MK4S": {
        "label": "Prusa MK4S (uploads don't work)",
        "description": (
            "Modern MK4S dashboard. File listing, telemetry, Set Ready, "
            "and print start all work. If you want the newer UI and "
            "can live without upload-from-Connect."
        ),
        "recommended_for": (
            "Users who prefer the modern look and upload via Mainsail/Fluidd"
        ),
        "warning": (
            "⚠ Uploading files from Connect's UI does NOT work for MK4S — "
            "the Buddy WebSocket protocol this dashboard uses is not "
            "implementable over konnect's HTTPS-polling SDK. Upload via "
            "Mainsail/Fluidd instead; files then appear in Connect's "
            "file list. Pick HT90 if Connect-side uploads matter."
        ),
    },
    "COREONE": {
        "label": "Prusa Core One (uploads don't work)",
        "description": (
            "Core One dashboard including chamber temperature controls. "
            "Same feature set as MK4S plus chamber — konnect forwards "
            "SET_VALUE chamber_target_temp commands to your "
            "[heater_chamber] if you have one."
        ),
        "recommended_for": (
            "CoreXY with heated chamber if you tolerate upload-via-Mainsail"
        ),
        "warning": (
            "⚠ Uploading files from Connect's UI does NOT work for Core One "
            "(same WebSocket limitation as MK4S). Upload via Mainsail/Fluidd "
            "instead. Pick HT90 if Connect-side uploads matter — HT90's "
            "chamber controls work too."
        ),
    },
}


def _add_printer_url(printer: KonnectPrinter) -> str:
    """Where a person approves this printer.

    Cloud: the deep link that pre-fills the code. Custom server: its
    Add-a-printer page — the pending registration appears there by
    itself; the person just checks the code matches and clicks Approve.
    """
    if printer.backend == "pcl":
        return f"{printer.server}/printers/add"
    return f"{printer.server}/add-printer/connect/{printer.type}/{printer.code}"


def create_app(printer: KonnectPrinter) -> Flask:
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False
    app.printer = printer  # type: ignore[attr-defined]

    _register_static_routes(app)
    _register_api_routes(app, printer)
    return app


def run_in_thread(printer: KonnectPrinter, debug: bool = False) -> Thread:
    app = create_app(printer)

    def target():
        app.run(
            host=printer.cfg.web_host,
            port=printer.cfg.web_port,
            threaded=True,
            debug=debug,
            use_reloader=False,
        )

    thread = Thread(target=target, name="konnect-web", daemon=True)
    thread.start()
    return thread


# ---- routes ----------------------------------------------------------------


def _register_static_routes(app: Flask) -> None:
    @app.route("/")
    def root():  # noqa: ANN202
        # Relative redirect so it works both at :7130/ and /konnect/
        # (the nginx-proxied path). Absolute "/static/…" would land
        # on Mainsail's root rather than /konnect/static/… .
        return redirect("static/index.html", code=302)

    @app.route("/static/<path:subpath>")
    def static_files(subpath: str):  # noqa: ANN202
        return send_from_directory(STATIC_DIR, subpath)

    @app.route("/qr")
    def qr_png():  # noqa: ANN202
        """Render any string as a QR PNG. Used by the registration step."""
        data = request.args.get("data", "")
        if not data:
            return jsonify({"error": "data query required"}), HTTPStatus.BAD_REQUEST
        img = qrcode.make(data, box_size=8, border=2)
        buf = BytesIO()
        img.save(buf, format="PNG")
        return Response(
            buf.getvalue(),
            mimetype="image/png",
            headers={"Cache-Control": "no-store"},
        )


def _register_api_routes(app: Flask, printer: KonnectPrinter) -> None:
    # ---- connection / registration ----
    @app.route("/connection", methods=["GET"])
    def get_connection():  # noqa: ANN202
        registration = RegistrationStatus.NO_REGISTRATION
        if printer.token:
            registration = RegistrationStatus.FINISHED
        elif printer.code:
            registration = RegistrationStatus.IN_PROGRESS

        status: dict[str, object] = {"ok": True, "message": "OK"}
        if not printer.is_initialised():
            status = {"ok": False, "message": "Printer isn't initialised"}
        elif not printer.token:
            status = {"ok": False, "message": "Connect isn't configured"}

        # Build the full "add printer" URL whenever we have a code; lets
        # the KlipperScreen panel re-render a QR after a restart without
        # the user having to click through POST /connection again.
        add_url = None
        if printer.code and printer.server:
            add_url = _add_printer_url(printer)

        return jsonify({
            "initialised": printer.is_initialised(),
            "status": status,
            "token": printer.token or None,
            "code": printer.code,
            "url": add_url,
            "registration": registration.value,
            "server": printer.server,
            "backend": printer.backend,
            "cloud_host": printer.CLOUD_HOST,
        })

    @app.route("/connection", methods=["POST"])
    def post_connection():  # noqa: ANN202
        if printer.token:
            return jsonify({"error": "Already registered"}), HTTPStatus.CONFLICT

        # Tear down any pending camera registration so it doesn't
        # register under the old owner.
        if printer.camera is not None and printer.camera.is_registered:
            printer.unregister_camera()

        payload = request.get_json(silent=True) or {}
        opts = payload.get("connect") or {}
        hostname = (opts.get("hostname") or "").strip()
        if not hostname:
            # No server named: the operator-pinned custom server from
            # konnect.cfg if this install was configured for one,
            # otherwise the official Prusa Connect.
            if printer.cfg.backend == "pcl" and printer.cfg.pcl_host:
                hostname = printer.cfg.pcl_host
                tls = bool(opts.get("tls", printer.cfg.pcl_tls))
                port = int(opts.get("port") or printer.cfg.pcl_port or 0)
            else:
                hostname = printer.CLOUD_HOST
                tls, port = True, 443
        else:
            custom = hostname != printer.CLOUD_HOST
            tls = bool(opts.get("tls", not custom))
            port = int(opts.get("port") or (8080 if custom else 443))

        try:
            gethostbyname(hostname)
        except OSError:
            return jsonify({"error": "Can't resolve hostname"}), HTTPStatus.BAD_REQUEST

        url = printer.connect_url(hostname, tls, port)
        # A custom server has no /info; probe the registration route we
        # are about to use. Any HTTP answer — an error status included —
        # proves a server is there.
        probe = "/info" if hostname == printer.CLOUD_HOST else "/p/register"
        try:
            with urlopen(f"{url}{probe}", timeout=5):
                pass
        except HTTPError:
            pass
        except Exception:  # noqa: BLE001
            return (
                jsonify({"error": "Can't reach Connect"}),
                HTTPStatus.BAD_REQUEST,
            )

        try:
            # Switches transport when the server kind changed: the
            # official cloud speaks the SDK's long-polling, a custom
            # address the xBuddy WebSocket dialect.
            printer.set_server(hostname, tls, port)
        except RuntimeError as exc:
            return jsonify({"error": str(exc)}), HTTPStatus.CONFLICT

        code = printer.register()
        return jsonify({
            "url": _add_printer_url(printer),
            "code": code,
            "server": printer.server,
            "backend": printer.backend,
        })

    @app.route("/connection", methods=["DELETE"])
    def delete_connection():  # noqa: ANN202
        printer.token = None
        printer._on_registered(None)  # persist empty token + unregister cam
        printer.unregister_camera()
        return jsonify({}), HTTPStatus.OK

    # ---- status / ready ----
    @app.route("/status", methods=["GET"])
    def get_status():  # noqa: ANN202
        return jsonify({"state": printer.state.value})

    @app.route("/debug/info", methods=["GET"])
    def get_debug_info():  # noqa: ANN202
        """Return the current INFO payload (what Connect would see).
        Useful for verifying the file tree the SDK is advertising."""
        try:
            info = printer.get_info()
            # Value-types are mostly JSON-safe except Source/Event/State
            return jsonify({
                k: (v.value if hasattr(v, "value") else v)
                for k, v in info.items()
            })
        except Exception as exc:  # noqa: BLE001
            return jsonify({"error": str(exc)}), 500

    @app.route("/debug/info/resend", methods=["POST"])
    def post_resend_info():  # noqa: ANN202
        """Force a fresh INFO event to Connect. Useful if Connect's
        cached view of the printer (files list, metadata) has drifted."""
        info = printer.emit_info()
        return jsonify({"ok": True, "file_count": _count_files(info.get("files"))})

    def _count_files(node):
        if not isinstance(node, dict):
            return 0
        if node.get("type") == "FILE":
            return 1
        return sum(_count_files(c) for c in (node.get("children") or []))

    @app.route("/resync", methods=["POST"])
    def post_resync():  # noqa: ANN202
        """Force a state reconciliation without waiting for the next
        periodic resync. Useful if Connect's UI gets out of sync with
        the printer (rare — usually the 60s periodic resync catches
        missed notifications, but gives the user an instant escape
        hatch)."""
        prev = printer.state.value
        printer.actions.queue.put(("konnect_resync", printer.moonraker, {}))
        return jsonify({"ok": True, "previous_state": prev})

    @app.route("/set_printer_ready/<ready>", methods=["POST"])
    def set_printer_ready(ready: str):  # noqa: ANN202
        if ready.lower() in ("true", "1", "y", "yes"):
            printer.set_state(State.READY, Source.USER)
        else:
            printer.set_state(State.IDLE, Source.USER)
        return jsonify({})

    # ---- identity ----
    @app.route("/printer", methods=["GET"])
    def get_printer():  # noqa: ANN202
        factory = getattr(printer, "factory", {}) or {}
        return jsonify({
            "serial_number": printer.sn,
            "fingerprint": printer.fingerprint,
            "printer_type": printer.type.name if printer.type else None,
            "firmware": printer.firmware,
            "name": printer.db.get("printer_name") or None,
            "location": printer.db.get("printer_location") or None,
            # What the machine says about itself, when its Moonraker
            # publishes it (Snapmaker): offered in the identity step.
            "factory_serial": factory.get("serial_number"),
            "device_name": factory.get("device_name"),
            "machine_type": factory.get("machine_type"),
        })

    @app.route("/printer", methods=["POST"])
    def post_printer():  # noqa: ANN202
        if printer.token:
            # Serial must be stable once registered, or Connect will think
            # this is a different printer and stop recognizing us.
            return (
                jsonify({
                    "error": (
                        "Cannot change identity after registration. "
                        "DELETE /connection first."
                    ),
                }),
                HTTPStatus.CONFLICT,
            )
        payload = request.get_json(silent=True) or {}
        if wanted := (payload.get("printer_type") or "").strip():
            member = getattr(PrinterType, wanted, None)
            if member is None or not isinstance(member, PrinterType):
                return (
                    jsonify({"error": f"Unknown printer type {wanted!r}"}),
                    HTTPStatus.BAD_REQUEST,
                )
            if (wanted in printer_types.CUSTOM_SERVER_ONLY
                    and printer.backend != "pcl"):
                return (
                    jsonify({"error": (
                        f"{wanted} is only understood by a custom server; "
                        "the official Prusa Connect needs a Prusa model"
                    )}),
                    HTTPStatus.BAD_REQUEST,
                )
            if printer.type is not member:
                printer.set_type(member)
        if sn := (payload.get("serial_number") or "").strip():
            if sn != printer.sn:
                try:
                    printer.set_serial(sn)
                except ValueError as exc:
                    return jsonify({"error": str(exc)}), HTTPStatus.BAD_REQUEST
        if name := payload.get("name"):
            printer.db.set("printer_name", value=name)
        if location := payload.get("location"):
            printer.db.set("printer_location", value=location)
        return jsonify({"ok": True, "restart_required": True})

    @app.route("/printer-types", methods=["GET"])
    def get_printer_types():  # noqa: ANN202
        """Return the curated types the UI offers.

        `current` is whatever `printer_type` is in konnect.cfg.
        `default` is the wizard's recommended pre-selection (HT90).
        Power users can still set any other PrinterType enum member
        via `printer_type = ...` in konnect.cfg — we just don't
        surface legacy / unusual models in the default wizard.
        """
        is_pcl = printer.backend == "pcl"
        hints = PRINTER_TYPE_HINTS_PCL if is_pcl else PRINTER_TYPE_HINTS
        order = tuple(hints) if is_pcl else SUPPORTED_PRINTER_TYPES
        types = []
        for name in order:
            member = getattr(PrinterType, name, None)
            if member is None:  # unknown SDK version, skip
                continue
            hint = hints[name]
            types.append({
                "name": member.name,
                "value": list(member.value),
                "label": hint["label"],
                "description": hint["description"],
                "recommended_for": hint["recommended_for"],
                "warning": hint.get("warning", ""),
            })
        default = DEFAULT_PRINTER_TYPE
        if is_pcl:
            default = DEFAULT_PRINTER_TYPE_PCL
            if getattr(getattr(printer, "actions", None), "snapmaker", False):
                default = DEFAULT_PRINTER_TYPE_SNAPMAKER
        return jsonify({
            "current": printer.type.name if printer.type else None,
            "default": default,
            "backend": printer.backend,
            "types": types,
        })

    # ---- camera ----
    @app.route("/cameras", methods=["GET"])
    def get_cameras():  # noqa: ANN202
        # Moonraker's webcam registry first (the canonical source on
        # e.g. the Snapmaker U1 extended firmware), then crowsnest.conf
        # (classic MainsailOS stacks). URLs are rewritten onto the host
        # the browser reached us by, so they work from another machine.
        host = request.host
        detected = discover_moonraker_webcams(printer.moonraker, host)
        seen = {c.snapshot_url for c in detected}
        detected += [
            c for c in discover_crowsnest_cameras(
                printer.cfg.crowsnest_config,
                host=host.rsplit(":", 1)[0],
            ) if c.snapshot_url not in seen
        ]
        suggested = detected[0].snapshot_url if detected else ""
        hint = ""
        if printer.backend == "pcl":
            hint = (
                "Camera push is a cloud feature. Your Connect server pulls "
                "streams itself — declare an mjpeg camera resource on the "
                "server pointing at a snapshot URL"
                + (f", e.g. {suggested}" if suggested else " below")
                + "."
            )
        return jsonify({
            "detected": [c.as_dict() for c in detected],
            "suggested": suggested,
            "hint": hint,
            "selected": {
                "snapshot_url": printer.db.get("camera", "snapshot_url") or "",
                "stream_name": printer.db.get("camera", "stream_name") or "",
                "token": printer.db.get("camera", "token") or "",
                "registered": bool(
                    printer.camera is not None and printer.camera.is_registered,
                ),
            },
        })

    @app.route("/camera", methods=["POST"])
    def post_camera():  # noqa: ANN202
        if not printer.token:
            return (
                jsonify({"error": "Printer not registered"}),
                HTTPStatus.BAD_REQUEST,
            )
        payload = request.get_json(silent=True) or {}
        snapshot_url = payload.get("snapshot_url", "").strip()
        stream_name = payload.get("stream_name", "").strip()
        if not snapshot_url:
            return (
                jsonify({"error": "snapshot_url required"}),
                HTTPStatus.BAD_REQUEST,
            )
        printer.set_camera(snapshot_url, stream_name)
        return jsonify({"ok": True})

    @app.route("/camera", methods=["DELETE"])
    def delete_camera():  # noqa: ANN202
        printer.unregister_camera()
        printer.db.set("camera", "snapshot_url", value="")
        printer.db.set("camera", "stream_name", value="")
        return jsonify({"ok": True})
