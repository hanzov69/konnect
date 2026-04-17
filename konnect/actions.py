"""Moonraker event → Connect telemetry/events translator.

One consumer thread per process, fed by `Moonraker.queue`. Handles:

  on_open                        → re-subscribe printer.objects
  notify_klippy_ready            → re-subscribe (Klipper restarted)
  notify_klippy_shutdown         → force ERROR state
  notify_klippy_disconnected     → force ERROR state
  notify_status_update           → compute telemetry diff, forward
  notify_history_changed         → update job_id tracker

The set of subscribed objects is intentionally looser than HT90's — we
subscribe to things vanilla Klipper always has (extruder, heater_bed,
print_stats, display_status, gcode_move, toolhead) and a handful of
optional ones (heater_chamber, filament_switch_sensor, save_variables,
pause_resume). Missing objects silently stay None; Connect handles that.
"""
from __future__ import annotations

import logging
from queue import Empty, Queue
from threading import Thread
from time import time
from typing import Callable, TYPE_CHECKING

from prusa.connect.printer.const import Source, State

from .models import Info, Job, Telemetry
from .util import nested_get

if TYPE_CHECKING:
    from .printer import KonnectPrinter

log = logging.getLogger(__name__)

# Send full telemetry at least every 5 min even if unchanged (keeps
# Connect's "online" flag green when the printer is idle and quiet).
TELEMETRY_TIME_THRESHOLD = 300
# Send an empty-but-timestamped telemetry ping every 5 seconds so Connect's
# presence indicator stays green between diff-triggered bursts.
MIN_TELEMETRY_AGE = 5
# Defensive resync: query Moonraker's actual state every N seconds and
# reconcile. Catches state-drift bugs where a notify_status_update is
# dropped (moonraker WS reconnect, klippy restart, etc.) and we end up
# reporting stale state to Connect (e.g. still PRINTING after a cancel
# → Connect rejects telemetry with 503 until we re-sync).
RESYNC_INTERVAL = 60

# Moonraker print_stats.state → SDK State.
MOON_2_CONNECT = {
    "standby": State.IDLE,
    "printing": State.PRINTING,
    "paused": State.PAUSED,
    "complete": State.FINISHED,
    "cancelled": State.STOPPED,
    "error": State.ERROR,
}


def to_connect_state(state: str | None) -> State:
    return MOON_2_CONNECT.get(state or "", State.BUSY)


Callback = Callable[[object, dict], None]


class Actions:
    QUEUE_TIMEOUT = 1.0

    def __init__(self, printer: "KonnectPrinter"):
        self.printer = printer
        self.telemetry = Telemetry()
        self.job = Job()
        self.info = Info()
        self.last_telemetry = 0.0
        self.last_online = 0.0
        self.queue: Queue = Queue()
        self._stop = False

        self._callbacks: dict[str, Callback] = {
            "notify_status_update": self.send_telemetry,
            "on_open": self.subscribe,
            "notify_klippy_ready": self.subscribe,
            "notify_klippy_shutdown": self.set_error_state,
            "notify_klippy_disconnected": self.set_error_state,
            "notify_history_changed": self.history_job_changed,
            # Synthetic event — triggered by the periodic timer below
            # or by POST /resync. Re-queries current state from
            # Moonraker (no re-subscribe) and feeds it through
            # send_telemetry so any drift gets corrected.
            "konnect_resync": self.resync,
        }
        self._last_resync = 0.0
        self._thread = Thread(target=self.loop, name="konnect-actions", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop = True

    def wait(self) -> None:
        self._thread.join()

    def loop(self) -> None:
        self._stop = False
        while not self._stop:
            # Process a queued event if available. Empty queue just
            # falls through to the periodic-resync check below.
            try:
                item = self.queue.get(timeout=self.QUEUE_TIMEOUT)
            except Empty:
                item = None
            if item is not None:
                try:
                    method, obj, params = item
                    if method in self._callbacks:
                        self._callbacks[method](obj, params)
                    elif method and method != "notify_proc_stat_update":
                        log.debug("moonraker %s: %s", method, params)
                except Exception:  # noqa: BLE001
                    log.exception("actions dispatcher raised")

            # Periodic defensive resync — runs whether or not the
            # queue had items this tick. Only fires if the Moonraker
            # WS is up; otherwise the query would just time out.
            moonraker = getattr(self.printer, "moonraker", None)
            now = time()
            if (
                moonraker is not None
                and moonraker.connected
                and now - self._last_resync > RESYNC_INTERVAL
            ):
                self._last_resync = now
                try:
                    self.resync(moonraker, {})
                except Exception:  # noqa: BLE001
                    log.exception("periodic resync failed")

    # ---- subscribe & telemetry -----------------------------------------

    @property
    def subscribe_objects(self) -> dict:
        """Objects we ask Moonraker to keep us updated about.

        Objects that may or may not exist on a given printer: listing them
        is safe — Moonraker just omits missing ones from the response.
        """
        return {
            "display_status": ["progress", "message"],
            "extruder": ["target", "temperature"],
            "fan": ["speed"],
            "filament_switch_sensor fil_sensor": ["filament_detected"],
            "gcode_move": ["extrude_factor", "speed_factor"],
            "heater_bed": ["target", "temperature"],
            "heater_chamber": ["temperature", "target"],
            "print_stats": [
                "total_duration", "state", "filament_used", "print_duration",
            ],
            "pause_resume": ["pause_reason", "is_paused"],
            "save_variables": ["variables"],
            "toolhead": ["estimated_print_time", "position"],
            "virtual_sdcard": ["progress"],
        }

    def subscribe(self, moonraker, _params) -> None:
        """(Re-)subscribe and seed state immediately from the response."""
        result = moonraker(
            "printer.objects.subscribe",
            {"objects": self.subscribe_objects},
            timeout=2.0,
        )
        if not result:
            return
        status = result.get("status", {})
        self.send_telemetry(moonraker, status)
        # On reconnect mid-print, repopulate job info from history.
        if self.printer.state in (State.PRINTING, State.FINISHED, State.STOPPED):
            history = moonraker("server.history.list", {"limit": 1}, timeout=2.0)
            if history and (jobs := history.get("jobs", [])):
                self.history_job_changed(moonraker, {"action": "added", "job": jobs[0]})

    def resync(self, moonraker, _params) -> None:
        """Query Moonraker's current state and reconcile.

        Unlike subscribe(), this doesn't re-subscribe — it uses a
        one-shot `printer.objects.query` to get current values and
        passes them through the normal telemetry pipeline. If the
        printer's actual state differs from our cached state
        (because we missed a notify_status_update), send_telemetry
        will detect the mismatch and emit a state-change event to
        Connect, healing the drift.
        """
        result = moonraker(
            "printer.objects.query",
            {"objects": self.subscribe_objects},
            timeout=2.0,
        )
        if not result:
            return
        status = result.get("status", {})
        if not status:
            return
        prev_state = self.printer.state
        self.send_telemetry(moonraker, status)
        if prev_state != self.printer.state:
            log.info(
                "resync reconciled state: %s → %s",
                prev_state.value, self.printer.state.value,
            )

    def process_telemetry(self, params: dict) -> Telemetry:
        new = Telemetry()
        g = nested_get  # shorten — lots of uses below
        new.temp_bed = g(params, "heater_bed", "temperature")
        new.target_bed = g(params, "heater_bed", "target")
        new.temp_nozzle = g(params, "extruder", "temperature")
        new.target_nozzle = g(params, "extruder", "target")
        new.temp_chamber = g(params, "heater_chamber", "temperature")
        new.target_chamber = g(params, "heater_chamber", "target")

        material = g(params, "save_variables", "variables", "loaded_filament")
        if material and material != "NONE":
            new.material = material

        pos = g(params, "toolhead", "position")
        if pos:
            new.axis_x = round(pos[0], 1)
            new.axis_y = round(pos[1], 1)
            new.axis_z = round(pos[2], 2)
        # During printing, X/Y change constantly — skip them to keep
        # telemetry diffs meaningful (matches HT90 behavior).
        if self.printer.state == State.PRINTING:
            self.telemetry.axis_x = new.axis_x = None
            self.telemetry.axis_y = new.axis_y = None

        # Progress: prefer display_status if set, else virtual_sdcard.
        progress = g(params, "display_status", "progress")
        if progress is None:
            progress = g(params, "virtual_sdcard", "progress")
        if progress is not None:
            new.progress = round(progress * 100, 1)

        if time_printing := g(params, "print_stats", "print_duration"):
            new.time_printing = int(time_printing)

        # Klipper doesn't natively expose time_remaining — the standard
        # approximation is (estimated_print_time - print_duration) when
        # both are present, else leave it None.
        est = g(params, "toolhead", "estimated_print_time")
        dur = g(params, "print_stats", "print_duration")
        if est and dur and est > dur:
            new.time_remaining = int(est - dur)

        new.filament = g(params, "print_stats", "filament_used")

        if speed := g(params, "gcode_move", "speed_factor"):
            new.speed = int(speed * 100)
        if flow := g(params, "gcode_move", "extrude_factor"):
            new.flow = int(flow * 100)
        return new

    def send_telemetry(self, _moonraker, params: dict) -> None:
        self._set_state(params)
        new = self.process_telemetry(params)
        diff = self.telemetry.update(new)
        now = time()
        # Full refresh every 5 min keeps Connect's dashboard accurate
        # even if nothing has changed.
        if (now - self.last_telemetry) > TELEMETRY_TIME_THRESHOLD:
            self.last_telemetry = self.last_online = now
            self.printer.telemetry(**self.telemetry.data())
        elif diff > 0:
            self.last_online = now
            self.printer.telemetry(**new.data())
        elif (now - self.last_online) > MIN_TELEMETRY_AGE:
            self.last_online = now
            self.printer.telemetry()  # empty — just ping presence

    def set_error_state(self, *_):
        self.printer.set_state(State.ERROR, Source.FIRMWARE)

    def _set_state(self, params: dict) -> None:
        """Compute new printer state from subscribed params + emit INFO.

        Klipper exposes print_stats.state with values
        {standby, printing, paused, complete, cancelled, error}. We map
        those directly; pause_resume adds nuance (filament runout etc.).
        """
        new_info = Info()
        new_info.tool = self.info.tool.__class__.from_variables(
            nested_get(params, "save_variables", "variables", "nozzle"),
            self.telemetry.material,
        )
        info_changed = self.info.update(new_info)

        if state_str := nested_get(params, "print_stats", "state"):
            state = to_connect_state(state_str)
        else:
            state = self.printer.state

        if state != self.printer.state:
            self.printer.set_state(state, Source.FIRMWARE)

        if info_changed:
            self.printer.event_cb(**new_info.data())

    def history_job_changed(self, _moonraker, params: dict) -> None:
        action = params.get("action")
        job = params.get("job", {})
        if action == "added":
            self.job.reset(job)
            self.printer.job_id = self.job.id
        elif action == "finished":
            self.printer.job_id = None
