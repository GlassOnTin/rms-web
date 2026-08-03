#!/usr/bin/env python3
"""
make_scorecard.py — nightly "you vs your neighbours" scorecard for the RMS dashboard.

Compares this station's last-night meteor count against how many meteors nearby GMN
stations triangulated the same night (from GMN's daily trajectory summary). Writes
scorecard.json for rms_web.py to render. Standard library only.

Caveat: the neighbour figures are multi-station *trajectory* participations (each
station detects more single-station meteors than get paired), and the GMN "yesterday"
daily file is a UTC day, so a UK night straddles a file boundary — treat as indicative.
"""
import os, re, json, math, glob, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RMS_DATA = os.environ.get("RMS_DATA", "/mnt/nvme/RMS_data")

def _rms_cfg(key, default=None):
    """Read a key (stationID/latitude/longitude) from the RMS .config so the
    scorecard tracks the live station identity. Env vars override; RMS_CONFIG
    sets the config path."""
    path = os.environ.get("RMS_CONFIG", os.path.expanduser("~/source/RMS/.config"))
    try:
        for line in open(path):
            line = line.split(";", 1)[0]
            if line.strip().startswith(key + ":"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return default

STATION_CODE = os.environ.get("STATION_CODE") or _rms_cfg("stationID") or "XX0001"
MLAT = float(os.environ.get("STATION_LAT") or _rms_cfg("latitude") or "50.8987692")
MLON = float(os.environ.get("STATION_LON") or _rms_cfg("longitude") or "-1.0586827")
TRAJ_URL = "https://globalmeteornetwork.org/data/traj_summary_data/daily/traj_summary_yesterday.txt"
UA = {"User-Agent": "rms-scorecard/1.0 (personal meteor station dashboard)"}
RADIUS_KM = 120.0

def hav(a, b, c, d):
    R = 6371; p = math.radians
    x = math.sin(p(c-a)/2)**2 + math.cos(p(a))*math.cos(p(c))*math.sin(p(d-b)/2)**2
    return 2*R*math.asin(math.sqrt(x))

# --- nearby stations from the cached UK station list --------------------------
near = {}
for line in open(os.path.join(HERE, "uk_stations.csv")):
    try:
        code, la, lo = line.split(",")
        d = hav(MLAT, MLON, float(la), float(lo))
        if d <= RADIUS_KM:
            near[code.strip()] = d
    except Exception:
        pass

# --- our own count for the latest processed night ----------------------------
our_count = None; our_dir = None; our_date = None
adirs = sorted(glob.glob(os.path.join(RMS_DATA, "ArchivedFiles", STATION_CODE + "_*")), reverse=True)
adirs = [d for d in adirs if os.path.isdir(d)]
if adirs:
    our_dir = os.path.basename(adirs[0])
    ftps = [f for f in glob.glob(os.path.join(adirs[0], "FTPdetectinfo_*.txt")) if "unfiltered" not in f]
    if ftps:
        m = re.search(r'Meteor Count\s*=\s*(\d+)', open(ftps[0]).read(500))
        if m: our_count = int(m.group(1))
    dm = re.search(r'_(\d{4})(\d{2})(\d{2})_', our_dir)
    if dm: our_date = f"{dm.group(1)}-{dm.group(2)}-{dm.group(3)}"

# --- GMN daily trajectories --------------------------------------------------
try:
    traj = urllib.request.urlopen(urllib.request.Request(TRAJ_URL, headers=UA), timeout=60).read().decode("utf-8", "replace")
except Exception:
    traj = ""
counts = {c: 0 for c in near}
total_global = 0; hit100 = 0; hit50 = 0
for line in traj.splitlines():
    if not line[:1].isdigit():
        continue
    total_global += 1
    codes = set(re.findall(r'[A-Z]{2}[0-9A-Za-z]{4}', line)) & set(near)
    if codes:
        for c in codes: counts[c] += 1
        if any(near[c] <= 100 for c in codes): hit100 += 1
        if any(near[c] <= 50 for c in codes): hit50 += 1

neigh = sorted(
    [{"code": c, "dist": round(near[c], 1), "count": counts[c],
      "url": f"https://globalmeteornetwork.org/weblog/{c[:2]}/{c}/"}
     for c in near if counts[c] > 0],
    key=lambda x: (-x["count"], x["dist"]))

out = {"our_code": STATION_CODE, "our_count": our_count, "our_date": our_date, "our_dir": our_dir,
       "total_global": total_global, "near_within_100": hit100, "near_within_50": hit50,
       "n_near": len(near), "n_active": len(neigh), "neighbours": neigh[:24]}
json.dump(out, open(os.path.join(HERE, "scorecard.json"), "w"), indent=1)
print(f"scorecard: us={our_count} ({our_date}) vs {hit100} within 100km / {hit50} within 50km; "
      f"{len(neigh)} active neighbours of {len(near)}; global {total_global}")
