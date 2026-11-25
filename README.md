# rms-web

A tiny, read-only **LAN web dashboard for an [RMS](https://github.com/CroatianMeteorNetwork/RMS) /
[Global Meteor Network](https://globalmeteornetwork.org/) meteor-camera station**, plus a
**"neighbours" map** showing nearby stations and where their fields of view overlap yours.

![Tonight's cumulative stack, annotated live](docs/hero_stack.png)
*The **/tonight** page's live cumulative stack of the night so far — star trails, satellites and
aircraft — annotated in real time through the station's own astrometric calibration (platepar):
bright-star labels ride the trail heads, with the celestial pole, cardinal directions and (when
up) the Moon, planets and ecliptic. If a label drifts off its star, your astrometry has shifted —
the overlay doubles as a free calibration monitor.*

RMS itself has no local web UI — you review the night with desktop tools or wait for the GMN
site. This fills that gap: point a browser at the Pi and browse last night's stacks, detections,
timelapses and reports, watch tonight's capture build in real time, jump to the live camera, and
see your place in the network.

No framework, four pieces:

- **`rms_web.py`** - a single-file HTTP server (Python **standard library only**). Serves the
  contents of `RMS_data/` as a browsable dashboard: nightly captured stacks, interactive meteor
  study pages (`/det`, `/fr`, `/ff`) with frame-by-frame playback, SVG light curves and track
  linearity plots, pre-ML candidate inspection, raw `FR_*.bin` fireball cutouts, arbitrary 10 s
  FF block lookup by UTC time, timelapses, a live-view link (MediaMTX WebRTC), and the neighbours
  page. Path-sanitised (no directory traversal).
- **`detection_media.py`** - lazy media and metadata builder invoked by `rms_web.py` inside the
  RMS virtualenv (`nice -n 15`). Reads `FTPdetectinfo` (confirmed and `_unfiltered.txt`),
  `*_radiants.txt`, `FR_*.bin` cutouts, and `FF_*.fits` blocks (with fallback to `CapturedFiles/`
  while still on disk), writing thumbnails, sky-annotated frames, `maxpixel - avepixel` residuals,
  and 12.5 fps MP4 clips into `detcache/`.
- **`make_tonight_preview.py`** — a small daemon behind the **/tonight** page: incrementally
  stacks tonight's FF files as they are written and renders the latest-sky and cumulative-stack
  previews. Its overlay modules annotate each render through the live platepar:
  - `sky_overlay.py` — bright stars (IAU names), Moon + planets (pyephem), ecliptic, north
    celestial pole and N/E/S/W, all projected with `RMS.Astrometry` so labels land exactly where
    the calibration says they should;
  - `sat_overlay.py` — sunlit satellite passes (CelesTrak TLEs, ~11k incl. Starlink, SGP4 via
    pyephem) drawn as faint streaks and logged to a pass table;
  - `adsb_overlay.py` — near-overhead aircraft from live ADS-B ([adsb.fi](https://adsb.fi)),
    dead-reckoned to the frame time, labelled with callsign + altitude and logged.

  Together with the page's live per-FF detection counts, every streak in the stack is
  accounted for: **matches a satellite pass → satellite; matches an aircraft pass → aircraft;
  neither → meteor candidate.**
- **`make_neighbours_map.py`** — renders `neighbours_map.png` + `neighbours.json`: an
  OpenStreetMap basemap with your camera's ~100 km-altitude footprint, nearby GMN stations
  (classified by triangulation baseline), and a few real GMN 100 km field-of-view polygons so
  the sky overlap is visible. `--refresh` re-pulls the live station list from GMN first.

## Screenshots

**Latest sky (~10 s)** — one FF block, annotated live; predicted satellite streaks are drawn
faint so the sky stays legible:

![Latest sky with live annotation](docs/hero_latest.png)

**Neighbours** — your triangulation partners and where their 100 km fields of view overlap
yours (each station links to its GMN weblog):

![Neighbours map](docs/neighbours_map.jpg)

## Requirements

- **Server:** Python 3.9+ — no third-party packages.
- **Neighbours map:** `matplotlib`, `pillow` (`pip install -r requirements.txt`).
- **Tonight preview + overlays:** run inside your RMS virtualenv (needs `RMS`, `numpy`,
  `pillow`, `pyephem`). Optional — the dashboard works without them.
- Internet access (for the neighbours map only: OSM tiles + GMN KML). Tiles are cached in
  `osm_cache/`.

## Quick install (systemd)

On a standard GMN Raspberry Pi or Linux station (`~/source/RMS`, `~/vRMS`), clone and run
`install.sh`:

```bash
cd ~/source
git clone https://github.com/GlassOnTin/rms-web.git
cd rms-web
./install.sh
```

`install.sh` reads `stationID` and `data_dir` from `~/source/RMS/.config`, installs `matplotlib`
and `pillow` into `~/vRMS` if missing, writes `/etc/rms-web/rms-web.env`, enables the dashboard
(`rms-web.service`), live preview daemon (`rms-tonight-preview.service`), and background refresh
timers (`neighbours` + `scorecard`), and builds the initial neighbours map.

For a second camera on the same host:

```bash
./install.sh --instance cam2 --rms-source ~/source/RMS-cam2 --venv ~/vRMS-cam2 --port 8081
```

Preview without root via `./install.sh --dry-run`; remove units via `./install.sh --uninstall`.

## Run manually

```bash
# dashboard (auto-discovers data_dir and stationID from ~/source/RMS/.config, listens on :8080)
python3 rms_web.py

# generate the neighbours map
~/vRMS/bin/python make_neighbours_map.py --refresh
```

Then open `http://<host>:8080/`.

### Configuration (environment variables)

| Variable | Default | Used by |
|---|---|---|
| `RMS_CONFIG` | `~/source/RMS/.config` | config file read for `data_dir`, `stationID`, `latitude`, `longitude`, `device` |
| `RMS_DATA` | `data_dir` from `.config` (`~/RMS_data`) | server + preview - path to RMS data dir |
| `RMS_WEB_PORT` | `8080` | server - listen port |
| `STATION_LAT` / `STATION_LON` | from RMS `.config` | map + Europe cloud chart - camera position |
| `STATION_NAME` | `stationID` from RMS `.config` | map + header label |
| `RMS_SOURCE` | `~/source/RMS` | media/overlays - path to local RMS clone |
| `RMS_WEB_VENV_PY` | `~/vRMS/bin/python3` | server - RMS virtualenv Python for `detection_media.py` |
| `RMS_CAPTURE_SERVICE` | `rms-capture` | server - systemd unit checked alongside `RMS.StartCapture` + FF file age |
| `RMS_WEB_DETCACHE` | `<repo>/detcache` | server - cache directory for per-event JPEGs/MP4s |
| `RMS_WEB_CALIB` | `0` | server - set `1` to expose `/tonight` platepar solve/refit/bump buttons |
| `STATION_FOOTPRINT_KM` | `100` | map - approx ground radius covered at 100 km altitude |

## Security

LAN use only — there is **no authentication**. It is read-only and only serves image/text/mp4
files from within the data dir, but do not expose it to the public internet.

## Attribution & licence

- Basemap © **OpenStreetMap** contributors (ODbL). Please respect the
  [OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/).
- Station field-of-view data: **Global Meteor Network** (CC BY 4.0).
- Code: **MIT** — see [LICENSE](LICENSE).
