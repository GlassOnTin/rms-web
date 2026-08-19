#!/usr/bin/env python3
"""
adsb_overlay.py — aircraft labels for the tonight-preview latest-sky image plus
a pass log (air_passes.json) for the /tonight page. Companion to sat_overlay:
satellites via TLE, aircraft via ADS-B; an unlabelled streak is a meteor.

Positions come from the adsb.fi open-data API (Ian feeds adsb.fi) centred on the
station, cached ~15 s. Each aircraft is dead-reckoned along its reported
track/groundspeed from its last fix to the FF block's start/end times, converted
to az/el with flat-earth ENU (fine at <20 km), then through altAz2RADec and the
live platepar. Only near-overhead traffic (el > 45 deg) enters a zenith FOV.
"""
import os, json, time, math, calendar, urllib.request
import datetime as _dt
import numpy as np

from sky_overlay import _platepar, _getfont, raDecToXYPP, date2JD
from RMS.Astrometry.Conversions import altAz2RADec

HERE = os.path.dirname(os.path.abspath(__file__))
PASSES_JSON = os.path.join(HERE, "air_passes.json")
URL = "https://opendata.adsb.fi/api/v2/lat/{:.4f}/lon/{:.4f}/dist/20"
BLOCK_S = 10.24
EL_MIN = 45.0                       # deg; zenith FOV bottoms out ~50
AIR_COL = (255, 120, 120)
ROSE = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")

_cache = {"t": 0.0, "now": 0.0, "ac": []}

def _fetch(pp):
    t = time.time()
    if t - _cache["t"] < 15:
        return _cache["ac"], _cache["now"]
    _cache["t"] = t                       # even on failure: don't hammer the API
    try:
        with urllib.request.urlopen(URL.format(pp.lat, pp.lon), timeout=8) as r:
            d = json.load(r)
        _cache["ac"] = d.get("aircraft") or []
        _cache["now"] = float(d.get("now", t))
    except Exception:
        pass                              # stale (or empty) cache keeps working
    return _cache["ac"], _cache["now"]

def _enu_azel(pp, lat, lon, alt_m):
    north = (lat - pp.lat) * 111320.0
    east = (lon - pp.lon) * 111320.0 * math.cos(math.radians(pp.lat))
    up = alt_m - float(pp.elev)
    horiz = math.hypot(north, east)
    return math.degrees(math.atan2(east, north)) % 360.0, \
           math.degrees(math.atan2(up, horiz))

def _log_pass(night, dt, name, alt_ft, path_str, seg=None):
    """Log a pass; on repeat sightings of the same flight (a pass spans several FF
    blocks) EXTEND its recorded pixel segment so the log holds the full trail."""
    try:
        d = json.load(open(PASSES_JSON))
    except Exception:
        d = {}
    if d.get("dir") != night:
        d = {"dir": night, "passes": []}
    ts = dt.strftime("%H:%M:%S")
    def s(t):
        h, m, sec = t.split(":"); return int(h) * 3600 + int(m) * 60 + int(sec)
    for p in d["passes"][-40:]:
        if p["name"] == name and abs(s(p["t"]) - s(ts)) < 240:
            if seg:                                   # stretch trail to latest block's end
                p["x1"], p["y1"] = round(seg[2]), round(seg[3])
                json.dump(d, open(PASSES_JSON, "w"))
            return
    rec = {"t": ts, "name": name, "alt": alt_ft, "path": path_str}
    if seg:
        rec.update(x0=round(seg[0]), y0=round(seg[1]), x1=round(seg[2]), y1=round(seg[3]))
    d["passes"].append(rec)
    d["passes"] = d["passes"][-200:]
    json.dump(d, open(PASSES_JSON, "w"))

def annotate_stack_aircraft(img, night):
    """Label tonight's logged aircraft trails on the cumulative stack, in place —
    the trail is burned into the maxpixel stack exactly where the live overlay saw
    it, so a historical label at the recorded pixel segment always sits on it."""
    from PIL import Image as _Image, ImageDraw
    try:
        d = json.load(open(PASSES_JSON))
    except Exception:
        return img
    if d.get("dir") != night:
        return img
    passes = [p for p in d.get("passes", []) if "x0" in p]
    if not passes:
        return img
    W, H = img.size
    layer = _Image.new("RGBA", img.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(layer)
    font = _getfont()
    def text(x, y, s):
        lx = min(max(x, 4), W - 9 * len(s) - 6)
        ly = min(max(y, 4), H - 20)
        dr.text((lx + 1, ly + 1), s, fill=(0, 0, 0, 220), font=font)
        dr.text((lx, ly), s, fill=AIR_COL + (230,), font=font)

    def clip(x0, y0, x1, y1):
        """Liang–Barsky: param range of the segment inside the frame, or None."""
        dx, dy = x1 - x0, y1 - y0
        t0, t1 = 0.0, 1.0
        for p, q in ((-dx, x0), (dx, W - x0), (-dy, y0), (dy, H - y0)):
            if p == 0:
                if q < 0: return None
                continue
            r = q / p
            if p < 0:
                if r > t1: return None
                t0 = max(t0, r)
            else:
                if r < t0: return None
                t1 = min(t1, r)
        return t0, t1

    # no line: the trail itself is bright in the stack, and ADS-B geometry (baro
    # altitude) sits degrees off the optics. Label the two points where the trail
    # crosses the frame border — where the aircraft enters and leaves the image —
    # entry with the full story, exit with a short tag, so crossing trails stay
    # attributable.
    for i, p in enumerate(passes):
        x0, y0, x1, y1 = p["x0"], p["y0"], p["x1"], p["y1"]
        c = clip(x0, y0, x1, y1)
        if c is None:
            continue
        dx, dy = x1 - x0, y1 - y0
        pad = 30.0 / max(1.0, (dx * dx + dy * dy) ** 0.5)   # ~30 px inward from the border
        (ta, tb) = c
        off = -20 + (i % 3) * 28                            # stagger, so tags sharing a corner miss each other
        ex, ey = x0 + (ta + pad) * dx, y0 + (ta + pad) * dy
        qx, qy = x0 + (tb - pad) * dx, y0 + (tb - pad) * dy
        text(ex + 8, ey + off, "✈ {} {:,}ft {}".format(p["name"], p["alt"], p["t"][:5]))
        text(qx + 8, qy + off, "✈ {}".format(p["name"].split()[0]))
    return _Image.alpha_composite(img.convert("RGBA"), layer).convert("RGB")

def annotate_aircraft(img, dt, night=""):
    """Draw predicted aircraft streaks for the FF block starting at UTC dt onto
    `img` (RGB PIL); log FOV passes. Returns img."""
    from PIL import ImageDraw
    pp = _platepar()
    W, H = img.size
    block_epoch = calendar.timegm(dt.timetuple())
    if time.time() - block_epoch > 90:
        return img                        # image too stale to match live traffic
    ac_list, data_now = _fetch(pp)
    if not ac_list:
        return img

    dr = ImageDraw.Draw(img)
    font = _getfont()
    for a in ac_list:
        try:
            lat, lon = a["lat"], a["lon"]
            alt = a.get("alt_geom", a.get("alt_baro"))
            if not isinstance(alt, (int, float)):
                continue                  # "ground" etc.
            gs = float(a.get("gs") or 0.0) * 0.514444        # kt -> m/s
            trk = math.radians(float(a.get("track") or 0.0))
            fix_epoch = data_now - float(a.get("seen_pos") or 0.0)
            alt_m = alt * 0.3048
            pts, azs = [], []
            for te in (block_epoch, block_epoch + BLOCK_S):
                dtt = te - fix_epoch
                la = lat + gs * dtt * math.cos(trk) / 111320.0
                lo = lon + gs * dtt * math.sin(trk) / (111320.0 * math.cos(math.radians(lat)))
                az, el = _enu_azel(pp, la, lo, alt_m)
                if el < EL_MIN:
                    pts = []; break
                t = _dt.datetime.utcfromtimestamp(te)
                jd = date2JD(t.year, t.month, t.day, t.hour, t.minute,
                             t.second + t.microsecond / 1e6)
                ra, dec = altAz2RADec(az, el, jd, pp.lat, pp.lon)
                x, y = raDecToXYPP(np.array([ra]), np.array([dec]), jd, pp)
                pts.append((float(x[0]), float(y[0])))
                azs.append(az)
        except Exception:
            continue
        if len(pts) != 2:
            continue
        (x0, y0), (x1, y1) = pts
        if not ((0 <= x0 <= W and 0 <= y0 <= H) or (0 <= x1 <= W and 0 <= y1 <= H)):
            continue
        name = (a.get("flight") or "").strip() or a.get("hex", "?")
        label = "{} {}ft".format(name, int(alt))
        dr.line([x0, y0, x1, y1], fill=AIR_COL, width=2)
        lx = min(max(x1 + 5, 4), W - 8 * len(label) - 6)
        ly = min(max(y1 - 8, 4), H - 20)
        dr.text((lx + 1, ly + 1), label, fill=(0, 0, 0), font=font)
        dr.text((lx, ly), label, fill=AIR_COL, font=font)
        if night:
            path_str = "{}→{}".format(ROSE[int(round(azs[0] / 45.0)) % 8],
                                           ROSE[int(round(azs[1] / 45.0)) % 8])
            _log_pass(night, dt, name, int(alt), path_str, seg=(x0, y0, x1, y1))
    return img
