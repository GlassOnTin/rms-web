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
`scale=728:544,tmix=frames=8,curves=all='0/0 0.05/0.4 0.15/0.75 0.4/0.95 1/1'`
with the encoder at `-crf 23`. The raw sensor feed is very dark (~4/255) and noisy,
so a strong `curves` shadow-lift makes cloud/sky detail visible. The two anti-flicker
pieces matter: **`tmix`** temporally averages 8 frames (kills the per-frame noise the
encoder chokes on), and **`-crf 23`** (constant-quality instead of a target bitrate)
stops the h264 rate-control from pulsing brightness on the noisy dark input. A fixed
curve (rather than adaptive `normalize`, which uses noisy min/max and needs a warm-up)
means there's no per-frame statistic to chase — so no adaptive flicker. Trade-off: the
fixed curve brightens toward white at twilight/dawn; if that's annoying, swap the
`curves=...` for `median=radius=1,normalize=smoothing=60` (adaptive, median makes it
percentile-robust) — hot-reloads, no restart.

## Deploy / restart notes
- **mediamtx.yml hot-reloads** — mediamtx watches the file and reloads on change
  (the new `runOnDemand` command applies on the next viewer). **No restart needed**,
  so no cascade to capture.
- **rms-csi-bridge.sh** changes need `sudo systemctl restart rms-csi-bridge` — and
  because `rms-csi-bridge` `Requires=mediamtx` and capture reads its loopback, that
  **bounces `rms-capture`** (a brief science-capture blip). Restart sparingly.
- **Do not `systemctl restart mediamtx`** unless necessary: the `Requires=` chain
  cascades bridge → capture. Prefer the hot-reload.
