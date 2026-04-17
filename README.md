# konnect

Prusa Connect for any Klipper/Moonraker printer.

A Python service + onboarding web UI + KlipperScreen panel that registers a
generic Klipper printer with [connect.prusa3d.com](https://connect.prusa3d.com)
— the same cloud dashboard that ships on Prusa's own printers. Built on
Prusa's open SDK (`prusa.connect.sdk.printer`); architecturally modeled on the
Trilab HT90 integration.

## What you get

- Registration via the standard 8-char code / QR flow
- Live telemetry (temps, progress, job state, position) pushed from Moonraker
  to Connect's `/p/telemetry`
- Remote control: start / pause / resume / stop / set ready / chamber target
  temp — handled via Moonraker RPC
- Your `~/printer_data/gcodes` exposed as the `/local` filesystem in Connect
- Webcam: auto-detects Crowsnest streams, pushes snapshots on Connect's
  trigger schedule
- Standalone onboarding UI at `http://<printer>/konnect/`
- KlipperScreen "Prusa Connect" tile with a live Ready toggle that reflects
  Connect-initiated state changes within a few seconds
- Self-healing: periodic state reconciliation catches missed Moonraker
  notifications; `/resync` endpoint for on-demand recovery without a restart

## Pick a printer type

Prusa Connect has no "generic" type, so konnect advertises as one of Prusa's
own. The choice affects which UI Connect shows; it does **not** require
Prusa hardware or constrain what gcode you can print. Edit
`~/printer_data/config/konnect.cfg` to change.

konnect ships with the two printer types that actually matter:

| `printer_type =` | When to pick | What Connect shows |
|---|---|---|
| **`MK4S`** (default) | FDM printer **without** an actively heated chamber. Voron Trident (unheated), Ender-class, RatRig, VzBot, CoreXY without chamber heat, or any generic Klipper printer. | MK4S dashboard |
| **`COREONE`** | FDM printer **with** an actively heated chamber controlled by Klipper (`[heater_chamber]` defined with a target). Voron 2.4 with chamber heater, enclosed industrial-class CoreXY. | Core One dashboard **with chamber temperature controls**; konnect forwards `SET_VALUE chamber_target_temp` commands to your `[heater_chamber]` |

Why `MK4S` specifically: Prusa Connect's **Set Ready** / **Cancel Ready** UI
buttons are wired for modern Buddy-platform types (MK4-family, Core One,
etc.) and only those types get `SET_PRINTER_READY` commands pushed back to
the printer. Older types like `I3MK3S` display the button but don't dispatch
the command.

Both MK4S and Core One are injected into the SDK's closed `PrinterType`
enum at startup (see [konnect/printer_types.py](konnect/printer_types.py));
the tuples come directly from Prusa's Buddy firmware source.

Power-user note: other enum members — `I3MK3S`, `HT90`, `MK4`, `MINI`, `XL`,
`IX`, `MK3_5{,S}`, `MK3_9{,S}`, `SL1{,S}`, `M1` — are still accessible via
`printer_type = <NAME>` in `konnect.cfg`, but the onboarding UI only
surfaces MK4S and Core One.

> Change `printer_type` **before** first registration. Switching after
> registering makes Connect see a different printer type under the same
> fingerprint — click *Unregister* (or `curl -X DELETE
> http://<printer>/konnect/connection`) and re-register.

## Firmware version

Connect displays a `Firmware` field for each printer. konnect defaults to
`firmware_version = 6.4.0+6969` — a plausible Buddy-firmware-style string
that matches the MK4S/Core One naming convention. Override in
`konnect.cfg`, or leave empty to report whatever Klippy's
`machine.system_info` exposes as the OS distribution version.

## Install

Tested on MainsailOS / FluiddPi on Raspberry Pi. Requires Python 3.9+.
Run as the user that owns `~/printer_data` (not root).

```sh
cd ~
tar -xzf konnect-0.2.0.tar.gz
mv konnect-0.2.0 konnect    # or however the tarball unpacks
./konnect/scripts/install.sh
```

That will:

1. Create `~/konnect-env` virtualenv and `pip install -e` the package (SDK
   pinned to a master commit that has `Command.SET_VALUE` for chamber
   control — the PyPI 0.8.1 release predates it)
2. Seed `~/printer_data/config/konnect.cfg` with MK4S defaults
3. Install `konnect.service` as a systemd unit and start it
4. Add `konnect` to the moonraker-allowed services file
   (`~/printer_data/moonraker.asvc` if that's what your setup uses,
   otherwise `/etc/moonraker.asvc`) so Moonraker can restart konnect via
   update_manager
5. Inject a `location /konnect/ {...}` block **inside** the existing
   Mainsail/Fluidd `server{}` in your nginx site config (marker-wrapped for
   easy removal); runs `nginx -t` and rolls back on failure
6. Append `[update_manager konnect]` to `moonraker.conf` for auto-updates,
   filtering out any sections (e.g. `[authorization]`) that already exist
   to avoid moonraker parse errors
7. Symlink `klipperscreen/panels/konnect.py` into `~/KlipperScreen/panels/`,
   add the menu-include entry **before** KlipperScreen's
   `#~# --- Do not edit below this line ---` marker (so KS doesn't wipe it
   on shutdown), and install `qrcode[pil]` into KlipperScreen's venv

Every change is recorded in `~/.konnect/install-<TS>/manifest.txt`, with
pre-modification backups in `~/.konnect/install-<TS>/backup/` — see
[scripts/status.sh](scripts/status.sh) to inspect.

Flags: `--skip-venv`, `--no-klipperscreen`, `--no-nginx`,
`--moonraker-conf <path>`, `--konnect-port <n>`.

## Use it

1. Open `http://<printer-ip>/konnect/` (proxied through Mainsail/Fluidd's
   nginx) — or `http://<printer-ip>:7130/` if you prefer direct.
2. **Printer type** — MK4S (default) or Core One. See the table above.
3. **Identity** — accept the MAC-derived default serial, or set your own.
   Name/location optional. Once registered, the serial is locked
   (fingerprint is `sha256(sn + cpu_serial)×100`, which Connect binds to
   your token).
4. **Register** — click *Get code*, enter the 8-char code at
   [connect.prusa3d.com/add](https://connect.prusa3d.com/add) or scan the QR
   on the page. The browser polls and auto-advances when Connect finishes
   registration.
5. **Webcam (optional)** — pick a detected Crowsnest stream or enter a
   custom snapshot URL. Skip if you don't have one.
6. **Done.** Dashboard shows state, token, and camera status. The same
   flow is available on KlipperScreen's "Prusa Connect" tile for
   touchscreen use.

## Verify & troubleshoot

```sh
./konnect/scripts/status.sh          # audit manifest + installed state
systemctl status konnect             # service state
tail -f ~/printer_data/logs/konnect.log     # app log (also in journald)
journalctl -u konnect -f                    # systemd journal
curl http://127.0.0.1:7130/status    # live state probe
curl -X POST http://127.0.0.1:7130/resync   # force-reconcile with Moonraker
```

Common issues:

- **"Printer isn't initialised"** — konnect is waiting on Moonraker. Check
  `moonraker` is active and `http://127.0.0.1:7125/server/info` returns 200.
- **State stuck after cancel/abort** — should self-heal within 60 s via
  periodic resync; `curl -X POST http://127.0.0.1:7130/resync` triggers it
  immediately.
- **No webcam detected** — `crowsnest.conf` needs a `[cam <name>]` section
  with a `port`. You can also enter a custom snapshot URL in the UI.
- **Chamber controls missing in Connect** — you're registered as `MK4S`.
  Set `printer_type = COREONE` in `konnect.cfg`, Unregister, restart
  konnect, re-register.
- **KlipperScreen panel not showing** — restart KlipperScreen:
  `sudo systemctl restart KlipperScreen`.
- **Ready toggle in Connect doesn't update the printer** — only MK4S /
  Core One types get `SET_PRINTER_READY` commands dispatched. Older types
  (I3MK3S, etc.) don't, even though Connect's UI shows the button.
- **HTTP debug** — set `Environment=KONNECT_HTTP_SPY=1` in a
  `/etc/systemd/system/konnect.service.d/*.conf` override and restart to
  log every Connect request/response to `/tmp/konnect_http.log`.

## Uninstall

```sh
./konnect/scripts/uninstall.sh           # surgical — preserves post-install edits
./konnect/scripts/uninstall.sh --restore-backup   # byte-identical revert from backups
./konnect/scripts/uninstall.sh --dry-run          # preview without applying
```

Flags: `--keep-venv`, `--keep-config`, `--keep-db`, `--keep-backups`,
`--install-dir <path>`, `--yes`.

## License

[CC BY-NC 4.0](LICENSE.txt) (Creative Commons Attribution-NonCommercial 4.0
International). You may share and adapt the material for **non-commercial
purposes only**, with attribution. Commercial use is strictly forbidden.
Don't be jerks.

Uses the open-source/freeware codebase from
[prusa.connect.sdk.printer](https://github.com/prusa3d/Prusa-Connect-SDK-Printer);
architecture informed by Trilab's HT90 firmware integration.
