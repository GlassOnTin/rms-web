# rms-web

A tiny, read-only **LAN web dashboard for an [RMS](https://github.com/CroatianMeteorNetwork/RMS) /
[Global Meteor Network](https://globalmeteornetwork.org/) meteor-camera station**, plus a
**"neighbours" map** showing nearby stations and where their fields of view overlap yours.

RMS itself has no local web UI — you review the night with desktop tools or wait for the GMN
site. This fills that gap: point a browser at the Pi and browse last night's stacks, detections,
timelapses and reports, jump to the live camera, and see your place in the network.

Two pieces, no framework:

- **`rms_web.py`** — a single-file HTTP server (Python **standard library only**). Serves the
  contents of `RMS_data/` as a browsable dashboard: nightly captured stacks, detection
  thumbnails, timelapses and observation reports, a live-view link (MediaMTX WebRTC), and the
  neighbours page. Read-only, GET-only, path-sanitised (no directory traversal).
- **`make_neighbours_map.py`** — renders `neighbours_map.png` + `neighbours.json`: an
  OpenStreetMap basemap with your camera's ~100 km-altitude footprint, nearby GMN stations
  (classified by triangulation baseline), and a few real GMN 100 km field-of-view polygons so
  the sky overlap is visible. `--refresh` re-pulls the live station list from GMN first.

## Screenshots

The dashboard home lists processed nights; the **Neighbours** page maps your triangulation
partners. (Add your own screenshots here.)

## Requirements

- **Server:** Python 3.9+ — no third-party packages.
- **Neighbours map:** `matplotlib`, `pillow` (`pip install -r requirements.txt`).
- Internet access (for the neighbours map only: OSM tiles + GMN KML). Tiles are cached in
  `osm_cache/`.

## Run

```bash
# dashboard (defaults: serves /mnt/nvme/RMS_data on :8080)
RMS_DATA=/mnt/nvme/RMS_data RMS_WEB_PORT=8080 python3 rms_web.py

# generate the neighbours map (defaults to Denmead, Hampshire — override via env)
STATION_LAT=50.90 STATION_LON=-1.06 STATION_NAME="My Site" \
  python3 make_neighbours_map.py --refresh
```

Then open `http://<host>:8080/`.

### Configuration (environment variables)

| Variable | Default | Used by |
|---|---|---|
| `RMS_DATA` | `/mnt/nvme/RMS_data` | server — path to RMS data dir |
| `RMS_WEB_PORT` | `8080` | server — listen port |
| `STATION_LAT` / `STATION_LON` | Denmead | map — your camera position |
| `STATION_NAME` | `Denmead` | map — label |
| `STATION_FOOTPRINT_KM` | `100` | map — approx ground radius covered at 100 km altitude |

## Deploy (systemd)

Example units in [`deploy/`](deploy/) (adjust the `User=` and the `/home/ian/...` paths for your
system):

- `rms-web.service` — runs the dashboard, `Restart=always`.
- `rms-neighbours-refresh.service` + `.timer` — regenerates the neighbours map weekly (so
  newly-joined stations appear).

```bash
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now rms-web.service rms-neighbours-refresh.timer
```

## Security

LAN use only — there is **no authentication**. It is read-only and only serves image/text/mp4
files from within the data dir, but do not expose it to the public internet.

## Attribution & licence

- Basemap © **OpenStreetMap** contributors (ODbL). Please respect the
  [OSM tile usage policy](https://operations.osmfoundation.org/policies/tiles/).
- Station field-of-view data: **Global Meteor Network** (CC BY 4.0).
- Code: **MIT** — see [LICENSE](LICENSE).
