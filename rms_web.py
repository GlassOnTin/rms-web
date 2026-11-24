#!/usr/bin/env python3
"""
rms_web.py — tiny read-only LAN dashboard for browsing RMS_data.

Serves the nightly RMS output (captured stacks, detection thumbnails, reports,
timelapses) over HTTP so they can be viewed in a browser without SSH. Read-only
(GET only), stdlib-only, path-sanitised to stay within RMS_data. NOT for exposure
to the public internet — LAN use only.
"""
import os, re, html, time, glob, datetime, urllib.parse, urllib.request, mimetypes, subprocess
import json, threading, statistics
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.environ.get("RMS_DATA", "/mnt/nvme/RMS_data")
ARCHIVED = os.path.join(ROOT, "CapturedFiles")   # nights live here; ArchivedFiles holds detections
ARCH_DET = os.path.join(ROOT, "ArchivedFiles")
PORT = int(os.environ.get("RMS_WEB_PORT", "8080"))
ASSET_DIR = os.path.dirname(os.path.abspath(__file__))   # neighbours_map.png / neighbours.json live here
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".mp4", ".txt", ".log"}

# Per-detection media (thumbnails / full frames / FF-reconstruction clips) is
# produced lazily by detection_media.py under the RMS venv (this server stays
# stdlib-only) and cached here, keyed on the night's FTPdetectinfo mtime.
DETCACHE = os.environ.get("RMS_WEB_DETCACHE") or os.path.join(ASSET_DIR, "detcache")
DET_INDEX_VERSION = 2
DETGEN = os.path.join(ASSET_DIR, "detection_media.py")
VENV_PY = os.path.expanduser(os.environ.get("RMS_WEB_VENV_PY", "~/vRMS/bin/python3"))
# systemd capture unit to report status for (2nd instances run their own unit name)
CAPTURE_SERVICE = os.environ.get("RMS_CAPTURE_SERVICE", "rms-capture")
# Calibration UI + solvers (tilt/bump/refit) are wired to the main station's
# RMS clone and .config; a 2nd instance without its own calibration keeps them off.
CALIB_TOOLS = os.environ.get("RMS_WEB_CALIB", "1" if CAPTURE_SERVICE == "rms-capture" else "0") == "1"

def rms_cfg(key, default=None):
    """Read a key (e.g. stationID) from the RMS .config so the header tracks the live station code."""
    path = os.environ.get("RMS_CONFIG", os.path.expanduser("~/source/RMS/.config"))
    try:
        for line in open(path):
            line = line.split(";", 1)[0]
            if line.strip().startswith(key + ":"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return default

# no baked-in identity: env override, else the station code from the RMS .config
STATION_NAME = os.environ.get("STATION_NAME") or rms_cfg("stationID") or "RMS"

def real_within(root, rel):
    """Resolve root/rel and confirm it stays inside root; else None (anti-traversal)."""
    p = os.path.realpath(os.path.join(root, rel))
    if p == os.path.realpath(root) or p.startswith(os.path.realpath(root) + os.sep):
        return p
    return None

_NIGHT_DATE = re.compile(r"_(\d{8}_\d{6})_")


def _night_datekey(name):
    """Sort key = the YYYYMMDD_HHMMSS timestamp, NOT the raw name. The station code
    prefix changes over a station's life -- a legacy 'XX0001' test id sorts after
    'UK00DY' (X > U) and would otherwise float stale test nights to the top."""
    m = _NIGHT_DATE.search(name)
    return m.group(1) if m else ""


def nights():
    """Detection-archive dirs (one per processed night), newest first (by date)."""
    try:
        ds = [d for d in os.listdir(ARCH_DET) if os.path.isdir(os.path.join(ARCH_DET, d))]
    except FileNotFoundError:
        ds = []
    return sorted(ds, key=_night_datekey, reverse=True)

def find_suffix(dirpath, suffix):
    try:
        for f in sorted(os.listdir(dirpath)):
            if f.endswith(suffix):
                return f
    except FileNotFoundError:
        pass
    return None

def fileurl(abspath):
    rel = os.path.relpath(abspath, ROOT)
    return "/file?p=" + urllib.parse.quote(rel)

# ---- per-detection media (see detection_media.py) --------------------------

_det_locks, _det_locks_guard = {}, threading.Lock()

def _det_lock(night):
    with _det_locks_guard:
        return _det_locks.setdefault(night, threading.Lock())

def _det_gen(mode, night_dir, cache_dir, *args, timeout=300):
    """Run the venv-side generator; False (never an exception) on any failure so
    the night page degrades to the montage-only view."""
    try:
        # nice: media reconstruction must yield CPU to live capture/detection
        r = subprocess.run(["nice", "-n", "15", VENV_PY, DETGEN, mode, night_dir, cache_dir, *map(str, args)],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            print("detection_media {} failed: {}".format(mode, r.stderr.strip()[-400:]))
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        print("detection_media {} failed: {}".format(mode, e))
        return False

def det_index(name):
    """(night_dir, cache_dir, index dict or None) for a night; (re)builds the
    v2 index (confirmed detections, unfiltered KHT candidates, FR raw clips,
    and captured FF availability) when missing or stale."""
    night_dir = real_within(ARCH_DET, name)
    if not night_dir or not os.path.isdir(night_dir):
        return None, None, None
    base = os.path.basename(night_dir)
    ftp = os.path.join(night_dir, "FTPdetectinfo_{}.txt".format(base))
    unf = os.path.join(night_dir, "FTPdetectinfo_{}_unfiltered.txt".format(base))
    mtimes = [int(os.path.getmtime(p)) for p in (ftp, unf) if os.path.isfile(p)]
    has_fr = any(f.startswith("FR_") and f.endswith(".bin") for f in os.listdir(night_dir))
    if not mtimes and not has_fr:
        return night_dir, os.path.join(DETCACHE, base), None
    src_mtime = max(mtimes) if mtimes else 0
    cap_dir = real_within(ARCHIVED, base)
    cap_present = bool(cap_dir and os.path.isdir(cap_dir))
    cache_dir = os.path.join(DETCACHE, base)
    jp = os.path.join(cache_dir, "detections.json")
    with _det_lock(name):
        idx = None
        if os.path.isfile(jp):
            try:
                idx = json.load(open(jp))
            except (OSError, ValueError):
                idx = None
        stale = (idx is None
                 or idx.get("version") != DET_INDEX_VERSION
                 or idx.get("src_mtime") != src_mtime
                 or (bool(idx.get("captured_available")) != cap_present))
        if stale:
            idx = None
            if _det_gen("index", night_dir, cache_dir):
                try:
                    idx = json.load(open(jp))
                except (OSError, ValueError):
                    idx = None
    return night_dir, cache_dir, idx

def det_media(name, i, kind, group="detections"):
    """Absolute path of detection i's thumb/full/ann/resid/video/cropvideo file
    in group ('detections' or 'unfiltered'), generating lazily on first request."""
    night_dir, cache_dir, idx = det_index(name)
    entries = (idx or {}).get(group, [])
    if not idx or not (0 <= i < len(entries)):
        return None
    d = entries[i]
    if kind not in d:
        return None
    p = os.path.join(cache_dir, d[kind])
    if not os.path.isfile(p) and d.get("has_ff"):
        with _det_lock(name):
            if not os.path.isfile(p):
                if kind == "thumb":
                    _det_gen("index", night_dir, cache_dir)
                else:
                    _det_gen(kind, night_dir, cache_dir, group, i)
    return p if os.path.isfile(p) else None

def fr_media(name, f_idx, kind):
    """Absolute path of FR_*.bin clip f_idx's thumb/full/video/cropvideo file."""
    night_dir, cache_dir, idx = det_index(name)
    clips = (idx or {}).get("fr_clips", [])
    if not idx or not (0 <= f_idx < len(clips)):
        return None
    fc = clips[f_idx]
    if kind not in fc:
        return None
    p = os.path.join(cache_dir, fc[kind])
    mode_map = {"full": "frfull", "video": "frvid", "cropvideo": "frcropvid"}
    if not os.path.isfile(p) and kind in mode_map:
        with _det_lock(name + ":fr"):
            if not os.path.isfile(p):
                _det_gen(mode_map[kind], night_dir, cache_dir, f_idx)
    return p if os.path.isfile(p) else None

def _blk_base(ff):
    """Cache basename for a flagged block — must match detection_media.blockBase."""
    stem = ff[3:] if ff.startswith("FF_") else ff
    parts = stem.replace(".fits", "").split("_")
    if len(parts) >= 4 and len(parts[1]) == 8 and parts[1].isdigit():
        core = "_".join(parts[1:])
    else:
        core = stem.replace(".fits", "")
    return "blk_" + core[:20]

def _ff_datetime(ff_name):
    """Parse FF_<STATION>_YYYYMMDD_HHMMSS_mmm_NNNNNNN.fits -> datetime or None."""
    m = re.match(r"^FF_[A-Za-z0-9]+_(\d{8})_(\d{6})_(\d{3})_\d+\.fits$", ff_name or "")
    if not m:
        return None
    try:
        dt = datetime.datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
        return dt + datetime.timedelta(milliseconds=int(m.group(3)))
    except ValueError:
        return None

def list_night_ffs(name):
    """Sorted unique FF_*.fits filenames available for `name` (CapturedFiles + ArchivedFiles)."""
    night_dir = real_within(ARCH_DET, name)
    if not night_dir or not os.path.isdir(night_dir):
        return []
    cap_dir = real_within(ARCHIVED, os.path.basename(night_dir))
    seen = set()
    for d in (cap_dir, night_dir):
        if d and os.path.isdir(d):
            try:
                for f in os.listdir(d):
                    if f.startswith("FF_") and f.endswith(".fits") and _ff_datetime(f):
                        seen.add(f)
            except OSError:
                pass
    return sorted(seen, key=_night_datekey)

def resolve_night_ff(name, ff_param="", time_param=""):
    """Resolve either an explicit `FF_*.fits` or a UTC time (`HH:MM:SS` / `HH:MM`) to
    `(ff_name, prev_ff, next_ff, total_count, delta_s)` within `name`."""
    ffs = list_night_ffs(name)
    if not ffs:
        return None, None, None, 0, 0.0
    chosen, delta_s = None, 0.0
    if ff_param and re.match(r"^FF_[A-Za-z0-9_]+\.fits$", ff_param):
        if ff_param in ffs:
            chosen = ff_param
    elif time_param:
        s = time_param.strip().upper().replace("UTC", "").replace("UT", "").strip()
        if s.startswith("FF_") and s.endswith(".FITS"):
            for f in ffs:
                if f.upper() == s:
                    chosen = f
                    break
        else:
            m = re.match(r"^(\d{1,2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?$", s)
            if m:
                hh, mm = int(m.group(1)), int(m.group(2))
                ss = int(m.group(3) or 0)
                frac = float("0." + m.group(4)) if m.group(4) else 0.0
                if 0 <= hh < 24 and 0 <= mm < 60 and 0 <= ss < 60:
                    # Match against each FF block's [t0, t0 + 10.3 s] window across dusk->dawn rollover
                    best_f, best_abs = None, 9e9
                    for f in ffs:
                        dt = _ff_datetime(f)
                        if not dt:
                            continue
                        cand = dt.replace(hour=hh, minute=mm, second=ss, microsecond=int(frac * 1e6))
                        # Handle midnight boundary cleanly if cand differs by ~24h
                        for shift_d in (-1, 0, 1):
                            c2 = cand + datetime.timedelta(days=shift_d)
                            # Offset from block start; blocks span ~10.24 s
                            off = (c2 - dt).total_seconds()
                            dist = 0.0 if (0.0 <= off <= 10.35) else min(abs(off), abs(off - 10.24))
                            if dist < best_abs:
                                best_abs = dist
                                best_f = f
                                delta_s = off
                    if best_f and best_abs <= 600.0:
                        chosen = best_f
    if not chosen:
        return None, None, None, len(ffs), 0.0
    pos = ffs.index(chosen)
    prev_ff = ffs[pos - 1] if pos > 0 else None
    next_ff = ffs[pos + 1] if pos < len(ffs) - 1 else None
    return chosen, prev_ff, next_ff, len(ffs), delta_s

def night_block_media(name, ff, kind):
    """Path to an archived/captured night FF block's thumb/full/ann/resid/video."""
    if not re.match(r"^FF_[A-Za-z0-9_]+\.fits$", ff or ""):
        return None
    night_dir = real_within(ARCH_DET, name)
    if not night_dir or not os.path.isdir(night_dir):
        return None
    base = os.path.basename(night_dir)
    cap_dir = real_within(ARCHIVED, base)
    exists = (os.path.isfile(os.path.join(night_dir, ff))
              or bool(cap_dir and os.path.isfile(os.path.join(cap_dir, ff))))
    if not exists:
        return None
    cache_dir = os.path.join(DETCACHE, base)
    suffix = {"thumb": "_thumb.jpg", "full": "_full.jpg", "ann": "_ann.jpg",
              "resid": "_resid.jpg", "video": ".mp4"}[kind]
    p = os.path.join(cache_dir, _blk_base(ff) + suffix)
    if not os.path.isfile(p):
        with _det_lock("nblk:" + base + ":" + ff):
            if not os.path.isfile(p):
                _det_gen("block" + kind, night_dir, cache_dir, ff)
    return p if os.path.isfile(p) else None

def tonight_block_media(ff, kind):
    """Path to a live flagged-block's thumb/video/full for /tonight, generating
    lazily from the current capture dir (tonight.json 'dir'). None if unavailable."""
    if not re.match(r'^FF_[A-Za-z0-9_]+\.fits$', ff or ""):
        return None
    jp = os.path.join(ASSET_DIR, "tonight.json")
    if not os.path.isfile(jp):
        return None
    try:
        cur = json.load(open(jp)).get("dir")
    except (OSError, ValueError):
        return None
    cap_dir = real_within(ARCHIVED, cur or "")
    if not cap_dir or not os.path.isfile(os.path.join(cap_dir, ff)):
        return None
    cache_dir = os.path.join(DETCACHE, os.path.basename(cap_dir))
    suffix = {"thumb": "_thumb.jpg", "video": ".mp4", "full": "_full.jpg"}[kind]
    p = os.path.join(cache_dir, _blk_base(ff) + suffix)
    if not os.path.isfile(p):
        with _det_lock("tblk:" + ff):
            if not os.path.isfile(p):
                _det_gen("block" + kind, cap_dir, cache_dir, ff)
    return p if os.path.isfile(p) else None

def capture_status():
    def active(s):
        try:
            return subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True).stdout.strip()
        except Exception:
            return "?"
    cap = active(CAPTURE_SERVICE)
    # newest capturing night (may be tonight, pre-processing)
    try:
        cur = sorted([d for d in os.listdir(ARCHIVED) if os.path.isdir(os.path.join(ARCHIVED, d))],
                     key=_night_datekey, reverse=True)
        cur = cur[0] if cur else "—"
    except FileNotFoundError:
        cur = "—"
    # Three-state label: the systemd unit is "active" all day (StartCapture idles
    # until dusk), so actual liveness comes from FF file age in the newest night
    # dir — only capture creates FF files (one every ~10 s).
    if cap != "active":
        state = "stopped" if cap in ("inactive", "?") else cap
    else:
        state = "waiting for dusk"
        if cur != "—":
            try:
                d = os.path.join(ARCHIVED, cur)
                newest = max((os.path.getmtime(os.path.join(d, f))
                              for f in os.listdir(d) if f.startswith("FF_") and f.endswith(".fits")),
                             default=0)
                if newest and time.time() - newest < 900:
                    state = "live"
            except OSError:
                pass
    return state, cur

_statrep_cache = {"t": 0.0, "present": None}

def ukmon_status():
    """(label, cls, title) for a UKMON upload-health pill, or None when
    ukmon-pitools isn't installed (keeps the dashboard generic).
    Health = freshness of the newest pitools nightly log (ukmon_log_*.log in the
    RMS log dir; a clean run ends with 'done'), plus a cached daily check that
    this station appears in UKMON's public network status report."""
    ini = os.path.expanduser("~/source/ukmon-pitools/ukmon.ini")
    if not os.path.isfile(ini):
        return None
    try:
        m = re.search(r"LOCATION=(\S+)", open(ini).read())
    except OSError:
        return None
    loc = m.group(1) if m else ""
    if loc in ("", "NOTCONFIGURED"):
        return ("awaiting registration", "wait", "SSH key sent; waiting for the UKMON team")
    logs = glob.glob(os.path.join(ROOT, "logs", "ukmon_log_*.log"))
    title = "location: " + loc
    # public network status report — cached, so a slow/failed fetch can't hurt page loads
    if time.time() - _statrep_cache["t"] > 6 * 3600:
        _statrep_cache["t"] = time.time()
        try:
            req = urllib.request.Request("https://archive.ukmeteors.co.uk/reports/statrep.html",
                                         headers={"User-Agent": "rms-web/1.0"})
            page = urllib.request.urlopen(req, timeout=5).read().decode(errors="replace")
            code = rms_cfg("stationID") or ""
            _statrep_cache["present"] = bool(code) and code in page
        except Exception:
            _statrep_cache["present"] = None
    if _statrep_cache["present"] is True:
        title += " · listed in UKMON network report"
    if not logs:
        return ("no upload yet", "off", title)
    newest = max(logs, key=os.path.getmtime)
    age_h = (time.time() - os.path.getmtime(newest)) / 3600.0
    when = datetime.datetime.fromtimestamp(os.path.getmtime(newest)).strftime("%H:%M")
    try:
        tail = open(newest, errors="replace").read()[-4000:]
    except OSError:
        tail = ""
    if "ERROR" in tail:
        return ("errors " + when, "off", title + " · see " + os.path.basename(newest))
    if age_h > 26:
        return ("stale %dh" % age_h, "off", title)
    return ("uploaded " + when, "on", title)


def capture_window():
    """Tonight's planned capture window from the newest RMS logs: StartCapture
    logs 'Next start time: ... UTC' (dusk) and '... to start recording for X hrs'
    (dawn = start + duration). Reuses RMS's own ephemeris decision instead of
    recomputing sun altitudes here. After dawn the log's last 'Next start time'
    already points at the following dusk, which is what the waiting state wants.
    Reads the last 3 rotated log files so mid-night TimedRotatingFileHandler
    rollovers don't drop the dusk start message."""
    try:
        logs = os.path.join(ROOT, "logs")
        recent = sorted((f for f in os.listdir(logs) if f.startswith("log_") and f.endswith(".log")),
                        key=_night_datekey)[-3:]
        txt = "".join(open(os.path.join(logs, f), errors="replace").read() for f in recent)
        starts = re.findall(r"Next start time: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", txt)
        hrs = re.findall(r"to start recording for ([\d.]+) hrs", txt)
        if not starts:
            return None, None
        dusk = datetime.datetime.strptime(starts[-1], "%Y-%m-%d %H:%M:%S")
        dawn = dusk + datetime.timedelta(hours=float(hrs[-1])) if hrs else None
        return dusk, dawn
    except Exception:
        return None, None

def ml_retention(n_nights=6):
    """Historical dawn-ML retention = (final meteors)/(pre-ML candidates) over recent archived
    nights. A true per-block ML score isn't reproducible live (the model needs the dawn crop,
    which needs the capture-time detection context), so /tonight shows the raw pre-ML count with
    this data-backed 'expect ~N real' instead. Returns {ratio, rows:[(unf,final)...]} or None."""
    rows, tot_u, tot_f = [], 0, 0
    for d in sorted(glob.glob(os.path.join(ARCH_DET, STATION_NAME + "_*")), key=os.path.getmtime, reverse=True):
        name = os.path.basename(d)
        unf = os.path.join(d, "FTPdetectinfo_%s_unfiltered.txt" % name)
        fin = os.path.join(d, "FTPdetectinfo_%s.txt" % name)
        if not (os.path.isfile(unf) and os.path.isfile(fin)):
            continue
        try:
            mu = re.search(r"Meteor Count = (\d+)", open(unf).read())
            mf = re.search(r"Meteor Count = (\d+)", open(fin).read())
        except OSError:
            continue
        if not (mu and mf):
            continue
        u, f = int(mu.group(1)), int(mf.group(1))
        if u == 0:
            continue
        rows.append((u, f)); tot_u += u; tot_f += f
        if len(rows) >= n_nights:
            break
    if not rows or tot_u == 0:
        return None
    return {"ratio": tot_f/tot_u, "rows": rows, "tot_u": tot_u, "tot_f": tot_f}

def tonight_detections():
    """Live meteor candidates from the newest RMS logs: per-FF 'detected meteors: N'
    lines emitted by the real-time detector (pre-ML; the ML filter prunes at dawn).
    Spans mid-night log rotations and filters to the latest noon-to-noon UTC night."""
    try:
        logs = os.path.join(ROOT, "logs")
        recent = [os.path.join(logs, f) for f in sorted(
            (f for f in os.listdir(logs) if f.startswith("log_") and f.endswith(".log")),
            key=_night_datekey)[-3:]]
        out = subprocess.run(["grep", "-h", "-F", "detected meteors:", *recent],
                             capture_output=True, text=True).stdout
    except Exception:
        return 0, []
    entries = [(m.group(1), int(m.group(2)))
               for m in re.finditer(r"(FF_\S+\.fits) detected meteors: (\d+)", out)]
    if not entries:
        return 0, []
    last_key = _night_datekey(entries[-1][0])
    try:
        last_dt = datetime.datetime.strptime(last_key, "%Y%m%d_%H%M%S")
        noon_cut = ((last_dt - datetime.timedelta(hours=12)).strftime("%Y%m%d") + "_120000")
    except ValueError:
        noon_cut = ""
    total, hits, seen = 0, [], set()
    for ff, n in entries:
        if _night_datekey(ff) < noon_cut or ff in seen:
            continue
        seen.add(ff)
        total += 1
        if n:
            hits.append((ff, n))
    return total, hits

def recent_stars():
    """Latest and recent-peak 'Detected stars: N' from the current RMS logs — a live sky-clarity
    readout. RMS already extracts this per FF block during capture, so surfacing it is free (we
    just tail the logs, no reprocessing). Returns (latest_nonzero, recent_peak) or (None, None)."""
    try:
        logs = os.path.join(ROOT, "logs")
        recent = [os.path.join(logs, f) for f in sorted(
            (f for f in os.listdir(logs) if f.startswith("log_") and f.endswith(".log")),
            key=_night_datekey)[-2:]]
        out = subprocess.run(["tail", "-q", "-n", "400", *recent],
                             capture_output=True, text=True).stdout
    except Exception:
        return None, None
    vals = [int(m) for m in re.findall(r"Detected stars: (\d+)", out)]
    if not vals:
        return None, None
    # 'Latest' skips interleaved 0s (one cloud band across a single 10 s frame, scintillation) —
    # but only while they're interleaved. 12 consecutive zero frames (~2 min) means the sky is
    # genuinely gone: report 0 rather than the last clear count, which would otherwise sit on
    # the card for the rest of the night reading like a clear sky.
    if len(vals) >= 12 and not any(vals[-12:]):
        return 0, 0
    latest = next((v for v in reversed(vals) if v > 0), vals[-1])   # skip the interleaved 0s
    return latest, max(vals[-25:])

def _bin_points(raw, bin_minutes=3, trim_hours=None):
    """Bin (dt, count) to bin_minutes keeping the MAX per bin (clarity peak, ignoring the
    interleaved 0s / frame jitter). trim_hours drops anything older than trim_hours before the
    last point (used live to shed the previous night's leftover log lines)."""
    bins = {}
    for dt, n in raw:
        key = int(dt.timestamp() // (bin_minutes * 60))
        cur = bins.get(key)
        if cur is None or n > cur[1]:
            bins[key] = (dt, n)
    pts = sorted(bins.values(), key=lambda t: t[0])
    if trim_hours and pts:
        tlast = pts[-1][0]
        pts = [(dt, n) for dt, n in pts if (tlast - dt).total_seconds() <= trim_hours * 3600]
    return pts

def star_history(bin_minutes=3):
    """Tonight's star count over time (live, from the RMS logs). Spans capture restarts.
    Free: RMS already logs 'Detected stars: N' per FF block."""
    rx = re.compile(r"(\d{4})/(\d\d)/(\d\d) (\d\d):(\d\d):(\d\d).*?Detected stars: (\d+)")
    cutoff = time.time() - 18 * 3600
    raw = []
    for lg in glob.glob(os.path.join(ROOT, "logs", "log_%s_*.log" % STATION_NAME)):
        try:
            if os.path.getmtime(lg) < cutoff:
                continue
            # grep the star lines directly instead of tail -n: on a cloudy night the
            # log grows to ~60k+ lines (mostly compression chatter) and a fixed line
            # tail covered only the last few hours, truncating the chart's left edge.
            # The 18 h mtime cutoff above bounds how many files are read.
            txt = subprocess.run(["grep", "-a", "-h", "Detected stars: ", lg],
                                 capture_output=True).stdout.decode("utf-8", "replace")
        except Exception:
            continue
        for m in rx.finditer(txt):
            y, mo, d, h, mi, s = (int(x) for x in m.groups()[:6])
            try:
                raw.append((datetime.datetime(y, mo, d, h, mi, s), int(m.group(7))))
            except ValueError:
                continue
    return _bin_points(raw, bin_minutes, trim_hours=10)

def star_history_calstars(night_dir, bin_minutes=3):
    """Per-FF star count over an archived night, parsed from its CALSTARS file (count = star
    rows per FF block; timestamp from the FF name). Dependency-free — no RMS import."""
    files = glob.glob(os.path.join(night_dir, "CALSTARS*.txt"))
    if not files:
        return []
    raw, ffdt, cnt = [], None, 0
    starrx = re.compile(r"^\s*\d")               # star-data rows start with a number (Y coord)
    try:
        with open(files[0], errors="replace") as fh:
            for line in fh:
                if line.startswith("FF_"):
                    if ffdt is not None:
                        raw.append((ffdt, cnt))
                    p = line.strip().split("_")   # FF_STATION_YYYYMMDD_HHMMSS_...
                    try:
                        ffdt = datetime.datetime.strptime(p[2] + p[3], "%Y%m%d%H%M%S")
                    except (IndexError, ValueError):
                        ffdt = None
                    cnt = 0
                elif ffdt is not None and starrx.match(line):
                    cnt += 1
            if ffdt is not None:
                raw.append((ffdt, cnt))
    except OSError:
        return []
    return _bin_points(raw, bin_minutes)

STAR_BASE = {"at": 0.0, "base": None}
STAR_BASE_SINCE = "20260906"   # nights from this date on share the current detection config

def star_baseline():
    """Clear-sky star-count baseline per UTC hour, in the same 3-min-max-bin units as the live
    chart: for each hour, the highest hourly-median value seen across archived nights. Cloudy
    nights sit below it, so the max tracks the clearest sky recorded at that hour and
    self-updates as nights archive. Caveat: hours not yet covered by a night at the current
    detection config (dusk 19/20, dawn 04 until such a night reaches them) keep a stale value.
    Cached for 1 h. Nights before STAR_BASE_SINCE are excluded: they were captured at a
    different detection config (intensity_threshold was lowered 12 -> 6 on 2026-09-06,
    roughly doubling counts) and a hazy Aug-27 night at the old config recorded 91-110
    "stars"/FF from cloud edges, which the across-night max otherwise adopts."""
    if STAR_BASE["base"] is not None and time.time() - STAR_BASE["at"] < 3600:
        return STAR_BASE["base"]
    base = {}
    for d in glob.glob(os.path.join(ARCH_DET, STATION_NAME + "_%s*" % STAR_BASE_SINCE)):
        pts = star_history_calstars(d, bin_minutes=3)
        if len(pts) < 100:
            continue
        byh = {}
        for dt, n in pts:
            if n > 0:
                byh.setdefault(dt.hour, []).append(n)
        for h, v in byh.items():
            if len(v) >= 12:                     # >= 36 min of that hour actually observed
                med = statistics.median(v)
                base[h] = max(base.get(h, 0), med)
    if base:
        STAR_BASE.update(at=time.time(), base=base)
        return base
    return STAR_BASE["base"] or {}

def star_projection(pts, dawn):
    """Extend tonight's observed star curve to dawn as a dashed projection. Backtested shape
    model: the current sky state (hourly-median level of the last two complete observed hours
    vs the clear-sky baseline for those hours) scales the baseline's hour shape forward, so a
    hazy or partly cloudy night continues at its own reduced level. Backtest over archived
    Sep-2026 nights (cut at 22:00/23:00/00:00): RMSE ~10 stars (~14% of the clear level) vs
    ~11 for flat persistence; it cannot see the sky changing after the cut, which is exactly
    when it is most wrong (20-40 stars on nights where cloud broke or closed). Returns
    [(dt, mid, lo, hi)...] with a ±20% band, or [] when a projection isn't meaningful."""
    if dawn is None or len(pts) < 3:
        return []
    now = pts[-1][0]
    if now >= dawn - datetime.timedelta(minutes=15):
        return []
    base = star_baseline()
    if not base:
        return []
    # Zero-star bins mean the camera saw nothing (cloud, failed extraction), not a thin sky.
    # Including them drags the sky-state factor down: on tonight's partly-cloudy dusk the
    # pre-fix s landed at ~0.37 while the sky was actually clear and reading ~58 stars.
    nz = [(dt, n) for dt, n in pts if n > 0]
    if len(nz) < 3:
        return []
    last_nz = nz[-1][0]
    if not pts[-1][1] and (now - last_nz) > datetime.timedelta(minutes=30):
        return []   # effectively overcast right now - nothing sensible to extrapolate
    # sky state from the last two hours of nonzero bins (rolling window, not wall-clock hours:
    # tonight's hour 21 held ~35 min of cloud before the clearing and would poison a wall-clock hour)
    win = [(dt, n) for dt, n in nz if dt >= last_nz - datetime.timedelta(hours=2)]
    byh = {}
    for dt, n in win:
        byh.setdefault(dt.hour, []).append(n)
    hours = sorted(h for h, v in byh.items() if len(v) >= 4)[-2:]
    fs = []
    for h in hours:
        if h in base:
            fs.append(statistics.median(byh[h]) / base[h])
    if not fs:
        return []
    s = max(0.15, min(1.1, statistics.median(fs)))
    # seam: the smoothed current level, not the last (spiky) 3-min max bin
    cur = hours[-1]
    seam_v = float(statistics.median(byh[cur])) if len(byh.get(cur, [])) >= 4 else float(nz[-1][1])
    out = [(now, seam_v, seam_v, seam_v)]
    h = (now + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    while h <= dawn:
        if h.hour in base:
            v = s * base[h.hour]
            out.append((h, v, 0.8 * v, 1.2 * v))
        h += datetime.timedelta(hours=1)
    if len(out) < 2:
        return []
    dt, m, lo, hi = out[-1]
    out.append((dawn, m, lo, hi))
    return out

def star_chart_svg(pts, proj=None, dawn=None, dusk=None):
    """Inline SVG line/area chart of star count vs time, with the plate-solve thresholds as
    labelled status reference lines (same amber/green as the card). Single series -> no legend
    (the heading names it). Dark-theme only, matching the dashboard; no JS. proj (from
    star_projection) is drawn as a dashed continuation with a ±20% band, and the x-axis
    extends to dawn. With dusk/dawn given, the axis is pinned to dusk..dawn (same window
    wx_chart_svg uses, so the two charts' hour gridlines align vertically)."""
    if len(pts) < 3:
        return ""
    W, H, ml, mr, mt, mb = 720, 190, 34, 46, 14, 24
    pw, ph = W - ml - mr, H - mt - mb
    t0, t1 = pts[0][0], pts[-1][0]
    if proj:
        t1 = max(t1, proj[-1][0])
    if dusk and dusk < t1:
        t0 = dusk
    if dawn:
        t1 = dawn
    pts = [p for p in pts if t0 <= p[0] <= t1] or pts
    span = max((t1 - t0).total_seconds(), 60.0)
    ymax = max(110, ((max(n for _, n in pts) // 10) + 1) * 10)
    if proj:
        ymax = max(ymax, ((max(hi for _, _, _, hi in proj) // 10) + 1) * 10)
    X = lambda dt: ml + pw * ((dt - t0).total_seconds() / span)
    Y = lambda n: mt + ph * (1 - min(n, ymax) / ymax)
    pl = " ".join("%.1f,%.1f" % (X(dt), Y(n)) for dt, n in pts)
    # close the shade under the last OBSERVED point, not the chart edge (t1 reaches dawn when
    # a projection is drawn) - otherwise the fill slopes to zero across the unobserved span
    area = "%.1f,%.1f " % (X(t0), mt + ph) + pl + " %.1f,%.1f" % (X(pts[-1][0]), mt + ph)
    thr = ""
    for v, col, lab in [(40, "#e2c98b", "40 recal"), (70, "#a9d97a", "70 solve"), (100, "#7ee29a", "100 ✓")]:
        if v <= ymax:
            yy = Y(v)
            thr += ("<line x1='%.1f' y1='%.1f' x2='%.1f' y2='%.1f' stroke='%s' stroke-width='1' "
                    "stroke-dasharray='4 4' opacity='0.65'/>"
                    "<text x='%.1f' y='%.1f' fill='%s' font-size='10' dominant-baseline='middle'>%s</text>"
                    % (ml, yy, ml + pw, yy, col, ml + pw + 3, yy, col, lab))
    xt = ""
    hh = t0.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
    while hh < t1:
        xx = X(hh)
        xt += ("<line x1='%.1f' y1='%.1f' x2='%.1f' y2='%.1f' stroke='#2a3550' stroke-width='1'/>"
               "<text x='%.1f' y='%.1f' fill='#8a97ab' font-size='10' text-anchor='middle'>%02d:00</text>"
               % (xx, mt, xx, mt + ph, xx, H - 6, hh.hour))
        hh += datetime.timedelta(hours=1)
    yax = ("<text x='%.1f' y='%.1f' fill='#8a97ab' font-size='10' text-anchor='end'>%d</text>"
           "<text x='%.1f' y='%.1f' fill='#8a97ab' font-size='10' text-anchor='end'>0</text>"
           % (ml - 4, mt + 9, ymax, ml - 4, mt + ph))
    pd = ""
    if proj and len(proj) >= 2:
        band = (" ".join("%.1f,%.1f" % (X(dt), Y(hi)) for dt, _, _, hi in proj) + " " +
                " ".join("%.1f,%.1f" % (X(dt), Y(lo)) for dt, _, lo, _ in reversed(proj)))
        mid = " ".join("%.1f,%.1f" % (X(dt), Y(m)) for dt, m, _, _ in proj)
        pd = ("<polygon points='%s' fill='#7db4ff' opacity='0.07'/>"
              "<polyline points='%s' fill='none' stroke='#7db4ff' stroke-width='1.5' "
              "stroke-dasharray='5 5' stroke-linejoin='round' opacity='0.9'/>" % (band, mid))
    return ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 %d %d' "
            "style='width:100%%;max-width:%dpx;height:auto;margin:6px 0' "
            "role='img' aria-label='Star count through tonight'>"
            "<rect x='%d' y='%d' width='%d' height='%d' fill='none' stroke='#1e2740'/>"
            "%s%s"
            "<polygon points='%s' fill='#7db4ff' opacity='0.12'/>"
            "<polyline points='%s' fill='none' stroke='#7db4ff' stroke-width='2' "
            "stroke-linejoin='round' stroke-linecap='round'/>%s%s</svg>"
            % (W, H, W, ml, mt, pw, ph, xt, thr, area, pl, pd, yax))

def wx_forecast():
    """Load the meteociel WRF-NMM cache written by the preview daemon (meteociel_wx.py),
    or None. Kept stdlib-only; decoding happens in the daemon (needs Pillow)."""
    p = os.path.join(ASSET_DIR, "wx_wrf.json")
    try:
        with open(p) as f:
            wx = json.load(f)
    except (OSError, ValueError):
        return None
    return wx if wx.get("frames") else None

def _wx_parse(ts):
    # '2026-09-10T03:00Z' -> naive UTC datetime (fromisoformat on 3.10 rejects 'Z')
    return datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).replace(tzinfo=None)

def wx_chart_svg(wx, dusk=None, dawn=None):
    """Three translucent shaded series (high/mid/low cloud cover %) from the meteociel
    WRF-NMM frames, on a UTC axis spanning dusk->dawn when the capture window is known.
    Series are drawn high first so the low deck (what matters most for the camera) sits
    on top. Frame values are hourly model output, not observations."""
    frames = []
    for e, fr in wx.get("frames", {}).items():
        try:
            frames.append((_wx_parse(fr["valid_utc"]), fr["low"], fr["mid"], fr["high"]))
        except (KeyError, ValueError):
            continue
    frames.sort()
    if len(frames) < 2:
        return ""
    now = datetime.datetime.now()
    if dusk and dawn:
        t0, t1 = dusk, dawn     # pinned: the forecast is for the upcoming night, whatever the
    else:                       # newest run covers yet - partial coverage plots where it can
        t0, t1 = frames[0][0], frames[-1][0]
    pts = [(t, lo, mi, hi) for t, lo, mi, hi in frames if t0 <= t <= t1]
    if len(pts) < 2:
        return ""
    W, H, ml, mr, mt, mb = 720, 170, 34, 46, 14, 24   # ml/mr match star_chart_svg so hour gridlines align
    pw, ph = W - ml - mr, H - mt - mb
    span = max((t1 - t0).total_seconds(), 60.0)
    X = lambda t: ml + pw * ((t - t0).total_seconds() / span)
    Y = lambda v: mt + ph * (1 - min(v, 100) / 100.0)
    series = [(2, "#7db4ff", "high"), (1, "#a9d97a", "mid"), (0, "#e28b8b", "low")]
    sv = ""
    for idx, col, _name in series:          # high first, so low paints on top
        pl = " ".join("%.1f,%.1f" % (X(t), Y(v[idx])) for t, *v in pts)
        area = ("%.1f,%.1f " % (X(pts[0][0]), mt + ph) + pl +
                " %.1f,%.1f" % (X(pts[-1][0]), mt + ph))
        sv += ("<polygon points='%s' fill='%s' fill-opacity='0.22' stroke='%s' "
               "stroke-width='1.5' stroke-opacity='0.9' stroke-linejoin='round'/>"
               % (area, col, col))
    xt = ""
    hh = t0.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1)
    while hh < t1:
        xx = X(hh)
        xt += ("<line x1='%.1f' y1='%.1f' x2='%.1f' y2='%.1f' stroke='#2a3550' stroke-width='1'/>"
               "<text x='%.1f' y='%.1f' fill='#8a97ab' font-size='10' text-anchor='middle'>%02d</text>"
               % (xx, mt, xx, mt + ph, xx, H - 6, hh.hour))
        hh += datetime.timedelta(hours=1)
    if t0 <= now <= t1:                     # 'now' marker between the model and the sky
        xn = X(now)
        xt += ("<line x1='%.1f' y1='%.1f' x2='%.1f' y2='%.1f' stroke='#8a97ab' "
               "stroke-width='1' stroke-dasharray='3 3'/>"
               "<text x='%.1f' y='%.1f' fill='#8a97ab' font-size='10' text-anchor='middle'>now</text>"
               % (xn, mt, xn, mt + ph, xn, mt + ph + 10))
    yax = ("<text x='%.1f' y='%.1f' fill='#8a97ab' font-size='10' text-anchor='end'>100%%</text>"
           "<text x='%.1f' y='%.1f' fill='#8a97ab' font-size='10' text-anchor='end'>0</text>"
           % (ml - 4, mt + 9, ml - 4, mt + ph))
    return ("<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 %d %d' "
            "style='width:100%%;max-width:%dpx;height:auto;margin:6px 0' "
            "role='img' aria-label='Cloud cover forecast through tonight'>"
            "<rect x='%d' y='%d' width='%d' height='%d' fill='none' stroke='#1e2740'/>"
            "%s%s%s</svg>"
            % (W, H, W, ml, mt, pw, ph, xt, sv, yax))

def det_lightcurve_svg(d):
    """Inline dark-theme SVG light curve: Apparent Magnitude (inverted, brightest up)
    or Integrated Intensity on left axis, overlaid with SNR (dashed green) on right axis."""
    segs = d.get("segments") or []
    if len(segs) < 2:
        return ""
    W, H, ml, mr, mt, mb = 720, 185, 46, 48, 16, 26
    pw, ph = W - ml - mr, H - mt - mb
    f0, f1 = segs[0]["fr"], segs[-1]["fr"]
    fspan = max(f1 - f0, 1.0)
    X = lambda fr: ml + pw * ((fr - f0) / fspan)

    mags = [s["mag"] for s in segs if s.get("mag") is not None]
    use_mag = bool(d.get("has_mag")) and len(mags) >= 2
    if use_mag:
        m_hi = min(mags) - 0.35   # brightest (top)
        m_lo = max(mags) + 0.35   # faintest (bottom)
        if m_lo - m_hi < 1.0:
            mid = 0.5 * (m_lo + m_hi)
            m_hi, m_lo = mid - 0.5, mid + 0.5
        Y1 = lambda s: mt + ph * ((s["mag"] - m_hi) / (m_lo - m_hi))
        pts1 = [(s["fr"], Y1(s), s) for s in segs if s.get("mag") is not None]
        y1_top = "%+.1fm" % m_hi
        y1_bot = "%+.1fm" % m_lo
    else:
        imax = max(max((s.get("inten") or 0) for s in segs), 10) * 1.12
        Y1 = lambda s: mt + ph * (1.0 - min(s.get("inten") or 0, imax) / imax)
        pts1 = [(s["fr"], Y1(s), s) for s in segs]
        y1_top = "%d" % int(round(imax))
        y1_bot = "0"

    smax = max(max((s.get("snr") or 0.0) for s in segs), 5.0) * 1.15
    Y2 = lambda snr: mt + ph * (1.0 - min(snr, smax) / smax)

    grid = ""
    n_ticks = min(6, max(2, len(segs) - 1))
    for k in range(n_ticks + 1):
        fr_t = f0 + fspan * (k / float(n_ticks))
        xx = X(fr_t)
        dt_s = (fr_t - f0) / float(d.get("fps") or 25.0)
        grid += ("<line x1='%.1f' y1='%d' x2='%.1f' y2='%d' stroke='#1e2740' stroke-width='1'/>"
                 "<text x='%.1f' y='%d' fill='#8a97ab' font-size='10' text-anchor='middle'>f%.0f (+%.2fs)</text>"
                 % (xx, mt, xx, mt + ph, xx, H - 6, fr_t, dt_s))
    for frac in (0.25, 0.5, 0.75):
        yy = mt + ph * frac
        grid += "<line x1='%d' y1='%.1f' x2='%d' y2='%.1f' stroke='#1a2236' stroke-dasharray='3 3'/>" % (
            ml, yy, ml + pw, yy)

    pl1 = " ".join("%.1f,%.1f" % (X(fr), yy) for fr, yy, _ in pts1)
    area1 = ("%.1f,%d " % (X(pts1[0][0]), mt + ph)) + pl1 + (" %.1f,%d" % (X(pts1[-1][0]), mt + ph))
    dots1 = ""
    pk_fr = d.get("peak_frame")
    for fr, yy, s in pts1:
        is_pk = pk_fr is not None and abs(fr - pk_fr) < 0.05
        col = "#ffd25a" if is_pk else ("#ff8c64" if s.get("sat", 0) > 0 else "#7db4ff")
        r = 4.2 if is_pk else 2.6
        dots1 += "<circle cx='%.1f' cy='%.1f' r='%.1f' fill='%s'/>" % (X(fr), yy, r, col)

    pl2 = " ".join("%.1f,%.1f" % (X(s["fr"]), Y2(s.get("snr") or 0.0)) for s in segs)
    axes = (
        "<text x='%d' y='%d' fill='#7db4ff' font-size='10' text-anchor='end'>%s</text>"
        "<text x='%d' y='%d' fill='#7db4ff' font-size='10' text-anchor='end'>%s</text>"
        "<text x='%d' y='%d' fill='#a9d97a' font-size='10'>SNR %.0f</text>"
        "<text x='%d' y='%d' fill='#a9d97a' font-size='10'>0</text>"
        % (ml - 5, mt + 9, y1_top, ml - 5, mt + ph, y1_bot,
           ml + pw + 4, mt + 9, smax, ml + pw + 4, mt + ph)
    )
    return (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 %d %d' "
        "style='width:100%%;max-width:%dpx;height:auto;margin:6px 0' role='img' aria-label='Light curve'>"
        "<rect x='%d' y='%d' width='%d' height='%d' fill='#0d1220' stroke='#1e2740'/>"
        "%s"
        "<polygon points='%s' fill='#7db4ff' opacity='0.14'/>"
        "<polyline points='%s' fill='none' stroke='#a9d97a' stroke-width='1.4' stroke-dasharray='4 3' opacity='0.85'/>"
        "<polyline points='%s' fill='none' stroke='#7db4ff' stroke-width='2' stroke-linejoin='round'/>"
        "%s%s</svg>"
        % (W, H, W, ml, mt, pw, ph, grid, area1, pl2, pl1, dots1, axes)
    )


def det_kinematics_svg(d):
    """Inline dark-theme SVG of cross-track centroid residual (px, left axis) and
    frame-to-frame speed (px/frame, right axis) across the track."""
    segs = d.get("segments") or []
    if len(segs) < 3:
        return ""
    W, H, ml, mr, mt, mb = 720, 165, 46, 48, 14, 24
    pw, ph = W - ml - mr, H - mt - mb
    f0, f1 = segs[0]["fr"], segs[-1]["fr"]
    fspan = max(f1 - f0, 1.0)
    X = lambda fr: ml + pw * ((fr - f0) / fspan)

    cmax = max(2.0, max(abs(s.get("cross_px") or 0.0) for s in segs) * 1.25)
    Yc = lambda c: mt + ph * (0.5 - (c / (2.0 * cmax)))
    smax = max(2.0, max((s.get("spd_px") or 0.0) for s in segs) * 1.2)
    Ys = lambda v: mt + ph * (1.0 - min(v, smax) / smax)

    mid_y = Yc(0.0)
    band = ""
    if 1.0 < cmax:
        y_p1, y_m1 = Yc(1.0), Yc(-1.0)
        band = ("<rect x='%d' y='%.1f' width='%d' height='%.1f' fill='#7ee29a' opacity='0.06'/>"
                % (ml, y_p1, pw, y_m1 - y_p1))
    plc = " ".join("%.1f,%.1f" % (X(s["fr"]), Yc(s.get("cross_px") or 0.0)) for s in segs)
    pls = " ".join("%.1f,%.1f" % (X(s["fr"]), Ys(s.get("spd_px") or 0.0)) for s in segs)
    dots = "".join("<circle cx='%.1f' cy='%.1f' r='2.5' fill='#7ee29a'/>"
                   % (X(s["fr"]), Yc(s.get("cross_px") or 0.0)) for s in segs)
    axes = (
        "<line x1='%d' y1='%.1f' x2='%d' y2='%.1f' stroke='#2f4a63' stroke-dasharray='4 4'/>"
        "<text x='%d' y='%d' fill='#7ee29a' font-size='10' text-anchor='end'>+%.1fpx</text>"
        "<text x='%d' y='%.1f' fill='#8a97ab' font-size='10' text-anchor='end'>0</text>"
        "<text x='%d' y='%d' fill='#7ee29a' font-size='10' text-anchor='end'>−%.1fpx</text>"
        "<text x='%d' y='%d' fill='#e2c98b' font-size='10'>%.1f px/f</text>"
        "<text x='%d' y='%d' fill='#e2c98b' font-size='10'>0</text>"
        % (ml, mid_y, ml + pw, mid_y,
           ml - 5, mt + 9, cmax, ml - 5, mid_y + 3, ml - 5, mt + ph, cmax,
           ml + pw + 4, mt + 9, smax, ml + pw + 4, mt + ph)
    )
    return (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 %d %d' "
        "style='width:100%%;max-width:%dpx;height:auto;margin:6px 0' role='img' aria-label='Track linearity'>"
        "<rect x='%d' y='%d' width='%d' height='%d' fill='#0d1220' stroke='#1e2740'/>"
        "%s%s"
        "<polyline points='%s' fill='none' stroke='#e2c98b' stroke-width='1.4' stroke-dasharray='4 3' opacity='0.85'/>"
        "<polyline points='%s' fill='none' stroke='#7ee29a' stroke-width='1.8' stroke-linejoin='round'/>"
        "%s</svg>"
        % (W, H, W, ml, mt, pw, ph, band, axes, pls, plc, dots)
    )


def fr_lightcurve_svg(fc):
    """Inline SVG for an FR_*.bin raw cutout's per-frame integrated flux & peak pixel ADU."""
    frs = fc.get("frames") or []
    if len(frs) < 2:
        return ""
    W, H, ml, mr, mt, mb = 720, 180, 50, 46, 16, 26
    pw, ph = W - ml - mr, H - mt - mb
    f0, f1 = frs[0]["fr"], frs[-1]["fr"]
    fspan = max(f1 - f0, 1)
    X = lambda fr: ml + pw * ((fr - f0) / float(fspan))
    flmax = max(max(r.get("flux", 0) for r in frs), 10) * 1.1
    Yf = lambda v: mt + ph * (1.0 - min(v, flmax) / flmax)
    Ya = lambda a: mt + ph * (1.0 - min(a, 255) / 255.0)

    grid = ""
    n_ticks = min(6, max(2, len(frs) - 1))
    fps = float(fc.get("fps") or 25.0)
    for k in range(n_ticks + 1):
        fr_t = f0 + fspan * (k / float(n_ticks))
        xx = X(fr_t)
        grid += ("<line x1='%.1f' y1='%d' x2='%.1f' y2='%d' stroke='#1e2740'/>"
                 "<text x='%.1f' y='%d' fill='#8a97ab' font-size='10' text-anchor='middle'>f%.0f (+%.1fs)</text>"
                 % (xx, mt, xx, mt + ph, xx, H - 6, fr_t, (fr_t - f0) / fps))
    pl_fl = " ".join("%.1f,%.1f" % (X(r["fr"]), Yf(r["flux"])) for r in frs)
    area_fl = ("%.1f,%d " % (X(f0), mt + ph)) + pl_fl + (" %.1f,%d" % (X(f1), mt + ph))
    pl_ad = " ".join("%.1f,%.1f" % (X(r["fr"]), Ya(r["peak_adu"])) for r in frs)
    sat_y = Ya(254)
    axes = (
        "<line x1='%d' y1='%.1f' x2='%d' y2='%.1f' stroke='#ff8c64' stroke-dasharray='3 3' opacity='0.5'/>"
        "<text x='%d' y='%d' fill='#7db4ff' font-size='10' text-anchor='end'>%d</text>"
        "<text x='%d' y='%d' fill='#7db4ff' font-size='10' text-anchor='end'>0</text>"
        "<text x='%d' y='%d' fill='#ff8c64' font-size='10'>255 ADU</text>"
        "<text x='%d' y='%d' fill='#8a97ab' font-size='10'>0</text>"
        % (ml, sat_y, ml + pw, sat_y, ml - 5, mt + 9, int(round(flmax)), ml - 5, mt + ph,
           ml + pw + 4, mt + 9, ml + pw + 4, mt + ph)
    )
    return (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 %d %d' "
        "style='width:100%%;max-width:%dpx;height:auto;margin:6px 0' role='img' aria-label='FR cutout flux'>"
        "<rect x='%d' y='%d' width='%d' height='%d' fill='#0d1220' stroke='#1e2740'/>"
        "%s%s"
        "<polygon points='%s' fill='#7db4ff' opacity='0.14'/>"
        "<polyline points='%s' fill='none' stroke='#ff8c64' stroke-width='1.4' stroke-dasharray='4 3'/>"
        "<polyline points='%s' fill='none' stroke='#7db4ff' stroke-width='2' stroke-linejoin='round'/>"
        "</svg>"
        % (W, H, W, ml, mt, pw, ph, grid, axes, area_fl, pl_ad, pl_fl)
    )


def study_player_html(vid_sources, img_sources, f0_default, peak_fr, cam_fps, ff_start_epoch_ms=0):
    """Interactive HTML5 video + frame-stepping + playback-rate + still-view switcher.
    vid_sources: [(label, url, f0_clip), ...]
    img_sources: [(label, url), ...]"""
    if not vid_sources:
        return "<p class=muted>No FF/FR media available on disk for this event.</p>"
    vtabs = ""
    for k, (lbl, url, f0_c) in enumerate(vid_sources):
        act = " active" if k == 0 else ""
        vtabs += (f"<button type=button class='sbtn vtab{act}' data-url='{html.escape(url, True)}' "
                  f"data-f0='{f0_c}' onclick='switchVid(this)'>{html.escape(lbl)}</button>")
    itabs = ""
    for k, (lbl, url) in enumerate(img_sources):
        act = " active" if k == 0 else ""
        itabs += (f"<button type=button class='sbtn itab{act}' data-url='{html.escape(url, True)}' "
                  f"onclick='switchImg(this)'>{html.escape(lbl)}</button>")

    init_vurl, init_f0 = vid_sources[0][1], vid_sources[0][2]
    init_iurl = img_sources[0][1] if img_sources else ""
    img_block = ""
    if img_sources:
        img_block = (
            f"<div style='margin-top:16px'>"
            f"<div class=sbar><span class=muted>Still view:</span> {itabs} "
            f"<a id=study-img-open href='{html.escape(init_iurl, True)}' target=_blank "
            f"style='margin-left:auto;font-size:12px'>open full-res &nearr;</a></div>"
            f"<a id=study-img-link href='{html.escape(init_iurl, True)}' target=_blank>"
            f"<img id=study-img src='{html.escape(init_iurl, True)}' style='margin-top:6px'></a></div>"
        )

    return (
        f"<div class=sbar><span class=muted>Clip view:</span> {vtabs}</div>"
        f"<video id=study-vid controls autoplay muted loop playsinline "
        f"style='width:100%;max-height:560px;background:#000;border-radius:8px;margin-top:6px' "
        f"src='{html.escape(init_vurl, True)}'></video>"
        f"<div class=sbar style='margin-top:6px'>"
        f"<button type=button class=sbtn onclick='jumpStart()' title='Jump to clip start'>⏮ start</button>"
        f"<button type=button class=sbtn onclick='stepFrame(-1)' title='Step back 1 frame (Left arrow)'>◀ −1 fr</button>"
        f"<button type=button class=sbtn onclick='togglePlay()' title='Play / pause (Space)'>⏯ play/pause</button>"
        f"<button type=button class=sbtn onclick='stepFrame(1)' title='Step forward 1 frame (Right arrow)'>+1 fr ▶</button>"
        + (f"<button type=button class=sbtn onclick='jumpPeak()' title='Jump to peak brightness frame'>★ peak (f{peak_fr:.0f})</button>"
           if peak_fr is not None else "")
        + f"<span class=muted style='margin-left:8px'>Sky speed:</span>"
        f"<button type=button class='sbtn rtab' onclick='setSkyRate(0.1,this)'>0.1×</button>"
        f"<button type=button class='sbtn rtab' onclick='setSkyRate(0.25,this)'>0.25×</button>"
        f"<button type=button class='sbtn rtab active' onclick='setSkyRate(0.5,this)'>0.5×</button>"
        f"<button type=button class='sbtn rtab' onclick='setSkyRate(1.0,this)'>1×</button>"
        f"<span id=fr-readout class=pill style='margin-left:auto;font-family:monospace'>frame —</span>"
        f"</div>"
        f"<p class=muted style='margin:4px 0 0;font-size:12px'>Keys: <code>&larr;</code> / <code>&rarr;</code> "
        f"step single camera frames · <code>Space</code> play/pause · clip encoded at 12.5 fps (0.5× sky speed).</p>"
        + img_block +
        f"<script>\n"
        f"(function(){{\n"
        f"  const vid = document.getElementById('study-vid');\n"
        f"  const ro = document.getElementById('fr-readout');\n"
        f"  const CLIP_FPS = 12.5, CAM_FPS = {float(cam_fps or 25.0):.3f};\n"
        f"  const FF_EPOCH = {int(ff_start_epoch_ms)};\n"
        f"  let clipF0 = {float(init_f0):.3f}, skyRate = 0.5;\n"
        f"  const PEAK_FR = {('null' if peak_fr is None else f'{float(peak_fr):.3f}')};\n"
        f"  function upd(){{\n"
        f"    const fr = clipF0 + vid.currentTime * CLIP_FPS;\n"
        f"    let s = 'frame ' + fr.toFixed(1);\n"
        f"    if (FF_EPOCH > 0) {{\n"
        f"      const dt = new Date(FF_EPOCH + (fr / CAM_FPS) * 1000.0);\n"
        f"      s += ' · ' + dt.toISOString().slice(11, 23) + ' UT';\n"
        f"    }}\n"
        f"    ro.textContent = s;\n"
        f"  }}\n"
        f"  vid.addEventListener('timeupdate', upd);\n"
        f"  vid.addEventListener('seeked', upd);\n"
        f"  vid.addEventListener('loadedmetadata', function(){{ vid.playbackRate = skyRate * (CAM_FPS / CLIP_FPS); upd(); }});\n"
        f"  window.stepFrame = function(n){{ vid.pause(); vid.currentTime = Math.max(0, Math.min(vid.duration||999, vid.currentTime + n / CLIP_FPS + 0.001)); }};\n"
        f"  window.togglePlay = function(){{ if (vid.paused) vid.play(); else vid.pause(); }};\n"
        f"  window.jumpStart = function(){{ vid.pause(); vid.currentTime = 0; }};\n"
        f"  window.jumpPeak = function(){{ if (PEAK_FR !== null) {{ vid.pause(); vid.currentTime = Math.max(0, (PEAK_FR - clipF0) / CLIP_FPS); }} }};\n"
        f"  window.setSkyRate = function(r, btn){{\n"
        f"    skyRate = r; vid.playbackRate = r * (CAM_FPS / CLIP_FPS);\n"
        f"    document.querySelectorAll('.rtab').forEach(b => b.classList.remove('active'));\n"
        f"    if (btn) btn.classList.add('active');\n"
        f"  }};\n"
        f"  window.switchVid = function(btn){{\n"
        f"    const curFr = clipF0 + vid.currentTime * CLIP_FPS;\n"
        f"    const wasPaused = vid.paused;\n"
        f"    clipF0 = parseFloat(btn.getAttribute('data-f0') || '0');\n"
        f"    vid.src = btn.getAttribute('data-url');\n"
        f"    vid.addEventListener('loadedmetadata', function once(){{\n"
        f"      vid.removeEventListener('loadedmetadata', once);\n"
        f"      vid.currentTime = Math.max(0, (curFr - clipF0) / CLIP_FPS);\n"
        f"      vid.playbackRate = skyRate * (CAM_FPS / CLIP_FPS);\n"
        f"      if (!wasPaused) vid.play();\n"
        f"    }});\n"
        f"    document.querySelectorAll('.vtab').forEach(b => b.classList.remove('active'));\n"
        f"    btn.classList.add('active');\n"
        f"  }};\n"
        f"  window.switchImg = function(btn){{\n"
        f"    const u = btn.getAttribute('data-url');\n"
        f"    const im = document.getElementById('study-img');\n"
        f"    if (im) im.src = u;\n"
        f"    const a1 = document.getElementById('study-img-open'), a2 = document.getElementById('study-img-link');\n"
        f"    if (a1) a1.href = u; if (a2) a2.href = u;\n"
        f"    document.querySelectorAll('.itab').forEach(b => b.classList.remove('active'));\n"
        f"    btn.classList.add('active');\n"
        f"  }};\n"
        f"  document.addEventListener('keydown', function(e){{\n"
        f"    if (e.target && ['INPUT','TEXTAREA','SELECT'].includes(e.target.tagName)) return;\n"
        f"    if (e.key === 'ArrowLeft') {{ e.preventDefault(); window.stepFrame(-1); }}\n"
        f"    else if (e.key === 'ArrowRight') {{ e.preventDefault(); window.stepFrame(1); }}\n"
        f"    else if (e.key === ' ') {{ e.preventDefault(); window.togglePlay(); }}\n"
        f"  }});\n"
        f"}})();\n</script>"
    )


PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
{refresh}
<title>{title}</title><style>
:root{{color-scheme:dark}}
body{{margin:0;background:#0a0d14;color:#c8d0dc;font:15px/1.5 system-ui,sans-serif}}
a{{color:#7db4ff;text-decoration:none}} a:hover{{text-decoration:underline}}
header{{padding:14px 20px;background:#111726;border-bottom:1px solid #1e2740;position:sticky;top:0;z-index:10;display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}}
header h1{{font-size:17px;margin:0;color:#eaf0fa}}
.pill{{font-size:12px;padding:2px 9px;border-radius:20px;background:#1a2740;display:inline-block}}
.on{{background:#123a1e;color:#7ee29a}} .off{{background:#3a1414;color:#e28b8b}} .wait{{background:#3a2f14;color:#e2c98b}}
main{{padding:20px;max-width:1100px;margin:0 auto}}
.muted{{color:#7c879b;font-size:13px}}
img{{max-width:100%;border-radius:8px;display:block;background:#000}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px;margin-top:12px}}
.card{{background:#111726;border:1px solid #1e2740;border-radius:10px;padding:10px}}
.card h3{{font-size:13px;margin:8px 2px 2px;font-weight:600}}
pre{{white-space:pre-wrap;background:#0d1220;border:1px solid #1e2740;border-radius:8px;padding:12px;font-size:12px;overflow:auto;max-height:340px}}
.hero{{margin:8px 0 4px}} h2{{font-size:16px;margin:20px 0 6px;color:#eaf0fa}}
.sbar{{display:flex;gap:6px;align-items:center;flex-wrap:wrap}}
.sbtn{{padding:4px 10px;cursor:pointer;background:#151e30;color:#c8d0dc;border:1px solid #263552;border-radius:6px;font-size:12px}}
.sbtn:hover{{border-color:#4c72a8;color:#fff}}
.sbtn.active{{background:#213659;border-color:#7db4ff;color:#eaf0fa;font-weight:600}}
.stbl{{border-collapse:collapse;width:100%;font-size:12px;font-variant-numeric:tabular-nums}}
.stbl th,.stbl td{{padding:4px 7px;border-bottom:1px solid #1a2236;text-align:right}}
.stbl th{{color:#8a97ab;font-weight:600;position:sticky;top:0;background:#0d1220}}
.stbl td:first-child,.stbl th:first-child{{text-align:left}}
details.sbox{{background:#0e1422;border:1px solid #1e2740;border-radius:10px;padding:10px 14px;margin:14px 0}}
details.sbox>summary{{cursor:pointer;font-weight:600;color:#dce6f7;font-size:14px}}
input.sinp{{background:#0d1220;color:#eaf0fa;border:1px solid #2f4a63;border-radius:6px;padding:5px 9px;font-size:13px;font-family:monospace}}
</style></head><body>
<header><h1>🌠 {station} meteor station</h1>
<span class="pill {capcls}">capture: {cap}</span>{ukmonpill}
<span class="muted">tonight: {cur}</span>
<a href="/tonight">tonight</a>
<a href="/">nights</a>
<a href="/neighbours">neighbours</a>
<a href="/scorecard">scorecard</a>
<a href="http://{host}:8889/cam1" target=_blank>● live view</a>
</header><main>{body}</main></body></html>"""

class H(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body, extra=None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        if extra:
            for k, v in extra.items():
                self.send_header(k, v)
        self.end_headers()
        if body is not None:
            self.wfile.write(body)

    def log_message(self, *a):  # quiet
        pass

    def page(self, title, body, refresh=""):
        cap, cur = capture_status()
        host = self.headers.get("Host", "meteor.local").split(":")[0]
        st = nights()
        code = rms_cfg("stationID") or (st[0].split("_")[0] if st else (cur.split("_")[0] if cur != "—" else ""))
        station = (f"{STATION_NAME} ({code})" if code and code != "XX0001"
                   and code != STATION_NAME else STATION_NAME)
        uk = ukmon_status()
        ukmonpill = ("" if uk is None else
                     "\n<span class=\"pill {}\" title=\"{}\">ukmon: {}</span>".format(
                         uk[1], html.escape(uk[2], quote=True), html.escape(uk[0])))
        htmlout = PAGE.format(title=html.escape(title), station=html.escape(station),
                              cap=html.escape(cap),
                              capcls={"live": "on", "waiting for dusk": "wait"}.get(cap, "off"),
                              ukmonpill=ukmonpill,
                              cur=html.escape(cur), host=html.escape(host), body=body, refresh=refresh)
        self._send(200, "text/html; charset=utf-8", htmlout.encode())

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            return self.home()
        if u.path == "/night":
            return self.night(q.get("d", [""])[0])
        if u.path == "/det":
            return self.det(q.get("d", [""])[0], q.get("i", [None])[0], q.get("u", [None])[0])
        if u.path in ("/detthumb", "/detvid", "/detcropvid", "/detfull", "/detann", "/detresid"):
            kind = {"/detthumb": "thumb", "/detvid": "video", "/detcropvid": "cropvideo",
                    "/detfull": "full", "/detann": "ann", "/detresid": "resid"}[u.path]
            return self.det_asset(q.get("d", [""])[0], q.get("i", [None])[0], q.get("u", [None])[0], kind)
        if u.path == "/fr":
            return self.fr(q.get("d", [""])[0], q.get("f", ["-1"])[0])
        if u.path in ("/frthumb", "/frvid", "/frcropvid", "/frfull"):
            kind = {"/frthumb": "thumb", "/frvid": "video", "/frcropvid": "cropvideo", "/frfull": "full"}[u.path]
            return self.fr_asset(q.get("d", [""])[0], q.get("f", ["-1"])[0], kind)
        if u.path == "/ff":
            return self.ff(q.get("d", [""])[0], q.get("ff", [""])[0], q.get("t", [""])[0])
        if u.path in ("/ffthumb", "/ffvid", "/fffull", "/ffann", "/ffresid"):
            kind = {"/ffthumb": "thumb", "/ffvid": "video", "/fffull": "full",
                    "/ffann": "ann", "/ffresid": "resid"}[u.path]
            return self.ff_asset(q.get("d", [""])[0], q.get("ff", [""])[0], kind)
        if u.path == "/tdet":
            return self.tdet(q.get("ff", [""])[0])
        if u.path in ("/tdetthumb", "/tdetvid", "/tdetfull"):
            kind = {"/tdetthumb": "thumb", "/tdetvid": "video", "/tdetfull": "full"}[u.path]
            p = tonight_block_media(q.get("ff", [""])[0], kind)
            if not p:
                return self._send(404, "text/plain", b"media unavailable")
            return self.serve_binary(p, "video/mp4" if kind == "video" else "image/jpeg")
        if u.path == "/file":
            return self.serve_file(q.get("p", [""])[0])
        if u.path == "/neighbours":
            return self.neighbours()
        if u.path == "/neighbours_map.png":
            return self.serve_asset("neighbours_map.png", "image/png")
        if u.path == "/tonight":
            return self.tonight()
        if u.path == "/tonight_stack.png":
            return self.serve_asset("tonight_stack.png", "image/png")
        if u.path == "/tonight_latest.png":
            return self.serve_asset("tonight_latest.png", "image/png")
        if u.path == "/tonight_detected.png":
            return self.serve_asset("tonight_detected.png", "image/png")
        if u.path == "/scorecard":
            return self.scorecard()
        self._send(404, "text/plain", b"not found")

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        # Calibration solvers are only exposed when RMS_WEB_CALIB is set (they are
        # wired to one station's RMS clone; a 2nd instance must not spawn them).
        if u.path in ("/plate_solve", "/refit_distortion", "/bump_solve") and not CALIB_TOOLS:
            return self._send(404, "text/plain", b"not found")
        # Request a full rebuild of tonight's cumulative stack. The web app is read-only, so
        # rather than orchestrating the daemon (it can't sudo/restart it), it drops a flag file
        # that the preview daemon picks up on its next loop and rebuilds in-process.
        if u.path == "/rebuild_stack":
            try:
                open(os.path.join(ASSET_DIR, ".tonight_rebuild_request"), "w").close()
            except OSError:
                pass
            return self._send(303, "text/plain", b"", extra={"Location": "/tonight"})
        # Calibration actions: spawn the existing solver/refit scripts in FORCE mode (they
        # deploy only if their sanity gates pass; capture is untouched). Detached so the request
        # returns immediately; each script manages its own .tilt_solving/.tilt_refitting flag.
        if u.path == "/plate_solve":
            self._spawn_calib(os.path.join(ASSET_DIR, "auto_tilt_solve.py"))
            return self._send(303, "text/plain", b"", extra={"Location": "/tonight"})
        if u.path == "/refit_distortion":
            self._spawn_calib(os.path.join(ASSET_DIR, "auto_refit_distortion.py"))
            return self._send(303, "text/plain", b"", extra={"Location": "/tonight"})
        # Bump recovery: correspondence grid-search + pointing fit + union refit (works on
        # this fisheye where astrometry.net can't). Idempotent — its own O_EXCL lock makes
        # repeat/concurrent clicks a no-op, so a double POST can't launch two solves.
        if u.path == "/bump_solve":
            self._spawn_calib(os.path.join(ASSET_DIR, "bump_solve.py"))
            return self._send(303, "text/plain", b"", extra={"Location": "/tonight"})
        self._send(404, "text/plain", b"not found")

    @staticmethod
    def _spawn_calib(script):
        try:
            env = dict(os.environ, FORCE="1")
            subprocess.Popen([VENV_PY, script], env=env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        except Exception:
            pass

    def scorecard(self):
        import json as _json
        jp = os.path.join(ASSET_DIR, "scorecard.json")
        if not os.path.isfile(jp):
            return self.page("Scorecard", "<h2>Scorecard</h2><p class=muted>Not generated yet — "
                             "it appears after the first night is processed and GMN publishes daily trajectories.</p>")
        d = _json.load(open(jp))
        us = d.get("our_count")
        us_str = "—" if us is None else str(us)
        rows = ""
        for n in d.get("neighbours", []):
            rows += (f"<tr><td><a href='{html.escape(n['url'])}' target=_blank><b>{html.escape(n['code'])}</b></a></td>"
                     f"<td>{n['dist']:.0f} km</td><td><b>{n['count']}</b></td></tr>")
        verdict = ""
        if us == 0 and d.get("near_within_100", 0) > 0:
            verdict = ("<p style='color:#e8590c'><b>Sensitivity gap</b> — your neighbours caught meteors you "
                       "missed under the same sky. Tuning (gain / detection threshold / calibration) is the lever.</p>")
        elif us and us > 0:
            verdict = "<p style='color:#2f9e44'><b>On the board</b> — you're detecting meteors. 🌠</p>"
        body = (
            f"<h2>You vs your neighbours <span class=muted style='font-size:13px'>· night of "
            f"{html.escape(d.get('our_date') or '?')}</span></h2>"
            f"<div style='display:flex;gap:16px;flex-wrap:wrap;margin:10px 0'>"
            f"<div class=card style='border-color:#e23b3b'><b style='font-size:22px'>{us_str}</b><br>"
            f"<span class=muted>you ({html.escape(d.get('our_code','?'))})</span></div>"
            f"<div class=card><b style='font-size:22px'>{d.get('near_within_50',0)}</b><br>"
            f"<span class=muted>neighbours &le;50 km</span></div>"
            f"<div class=card><b style='font-size:22px'>{d.get('near_within_100',0)}</b><br>"
            f"<span class=muted>neighbours &le;100 km</span></div>"
            f"<div class=card><b style='font-size:22px'>{d.get('total_global',0)}</b><br>"
            f"<span class=muted>GMN worldwide</span></div></div>"
            + verdict +
            f"<h3>Active neighbours last night ({d.get('n_active',0)} of {d.get('n_near',0)} nearby)</h3>"
            f"<table style='border-collapse:collapse;width:100%;font-size:13px'>"
            f"<tr style='text-align:left;color:#7c879b'><th>station</th><th>distance</th>"
            f"<th>meteors triangulated</th></tr>{rows}</table>"
            f"<p class=muted style='margin-top:10px'>Counts are multi-station <em>trajectory</em> participations "
            f"from GMN's daily summary — each station detects more single-station meteors than shown, so these are a "
            f"floor. Station codes link to their GMN pages. Refreshed each morning after processing.</p>")
        self.page("Scorecard", body)

    def tonight(self):
        import json as _json, time as _t
        jp = os.path.join(ASSET_DIR, "tonight.json")
        cap, _ = capture_status()
        refresh = '<meta http-equiv="refresh" content="20">'
        if not os.path.isfile(jp):
            return self.page("Tonight", "<h2>Tonight</h2><p class=muted>No capture preview yet — it "
                             "appears once tonight's capture is under way.</p>", refresh)
        d = _json.load(open(jp))
        stale = int(_t.time()) - d.get("last_added", 0)
        live = stale < 120 and cap == "live"
        mt = lambda n: int(os.path.getmtime(os.path.join(ASSET_DIR, n))) if os.path.isfile(os.path.join(ASSET_DIR, n)) else 0
        badge = "<span class='pill on'>● live</span>" if live else "<span class='pill off'>idle</span>"
        nproc, hits = tonight_detections()
        ncand = sum(n for _, n in hits)
        # data-backed 'expect ~N real' from historical dawn-ML retention (a true live per-block
        # score isn't reproducible without the dawn pipeline)
        _mlr = ml_retention()
        if _mlr and ncand:
            _recent = ", ".join("%d&rarr;%d" % (u, f) for u, f in _mlr["rows"][:4])
            ml_note = (" <span class=muted>· ~%.0f%% survive dawn ML+astrometry here (recent %s), "
                       "so expect ~%d real</span>" % (100*_mlr["ratio"], _recent,
                                                      round(ncand*_mlr["ratio"])))
            ml_short = " &middot; ~%d real after ML" % round(ncand*_mlr["ratio"])
        else:
            ml_note = ml_short = ""
        slatest, speak = recent_stars()          # live sky-clarity readout (free — from the RMS log)
        # Colour the star count by plate-solve readiness (judged on the recent PEAK — the metric
        # auto_tilt_solve gates on). Thresholds: per-FF recalibration converges from ~40 detected
        # stars; the auto blind-solve gate is 70; a reliable blind solve wants ~100 (it has failed
        # at 85 in thin cloud). ~1/4–1/3 of detections are sensor noise on this camera.
        if speak is None:
            star_col, star_msg = "#868e96", "capture idle"
        elif speak < 40:
            star_col, star_msg = "#e28b8b", "too few — hazy/cloudy"
        elif speak < 70:
            star_col, star_msg = "#e2c98b", "recalibration only"
        elif speak < 100:
            star_col, star_msg = "#a9d97a", "solve armed (borderline)"
        else:
            star_col, star_msg = "#7ee29a", "clear — solve-ready"
        star_card = (
            f"<div class=card><b style='color:{star_col}'>{slatest}</b><br>"
            f"<span class=muted>stars now · peak {speak} — {star_msg}</span></div>"
            if slatest is not None else
            "<div class=card><b>—</b><br><span class=muted>stars (capture idle)</span></div>")
        star_legend = (
            "<p class=muted style='margin:2px 0 12px;font-size:12px'>Stars = detected per 10&nbsp;s "
            "frame (RMS extraction; ~¼–⅓ are sensor noise). Plate-solve readiness by recent peak: "
            "<span style='color:#e28b8b'>&lt;40 hazy</span> · "
            "<span style='color:#e2c98b'>40–69 recalibration only</span> · "
            "<span style='color:#a9d97a'>70+ solve attempts</span> · "
            "<span style='color:#7ee29a'>100+ reliable solve</span>.</p>")
        _pts = star_history()
        _dusk, _dawn = capture_window()
        _now = datetime.datetime.now()
        # Daytime: the capture window returned is the upcoming night, but star_history's data is
        # the night that just ended — pinning the axis to the future window puts the curve
        # outside the plot box. So by day the chart falls back to its data-driven axis and the
        # wording says "last night"; at night both charts share the pinned dusk..dawn axis.
        _day = not (_dusk and _dawn and _dusk <= _now <= _dawn)
        if _day:
            _proj = None
            _svg = star_chart_svg(_pts)
        else:
            _proj = star_projection(_pts, _dawn)
            _svg = star_chart_svg(_pts, proj=_proj, dawn=_dawn, dusk=_dusk)
        _ahead = "%.1f h" % ((_dawn - _now).total_seconds() / 3600.0) \
            if (_dawn and _proj) else ""
        _pnote = ("<p class=muted style='margin:2px 0;font-size:12px'>Dashed: extrapolated to dawn "
                  "({ahead} ahead) from this camera's own star counts — tonight's live log against "
                  "the clear-sky hour shape of past archived nights; no weather forecast is "
                  "involved. &plusmn;20% band, assumes the sky holds. Most wrong when cloud breaks "
                  "or closes later in the night.</p>").format(ahead=_ahead) \
            if _proj else ""
        star_chart = ("<h3>Star count {} <span class=muted style='font-size:12px'>· UTC, "
                      "3-min peak</span></h3>".format("— last night" if _day else "tonight")
                      + _svg + _pnote) if _svg else ""
        # Cloud-cover forecast, decoded from the meteociel WRF-NMM maps by the preview
        # daemon (meteociel_wx.py). Model output at the station pixel — not observed here.
        _wx = wx_forecast()
        _wxsvg = wx_chart_svg(_wx, dusk=_dusk, dawn=_dawn) if _wx else ""
        _wxrun = _wx.get("run_utc", "?") if _wx else ""
        wx_chart = ""
        if _wxsvg:
            wx_chart = ("<h3>Cloud forecast <span class=muted style='font-size:12px'>"
                        "· Météociel WRF-NMM run {_wxrun} · % at Denmead</span></h3>"
                        "{_wxsvg}"
                        "<p class=muted style='margin:2px 0;font-size:12px'>Shaded bands: "
                        "<span style='color:#e28b8b'>low</span> · "
                        "<span style='color:#a9d97a'>mid</span> · "
                        "<span style='color:#7db4ff'>high</span> cloud cover, read off the "
                        "Météociel WRF-NMM maps at the station's location. A numerical "
                        "forecast, not measured at this site — cloud edges on the map are "
                        "coarse, and the camera can see through thin high cloud that the "
                        "model shows as overcast.</p>").format(_wxrun=_wxrun, _wxsvg=_wxsvg)
        if hits:
            # clickable grid: each flagged block -> detail page with a video snippet
            # (same idea as /night, but keyed on the FF block since tonight has no
            # FTPdetectinfo yet). Newest first.
            cards = ""
            for ff, n in hits[-24:][::-1]:
                tm = re.search(r"_\d{8}_(\d{2})(\d{2})(\d{2})_", ff)
                tmstr = "{}:{}:{} UTC".format(*tm.groups()) if tm else "—"
                qs = "ff=" + urllib.parse.quote(ff)
                cards += (f"<div class=card><a href='/tdet?{qs}'>"
                          f"<img src='/tdetthumb?{qs}' loading=lazy style='height:120px;width:auto'></a>"
                          f"<div style='font-size:12px;margin-top:4px'>{tmstr} · {n} seg<br>"
                          f"<a href='/tdet?{qs}'>video</a> · <a href='/tdetfull?{qs}' target=_blank>full</a></div></div>")
            dethtml = (f"<details style='margin:16px 0'>"
                       f"<summary style='cursor:pointer'><b>Detections {'— last night' if _day else 'tonight'}</b> "
                       f"<span class=muted>— {len(hits)} candidate{'s' if len(hits) != 1 else ''} "
                       f"({nproc} FF blocks screened){ml_short}, click to expand</span></summary>"
                       f"<p class=muted>Click a block for the video snippet. Pre-ML — most are cloud/"
                       f"aircraft/satellite artefacts the dawn filter removes.{ml_note}</p>"
                       f"<div class=grid>{cards}</div></details>")
        else:
            dethtml = (f"<details style='margin:16px 0'>"
                       f"<summary style='cursor:pointer'><b>Detections {'— last night' if _day else 'tonight'}</b> "
                       f"<span class=muted>— no candidates ({nproc} FF blocks screened)</span></summary>"
                       f"<p class=muted>No meteor candidates yet ({nproc} FF blocks screened so far).</p>"
                       f"</details>")
        # satellite pass log (written by the preview daemon's TLE propagation)
        sathtml = ""
        try:
            sp = _json.load(open(os.path.join(ASSET_DIR, "sat_passes.json")))
            if sp.get("dir") == d.get("night", d["dir"]) and sp.get("passes"):
                rows = "".join(f"<tr><td>{html.escape(p['t'])} UTC</td>"
                               f"<td>{html.escape(p['name'])}</td>"
                               f"<td class=muted>{html.escape(p.get('path',''))}</td></tr>"
                               for p in sp["passes"][-20:][::-1])
                n = len(sp["passes"])
                latest = sp["passes"][-1]
                sathtml = (f"<details style='margin:16px 0'>"
                           f"<summary style='cursor:pointer'><b>Satellites through the FOV</b> "
                           f"<span class=muted>— {n} pass{'es' if n != 1 else ''} tonight, "
                           f"latest {html.escape(latest['name'])} at {html.escape(latest['t'])} UTC "
                           f"(click to expand)</span></summary>"
                           f"<p class=muted>Predicted sunlit passes (TLE propagation) — a streak in the "
                           f"stack matching one of these times is a satellite, not a meteor. "
                           f"Showing the most recent 20.</p>"
                           f"<table style='border-spacing:12px 2px'><tr class=muted>"
                           f"<th align=left>time</th><th align=left>satellite</th>"
                           f"<th align=left>path</th></tr>{rows}</table></details>")
        except Exception:
            pass
        # aircraft pass log (adsb.fi via the preview daemon)
        airhtml = ""
        try:
            ap = _json.load(open(os.path.join(ASSET_DIR, "air_passes.json")))
            if ap.get("dir") == d.get("night", d["dir"]) and ap.get("passes"):
                rows = "".join(f"<tr><td>{html.escape(p['t'])} UTC</td>"
                               f"<td>{html.escape(p['name'])}</td>"
                               f"<td>{p.get('alt','?'):,} ft</td>"
                               f"<td class=muted>{html.escape(p.get('path',''))}</td></tr>"
                               for p in ap["passes"][-20:][::-1])
                n = len(ap["passes"])
                latest = ap["passes"][-1]
                airhtml = (f"<details style='margin:16px 0'>"
                           f"<summary style='cursor:pointer'><b>Aircraft through the FOV</b> "
                           f"<span class=muted>— {n} pass{'es' if n != 1 else ''} tonight, "
                           f"latest {html.escape(latest['name'])} at {html.escape(latest['t'])} UTC "
                           f"(click to expand)</span></summary>"
                           f"<p class=muted>Live ADS-B (adsb.fi) — beaded/strobed streaks at these "
                           f"times are aircraft. Showing the most recent 20.</p>"
                           f"<table style='border-spacing:12px 2px'><tr class=muted>"
                           f"<th align=left>time</th><th align=left>flight</th>"
                           f"<th align=left>altitude</th><th align=left>path</th></tr>{rows}</table></details>")
        except Exception:
            pass
        # Gated rebuild control: only when tonight spans >1 capture segment (a restart/reboot,
        # so the incremental stack may be missing frames). POST + confirm keeps the otherwise
        # read-only dashboard from triggering it accidentally; the daemon does the work.
        flag = os.path.join(ASSET_DIR, ".tonight_rebuild_request")
        if os.path.isfile(flag):
            rebuild_ctrl = ("<p class=muted style='margin:10px 0'>⏳ Rebuild queued — the "
                            "cumulative stack will refresh in a minute or two.</p>")
        elif d.get("segments", 1) > 1:
            rebuild_ctrl = (
                f"<form method=post action='/rebuild_stack' style='margin:10px 0' "
                f"onsubmit=\"return confirm('Rebuild tonight\\u2019s cumulative stack from all "
                f"{d.get('segments')} capture segments? Re-reads the whole night; takes a minute or two.')\">"
                f"<button type=submit style='padding:6px 12px;cursor:pointer;background:#1b2735;"
                f"color:#cfe3ff;border:1px solid #2f4a63;border-radius:6px;font-size:13px'>"
                f"↻ Rebuild stack</button> "
                f"<span class=muted style='font-size:12px'>capture restarted {d.get('segments')}× "
                f"tonight — rebuild to include frames from before the restart(s)</span></form>")
        else:
            rebuild_ctrl = ""

        # Calibration controls — plate solve (after a camera move) + distortion refit. LAN-only
        # dashboard; POST + confirm; the solver scripts are sanity-gated and deploy only a good
        # fit (capture untouched). They run in FORCE mode, bypassing the auto guards.
        HOME = os.path.expanduser("~")
        solving = os.path.isfile(os.path.join(HOME, ".tilt_solving"))
        refitting = os.path.isfile(os.path.join(HOME, ".tilt_refitting"))
        bumpsolving = os.path.isfile(os.path.join(HOME, ".tilt_bumpsolving"))
        busy = solving or refitting or bumpsolving
        solved = os.path.isfile(os.path.join(HOME, ".tilt_autosolve_done"))
        refitted = os.path.isfile(os.path.join(HOME, ".tilt_refit_done"))
        if busy:
            calib_state = "⏳ %s running… (refresh in a minute or two)" % (
                "plate solve" if solving else "distortion refit" if refitting else "bump recovery")
        elif solved and refitted:
            calib_state = "platepar solved + distortion refined"
        elif solved:
            calib_state = "pointing solved; distortion refit pending"
        else:
            calib_state = "not solved — auto plate-solve armed (runs on the next clear window)"

        def _cbtn(action, label, confirm, disabled=False):
            dis = " disabled title='needs a pointing solve first'" if disabled else ""
            return (f"<form method=post action='{action}' style='display:inline;margin-right:8px'"
                    f" onsubmit=\"return confirm('{confirm}')\">"
                    f"<button type=submit{dis} style='padding:6px 12px;cursor:pointer;background:#1b2735;"
                    f"color:#cfe3ff;border:1px solid #2f4a63;border-radius:6px;font-size:13px;"
                    f"opacity:{'0.5' if disabled else '1'}'>{label}</button></form>")

        calib_ctrl = (
            "<h3>Calibration</h3>"
            f"<p class=muted style='margin:6px 0'>Live platepar: {calib_state}. Re-solve after "
            "moving the camera; refit to sharpen the distortion; fix a bump if the camera was "
            "knocked. All are sanity-gated — a poor fit is rejected and capture is untouched.</p>"
            + ("" if busy else
               _cbtn("/plate_solve", "↻ Re-solve platepar",
                     "Re-solve the platepar now (blind plate solve)? Needs a clear sky (~100 stars); "
                     "a poor solve is rejected.")
               + _cbtn("/refit_distortion", "Refit distortion",
                       "Refit the distortion from recent frames now?", disabled=not solved)
               # Bump recovery: correspondence solve that works on this fisheye (+ hazy skies)
               # where the blind plate solve can't. Grid-search + pointing fit + union refit.
               + _cbtn("/bump_solve", "✚ Fix camera bump",
                       "Recover the platepar after a knock? Grid-searches the shift from the "
                       "current pointing, refits, and deploys only if the fit is good.")))
        dusk, dawn = capture_window()
        window = ""
        if dusk:
            window = f"Dusk <b>{dusk.strftime('%H:%M')}</b>"
            if dawn:
                window += (f" → dawn <b>{dawn.strftime('%H:%M')} UTC</b>"
                           f" ({(dawn - dusk).total_seconds()/3600:.1f} h)")
            else:
                window += " UTC"
            window = " " + window + "."
        body = (
            f"<h2>Tonight &nbsp;{badge}</h2>"
            f"<p class=muted>Night <b>{html.escape(d['dir'])}</b> — auto-refreshes every 20&nbsp;s.{window} "
            + (f"Last frame {stale}s ago." if live else
               "Capture not currently active; showing the most recent built preview.") + "</p>"
            + star_chart
            + wx_chart
            + f"<h3>Live view <span class=muted style='font-size:12px'>· "
            + ("last frame before capture ended" if _day else "latest ~10&nbsp;s frame")
            + f"</span></h3><img src='/tonight_latest.png?v={mt('tonight_latest.png')}'>"
            f"<div style='display:flex;gap:16px;flex-wrap:wrap;margin:10px 0'>"
            + star_card
            + f"<div class=card><b>{d['count']}</b><br><span class=muted>FF blocks (256 fr each)</span></div>"
            f"<div class=card><b>{d['frames']:,}</b><br><span class=muted>frames</span></div>"
            f"<div class=card><b>{d['duration_min']:.0f} min</b><br><span class=muted>captured {'last night' if _day else 'tonight'}</span></div>"
            f"<div class=card><b>{ncand}</b><br><span class=muted>meteor candidates (pre-ML)</span></div>"
            f"</div>"
            + star_legend
            + dethtml + sathtml + airhtml +
            ((f"<h3>Detected meteors — night stack</h3>"
              f"<p class=muted>Max-stack of the {d.get('det_blocks', 0)} unexplained candidate block"
              f"{'s' if d.get('det_blocks', 0) != 1 else ''} the real-time detector flagged"
              + (f" ({d['det_air']} more attributed to aircraft and excluded)" if d.get('det_air') else "")
              + f". Predicted satellite corridors and bright-star positions are masked per block, so "
              f"what remains is the unexplained streaks — the likely meteors. Pre-ML; the filter and "
              f"astrometry adjudicate at dawn.</p>"
              f"<img src='/tonight_detected.png?v={mt('tonight_detected.png')}'>")
             if d.get("det_blocks", 0) > 0 and mt("tonight_detected.png") else "") +
            f"<h3>{'Last night' if _day else 'Night so far'} — cumulative stack</h3>"
            + ((f"<p class=muted>Every star trail, satellite and meteor caught since "
                f"{html.escape(d['first_ff'])} (through {html.escape(d['last_ff'])}). Live preview — "
                f"RMS builds the clean final stack at dawn. Overlay: <span style='color:#78c8ff'>stars</span>, "
                f"<span style='color:#ffd25a'>planets</span>, <span style='color:#fff'>Moon/NCP</span>, "
                f"<span style='color:#c878dc'>ecliptic</span>, <span style='color:#ff8c64'>N/E/S/W</span> — "
                f"labels mark positions at the latest frame (trail heads), via the live platepar.</p>"
                f"<img src='/tonight_stack.png?v={mt('tonight_stack.png')}'>")
               # Guard on stack_built (set by the preview builder) so a cloudy night — when
               # no clear frame has entered tonight's stack — never shows the previous night's
               # stale stack.png under tonight's caption.
               if d.get("stack_built") and mt("tonight_stack.png") else
               f"<p class=muted>No clear-sky frames in tonight's stack yet — the cumulative "
               f"stack appears once the sky clears.</p>")
            + (calib_ctrl if CALIB_TOOLS else "")
            + rebuild_ctrl)          # rebuild control at the very end of the page
        self.page("Tonight", body, refresh)

    def serve_asset(self, name, ctype):
        p = os.path.join(ASSET_DIR, os.path.basename(name))
        if not os.path.isfile(p):
            return self._send(404, "text/plain", b"asset not found")
        with open(p, "rb") as fh:
            self._send(200, ctype, fh.read(), {"Cache-Control": "max-age=3600"})

    def neighbours(self):
        jp = os.path.join(ASSET_DIR, "neighbours.json")
        if not os.path.isfile(jp):
            return self.page("Neighbours", "<p class=muted>Neighbours map not generated yet. "
                             "Run <code>make_neighbours_map.py</code>.</p>")
        import json as _json
        d = _json.load(open(jp))
        mtime = int(os.path.getmtime(os.path.join(ASSET_DIR, "neighbours_map.png"))) if \
            os.path.isfile(os.path.join(ASSET_DIR, "neighbours_map.png")) else 0
        rows = ""
        for s in d.get("nearest", []):
            bcol = {"close": "#e8590c", "prime": "#2f9e44", "far": "#868e96"}.get(s["band"], "#868e96")
            code = html.escape(s['code'])
            url = "https://globalmeteornetwork.org/weblog/{}/{}/".format(code[:2], code)
            rows += (f"<tr><td><a href='{url}' target=_blank><b>{code}</b></a></td><td>{s['dist']:.0f} km</td>"
                     f"<td>{html.escape(s['brg'])}</td>"
                     f"<td><span style='color:{bcol}'>●</span> {html.escape(s['band'])}</td>"
                     f"<td class=muted>{s['lat']:.3f}, {s['lon']:.3f}</td></tr>")
        body = (
            f"<h2>Meteor-network neighbours</h2>"
            f"<p class=muted>Nearest GMN / UK-network stations to your camera ({d['lat']:.2f}, {d['lon']:.2f}). "
            f"Positions are 25&nbsp;km FOV centroids (±~10–20&nbsp;km); overlap uses each station's real "
            f"100&nbsp;km field of view.</p>"
            f"<div style='display:flex;gap:16px;flex-wrap:wrap;margin:10px 0'>"
            f"<div class=card><b>{d['within_50']}</b><br><span class=muted>within 50 km</span></div>"
            f"<div class=card><b>{d['within_100']}</b><br><span class=muted>within 100 km</span></div>"
            f"<div class=card><b>{d['within_200']}</b><br><span class=muted>share your sky (&le;200 km)</span></div>"
            f"<div class=card><b>{d['prime_30_150']}</b><br><span class=muted>prime 30–150 km baseline</span></div>"
            f"</div>"
            f"<img src='/neighbours_map.png?v={mtime}' style='margin:8px 0'>"
            f"<h3>Nearest 20 stations</h3>"
            f"<table style='border-collapse:collapse;width:100%;font-size:13px'>"
            f"<tr style='text-align:left;color:#7c879b'><th>station</th><th>dist</th><th>dir</th>"
            f"<th>baseline</th><th>approx position</th></tr>{rows}</table>"
            f"<p class=muted style='margin-top:10px'>Once registered &amp; uploading, GMN pairs these "
            f"automatically for trajectory/orbit solving. Sky overlap is near-total here — the "
            f"<span style='color:#2f9e44'>30–150 km</span> stations give the best triangulation geometry.</p>")
        self.page("Neighbours", body)

    def home(self):
        ns = nights()
        if not ns:
            return self.page("RMS", "<p class=muted>No processed nights yet. Tonight's results appear "
                             "after dawn processing. Use the live view in the meantime.</p>")
        # hero = latest night's stack
        latest = ns[0]
        ld = os.path.join(ARCH_DET, latest)
        stack = find_suffix(ld, "_captured_stack.jpg")
        hero = ""
        if stack:
            hero = (f'<h2>Latest night — <a href="/night?d={urllib.parse.quote(latest)}">{html.escape(latest)}</a></h2>'
                    f'<div class=hero><a href="/night?d={urllib.parse.quote(latest)}">'
                    f'<img src="{fileurl(os.path.join(ld, stack))}"></a></div>')
        cards = []
        for n in ns:
            d = os.path.join(ARCH_DET, n)
            thumb = find_suffix(d, "_captured_stack.jpg") or find_suffix(d, "_CAPTURED_thumbs.jpg")
            img = f'<img src="{fileurl(os.path.join(d, thumb))}">' if thumb else '<div class=muted>no stack</div>'
            cards.append(f'<div class=card><a href="/night?d={urllib.parse.quote(n)}">{img}'
                         f'<h3>{html.escape(n)}</h3></a></div>')
        self.page("RMS nights", hero + "<h2>All nights</h2><div class=grid>" + "".join(cards) + "</div>")

    @staticmethod
    def _det_card(name, d, v, is_unf=False):
        qkey = f"u={d['i']}" if is_unf else f"i={d['i']}"
        qs = f"d={urllib.parse.quote(name)}&{qkey}&v={v}"
        img = (f"<img src='/detthumb?{qs}' loading=lazy style='height:130px;width:auto;margin:0 auto'>"
               if d.get("has_ff") else "<div class=muted style='padding:36px 0;text-align:center'>FF aged out</div>")
        pills = []
        if d.get("shower"):
            sh = d["shower"]
            scol = "#7ee29a" if sh != "spo" else "#8a97ab"
            pills.append(f"<span class=pill style='color:{scol};padding:1px 7px'>{html.escape(sh)}</span>")
        if d.get("peak_mag") is not None:
            pills.append(f"<span class=pill style='color:#ffd25a;padding:1px 7px'>{d['peak_mag']:+.1f}m</span>")
        if d.get("fr_idx") is not None:
            pills.append("<span class='pill on' style='padding:1px 7px'>FR raw</span>")
        if is_unf:
            pills.append("<span class='pill wait' style='padding:1px 7px'>pre-ML</span>")
        meta2 = []
        if d.get("ang_vel_deg_s") is not None:
            meta2.append(f"{d['ang_vel_deg_s']:.1f}&deg;/s")
        elif d.get("pxfm") is not None:
            meta2.append(f"{d['pxfm']:.1f} px/f")
        if d.get("compass_beg") and d.get("el_beg") is not None:
            meta2.append(f"{html.escape(d['compass_beg'])} {d['el_beg']:.0f}&deg;")
        if d.get("peak_snr") is not None:
            meta2.append(f"SNR {d['peak_snr']:.0f}")
        links = [f"<a href='/det?{qs}'>study &rarr;</a>"]
        if d.get("has_ff"):
            links.append(f"<a href='/detann?{qs}' target=_blank>sky map</a>")
        if d.get("fr_idx") is not None:
            links.append(f"<a href='/fr?d={urllib.parse.quote(name)}&f={d['fr_idx']}'>raw FR</a>")
        return (
            f"<div class=card><a href='/det?{qs}'>{img}</a>"
            f"<div style='font-size:12px;margin-top:6px'>"
            f"<b>{html.escape(d['time'])}</b> · {d['dur']:.2f}s · {d['nseg']} fr"
            + (f"<div style='margin-top:3px'>{' '.join(pills)}</div>" if pills else "")
            + (f"<div class=muted style='font-size:11px;margin-top:2px'>{' · '.join(meta2)}</div>" if meta2 else "")
            + f"<div style='margin-top:4px'>{' · '.join(links)}</div></div></div>"
        )

    def night(self, name):
        safe = real_within(ARCH_DET, name)
        if not safe or not os.path.isdir(safe):
            return self._send(404, "text/plain", b"no such night")
        parts = [f"<h2>{html.escape(name)}</h2>"]
        _, _, idx = det_index(name)
        dets = (idx or {}).get("detections", [])
        unfs = (idx or {}).get("unfiltered", [])
        frs = (idx or {}).get("fr_clips", [])
        extra_ffs = (idx or {}).get("extra_ffs", [])
        cap_cnt = (idx or {}).get("captured_ff_count", 0)
        v = (idx or {}).get("src_mtime", 0)

        # Summary strip + FF block UTC jump bar
        pts_cal = star_history_calstars(safe)
        peak_stars = max((n for _, n in pts_cal), default=0)
        parts.append(
            "<div style='display:flex;gap:12px;flex-wrap:wrap;margin:10px 0'>"
            f"<div class=card><b>{len(dets)}</b><br><span class=muted>confirmed meteors (post-ML)</span></div>"
            f"<div class=card><b>{len(unfs)}</b><br><span class=muted>rejected KHT candidates</span></div>"
            f"<div class=card><b>{len(frs)}</b><br><span class=muted>raw FR video cutouts</span></div>"
            f"<div class=card><b>{peak_stars}</b><br><span class=muted>peak clear-sky stars / FF</span></div>"
            + (f"<div class=card><b>{cap_cnt:,}</b><br><span class=muted>FF blocks on disk "
               f"({html.escape(idx.get('first_ff_utc') or '?')}–{html.escape(idx.get('last_ff_utc') or '?')} UT)</span></div>"
               if cap_cnt else "")
            + "</div>"
        )

        if cap_cnt or extra_ffs or dets:
            chips = ""
            if extra_ffs and not dets:
                chips = " &nbsp;<span class=muted>Archived ref blocks:</span> " + " ".join(
                    f"<a class=sbtn href='/ff?d={urllib.parse.quote(name)}&ff={urllib.parse.quote(e['ff'])}'>"
                    f"{html.escape(e['time'])}</a>" for e in extra_ffs[:6]
                )
            rng_hint = (f"{idx.get('first_ff_utc')} – {idx.get('last_ff_utc')} UT"
                        if cap_cnt and idx.get("first_ff_utc") else "HH:MM:SS UT")
            parts.append(
                f"<form method=get action='/ff' class='card sbar' style='margin:10px 0'>"
                f"<input type=hidden name=d value='{html.escape(name, True)}'>"
                f"<b>Inspect 10 s FF block:</b> "
                f"<input class=sinp type=text name=t placeholder='{html.escape(rng_hint, True)}' size=15> "
                f"<button type=submit class='sbtn active'>Open block &rarr;</button>"
                f"<span class=muted style='font-size:12px'>Jump to any UTC time (e.g. a streak spotted in the stack or sister station)</span>"
                f"{chips}</form>"
            )

        if dets:
            cards = [self._det_card(name, d, v, is_unf=False) for d in dets]
            parts.append(
                f"<h3>Confirmed detections ({len(dets)}) "
                f"<span class=muted style='font-size:12px'>— click any meteor for frame-by-frame playback, "
                f"light curve, track linearity &amp; sky coordinates</span></h3>"
                f"<div class=grid>{''.join(cards)}</div>"
            )
        else:
            parts.append(
                "<p class=muted style='margin:10px 0'><b>0 post-ML detections</b> in canonical "
                "<code>FTPdetectinfo</code> — check the raw <code>FR_*.bin</code> cutouts and "
                "pre-ML KHT candidates below, or jump to any 10 s FF block by UTC time above.</p>"
            )

        if frs:
            fcards = []
            for fc in frs:
                fqs = f"d={urllib.parse.quote(name)}&f={fc['i']}&v={v}"
                meta = [f"{fc['dur']:.2f}s", f"{fc['nframes']} raw fr"]
                if fc.get("ang_vel_deg_s") is not None:
                    meta.append(f"{fc['ang_vel_deg_s']:.1f}&deg;/s")
                if fc.get("compass_beg") and fc.get("el_beg") is not None:
                    meta.append(f"{html.escape(fc['compass_beg'])} {fc['el_beg']:.0f}&deg;")
                badge = "<span class='pill on' style='padding:1px 7px'>raw sensor frames</span>"
                if fc.get("det_idx") is not None:
                    badge += (f" <a class=pill href='/det?d={urllib.parse.quote(name)}&i={fc['det_idx']}&v={v}'>"
                              f"det #{fc['det_idx']+1}</a>")
                elif fc.get("unf_idx") is not None:
                    badge += (f" <a class=pill href='/det?d={urllib.parse.quote(name)}&u={fc['unf_idx']}&v={v}'>"
                              f"pre-ML #{fc['unf_idx']+1}</a>")
                fcards.append(
                    f"<div class=card><a href='/fr?{fqs}'>"
                    f"<img src='/frthumb?{fqs}' loading=lazy style='height:130px;width:auto;margin:0 auto'></a>"
                    f"<div style='font-size:12px;margin-top:6px'>"
                    f"<b>{html.escape(fc['time'])}</b> · {' · '.join(meta)}"
                    f"<div style='margin-top:3px'>{badge}</div>"
                    f"<div style='margin-top:4px'><a href='/fr?{fqs}'>study raw clip &rarr;</a> · "
                    f"<a href='/frfull?{fqs}' target=_blank>composite image</a></div></div></div>"
                )
            parts.append(
                f"<h3>Bright-event / fireball raw cutouts ({len(frs)}) "
                f"<span class=muted style='font-size:12px'>— true 25 fps sensor patches from "
                f"<code>FR_*.bin</code> (VideoExtraction)</span></h3>"
                f"<div class=grid>{''.join(fcards)}</div>"
            )

        if unfs:
            ucards = [self._det_card(name, u_d, v, is_unf=True) for u_d in unfs]
            open_attr = " open" if not dets else ""
            parts.append(
                f"<details class=sbox{open_attr}>"
                f"<summary>Pre-ML / rejected KHT candidates ({len(unfs)}) "
                f"<span class=muted style='font-weight:400'>— filtered out by dawn ML or astrometry; "
                f"sky coords projected via night platepar</span></summary>"
                f"<div class=grid>{''.join(ucards)}</div></details>"
            )

        _svg = star_chart_svg(pts_cal)
        if _svg:
            parts.append("<h3>Star count through the night <span class=muted style='font-size:12px'>"
                         "· UTC, 3-min peak; dashed = plate-solve thresholds</span></h3>" + _svg)
        for label, suf in [("Detected meteors", "_DETECTED_thumbs.jpg"),
                           ("Detected meteors — night stack", "_meteors.jpg"),
                           ("Radiant associations", "_radiants.png"),
                           ("Captured stack (whole night)", "_captured_stack.jpg"),
                           ("Captured thumbnails (sampled across the night, incl. dawn)", "_CAPTURED_thumbs.jpg"),
                           ("Field sums", "_fieldsums.png")]:
            f = find_suffix(safe, suf)
            if f:
                parts.append(f"<h3>{label}</h3><img src='{fileurl(os.path.join(safe, f))}'>")

        diag_imgs = []
        for dlabel, dsuf in [("Observation summary", "_observation_summary.png"),
                             ("Astrometry calibration report", "_calib_report_astrometry.jpg"),
                             ("Photometry calibration report", "_calib_report_photometry.png"),
                             ("FF intervals", "_ff_intervals.png")]:
            df = find_suffix(safe, dsuf)
            if df:
                diag_imgs.append(f"<h4>{dlabel}</h4><img src='{fileurl(os.path.join(safe, df))}' loading=lazy>")
        if diag_imgs:
            parts.append("<details class=sbox><summary>Calibration &amp; diagnostic plots "
                         f"({len(diag_imgs)})</summary>{''.join(diag_imgs)}</details>")

        tl = find_suffix(safe, "_timelapse.mp4")
        if tl:
            parts.append(f"<h3>Timelapse</h3><video controls style='max-width:100%'>"
                         f"<source src='{fileurl(os.path.join(safe, tl))}' type=video/mp4></video>")
        rep = find_suffix(safe, "_observation_summary.txt") or find_suffix(safe, ".txt")
        if rep:
            try:
                with open(os.path.join(safe, rep), "r", errors="replace") as fh:
                    txt = fh.read()[:20000]
                parts.append(f"<h3>{html.escape(rep)}</h3><pre>{html.escape(txt)}</pre>")
            except OSError:
                pass
        self.page(name, "".join(parts))

    def det(self, name, i_s, u_s=None):
        is_unf = u_s is not None and i_s is None
        raw_s = u_s if is_unf else (i_s or "-1")
        try:
            i = int(raw_s)
        except ValueError:
            return self._send(404, "text/plain", b"bad detection index")
        _, _, idx = det_index(name)
        group = "unfiltered" if is_unf else "detections"
        dets = (idx or {}).get(group, [])
        if not (0 <= i < len(dets)):
            return self._send(404, "text/plain", b"no such detection")
        d = dets[i]
        v = idx.get("src_mtime", 0)
        qparam = "u" if is_unf else "i"
        qs = f"d={urllib.parse.quote(name)}&{qparam}={i}&v={v}"
        nav_links = []
        if i > 0:
            nav_links.append(f"<a class=sbtn href='/det?d={urllib.parse.quote(name)}&{qparam}={i-1}&v={v}'>&larr; prev</a>")
        nav_links.append(f"<a class=sbtn href='/night?d={urllib.parse.quote(name)}'>all night events</a>")
        if d.get("has_ff"):
            nav_links.append(f"<a class=sbtn href='/ff?d={urllib.parse.quote(name)}&ff={urllib.parse.quote(d['ff'])}'>full 10s FF block</a>")
        if d.get("fr_idx") is not None:
            nav_links.append(f"<a class='sbtn active' href='/fr?d={urllib.parse.quote(name)}&f={d['fr_idx']}&v={v}'>raw FR clip &rarr;</a>")
        if i < len(dets) - 1:
            nav_links.append(f"<a class=sbtn href='/det?d={urllib.parse.quote(name)}&{qparam}={i+1}&v={v}'>next &rarr;</a>")

        status_pill = ("<span class='pill wait'>pre-ML / rejected KHT candidate</span>"
                       if is_unf else "<span class='pill on'>confirmed meteor (post-ML)</span>")
        if d.get("shower"):
            sh = d["shower"]
            status_pill += f" <span class=pill style='color:#7ee29a'>shower: {html.escape(sh)}</span>"
        if d.get("max_sat_px", 0) > 0:
            status_pill += f" <span class='pill off'>saturated ({d['max_sat_px']} px)</span>"

        # Summary metric cards
        mcards = [
            f"<div class=card><b>{html.escape(d['time'])}</b><br>"
            f"<span class=muted>{html.escape(d['date'])} · {d['dur']:.2f}s ({d['nseg']} seg, f{d['fmin']:.1f}–{d['fmax']:.1f})</span></div>",
        ]
        if d.get("peak_mag") is not None:
            abs_s = f" · abs {d['abs_mag']:+.1f}m" if d.get("abs_mag") is not None else ""
            mcards.append(f"<div class=card><b style='color:#ffd25a'>{d['peak_mag']:+.2f} mag</b><br>"
                          f"<span class=muted>peak brightness · SNR {d.get('peak_snr') or 0:.1f}{abs_s}</span></div>")
        else:
            mcards.append(f"<div class=card><b>SNR {d.get('peak_snr') or 0:.1f}</b><br>"
                          f"<span class=muted>peak inten {d.get('peak_inten', 0):,} ADU</span></div>")

        if d.get("ang_vel_deg_s") is not None:
            av = d["ang_vel_deg_s"]
            hint = ("meteor speed range" if 3.0 <= av <= 75.0 else
                    "slow (<3°/s: aircraft/sat/cloud?)" if av < 3.0 else "very fast")
            mcards.append(f"<div class=card><b>{av:.1f} &deg;/s</b> · {d.get('ang_len_deg') or 0:.1f}&deg;<br>"
                          f"<span class=muted>{d.get('pxfm') or 0:.1f} px/fr · {hint}</span></div>")
        else:
            mcards.append(f"<div class=card><b>{d.get('pxfm') or 0:.1f} px/fr</b> · {d.get('px_len', 0):.0f} px<br>"
                          f"<span class=muted>linearity RMS {d.get('lin_rms_px', 0):.2f} px</span></div>")

        if d.get("has_cal") and d.get("az_beg") is not None:
            cal_tag = "" if d.get("cal_source") == "ftpdetectinfo" else " (approx platepar)"
            mcards.append(
                f"<div class=card><b>{html.escape(d.get('compass_beg') or '')} {d['el_beg']:.1f}&deg; &rarr; "
                f"{html.escape(d.get('compass_end') or '')} {d['el_end']:.1f}&deg;</b><br>"
                f"<span class=muted>Az {d['az_beg']:.1f}&deg;&rarr;{d['az_end']:.1f}&deg; · "
                f"RA/Dec {d['ra_beg']:.1f}&deg;,{d['dec_beg']:+.1f}&deg;&rarr;{d['ra_end']:.1f}&deg;,{d['dec_end']:+.1f}&deg;{cal_tag}</span></div>"
            )
        mcards.append(
            f"<div class=card><b>{d.get('lin_rms_px', 0):.2f} px</b><br>"
            f"<span class=muted>cross-track RMS ({'tight line' if d.get('lin_rms_px', 0) < 1.5 else 'scattered'})</span></div>"
        )

        # Video + image player
        ff_dt = _ff_datetime(d["ff"])
        epoch_ms = int(ff_dt.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000) if ff_dt else 0
        f0_clip = max(0, int(d["fmin"]) - 15)
        vid_sources, img_sources = [], []
        if d.get("has_ff"):
            if d.get("px_len", 0) < 280:
                vid_sources = [("Zoomed track crop", f"/detcropvid?{qs}", f0_clip),
                               ("Full frame (FF rebuild)", f"/detvid?{qs}", f0_clip)]
            else:
                vid_sources = [("Full frame (FF rebuild)", f"/detvid?{qs}", f0_clip),
                               ("Zoomed track crop", f"/detcropvid?{qs}", f0_clip)]
            img_sources = [
                ("Annotated sky + reticle", f"/detann?{qs}"),
                ("Clean maxpixel", f"/detfull?{qs}"),
                ("Transient residual (max − ave)", f"/detresid?{qs}"),
            ]
        if d.get("fr_idx") is not None:
            fr_list = idx.get("fr_clips", [])
            if 0 <= d["fr_idx"] < len(fr_list):
                fc = fr_list[d["fr_idx"]]
                fqs = f"d={urllib.parse.quote(name)}&f={fc['i']}&v={v}"
                vid_sources.insert(0, ("Raw sensor cutout (FR.bin)", f"/frcropvid?{fqs}", fc["fmin"]))
                vid_sources.append(("Raw sensor full context", f"/frvid?{fqs}", fc["fmin"]))

        player = study_player_html(vid_sources, img_sources, f0_clip, d.get("peak_frame"), d.get("fps", 25.0), epoch_ms)

        lc_svg = det_lightcurve_svg(d)
        kin_svg = det_kinematics_svg(d)
        plots = ""
        if lc_svg:
            ylbl = "Apparent Magnitude (blue, brighter up)" if d.get("has_mag") else "Integrated Intensity ADU (blue)"
            plots += (f"<h3>Photometric light curve <span class=muted style='font-size:12px'>· {ylbl} "
                      f"&amp; SNR (dashed green); gold dot = peak frame</span></h3>{lc_svg}")
        if kin_svg:
            plots += (f"<h3>Centroid linearity &amp; frame speed <span class=muted style='font-size:12px'>"
                      f"· cross-track residual px from best-fit line (green, RMS {d.get('lin_rms_px', 0):.2f} px) "
                      f"&amp; step speed px/frame (dashed amber)</span></h3>{kin_svg}")

        # Per-segment measurement table + CSV
        segs = d.get("segments") or []
        seg_html = ""
        if segs:
            rows = []
            csv_lines = ["frame,utc,x_px,y_px,az_deg,el_deg,ra_deg,dec_deg,inten_adu,mag,bck_adu,snr,sat_px,cross_px"]
            for s in segs:
                az_s = f"{s['az']:.2f}" if s.get("az") is not None else "—"
                el_s = f"{s['el']:.2f}" if s.get("el") is not None else "—"
                ra_s = f"{s['ra']:.3f}" if s.get("ra") is not None else "—"
                dc_s = f"{s['dec']:+.3f}" if s.get("dec") is not None else "—"
                mg_s = f"{s['mag']:+.2f}" if s.get("mag") is not None else "—"
                rows.append(
                    f"<tr><td>{s['fr']:.3f}</td><td>{html.escape(s['ut'])}</td>"
                    f"<td>{s['x']:.2f}</td><td>{s['y']:.2f}</td>"
                    f"<td>{az_s}</td><td>{el_s}</td><td>{ra_s}</td><td>{dc_s}</td>"
                    f"<td>{s['inten']:,}</td><td>{mg_s}</td><td>{s['bck']}</td>"
                    f"<td>{s['snr']:.2f}</td><td>{s['sat']}</td><td>{s['cross_px']:+.2f}</td></tr>"
                )
                csv_lines.append(
                    f"{s['fr']:.4f},{s['ut']},{s['x']:.2f},{s['y']:.2f},"
                    f"{s.get('az') or ''},{s.get('el') or ''},{s.get('ra') or ''},{s.get('dec') or ''},"
                    f"{s['inten']},{s.get('mag') if s.get('mag') is not None else ''},"
                    f"{s['bck']},{s['snr']:.2f},{s['sat']},{s['cross_px']:.3f}"
                )
            seg_html = (
                f"<details class=sbox><summary>Per-frame astrometry &amp; photometry ({len(segs)} segments)</summary>"
                f"<div style='max-height:360px;overflow:auto;margin-top:8px'>"
                f"<table class=stbl><tr><th>Frame#</th><th>UTC</th><th>X</th><th>Y</th>"
                f"<th>Az&deg;</th><th>El&deg;</th><th>RA&deg;</th><th>Dec&deg;</th>"
                f"<th>Inten</th><th>Mag</th><th>Bck</th><th>SNR</th><th>Sat</th><th>Cross px</th></tr>"
                f"{''.join(rows)}</table></div>"
                f"<details style='margin-top:8px'><summary class=muted style='cursor:pointer'>CSV export</summary>"
                f"<pre>{html.escape(chr(10).join(csv_lines))}</pre></details></details>"
            )

        kind_lbl = "pre-ML candidate" if is_unf else "detection"
        body = (
            f"<div class=sbar style='justify-content:space-between'>"
            f"<h2 style='margin:6px 0'><a href='/night?d={urllib.parse.quote(name)}'>{html.escape(name)}</a> "
            f"— {kind_lbl} {i+1} of {len(dets)}</h2>"
            f"<div class=sbar>{' '.join(nav_links)}</div></div>"
            f"<div style='margin:4px 0 10px'>{status_pill} &nbsp;<code class=muted>{html.escape(d['ff'])}</code>"
            + (f" <span class=muted>({html.escape(d.get('ff_source') or '')})</span>" if d.get("ff_source") else "")
            + f"</div>"
            f"<div style='display:flex;gap:12px;flex-wrap:wrap;margin:10px 0'>{''.join(mcards)}</div>"
            f"{player}{plots}{seg_html}"
        )
        self.page(f"{name} · {kind_lbl} {i+1}", body)

    def fr(self, name, f_s):
        try:
            f_idx = int(f_s)
        except ValueError:
            return self._send(404, "text/plain", b"bad FR index")
        _, _, idx = det_index(name)
        clips = (idx or {}).get("fr_clips", [])
        if not (0 <= f_idx < len(clips)):
            return self._send(404, "text/plain", b"no such FR clip")
        fc = clips[f_idx]
        v = idx.get("src_mtime", 0)
        qs = f"d={urllib.parse.quote(name)}&f={f_idx}&v={v}"
        nav = []
        if f_idx > 0:
            nav.append(f"<a class=sbtn href='/fr?d={urllib.parse.quote(name)}&f={f_idx-1}&v={v}'>&larr; prev FR</a>")
        nav.append(f"<a class=sbtn href='/night?d={urllib.parse.quote(name)}'>all night events</a>")
        if fc.get("has_ff"):
            nav.append(f"<a class=sbtn href='/ff?d={urllib.parse.quote(name)}&ff={urllib.parse.quote(fc['ff'])}'>parent 10s FF block</a>")
        if fc.get("det_idx") is not None:
            nav.append(f"<a class='sbtn active' href='/det?d={urllib.parse.quote(name)}&i={fc['det_idx']}&v={v}'>confirmed det #{fc['det_idx']+1}</a>")
        elif fc.get("unf_idx") is not None:
            nav.append(f"<a class='sbtn active' href='/det?d={urllib.parse.quote(name)}&u={fc['unf_idx']}&v={v}'>pre-ML #{fc['unf_idx']+1}</a>")
        if f_idx < len(clips) - 1:
            nav.append(f"<a class=sbtn href='/fr?d={urllib.parse.quote(name)}&f={f_idx+1}&v={v}'>next FR &rarr;</a>")

        ff_dt = _ff_datetime(fc["ff"])
        epoch_ms = int(ff_dt.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000) if ff_dt else 0
        vid_sources = [
            ("Zoomed raw sensor cutout", f"/frcropvid?{qs}", fc["fmin"]),
            ("Full-frame context", f"/frvid?{qs}", fc["fmin"]),
        ]
        img_sources = [("Composite max (FR + FF)", f"/frfull?{qs}")]
        if fc.get("has_ff"):
            bqs = f"d={urllib.parse.quote(name)}&ff={urllib.parse.quote(fc['ff'])}"
            img_sources.append(("Annotated sky (parent FF)", f"/ffann?{bqs}"))
            img_sources.append(("Transient residual (parent FF)", f"/ffresid?{bqs}"))

        mcards = [
            f"<div class=card><b>{html.escape(fc['time'])}</b><br>"
            f"<span class=muted>{html.escape(fc['date'])} · {fc['dur']:.2f}s ({fc['nframes']} raw frames, f{fc['fmin']}–{fc['fmax']})</span></div>",
            f"<div class=card><b>{fc['cutout_px']}&times;{fc['cutout_px']} px</b><br>"
            f"<span class=muted>raw cutout · peak {fc['peak_adu']} ADU · sat {fc['max_sat_px']} px</span></div>",
        ]
        if fc.get("ang_vel_deg_s") is not None and fc.get("az_beg") is not None:
            mcards.append(
                f"<div class=card><b>{fc['ang_vel_deg_s']:.1f} &deg;/s</b> · {fc.get('ang_len_deg') or 0:.1f}&deg;<br>"
                f"<span class=muted>{html.escape(fc.get('compass_beg') or '')} {fc['el_beg']:.0f}&deg; &rarr; "
                f"{html.escape(fc.get('compass_end') or '')} {fc['el_end']:.0f}&deg;</span></div>"
            )

        player = study_player_html(vid_sources, img_sources, fc["fmin"], fc.get("peak_frame"), fc.get("fps", 25.0), epoch_ms)
        svg = fr_lightcurve_svg(fc)
        plot = (f"<h3>Raw cutout flux &amp; peak pixel ADU <span class=muted style='font-size:12px'>"
                f"· background-subtracted cutout sum (blue) &amp; peak pixel ADU (dashed orange, 255 = saturation)</span></h3>{svg}"
                if svg else "")
        body = (
            f"<div class=sbar style='justify-content:space-between'>"
            f"<h2 style='margin:6px 0'><a href='/night?d={urllib.parse.quote(name)}'>{html.escape(name)}</a> "
            f"— raw FR clip {f_idx+1} of {len(clips)}</h2>"
            f"<div class=sbar>{' '.join(nav)}</div></div>"
            f"<p class=muted style='margin:2px 0 10px'><span class='pill on'>raw 25 fps cutout</span> "
            f"<code>{html.escape(fc['fr'])}</code></p>"
            f"<div style='display:flex;gap:12px;flex-wrap:wrap;margin:10px 0'>{''.join(mcards)}</div>"
            f"{player}{plot}"
        )
        self.page(f"{name} · FR {f_idx+1}", body)

    def ff(self, name, ff_param="", time_param=""):
        safe = real_within(ARCH_DET, name)
        if not safe or not os.path.isdir(safe):
            return self._send(404, "text/plain", b"no such night")
        chosen, prev_ff, next_ff, total_ffs, delta_s = resolve_night_ff(name, ff_param, time_param)
        if not chosen:
            return self._send(404, "text/plain", b"no matching FF block on disk for that night/time")
        bqs = f"d={urllib.parse.quote(name)}&ff={urllib.parse.quote(chosen)}"
        ff_dt = _ff_datetime(chosen)
        tmstr = ff_dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " UT" if ff_dt else chosen
        epoch_ms = int(ff_dt.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000) if ff_dt else 0

        nav = []
        if prev_ff:
            nav.append(f"<a class=sbtn href='/ff?d={urllib.parse.quote(name)}&ff={urllib.parse.quote(prev_ff)}'>&larr; −10s block</a>")
        nav.append(f"<a class=sbtn href='/night?d={urllib.parse.quote(name)}'>back to night</a>")
        if next_ff:
            nav.append(f"<a class=sbtn href='/ff?d={urllib.parse.quote(name)}&ff={urllib.parse.quote(next_ff)}'>+10s block &rarr;</a>")

        # Cross-link any detections/candidates/FR clips inside this FF block
        _, _, idx = det_index(name)
        rel = []
        if idx:
            v = idx.get("src_mtime", 0)
            for d in idx.get("detections", []):
                if d["ff"] == chosen:
                    rel.append(f"<a class='pill on' href='/det?d={urllib.parse.quote(name)}&i={d['i']}&v={v}'>confirmed det #{d['i']+1} ({html.escape(d['time'])})</a>")
            for u_d in idx.get("unfiltered", []):
                if u_d["ff"] == chosen:
                    rel.append(f"<a class='pill wait' href='/det?d={urllib.parse.quote(name)}&u={u_d['i']}&v={v}'>pre-ML #{u_d['i']+1} ({html.escape(u_d['time'])})</a>")
            for fc in idx.get("fr_clips", []):
                if fc["ff"] == chosen:
                    rel.append(f"<a class='pill on' href='/fr?d={urllib.parse.quote(name)}&f={fc['i']}&v={v}'>raw FR clip #{fc['i']+1}</a>")

        vid_sources = [("Full 10s FF block (256 frames)", f"/ffvid?{bqs}", 0)]
        img_sources = [
            ("Annotated sky (stars + compass)", f"/ffann?{bqs}"),
            ("Transient residual (max − ave)", f"/ffresid?{bqs}"),
            ("Clean maxpixel", f"/fffull?{bqs}"),
        ]
        init_fr = round(max(0.0, min(255.0, delta_s * 25.0)), 1) if (time_param and delta_s > 0) else None
        player = study_player_html(vid_sources, img_sources, 0, init_fr, 25.0, epoch_ms)

        jump_bar = (
            f"<form method=get action='/ff' class=sbar style='margin:8px 0'>"
            f"<input type=hidden name=d value='{html.escape(name, True)}'>"
            f"<span class=muted>Jump UTC:</span> "
            f"<input class=sinp type=text name=t value='{html.escape(time_param or (ff_dt.strftime('%H:%M:%S') if ff_dt else ''), True)}' size=12> "
            f"<button type=submit class=sbtn>Go</button>"
            f"<span class=muted style='font-size:12px'>({total_ffs:,} blocks available)</span></form>"
        )
        body = (
            f"<div class=sbar style='justify-content:space-between'>"
            f"<h2 style='margin:6px 0'><a href='/night?d={urllib.parse.quote(name)}'>{html.escape(name)}</a> "
            f"— FF block {html.escape(tmstr)}</h2>"
            f"<div class=sbar>{' '.join(nav)}</div></div>"
            f"<p class=muted style='margin:2px 0 6px'><code>{html.escape(chosen)}</code>"
            + (" &nbsp;" + " ".join(rel) if rel else "") + "</p>"
            + jump_bar + player
        )
        self.page(f"{name} · {chosen}", body)

    def tdet(self, ff):
        """Detail page for a live /tonight flagged block: video snippet + full frame."""
        if not tonight_block_media(ff, "thumb"):
            return self._send(404, "text/plain", b"no such detection block")
        qs = "ff=" + urllib.parse.quote(ff)
        tm = re.search(r"_\d{8}_(\d{2})(\d{2})(\d{2})_", ff)
        tmstr = "{}:{}:{} UTC".format(*tm.groups()) if tm else "—"
        ff_dt = _ff_datetime(ff)
        epoch_ms = int(ff_dt.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000) if ff_dt else 0
        player = study_player_html(
            [("Full 10 s live FF block", f"/tdetvid?{qs}", 0)],
            [("Maxpixel full frame", f"/tdetfull?{qs}"), ("Transient residual thumb", f"/tdetthumb?{qs}")],
            0, None, 25.0, epoch_ms,
        )
        body = (f"<h2><a href='/tonight'>Tonight</a> — detection block {tmstr}</h2>"
                f"<p class=muted><code>{html.escape(ff)}</code> · live candidate "
                f"(ML filter + astrometry refine at dawn)</p>{player}")
        self.page("Tonight · " + tmstr, body)

    def det_asset(self, name, i_s, u_s, kind):
        is_unf = u_s is not None and i_s is None
        raw_s = u_s if is_unf else (i_s or "-1")
        try:
            i = int(raw_s)
        except ValueError:
            return self._send(404, "text/plain", b"bad detection index")
        group = "unfiltered" if is_unf else "detections"
        p = det_media(name, i, kind, group=group)
        if not p:
            return self._send(404, "text/plain", b"media unavailable")
        ctype = "video/mp4" if kind in ("video", "cropvideo") else "image/jpeg"
        self.serve_binary(p, ctype)

    def fr_asset(self, name, f_s, kind):
        try:
            f_idx = int(f_s)
        except ValueError:
            return self._send(404, "text/plain", b"bad FR index")
        p = fr_media(name, f_idx, kind)
        if not p:
            return self._send(404, "text/plain", b"media unavailable")
        ctype = "video/mp4" if kind in ("video", "cropvideo") else "image/jpeg"
        self.serve_binary(p, ctype)

    def ff_asset(self, name, ff_name, kind):
        p = night_block_media(name, ff_name, kind)
        if not p:
            return self._send(404, "text/plain", b"media unavailable")
        ctype = "video/mp4" if kind == "video" else "image/jpeg"
        self.serve_binary(p, ctype)

    def serve_binary(self, path, ctype):
        """Serve a file with minimal single-range support (video seeking)."""
        try:
            size = os.path.getsize(path)
        except OSError:
            return self._send(404, "text/plain", b"not found")
        start, end, partial = 0, size - 1, False
        m = re.match(r"bytes=(\d*)-(\d*)$", self.headers.get("Range") or "")
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                if m.group(2):
                    end = min(int(m.group(2)), size - 1)
            else:   # suffix range: last N bytes
                start = max(0, size - int(m.group(2)))
            if start >= size or start > end:
                return self._send(416, "text/plain", b"range not satisfiable",
                                  {"Content-Range": f"bytes */{size}"})
            partial = True
        try:
            with open(path, "rb") as fh:
                fh.seek(start)
                data = fh.read(end - start + 1)
        except OSError:
            return self._send(404, "text/plain", b"unreadable")
        hdrs = {"Accept-Ranges": "bytes", "Cache-Control": "max-age=86400",
                "Content-Length": str(len(data))}
        if partial:
            hdrs["Content-Range"] = f"bytes {start}-{end}/{size}"
        self._send(206 if partial else 200, ctype, data, hdrs)

    def serve_file(self, rel):
        ext = os.path.splitext(rel)[1].lower()
        if ext not in ALLOWED_EXT:
            return self._send(403, "text/plain", b"type not allowed")
        p = real_within(ROOT, rel)
        if not p or not os.path.isfile(p):
            return self._send(404, "text/plain", b"not found")
        ctype = mimetypes.guess_type(p)[0] or "application/octet-stream"
        try:
            with open(p, "rb") as fh:
                data = fh.read()
        except OSError:
            return self._send(404, "text/plain", b"unreadable")
        self._send(200, ctype, data, {"Cache-Control": "max-age=60"})

if __name__ == "__main__":
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), H)
    print(f"rms-web serving {ROOT} on :{PORT}")
    srv.serve_forever()
