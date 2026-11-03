# konnect on the Snapmaker U1 (extended firmware mod)

A composable mod for
[SnapmakerU1-Extended-Firmware](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware)
that puts **Konnect** on the U1 itself:
the printer registers with your self-hosted PCL server over the xBuddy
WebSocket protocol and shows up there as a Prusa XL-style toolchanger
with all four heads.

## What the mod installs

Following the firmware's app conventions (OctoEverywhere is the
template):

| path | role |
|---|---|
| `/usr/local/share/extended-pkg/konnect` | package definition — `extended-pkg konnect download` fetches a pinned konnect release and builds its venv under `/oem/apps/konnect/` |
| `/usr/local/sbin/konnectd` | launcher — seeds `konnect.cfg` (backend pcl, printer_type XL), copies the PCL server address from `extended2.cfg`, execs `python -m konnect` |
| `/etc/init.d/S98konnect` | SysV service — gated on `remote_access.cloud = konnect`, 80M memory cap via limits-helper |
| `/etc/nginx/fluidd.d/konnect.conf` | onboarding UI at `http://<printer>/konnect/` |
| `firmware-config/functions/26_…/27_….yaml` | adds **konnect** as a provider in the stock *Cloud Access (Experimental)* dropdown (deep-merged into the stock item), plus an upgrade quick-action |
| `patches/etc/init.d/S99cloud.patch` | provider dispatch — selecting konnect starts it; selecting *None*/*OctoEverywhere* stops it (and vice versa), through the stock settings commands |
| `patches/etc/init.d/S99firmware-config.patch` | raises firmware-config's cgroup cap 48M→72M (observed oom-kill at the stock cap) |

konnect state (registration token, serial) lives in moonrakerdb;
`konnect.cfg` and logs live under `/home/lava/printer_data`, which
persists across reboots.

## Building the firmware

```sh
cd SnapmakerU1-Extended-Firmware
cp -r <konnect>/contrib/u1/overlays/mods/hanzov69 overlays/mods/
./dev.sh make build PROFILE=extended-hanzov69
```

## Before building: pin the release

`extended-pkg` installs a pinned release asset with a verified checksum.
The package definition points at the `konnect-u1-<version>.zip` asset of
the `u1-<version>` release on `github.com/hanzov69/konnect`. Cutting a
release:

```sh
scripts/release-u1.sh            # builds dist/konnect-u1-<v>.zip, prints sha256
git tag u1-<v> && git push origin u1-<v>
gh release create u1-<v> dist/konnect-u1-<v>.zip
# paste the printed sha256 into PKG_SHA256 in
# root/usr/local/share/extended-pkg/konnect
```

## Using it

1. Flash the firmware, SSH in (see the firmware's docs), set the PCL
   server:
   ```sh
   extended-config.py add /home/lava/printer_data/config/extended/extended2.cfg konnect pcl_host your-server.lan
   extended-config.py add /home/lava/printer_data/config/extended/extended2.cfg konnect pcl_port 8823
   ```
2. In the firmware-config web UI, set **Remote Access → Cloud Access
   (Experimental)** to *Konnect* (downloads the
   package on demand; mutually exclusive with OctoEverywhere, like any
   provider), or by hand:
   ```sh
   extended-pkg konnect download
   extended-config.py add /home/lava/printer_data/config/extended/extended2.cfg remote_access cloud konnect
   /etc/init.d/S99cloud restart
   ```
3. Open `http://<printer-ip>/konnect/`, click *Get code*, then approve
   the printer on your PCL server's *Add a printer* page.

Per-head temperatures of all four toolheads are reported the Buddy way
(`slot` telemetry); uploads from PCL's dashboard land in
`/home/lava/printer_data/gcodes` over the WebSocket inline transfer.

Camera: PCL pulls streams itself. Point a camera resource on the server
at the U1's camera snapshot endpoint (see the firmware's
`camera_support` docs for the URL on your build).

## Notes

- `printer_type = XL` is seeded because the XL is PCL's toolchanger
  profile; konnect auto-detects the U1's `extruder`…`extruder3`.
- The mod never touches stock files — it is all new `root/` files, so
  it composes cleanly with other mods and survives rebases onto new
  firmware releases.
- Raw G-code sent by the server is forwarded to Klipper; set
  `remote_gcode = false` in `konnect.cfg` to refuse it.
