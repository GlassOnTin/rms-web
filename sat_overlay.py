#!/usr/bin/env python3
"""
sat_overlay.py — satellite labels for the tonight-preview latest-sky image, and
a pass log for the /tonight page (the meteor-vs-satellite discriminator).

TLEs: CelesTrak groups (visual + stations + starlink), cached in tle_cache/ and
refreshed in a background thread when >18 h old (render never blocks; stale TLEs
keep working). Propagation: pyephem SGP4 — all ~11k sats compute in ~0.05 s.

Satellites cross the FOV in seconds, so labels only make sense on the LATEST
image: each in-FOV sunlit sat is drawn as its predicted streak across that
10.24 s block (start -> end, label at the head). The stack gets no marks; the
pass log (sat_passes.json) is its answer — match any streak by timestamp.
"""
import os, json, time, threading, urllib.request
import numpy as np
import ephem

from sky_overlay import _platepar, _getfont, raDecToXYPP, date2JD

HERE = os.path.dirname(os.path.abspath(__file__))
TLE_DIR = os.path.join(HERE, "tle_cache")
GROUPS = ("starlink-sup", "visual", "stations", "starlink", "active")   # dedupe keeps first: sup is freshest
TLE_URL = "https://celestrak.org/NORAD/elements/gp.php?GROUP={}&FORMAT=tle"
# Freshly launched batches (Starlink trains!) take days to reach the group
# files but appear in CelesTrak's supplemental feed almost immediately — an
# unmasked bright dashed train on 2026-08-20 motivated this. "active" covers
# OneWeb, rocket bodies and other operational sats missing from "visual".
SUP_URLS = {"starlink-sup":
            "https://celestrak.org/NORAD/elements/supplemental/sup-gp.php?FILE=starlink&FORMAT=tle"}
TLE_MAX_AGE = 18 * 3600
PASSES_JSON = os.path.join(HERE, "sat_passes.json")
BLOCK_S = 10.24                      # one FF block
ALT_MIN = 0.72                       # rad (~41 deg); zenith FOV bottoms out ~50
SAT_COL = (110, 255, 140)
ROSE = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")

_sats, _sats_key = [], None
_fetching = threading.Event()
_last_fetch_try = 0.0

def _fetch_tles():
    try:
        for g in GROUPS:
            path = os.path.join(TLE_DIR, g + ".tle")
            if os.path.isfile(path) and time.time() - os.path.getmtime(path) < TLE_MAX_AGE:
                continue
            req = urllib.request.urlopen(SUP_URLS.get(g) or TLE_URL.format(g), timeout=60)
            data = req.read()
            if len(data) > 500:                       # sanity: don't cache an error page
                tmp = path + ".tmp"
                open(tmp, "wb").write(data)
                os.replace(tmp, path)
    except Exception:
        pass                                          # stale cache keeps working
    finally:
        _fetching.clear()

def _load_sats():
    """Cached ephem sat objects, reparsed when any TLE file changes; kicks off a
    background refresh when the cache is old."""
    global _sats, _sats_key, _last_fetch_try
    os.makedirs(TLE_DIR, exist_ok=True)
    paths = [os.path.join(TLE_DIR, g + ".tle") for g in GROUPS]
    stale = any(not os.path.isfile(p) or time.time() - os.path.getmtime(p) > TLE_MAX_AGE
                for p in paths)
    if stale and not _fetching.is_set() and time.time() - _last_fetch_try > 3600:
        _last_fetch_try = time.time()
        _fetching.set()
        threading.Thread(target=_fetch_tles, daemon=True).start()
    key = tuple(os.path.getmtime(p) if os.path.isfile(p) else 0 for p in paths)
    if key != _sats_key:
        sats = []
        seen = set()                      # NORAD id; groups overlap heavily
        for p in paths:
            if not os.path.isfile(p):
                continue
            lines = open(p).read().strip().splitlines()
            for i in range(0, len(lines) - 2, 3):
                try:
                    cat = lines[i + 1][2:7]
                    if cat in seen:
                        continue
                    sats.append((lines[i].strip(), ephem.readtle(lines[i], lines[i+1], lines[i+2])))
                    seen.add(cat)
                except Exception:
                    pass
        _sats, _sats_key = sats, key
    return _sats

def _observer(pp, dt):
    obs = ephem.Observer()
    obs.lat, obs.lon = str(pp.lat), str(pp.lon)
    obs.elevation = float(pp.elev)
    obs.pressure = 0
    obs.date = dt.strftime("%Y/%m/%d %H:%M:%S.%f")
    return obs

def _topo_j2000(sat, obs):
    eq = ephem.Equatorial(sat.ra, sat.dec, epoch=obs.date)
    eq2 = ephem.Equatorial(eq, epoch=ephem.J2000)
    return np.degrees(float(eq2.ra)), np.degrees(float(eq2.dec))

def _log_pass(night, dt, name, path_str):
    """Append to the pass log, deduping the same sat within 4 min (a pass spans
    several FF blocks)."""
    try:
        d = json.load(open(PASSES_JSON))
    except Exception:
        d = {}
    if d.get("dir") != night:
        d = {"dir": night, "passes": []}
    ts = dt.strftime("%H:%M:%S")
    for p in d["passes"][-40:]:
        if p["name"] == name and abs(_hms_s(p["t"]) - _hms_s(ts)) < 240:
            return
    d["passes"].append({"t": ts, "name": name, "path": path_str})
    d["passes"] = d["passes"][-200:]
    json.dump(d, open(PASSES_JSON, "w"))

def _hms_s(t):
    h, m, s = t.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)

def streak_segments(dt, size=(1456, 1088)):
    """Predicted in-frame satellite streak segments for the FF block starting at
    UTC dt -> [(x0, y0, x1, y1)]. Geometry only (no drawing, no pass logging) —
    used by the tonight-preview daemon to mask known-satellite corridors out of
    the detected-meteors stack."""
    import datetime as _dt
    pp = _platepar()
    W, H = size
    segs = []
    mid = dt + _dt.timedelta(seconds=BLOCK_S / 2)
    obs = _observer(pp, mid)
    for name, sat in _load_sats():
        try:
            sat.compute(obs)
            if not (sat.alt > ALT_MIN and not sat.eclipsed):
                continue
            pts = []
            for t in (dt, dt + _dt.timedelta(seconds=BLOCK_S)):
                o = _observer(pp, t)
                sat.compute(o)
                ra, dec = _topo_j2000(sat, o)
                jd = date2JD(t.year, t.month, t.day, t.hour, t.minute,
                             t.second + t.microsecond / 1e6)
                x, y = raDecToXYPP(np.array([ra]), np.array([dec]), jd, pp)
                pts.append((float(x[0]), float(y[0])))
            (x0, y0), (x1, y1) = pts
            if (0 <= x0 <= W and 0 <= y0 <= H) or (0 <= x1 <= W and 0 <= y1 <= H):
                segs.append((x0, y0, x1, y1))
        except Exception:
            continue
    return segs


def annotate_sats(img, dt, night=""):
    """Draw predicted satellite streaks for the FF block starting at UTC dt onto
    `img` (RGB PIL). Also logs FOV passes. Returns img."""
    from PIL import ImageDraw
    pp = _platepar()
    W, H = img.size
    sats = _load_sats()
    if not sats:
        return img

    import datetime as _dt
    mid = dt + _dt.timedelta(seconds=BLOCK_S / 2)
    obs = _observer(pp, mid)
    cand = []
    for name, sat in sats:
        try:
            sat.compute(obs)
            if sat.alt > ALT_MIN and not sat.eclipsed:
                cand.append((name, sat))
        except Exception:
            pass

    # draw on a transparent layer, alpha-composited: streaks must whisper, not shout
    from PIL import Image as _Image
    layer = _Image.new("RGBA", img.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(layer)
    font = _getfont()
    drew = False
    for name, sat in cand:
        try:
            pts, azs = [], []
            for t in (dt, dt + _dt.timedelta(seconds=BLOCK_S)):
                o = _observer(pp, t)
                sat.compute(o)
                azs.append(np.degrees(float(sat.az)))
                ra, dec = _topo_j2000(sat, o)
                jd = date2JD(t.year, t.month, t.day, t.hour, t.minute,
                             t.second + t.microsecond / 1e6)
                x, y = raDecToXYPP(np.array([ra]), np.array([dec]), jd, pp)
                pts.append((float(x[0]), float(y[0])))
        except Exception:
            continue
        (x0, y0), (x1, y1) = pts
        # require a genuinely in-frame endpoint — a looser margin lets off-frame
        # streaks in, whose labels then clamp INTO the frame and float unanchored
        if not ((0 <= x0 <= W and 0 <= y0 <= H) or (0 <= x1 <= W and 0 <= y1 <= H)):
            continue
        dr.line([x0, y0, x1, y1], fill=SAT_COL + (55,), width=1)
        lx = min(max(x1 + 5, 4), W - 8 * len(name) - 6)
        ly = min(max(y1 - 8, 4), H - 20)
        dr.text((lx, ly), name, fill=SAT_COL + (135,), font=font)
        drew = True
        if night:
            path_str = "{}→{}".format(ROSE[int(round(azs[0] / 45.0)) % 8],
                                           ROSE[int(round(azs[1] / 45.0)) % 8])
            _log_pass(night, dt, name, path_str)
    if drew:
        img = _Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")
    return img
