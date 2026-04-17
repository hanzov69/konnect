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

from .camera import discover_crowsnest_cameras
from .printer import KonnectPrinter

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Hints shown in the onboarding UI next to each PrinterType. Keep these
# here (not in the SDK) so we can update wording without forking the SDK.
PRINTER_TYPE_HINTS: dict[str, dict[str, str]] = {
    "I3MK3S": {
        "label": "i3 MK3S (recommended default)",
        "description": (
            "Use for any bed-slinger or CoreXY printer WITHOUT an actively "
            "heated enclosure. Connect renders the standard i3-family UI. "
            "Safe choice for Voron, RatRig, Ender-class, VzBot, etc."
        ),
        "recommended_for": "Any generic Klipper printer without chamber heating",
    },
    "HT90": {
        "label": "Prusa Pro HT90 (for heated-chamber printers)",
        "description": (
            "Use if your printer has an actively heated chamber (target "
            "temp can be set, not just insulated). Connect exposes "
            "industrial-class UI including chamber temperature controls, "
            "and konnect will forward SET_VALUE chamber_target_temp "
            "commands to a Klipper [heater_chamber] config."
        ),
        "recommended_for": "Voron 2.4 / 3D-Chameleon with active chamber heater, industrial enclosed printers",
    },
    "I3MK3": {
        "label": "i3 MK3 (older MK3 revision)",
        "description": "Same UI as MK3S, one firmware generation older.",
        "recommended_for": "Rarely the best pick — prefer I3MK3S.",
    },
    "I3MK25": {
        "label": "i3 MK2.5",
        "description": "Legacy i3 MK2.5 profile.",
        "recommended_for": "Rarely the best pick — prefer I3MK3S.",
    },
    "I3MK25S": {
        "label": "i3 MK2.5S",
        "description": "Legacy i3 MK2.5S profile.",
        "recommended_for": "Rarely the best pick — prefer I3MK3S.",
    },
    "SL1": {
        "label": "SL1 (resin / MSLA)",
        "description": (
            "Only pick if you really have an MSLA-style resin printer — "
            "Connect expects resin-specific telemetry konnect does not "
            "emit. Not recommended for FDM Klipper."
        ),
        "recommended_for": "NOT recommended for FDM",
    },
    "SL1S": {
        "label": "SL1S (resin / MSLA)",
        "description": "Same caveat as SL1.",
        "recommended_for": "NOT recommended for FDM",
    },
    "M1": {
        "label": "M1",
        "description": "Prusa M1 profile.",
        "recommended_for": "Rarely the best pick — prefer I3MK3S or HT90.",
    },
}


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
            add_url = (
                f"{printer.server}/add-printer/connect/"
                f"{printer.type}/{printer.code}"
            )

        return jsonify({
            "initialised": printer.is_initialised(),
            "status": status,
            "token": printer.token or None,
            "code": printer.code,
            "url": add_url,
            "registration": registration.value,
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
        if connect_opts := payload.get("connect"):
            hostname = connect_opts.get(
                "hostname", printer.db.get("connect", "hostname"),
            )
            tls = connect_opts.get("tls", printer.db.get("connect", "tls"))
            port = int(connect_opts.get(
                "port", printer.db.get("connect", "port"),
            ) or 0)

            try:
                gethostbyname(hostname)
            except OSError:
                return jsonify({"error": "Can't resolve hostname"}), HTTPStatus.BAD_REQUEST

            url = printer.connect_url(hostname, tls, port)
            try:
                with urlopen(f"{url}/info", timeout=5):
                    pass
            except Exception:  # noqa: BLE001
                return (
                    jsonify({"error": "Can't reach Connect"}),
                    HTTPStatus.BAD_REQUEST,
                )

            printer.set_connection(url, "")
            printer.update_connect_settings(hostname, tls, port)

        code = printer.register()
        url = (
            f"{printer.server}/add-printer/connect/"
            f"{printer.type}/{code}"
        )
        return jsonify({"url": url, "code": printer.code})

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
        return jsonify({
            "serial_number": printer.sn,
            "fingerprint": printer.fingerprint,
            "printer_type": printer.type.name if printer.type else None,
            "firmware": printer.firmware,
            "name": printer.db.get("printer_name") or None,
            "location": printer.db.get("printer_location") or None,
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
        if sn := payload.get("serial_number"):
            printer.db.set("serial_number", value=sn)
        if name := payload.get("name"):
            printer.db.set("printer_name", value=name)
        if location := payload.get("location"):
            printer.db.set("printer_location", value=location)
        return jsonify({"ok": True, "restart_required": True})

    @app.route("/printer-types", methods=["GET"])
    def get_printer_types():  # noqa: ANN202
        types = []
        for member in PrinterType:
            hint = PRINTER_TYPE_HINTS.get(member.name, {})
            types.append({
                "name": member.name,
                "value": list(member.value),
                "label": hint.get("label", member.name),
                "description": hint.get("description", ""),
                "recommended_for": hint.get("recommended_for", ""),
            })
        return jsonify({
            "current": printer.type.name if printer.type else None,
            "types": types,
        })

    # ---- camera ----
    @app.route("/cameras", methods=["GET"])
    def get_cameras():  # noqa: ANN202
        detected = discover_crowsnest_cameras(printer.cfg.crowsnest_config)
        return jsonify({
            "detected": [c.as_dict() for c in detected],
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
