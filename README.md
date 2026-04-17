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

## Printer type

Prusa Connect has no "generic" type, so konnect advertises as one of
Prusa's own. The onboarding UI exposes three options, with different
trade-offs:

| Type | Telemetry | Set Ready | File list | Upload from Connect | Chamber controls |
|---|---|---|---|---|---|
| **`HT90`** (recommended) | ✓ | ✓ | ✓ legacy protocol | ✓ legacy `START_CONNECT_DOWNLOAD` | ✓ |
| `MK4S` | ✓ | ✓ | ✓ Buddy-style | ✗ requires Buddy WebSocket | — |
| `COREONE` | ✓ | ✓ | ✓ Buddy-style | ✗ requires Buddy WebSocket | ✓ |

Why the upload split: Connect uses `START_ENCRYPTED_DOWNLOAD` to push files
to MK4/MK4S/Core One printers, and the `/f/<iv>/raw` URL that serves the
ciphertext is only reachable while the printer is connected over Buddy's
WebSocket channel (`/p/ws`). The SDK konnect is built on uses HTTPS
long-polling, not WebSocket, so we can decrypt (AES-128-CTR) but can't
actually fetch the ciphertext. **HT90 sidesteps this** — its dashboard
uses `START_CONNECT_DOWNLOAD` / `START_URL_DOWNLOAD`, both of which work
over plain HTTPS and are implemented in the SDK's `DownloadMgr`.

If you pick MK4S/Core One for the modern dashboard, you can still upload
gcode via Mainsail/Fluidd and it shows up in Connect's file list (konnect
picks up the file via inotify and advertises it in the next
`SEND_FILE_INFO` response). The inverse — older `I3MK3S` etc. — is worse:
file listing works but Connect's UI doesn't dispatch `SET_PRINTER_READY`.

HT90 is the only type that gets you the whole workflow in Connect's UI.

The machinery for the extra types lives in
[konnect/printer_types.py](konnect/printer_types.py) (PrinterType enum
extensions) and [konnect/modern_fs.py](konnect/modern_fs.py) (Buddy-style
`storages` + per-path file info). If Prusa ever opens up the Buddy
WebSocket protocol, we can add the upload path without touching the rest.

> Change `printer_type` **before** first registration. Switching after
> registering makes Connect see a different printer type under the same
> fingerprint — click *Unregister* (or `curl -X DELETE
> http://<printer>/konnect/connection`) and re-register.

## Firmware version

Connect displays a `Firmware` field for each printer. By default konnect
picks a version string appropriate to your `printer_type`:

| `printer_type` | Default firmware string |
|---|---|
| `HT90` | `1.3.19+6969` |
| `MK4S` | `6.4.19+6969` |
| `COREONE` | `6.4.19+6969` |

The HT90 number matches real HT90 firmware release numbering; MK4S / Core
One match Prusa's Buddy firmware scheme — high enough that Connect's
modern-dashboard features unlock (upload button, etc.). The `+6969`
suffix marks the string as konnect rather than an authentic firmware
build.

Override by setting `firmware_version` in `~/printer_data/config/konnect.cfg`.
Leave it unset to use the defaults above.

## Install

Tested on MainsailOS / FluiddPi on Raspberry Pi. Requires Python 3.9+.
Run as the user that owns `~/printer_data` (not root).

### Quick install (curl one-liner)

```sh
curl -fsSL https://raw.githubusercontent.com/hanzov69/konnect/main/scripts/bootstrap.sh | bash
```

Passes flags through to `install.sh` via `bash -s --`:

```sh
curl -fsSL https://raw.githubusercontent.com/hanzov69/konnect/main/scripts/bootstrap.sh | \
    bash -s -- --no-klipperscreen
```

Env overrides (pin a release, use a fork, relocate the checkout):
`KONNECT_REF=0.2.1`, `KONNECT_REPO=…`, `KONNECT_DIR=…`.

### Manual install (tarball)

```sh
cd ~
tar -xzf konnect-0.2.1.tar.gz
mv konnect-0.2.1 konnect    # or however the tarball unpacks
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
- **Chamber controls do nothing** — you need `[heater_chamber]` in your
  Klipper config with a `min_temp` / `max_temp` / target temperature for
  Connect's chamber widget to have anything to drive.
- **KlipperScreen panel not showing** — restart KlipperScreen:
  `sudo systemctl restart KlipperScreen`.
- **Files not listed / uploads fail** — confirm `printer_type = HT90` in
  `konnect.cfg`. MK4-family and Core One dashboards on Connect don't
  honor the legacy file protocol konnect uses.
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
