"""Meteociel WRF-NMM 3-layer cloud-cover fetcher for European RMS stations.

Decodes the 'Couches nebulosite' map series into per-hour low/mid/high cloud
coverage at the station pixel and caches the result in wx_wrf.json for
rms_web.py to render. Called from the make_tonight_preview daemon loop; the
daemon's Pillow does the decoding, rms_web stays stdlib-only.
Station latitude/longitude come from STATION_LAT/STATION_LON or RMS .config
(rounded to 2 d.p., ~1 km); stations outside the Europe WRF-NMM domain cleanly
no-op.
"""

import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.request import Request, urlopen

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE_PATH = os.path.join(HERE, "wx_wrf.json")


def _rms_cfg(key, default=None):
    default_cfg = os.path.join(os.environ.get("RMS_SOURCE", "~/source/RMS"), ".config")
    path = os.path.expanduser(os.environ.get("RMS_CONFIG") or default_cfg)
    try:
        for line in open(path):
            line = line.split(";", 1)[0]
            if line.strip().startswith(key + ":"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return default


_raw_lat = os.environ.get("STATION_LAT") or _rms_cfg("latitude")
_raw_lon = os.environ.get("STATION_LON") or _rms_cfg("longitude")
STATION_LAT = round(float(_raw_lat), 2) if _raw_lat is not None else None
STATION_LON = round(float(_raw_lon), 2) if _raw_lon is not None else None

# Affine fit: x = A*lon + C, y = E*(lat-50) + F  (768x768 map pixels;
# the fit was done on the lat-50 offset, so keep that convention here)
FIT_A, FIT_C = 15.402, 305.87
FIT_E, FIT_F = -20.209, 442.114
if STATION_LAT is not None and STATION_LON is not None:
    PIXEL_X = int(round(FIT_A * STATION_LON + FIT_C))
    PIXEL_Y = int(round(FIT_E * (STATION_LAT - 50.0) + FIT_F))
    IN_BOUNDS = (34.0 <= STATION_LAT <= 62.0 and -15.0 <= STATION_LON <= 25.0
                 and 0 <= PIXEL_X < 768 and 0 <= PIXEL_Y < 768)
else:
    PIXEL_X = PIXEL_Y = -1
    IN_BOUNDS = False

# Palette value -> coverage percent (legend: 0/30/60/90/95/99).
# Pixels outside this exact palette (map background, borders, outlines) are
# rejected rather than snapped to the nearest level - see decode_pixel.
LEVELS = {0: 0, 102: 30, 136: 60, 170: 90, 204: 95, 255: 99}

BASE_URL = "https://modeles16.meteociel.fr/modeles/wrfnmm-eur/nmm-38-{ech}-0.png"
ECH_HOURS = list(range(1, 19))       # hourly frames 1..18 covers dusk to dawn
USER_AGENT = "rms-web/0.2 (+https://github.com/GlassOnTin/rms-web)"

UPDATE_INTERVAL_S = 600              # min seconds between fetch cycles
FRAME_DELAY_S = 1.0                  # pause between frames within a cycle
MAX_NEW_PER_CYCLE = 6                # after the first full fetch of a run

PILLOW_ERROR = None
try:
    from PIL import Image
except ImportError as _e:            # pragma: no cover
    PILLOW_ERROR = _e
    Image = None


def decode_pixel(png_bytes):
    """Return (low, mid, high) coverage percent at the station pixel.

    Strict: every sampled channel must be exactly one of the legend palette
    values. Map background (192,192,192) and border/outline colours are not in
    the palette; snapping them to the nearest legend entry would fabricate
    plausible-looking percentages for a station outside the plotted data, so
    such pixels raise instead and the frame is dropped (chart degrades to
    absent rather than lying). Limitation: dark map text (0,0,0) is
    indistinguishable from clear sky by value alone.
    """
    if Image is None:
        raise RuntimeError("PIL not importable: %s" % PILLOW_ERROR)
    im = Image.open(io.BytesIO(png_bytes)).convert("RGB")
    if not (0 <= PIXEL_X < im.width and 0 <= PIXEL_Y < im.height):
        raise ValueError("station pixel (%d,%d) outside the %dx%d map"
                         % (PIXEL_X, PIXEL_Y, im.width, im.height))
    r, g, b = im.getpixel((PIXEL_X, PIXEL_Y))
    if r not in LEVELS or g not in LEVELS or b not in LEVELS:
        raise ValueError("station pixel (%d,%d) is (%d,%d,%d) - not cloud data"
                         % (PIXEL_X, PIXEL_Y, r, g, b))
    return LEVELS[r], LEVELS[g], LEVELS[b]


def _infer_run(last_modified):
    """Guess the NWP run time from the image's Last-Modified header."""
    lm = parsedate_to_datetime(last_modified).astimezone(timezone.utc)
    slot = timedelta(hours=6)
    anchor = lm - timedelta(minutes=30)
    floor = datetime.min.replace(tzinfo=timezone.utc)
    run = floor + slot * int((anchor - floor).total_seconds() // slot.total_seconds())
    if lm - run > slot:
        run += slot
    return run


def _fetch_ech(ech):
    """Fetch one frame; return (bytes, inferred_run) or None on failure."""
    url = BASE_URL.format(ech=ech)
    try:
        req = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(req, timeout=30) as resp:
            data = resp.read()
            lm = resp.headers.get("Last-Modified")
    except Exception:
        return None
    if not lm or len(data) < 1000:
        return None
    return data, _infer_run(lm)


def _load_cache():
    try:
        with open(CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {"frames": {}}


def _save_cache(cache):
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=1)
    os.replace(tmp, CACHE_PATH)


def update(now=None):
    """Fetch any missing frames for the current run and refresh the cache.

    Self-throttled (UPDATE_INTERVAL_S); safe to call every daemon loop.
    Returns the cache dict, or None when nothing usable was fetched yet.
    """
    if not IN_BOUNDS:
        return None
    cache = _load_cache()
    last = cache.get("last_update_utc")
    if last:
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(last))
            if age.total_seconds() < UPDATE_INTERVAL_S:
                return cache if cache.get("frames") else None
        except ValueError:
            pass

    # Probe one frame to establish the current run.
    probe = _fetch_ech(ECH_HOURS[0])
    if probe is None:
        cache["last_update_utc"] = datetime.now(timezone.utc).isoformat()
        _save_cache(cache)
        return cache if cache.get("frames") else None
    data, run_dt = probe
    run_key = run_dt.strftime("%Y-%m-%dT%H:00Z")

    if cache.get("run_utc") != run_key:
        cache = {"run_utc": run_key, "frames": {}}

    frames = cache["frames"]
    if not frames:
        frames[str(ECH_HOURS[0])] = _frame_entry(run_dt, ECH_HOURS[0], data)

    todo = [e for e in ECH_HOURS if str(e) not in frames]
    if todo:
        first_fetch = len(frames) <= 1
        batch = todo if first_fetch else todo[:MAX_NEW_PER_CYCLE]
        for ech in batch:
            got = _fetch_ech(ech)
            if got is None:
                break                    # abort batch on first failure
            d, r = got
            if r.strftime("%Y-%m-%dT%H:00Z") != run_key:
                break                    # run rolled over mid-fetch; retry later
            frames[str(ech)] = _frame_entry(run_dt, ech, d)
            time.sleep(FRAME_DELAY_S)

    cache["last_update_utc"] = datetime.now(timezone.utc).isoformat()
    cache["station_pixel"] = [PIXEL_X, PIXEL_Y]
    if frames:
        _save_cache(cache)
        return cache
    return None


def _frame_entry(run_dt, ech, data):
    low, mid, high = decode_pixel(data)
    valid = run_dt + timedelta(hours=ech)
    return {"valid_utc": valid.strftime("%Y-%m-%dT%H:00Z"),
            "low": low, "mid": mid, "high": high}