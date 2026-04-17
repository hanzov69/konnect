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

# The two printer types konnect exposes to users. Every other SDK
# PrinterType member is still accessible via konnect.cfg if someone
# really wants it, but the UI intentionally narrows the choice to
# these two — they cover the "generic Klipper bed-slinger" and
# "Klipper with heated chamber" cases cleanly and are current-gen
# enough that Connect's UI treats them as first-class.
SUPPORTED_PRINTER_TYPES: tuple[str, ...] = ("MK4S", "COREONE")

# Hints shown in the onboarding UI for each type we expose.
PRINTER_TYPE_HINTS: dict[str, dict[str, str]] = {
    "MK4S": {
        "label": "Prusa MK4S (standard FDM, no heated chamber)",
        "description": (
            "Use for any bed-slinger or CoreXY printer WITHOUT an actively "
            "heated enclosure. Connect shows the MK4S dashboard. Safe for "
            "Voron Trident (unheated), Ender-class, RatRig, VzBot, and any "
            "generic Klipper printer that does not control a chamber heater."
        ),
        "recommended_for": "Any generic Klipper printer without active chamber heating",
    },
    "COREONE": {
        "label": "Prusa Core One (CoreXY with heated chamber)",
        "description": (
            "Use if your printer has an ACTIVELY heated chamber controlled "
            "by Klipper ([heater_chamber] with a target). Connect shows the "
            "Core One dashboard including chamber temperature controls, and "
            "konnect forwards SET_VALUE chamber_target_temp commands to "
            "your [heater_chamber]."
        ),
        "recommended_for": "Voron 2.4 with chamber heater, enclosed industrial-class CoreXY printers",
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
        """Return ONLY the curated types the UI offers (MK4S + Core One).
        Power users can still set any other PrinterType enum member via
        `printer_type = ...` in konnect.cfg — we just don't surface
        legacy / unusual models in the default wizard."""
        types = []
        for name in SUPPORTED_PRINTER_TYPES:
            member = getattr(PrinterType, name, None)
            if member is None:  # unknown SDK version, skip
                continue
            hint = PRINTER_TYPE_HINTS[name]
            types.append({
                "name": member.name,
                "value": list(member.value),
                "label": hint["label"],
                "description": hint["description"],
                "recommended_for": hint["recommended_for"],
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
