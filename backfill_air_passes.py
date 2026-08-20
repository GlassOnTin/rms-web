#!/usr/bin/env python3
"""backfill_air_passes.py — reconstruct missed aircraft passes from adsb.fi's
published half-hour heatmap slices and append them to air_passes.json, in the
exact format the live logger (adsb_overlay._log_pass) writes, so the /tonight
stack labels pick them up on the next preview cycle.

Use when the live overlay was down (e.g. the 2026-08-20 User-Agent 403) but the
trails are already burned into the stack. Heatmaps publish ~2h behind realtime.

Heatmap format (readsb, reverse-engineered 2026-08-19): 16-byte records of
4x uint32 LE. u0==0x0E7F7C9D -> slice timestamp (ms = u1<<32|u2, u3=interval
30000). u1==0x40000000 -> callsign record (hex=u0&0xFFFFFF, 8 ASCII bytes in
words 2-3). Else position: hex=u0&0xFFFFFF, lat/lon = int32(u1|u2)/1e6,
u3 = int16 alt_baro/25ft (low) + int16 gs*10kt (high). Baro-only altitude ->
labels can sit 1-3 deg off the optical trail; the stack labeller only marks
frame-border crossings, so that is acceptable.

Run under the RMS venv:
  ~/vRMS/bin/python backfill_air_passes.py --date 2026/08/20 --start 20:31 --end 21:47
"""
import argparse
import calendar
import datetime
import json
import math
import os
import struct
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, "/home/ian/source/RMS")

import numpy as np
from RMS.Formats.Platepar import Platepar
from RMS.Astrometry.ApplyAstrometry import raDecToXYPP
from RMS.Astrometry.Conversions import date2JD, altAz2RADec
from adsb_overlay import _enu_azel, EL_MIN, ROSE, PASSES_JSON

HM_URL = "https://globe.adsb.fi/globe_history/{date}/heatmap/{slot:02d}.bin.ttf"
UA = {"User-Agent": "rms-web-backfill/1.0 (github.com/GlassOnTin/rms-web)"}
TS_MAGIC = 0x0E7F7C9D
CS_MAGIC = 0x40000000


def fetch_slot(date, slot):
    url = HM_URL.format(date=date, slot=slot)
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
            return r.read()
    except Exception as e:
        print("  slot %02d unavailable (%s)" % (slot, e))
        return None


def parse_heatmap(buf):
    """-> (positions {hex: [(epoch_s, lat, lon, alt_ft)]}, callsigns {hex: str})"""
    pos, calls = {}, {}
    t_slice = None
    for off in range(0, len(buf) - 15, 16):
        u = struct.unpack_from("<4I", buf, off)
        if u[0] == TS_MAGIC:
            t_slice = ((u[1] << 32) | u[2]) / 1000.0
            continue
        if u[1] == CS_MAGIC:
            hexid = u[0] & 0xFFFFFF
            cs = struct.unpack_from("<8s", buf, off + 8)[0].decode("ascii", "ignore").strip("\0 ")
            if cs:
                calls[hexid] = cs
            continue
        if t_slice is None:
            continue
        hexid = u[0] & 0xFFFFFF
        lat = struct.unpack_from("<i", buf, off + 4)[0] / 1e6
        lon = struct.unpack_from("<i", buf, off + 8)[0] / 1e6
        alt_q, _gs_q = struct.unpack_from("<2h", buf, off + 12)
        if not (-90 < lat < 90 and -180 < lon < 180):
            continue
        pos.setdefault(hexid, []).append((t_slice, lat, lon, alt_q * 25.0))
    return pos, calls


def hexdb_lookup(hexid):
    try:
        url = "https://hexdb.io/api/v1/aircraft/%06x" % hexid
        with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=10) as r:
            d = json.load(r)
        return (d.get("Registration") or "").strip()
    except Exception:
        return ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", required=True, help="YYYY/MM/DD (UTC)")
    ap.add_argument("--start", required=True, help="HH:MM UTC")
    ap.add_argument("--end", required=True, help="HH:MM UTC")
    ap.add_argument("--platepar", default="/home/ian/source/RMS/platepar_cmn2010.cal")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    y, mo, dy = [int(v) for v in args.date.split("/")]
    h0, m0 = [int(v) for v in args.start.split(":")]
    h1, m1 = [int(v) for v in args.end.split(":")]
    t0 = calendar.timegm((y, mo, dy, h0, m0, 0))
    t1 = calendar.timegm((y, mo, dy, h1, m1, 0))
    slots = sorted({int((t - calendar.timegm((y, mo, dy, 0, 0, 0))) // 1800)
                    for t in range(t0, t1, 300)})

    pp = Platepar()
    pp.read(args.platepar, use_flat=False)

    positions, callsigns = {}, {}
    for slot in slots:
        buf = fetch_slot(args.date, slot)
        if not buf:
            continue
        p, c = parse_heatmap(buf)
        callsigns.update(c)
        for k, v in p.items():
            positions.setdefault(k, []).extend(v)
        print("  slot %02d: %d aircraft, %d callsigns" % (slot, len(p), len(c)))

    night = "%04d%02d%02d" % (y, mo, dy)  # dusk-anchored; evening hours so date == night key
    new_passes = []
    for hexid, track in positions.items():
        track = sorted(t for t in track if t0 <= t[0] <= t1)
        if len(track) < 2:
            continue
        # in-FOV samples: elevation gate exactly like the live overlay
        vis = []
        for te, lat, lon, alt_ft in track:
            az, el = _enu_azel(pp, lat, lon, alt_ft * 0.3048)
            if el >= EL_MIN:
                vis.append((te, lat, lon, alt_ft, az))
        if len(vis) < 2:
            continue
        # pixel segment endpoints at first/last visible sample, live-overlay style
        seg, azs = [], []
        for te, lat, lon, alt_ft, az in (vis[0], vis[-1]):
            az, el = _enu_azel(pp, lat, lon, alt_ft * 0.3048)
            t = datetime.datetime.utcfromtimestamp(te)
            jd = date2JD(t.year, t.month, t.day, t.hour, t.minute, t.second)
            ra, dec = altAz2RADec(az, el, jd, pp.lat, pp.lon)
            x, yy = raDecToXYPP(np.array([ra]), np.array([dec]), jd, pp)
            seg.append((float(x[0]), float(yy[0])))
            azs.append(az)
        (x0, y0), (x1, y1) = seg
        W, H = 1456, 1088
        if not ((0 <= x0 <= W and 0 <= y0 <= H) or (0 <= x1 <= W and 0 <= y1 <= H)):
            continue
        name = callsigns.get(hexid) or hexdb_lookup(hexid) or ("%06x" % hexid)
        alt_ft = int(vis[0][3])
        path_str = "{}→{}".format(ROSE[int(round(azs[0] / 45.0)) % 8],
                                  ROSE[int(round(azs[1] / 45.0)) % 8])
        ts = datetime.datetime.utcfromtimestamp(vis[0][0]).strftime("%H:%M:%S")
        new_passes.append({"t": ts, "name": name, "alt": alt_ft, "path": path_str,
                           "x0": round(x0), "y0": round(y0),
                           "x1": round(x1), "y1": round(y1)})

    new_passes.sort(key=lambda p: p["t"])
    print("%d backfilled passes:" % len(new_passes))
    for p in new_passes:
        print("  %s %-8s %6d ft  %s  (%d,%d)->(%d,%d)" % (
            p["t"], p["name"], p["alt"], p["path"], p["x0"], p["y0"], p["x1"], p["y1"]))
    if args.dry_run:
        return

    try:
        d = json.load(open(PASSES_JSON))
    except Exception:
        d = {}
    if d.get("dir") != night:
        d = {"dir": night, "passes": []}
    have = {(p["name"], p["t"][:5]) for p in d["passes"]}
    added = [p for p in new_passes if (p["name"], p["t"][:5]) not in have]
    d["passes"] = sorted(d["passes"] + added, key=lambda p: p["t"])[-200:]
    json.dump(d, open(PASSES_JSON, "w"))
    print("appended %d (of %d) to %s" % (len(added), len(new_passes), PASSES_JSON))


if __name__ == "__main__":
    main()
