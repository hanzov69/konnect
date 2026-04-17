"""KlipperScreen panel for konnect (Prusa Connect bridge).

Drops into vanilla KlipperScreen at panels/konnect.py (via the install
script). Handles all registration states in one panel by re-rendering on
each activate/refresh, driven by GET http://127.0.0.1:7130/connection.

Intentionally uses only the standard ScreenPanel + Gtk+3 API so it runs
on stock KlipperScreen. No wizard framework required.
"""
import logging
import os
from io import BytesIO

import gi
import qrcode
import requests

gi.require_version("Gtk", "3.0")
from gi.repository import GdkPixbuf, GLib, Gtk, Pango  # noqa: E402

from ks_includes.screen_panel import ScreenPanel  # noqa: E402

KONNECT_HOST = os.environ.get("KONNECT_HOST", "127.0.0.1")
KONNECT_PORT = int(os.environ.get("KONNECT_PORT", "7130"))
BASE_URL = f"http://{KONNECT_HOST}:{KONNECT_PORT}"
POLL_SECONDS = 3


class Panel(ScreenPanel):
    """konnect — Prusa Connect onboarding/status panel.

    KlipperScreen loads the module and instantiates the class literally
    named `Panel` (see screen.py `_load_panel().Panel(self, title,...)`).
    """
    def __init__(self, screen, title, **kwargs):
        super().__init__(screen, title)
        self.state_cache = None
        self._poll_source = None
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.box.set_margin_start(20)
        self.box.set_margin_end(20)
        self.box.set_margin_top(10)
        self.box.set_margin_bottom(10)
        self.content.add(self.box)

    # ---- ScreenPanel lifecycle hooks ----------------------------------

    def activate(self):
        self._schedule_refresh(immediate=True)

    def deactivate(self):
        self._cancel_poll()

    # ---- refresh / polling --------------------------------------------

    def _schedule_refresh(self, immediate: bool = False):
        self._cancel_poll()
        if immediate:
            self._refresh()
        self._poll_source = GLib.timeout_add_seconds(POLL_SECONDS, self._tick)

    def _cancel_poll(self):
        if self._poll_source is not None:
            GLib.source_remove(self._poll_source)
            self._poll_source = None

    def _tick(self):
        self._refresh()
        return True  # keep polling

    def _refresh(self):
        try:
            r = requests.get(f"{BASE_URL}/connection", timeout=2).json()
        except requests.RequestException as exc:
            self._render_error(f"konnect unreachable: {exc}")
            return
        prev = self.state_cache
        self.state_cache = r
        # Only rebuild the DOM when registration phase actually changes —
        # otherwise typing a code or scanning a QR flickers every 3s.
        if prev is None or prev.get("registration") != r.get("registration"):
            self._render(r)

    # ---- rendering -----------------------------------------------------

    def _clear(self):
        for ch in self.box.get_children():
            self.box.remove(ch)

    def _render(self, r: dict):
        self._clear()
        reg = r.get("registration")
        if reg == "FINISHED":
            self._render_registered(r)
        elif reg == "IN_PROGRESS":
            self._render_in_progress(r)
        else:
            self._render_unregistered(r)
        self._screen.show_all()

    def _label(self, markup: str, xalign: float = 0.5):
        lbl = Gtk.Label()
        lbl.set_markup(markup)
        lbl.set_line_wrap(True)
        lbl.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
        lbl.set_xalign(xalign)
        return lbl

    def _render_error(self, msg: str):
        self._clear()
        self.box.add(self._label(f"<span size='large' foreground='#e74c3c'>{msg}</span>"))
        self._screen.show_all()

    def _render_unregistered(self, _r: dict):
        self.box.add(self._label(
            "<span size='x-large'>Prusa Connect</span>",
        ))
        self.box.add(self._label(
            "Register this printer with Prusa Connect. You'll get a one-time "
            "code — enter it at "
            "<span foreground='#7777FF'>connect.prusa3d.com/add</span> "
            "or scan the QR that will appear.",
        ))
        tip = self._label(
            "<i>Tip: printer type is set in konnect.cfg (printer_type = "
            "I3MK3S or HT90). Change it BEFORE registering if you have an "
            "actively heated chamber — HT90 unlocks chamber controls in "
            "Connect. I3MK3S is the safe default.</i>",
        )
        self.box.add(tip)
        btn = self._gtk.Button("refresh", _("Start registration"), "color1")
        btn.connect("clicked", self._start_registration)
        self.box.add(btn)

    def _render_in_progress(self, r: dict):
        self.box.add(self._label(
            "<span size='x-large'>Enter this code at Prusa Connect</span>",
        ))
        code = r.get("code") or "—"
        self.box.add(self._label(
            f"<span size='xx-large' font_family='monospace'>{code}</span>",
        ))
        qr_url = r.get("url") or f"https://prusa.io/add?code={code}"
        pixbuf = self._make_qr_pixbuf(qr_url, 320)
        img = Gtk.Image.new_from_pixbuf(pixbuf)
        self.box.add(img)
        self.box.add(self._label(
            "Scan with your phone or visit "
            "<span foreground='#7777FF'>prusa.io/add</span> and enter the code.",
        ))
        cancel = self._gtk.Button("cancel", _("Cancel"), "color1")
        cancel.connect("clicked", self._cancel_registration)
        self.box.add(cancel)

    def _render_registered(self, r: dict):
        status = r.get("status") or {}
        ok = bool(status.get("ok"))
        color = "#27ae60" if ok else "#e74c3c"
        self.box.add(self._label(
            "<span size='x-large'>Prusa Connect — registered</span>",
        ))
        self.box.add(self._label(
            f"<span foreground='{color}'>{status.get('message', '')}</span>",
        ))
        # READY toggle row
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        row.add(self._label("Printer ready:", xalign=0))
        switch = Gtk.Switch()
        row.pack_end(switch, False, False, 0)
        self._fetch_and_wire_ready(switch)
        self.box.add(row)
        # Unregister
        btn = self._gtk.Button("cancel", _("Unregister"), "color1")
        btn.connect("clicked", self._confirm_unregister)
        self.box.add(btn)

    def _fetch_and_wire_ready(self, switch: Gtk.Switch):
        try:
            resp = requests.get(f"{BASE_URL}/status", timeout=2).json()
            switch.set_active(resp.get("state") == "READY")
        except requests.RequestException:
            pass

        def on_toggle(widget, _pspec):
            flag = "true" if widget.get_active() else "false"
            try:
                requests.post(f"{BASE_URL}/set_printer_ready/{flag}", timeout=2)
            except requests.RequestException as exc:
                logging.warning("set_printer_ready failed: %s", exc)

        switch.connect("notify::active", on_toggle)

    # ---- actions -------------------------------------------------------

    def _start_registration(self, _widget):
        try:
            resp = requests.post(
                f"{BASE_URL}/connection",
                json={
                    "connect": {
                        "hostname": "connect.prusa3d.com",
                        "tls": True,
                        "port": 443,
                    },
                },
                timeout=5,
            ).json()
        except requests.RequestException as exc:
            self._render_error(f"Register failed: {exc}")
            return
        # Force re-fetch so we transition to IN_PROGRESS with the URL.
        self.state_cache = {"registration": "_forced", "code": resp.get("code")}
        self._refresh()

    def _cancel_registration(self, _widget):
        try:
            requests.delete(f"{BASE_URL}/connection", timeout=5)
        except requests.RequestException as exc:
            logging.warning("cancel failed: %s", exc)
        self.state_cache = None
        self._refresh()

    def _confirm_unregister(self, _widget):
        dialog = Gtk.MessageDialog(
            transient_for=None,
            flags=Gtk.DialogFlags.MODAL,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.YES_NO,
            text="Unregister this printer from Prusa Connect?",
        )
        resp = dialog.run()
        dialog.destroy()
        if resp == Gtk.ResponseType.YES:
            self._cancel_registration(None)

    # ---- helpers -------------------------------------------------------

    def _make_qr_pixbuf(self, data: str, size_px: int) -> GdkPixbuf.Pixbuf:
        img = qrcode.make(data, box_size=10, border=2)
        buf = BytesIO()
        img.save(buf, format="PNG")
        loader = GdkPixbuf.PixbufLoader.new_with_type("png")
        loader.set_size(size_px, size_px)
        loader.write(buf.getvalue())
        loader.close()
        return loader.get_pixbuf()
