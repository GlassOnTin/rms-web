# deploy/ — systemd units & live-view stack (reference copies)

These are the version-controlled copies of the units and configs that run the
dashboard **and** the live WebRTC feed on the station. They are installed
out-of-band (not by pip); this dir is the source of truth — edit here, then copy
to the runtime path below.

## Dashboard
| repo file | runtime path | notes |
|---|---|---|
| `rms-web.service` | `/etc/systemd/system/` | the read-only LAN dashboard (`:8080`) |
| `rms-tonight-preview.service` | `/etc/systemd/system/` | live `/tonight` preview builder (`make_tonight_preview.py`) |
| `rms-neighbours-refresh.{service,timer}` | `/etc/systemd/system/` | nightly neighbours map |
| `rms-scorecard-refresh.{service,timer}` | `/etc/systemd/system/` | nightly scorecard |

## Live view (WebRTC)
The camera feed shown on `/tonight` ("Live view") and linked from the header
(`● live view` → `:8889/cam1`) is a separate stack:

```
IMX296 --rpicam-vid--> ffmpeg (rawvideo) --> v4l2loopback
                                              ├── /dev/video10  (RMS science capture reads this)
                                              └── /dev/video11  (web tee)
/dev/video11 --> mediamtx runOnDemand ffmpeg (scale + brightness stretch) --> WebRTC :8889/cam1
```

| repo file | runtime path | notes |
|---|---|---|
| `rms-csi-bridge.service` | `/etc/systemd/system/` | runs the bridge script below |
| `rms-csi-bridge.sh` | `/home/ian/source/rms-csi-bridge.sh` | rpicam-vid → ffmpeg → v4l2loopback (video10 + video11 tee) |
| `mediamtx.service` | `/etc/systemd/system/` | the mediamtx WebRTC server |
| `mediamtx.yml` | `/home/ian/source/mediamtx/mediamtx.yml` | `cam1` path = on-demand h264 encode of `/dev/video11`; the `-vf` carries the brightness stretch |

**Brightness stretch:** `mediamtx.yml`'s `cam1` filter is
`scale=728:544,gblur=sigma=1.0,normalize=smoothing=15` — a per-frame adaptive
auto-levels (like the dashboard's `sky_stretch`) so the dim raw sensor feed shows
cloud/sky detail dusk-to-dawn without blowing out at twilight. `gblur` first so a
hot pixel can't steal `normalize`'s range.

## Deploy / restart notes
- **mediamtx.yml hot-reloads** — mediamtx watches the file and reloads on change
  (the new `runOnDemand` command applies on the next viewer). **No restart needed**,
  so no cascade to capture.
- **rms-csi-bridge.sh** changes need `sudo systemctl restart rms-csi-bridge` — and
  because `rms-csi-bridge` `Requires=mediamtx` and capture reads its loopback, that
  **bounces `rms-capture`** (a brief science-capture blip). Restart sparingly.
- **Do not `systemctl restart mediamtx`** unless necessary: the `Requires=` chain
  cascades bridge → capture. Prefer the hot-reload.
