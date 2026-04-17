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
  temp, handled via Moonraker RPC
- Your `~/printer_data/gcodes` exposed as the `/local` filesystem in Connect
- Webcam: auto-detects Crowsnest streams, pushes snapshots on Connect's
  trigger schedule
- Standalone onboarding UI at `http://<printer>/konnect/`
- KlipperScreen "Prusa Connect" tile in the main menu

## Pick a printer type

Prusa Connect has no "generic" type — konnect advertises as one of Prusa's own.
The choice affects which UI Connect shows; it does **not** require Prusa
hardware or constrain what gcode you can print. Default is `I3MK3S`. Edit
`~/printer_data/config/konnect.cfg` to change.

| `printer_type =` | When to pick | What Connect shows |
|---|---|---|
| **`I3MK3S`** (default) | Standard FDM without actively heated chamber. Voron Trident, Ender-class, RatRig, VzBot, BambuX1-style bed-slinger. | i3-family dashboard |
| **`HT90`** | Actively heated chamber (Klipper `[heater_chamber]` defined with a target). Voron 2.4 with chamber heater, industrial enclosed printers. | Industrial dashboard **with chamber temperature controls** |
| `I3MK3` / `I3MK25` / `I3MK25S` | Legacy MK variants. Rarely better than `I3MK3S`. | Same UI family |
| `M1` | Prusa M1. | M1 dashboard |
| `SL1` / `SL1S` | MSLA resin — **not recommended for FDM.** | Resin dashboard |

> Change `printer_type` **before** first registration. Switching after
> registering makes Connect see a different type under the same fingerprint;
> `DELETE /connection` (or the Unregister button) and re-register.

## Install

Tested on MainsailOS / FluiddPi on Raspberry Pi. Requires Python 3.9+.
Run as the user that owns `~/printer_data` (not root).

```sh
cd ~
tar -xzf konnect-0.1.18.tar.gz
mv konnect-0.1.18 konnect    # or however the tarball unpacks
./konnect/scripts/install.sh
```

That will:

1. Create `~/konnect-env` virtualenv and install the package (pinned SDK
   commit — contains `Command.SET_VALUE` for chamber control)
2. Seed `~/printer_data/config/konnect.cfg` (edit `printer_type` here)
3. Install `konnect.service` as a systemd unit, start it
4. Add `konnect` to `/home/<user>/printer_data/moonraker.asvc` (or
   `/etc/moonraker.asvc`, whichever exists) so Moonraker can restart konnect
5. Inject a `location /konnect/ {...}` block into your Mainsail/Fluidd nginx
   site config (marker-wrapped); runs `nginx -t` and rolls back if it fails
6. Append `[update_manager konnect]` to `moonraker.conf` for auto-updates
7. Symlink `klipperscreen/panels/konnect.py` into `~/KlipperScreen/panels/`,
   include the menu entry, install `qrcode[pil]` into KlipperScreen's venv

Every change is recorded in `~/.konnect/install-<TS>/manifest.txt`, with
pre-modification backups in `~/.konnect/install-<TS>/backup/`.

Flags: `--skip-venv`, `--no-klipperscreen`, `--no-nginx`,
`--moonraker-conf <path>`, `--konnect-port <n>`.

## Use it

1. Open `http://<printer-ip>/konnect/` (proxied through Mainsail/Fluidd's
   nginx) — or `http://<printer-ip>:7130/` if you prefer direct.
2. **Step 1 — printer type:** see the table above.
3. **Step 2 — identity:** accept the MAC-derived default serial, or set your
   own. Name/location optional. Once registered, the serial is locked.
4. **Step 3 — register:** click *Get code*, enter the 8-char code at
   [connect.prusa3d.com/add](https://connect.prusa3d.com/add) (or scan the
   QR on the page). The browser polls and auto-advances when Connect
   finishes registration.
5. **Step 4 — webcam (optional):** pick a detected Crowsnest stream or
   enter a custom snapshot URL. Skip if you don't have one.
6. **Done.** Dashboard shows state, token, and camera status. The same flow
   is available on KlipperScreen's "Prusa Connect" tile for touchscreen use.

## Verify & troubleshoot

```sh
./konnect/scripts/status.sh          # checks everything the installer recorded
systemctl status konnect             # service state
tail -f ~/printer_data/logs/konnect.log   # app log (also in journald)
curl http://127.0.0.1:7130/status    # live probe
```

Common issues:

- **"Printer isn't initialised"** — konnect is waiting on Moonraker. Check
  `moonraker` is active and `http://127.0.0.1:7125/server/info` returns 200.
- **No webcam detected** — check `crowsnest.conf` has a `[cam <name>]`
  section with a `port` value. You can also enter a custom snapshot URL in
  the UI.
- **Chamber controls missing in Connect** — you're registered as `I3MK3S`.
  Set `printer_type = HT90` in `konnect.cfg`, Unregister, re-register.
- **KlipperScreen panel not showing** — restart KlipperScreen:
  `sudo systemctl restart KlipperScreen`.

## Uninstall

```sh
./konnect/scripts/uninstall.sh           # surgical — preserves post-install edits
./konnect/scripts/uninstall.sh --restore-backup   # byte-identical revert from backups
./konnect/scripts/uninstall.sh --dry-run          # preview without applying
```

Flags: `--keep-venv`, `--keep-config`, `--keep-db`, `--keep-backups`.

## License

[CC BY-NC 4.0](LICENSE.txt) (Creative Commons Attribution-NonCommercial 4.0
International). You may share and adapt the material for **non-commercial
purposes only**, with attribution. Commercial use requires separate
permission from the author. Uses the open-source
[prusa.connect.sdk.printer](https://github.com/prusa3d/Prusa-Connect-SDK-Printer);
architecture informed by Trilab's HT90 firmware.
