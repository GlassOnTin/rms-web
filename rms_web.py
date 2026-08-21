#!/usr/bin/env python3
"""
rms_web.py — tiny read-only LAN dashboard for browsing RMS_data.

Serves the nightly RMS output (captured stacks, detection thumbnails, reports,
timelapses) over HTTP so they can be viewed in a browser without SSH. Read-only
(GET only), stdlib-only, path-sanitised to stay within RMS_data. NOT for exposure
to the public internet — LAN use only.
"""
import os, re, html, time, glob, datetime, urllib.parse, urllib.request, mimetypes, subprocess
import json, threading
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
DETCACHE = os.path.join(ASSET_DIR, "detcache")
DETGEN = os.path.join(ASSET_DIR, "detection_media.py")
VENV_PY = os.path.expanduser(os.environ.get("RMS_WEB_VENV_PY", "~/vRMS/bin/python3"))

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
        r = subprocess.run([VENV_PY, DETGEN, mode, night_dir, cache_dir, *map(str, args)],
                           capture_output=True, text=True, timeout=timeout)
        if r.returncode != 0:
            print("detection_media {} failed: {}".format(mode, r.stderr.strip()[-400:]))
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired) as e:
        print("detection_media {} failed: {}".format(mode, e))
        return False

def det_index(name):
    """(night_dir, cache_dir, index dict or None) for a night; (re)builds the
    thumbnail index when missing or when FTPdetectinfo has been reprocessed."""
    night_dir = real_within(ARCH_DET, name)
    if not night_dir or not os.path.isdir(night_dir):
        return None, None, None
    ftp = os.path.join(night_dir, "FTPdetectinfo_{}.txt".format(os.path.basename(night_dir)))
    cache_dir = os.path.join(DETCACHE, os.path.basename(night_dir))
    jp = os.path.join(cache_dir, "detections.json")
    if not os.path.isfile(ftp):
        return night_dir, cache_dir, None
    with _det_lock(name):
        idx = None
        if os.path.isfile(jp):
            try:
                idx = json.load(open(jp))
            except (OSError, ValueError):
                idx = None
        if idx is None or idx.get("src_mtime") != int(os.path.getmtime(ftp)):
            idx = None
            if _det_gen("index", night_dir, cache_dir):
                try:
                    idx = json.load(open(jp))
                except (OSError, ValueError):
                    idx = None
    return night_dir, cache_dir, idx

def det_media(name, i, kind):
    """Absolute path of detection i's 'video'/'full'/'thumb' file, generating
    video/full on first request. None if the night/index/FF is unavailable."""
    night_dir, cache_dir, idx = det_index(name)
    if not idx or not (0 <= i < len(idx.get("detections", []))):
        return None
    d = idx["detections"][i]
    p = os.path.join(cache_dir, d[kind])
    if not os.path.isfile(p) and kind in ("video", "full") and d.get("has_ff"):
        with _det_lock(name):
            if not os.path.isfile(p):
                _det_gen(kind, night_dir, cache_dir, i)
    return p if os.path.isfile(p) else None

def capture_status():
    def active(s):
        try:
            return subprocess.run(["systemctl", "is-active", s], capture_output=True, text=True).stdout.strip()
        except Exception:
            return "?"
    cap = active("rms-capture")
    # newest capturing night (may be tonight, pre-processing)
    try:
        cur = sorted([d for d in os.listdir(ARCHIVED) if os.path.isdir(os.path.join(ARCHIVED, d))], reverse=True)
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
    """Tonight's planned capture window from the newest RMS log: StartCapture
    logs 'Next start time: ... UTC' (dusk) and '... to start recording for X hrs'
    (dawn = start + duration). Reuses RMS's own ephemeris decision instead of
    recomputing sun altitudes here. After dawn the log's last 'Next start time'
    already points at the following dusk, which is what the waiting state wants."""
    try:
        logs = os.path.join(ROOT, "logs")
        cur = sorted(f for f in os.listdir(logs) if f.startswith("log_") and f.endswith(".log"))[-1]
        txt = open(os.path.join(logs, cur), errors="replace").read()
        starts = re.findall(r"Next start time: (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})", txt)
        hrs = re.findall(r"to start recording for ([\d.]+) hrs", txt)
        if not starts:
            return None, None
        dusk = datetime.datetime.strptime(starts[-1], "%Y-%m-%d %H:%M:%S")
        dawn = dusk + datetime.timedelta(hours=float(hrs[-1])) if hrs else None
        return dusk, dawn
    except Exception:
        return None, None

def tonight_detections():
    """Live meteor candidates from the newest RMS log: per-FF 'detected meteors: N'
    lines emitted by the real-time detector (pre-ML; the ML filter prunes at dawn)."""
    try:
        logs = os.path.join(ROOT, "logs")
        cur = sorted(f for f in os.listdir(logs) if f.startswith("log_") and f.endswith(".log"))[-1]
        out = subprocess.run(["grep", "-F", "detected meteors:", os.path.join(logs, cur)],
                             capture_output=True, text=True).stdout
    except Exception:
        return 0, []
    total, hits = 0, []
    for m in re.finditer(r"(FF_\S+\.fits) detected meteors: (\d+)", out):
        total += 1
        n = int(m.group(2))
        if n:
            hits.append((m.group(1), n))
    return total, hits

PAGE = """<!doctype html><html><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
{refresh}
<title>{title}</title><style>
:root{{color-scheme:dark}}
body{{margin:0;background:#0a0d14;color:#c8d0dc;font:15px/1.5 system-ui,sans-serif}}
a{{color:#7db4ff;text-decoration:none}} a:hover{{text-decoration:underline}}
header{{padding:14px 20px;background:#111726;border-bottom:1px solid #1e2740;position:sticky;top:0;display:flex;gap:16px;align-items:baseline;flex-wrap:wrap}}
header h1{{font-size:17px;margin:0;color:#eaf0fa}}
.pill{{font-size:12px;padding:2px 9px;border-radius:20px;background:#1a2740}}
.on{{background:#123a1e;color:#7ee29a}} .off{{background:#3a1414;color:#e28b8b}} .wait{{background:#3a2f14;color:#e2c98b}}
main{{padding:20px;max-width:1100px;margin:0 auto}}
.muted{{color:#7c879b;font-size:13px}}
img{{max-width:100%;border-radius:8px;display:block;background:#000}}
.grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px;margin-top:16px}}
.card{{background:#111726;border:1px solid #1e2740;border-radius:10px;padding:10px}}
.card h3{{font-size:13px;margin:8px 2px 2px;font-weight:600}}
pre{{white-space:pre-wrap;background:#0d1220;border:1px solid #1e2740;border-radius:8px;padding:12px;font-size:12px;overflow:auto;max-height:340px}}
.hero{{margin:8px 0 4px}} h2{{font-size:15px;margin:24px 0 6px;color:#eaf0fa}}
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
            return self.det(q.get("d", [""])[0], q.get("i", ["-1"])[0])
        if u.path in ("/detthumb", "/detvid", "/detfull"):
            kind = {"/detthumb": "thumb", "/detvid": "video", "/detfull": "full"}[u.path]
            return self.det_asset(q.get("d", [""])[0], q.get("i", ["-1"])[0], kind)
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
        if hits:
            rows = ""
            for ff, n in hits[-20:][::-1]:   # newest first, cap at 20
                tm = re.search(r"_\d{8}_(\d{2})(\d{2})(\d{2})_", ff)
                tm = "{}:{}:{} UTC".format(*tm.groups()) if tm else "—"
                rows += (f"<tr><td>{tm}</td><td>{n}</td>"
                         f"<td class=muted>{html.escape(ff)}</td></tr>")
            tm_latest = re.search(r"_\d{8}_(\d{2})(\d{2})(\d{2})_", hits[-1][0])
            tm_latest = "{}:{}:{} UTC".format(*tm_latest.groups()) if tm_latest else "—"
            dethtml = (f"<details style='margin:16px 0'>"
                       f"<summary style='cursor:pointer'><b>Detections tonight</b> "
                       f"<span class=muted>— {ncand} candidate{'s' if ncand != 1 else ''}, "
                       f"latest at {tm_latest} (click to expand)</span></summary>"
                       f"<p class=muted>Real-time detector candidates ({nproc} FF blocks screened so far). "
                       f"The ML filter and astrometry refine these at dawn — expect the final count to be lower.</p>"
                       f"<table style='border-spacing:12px 2px'><tr class=muted>"
                       f"<th align=left>time</th><th align=left>n</th><th align=left>FF block</th></tr>{rows}</table></details>")
        else:
            dethtml = (f"<h3>Detections tonight</h3>"
                       f"<p class=muted>No meteor candidates yet ({nproc} FF blocks screened so far).</p>")
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
            f"<div style='display:flex;gap:16px;flex-wrap:wrap;margin:10px 0'>"
            f"<div class=card><b>{d['count']}</b><br><span class=muted>FF blocks (256 fr each)</span></div>"
            f"<div class=card><b>{d['frames']:,}</b><br><span class=muted>frames</span></div>"
            f"<div class=card><b>{d['duration_min']:.0f} min</b><br><span class=muted>captured tonight</span></div>"
            f"<div class=card><b>{ncand}</b><br><span class=muted>meteor candidates (pre-ML)</span></div>"
            f"</div>"
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
            f"<h3>Latest sky (~10 s)</h3>"
            f"<img src='/tonight_latest.png?v={mt('tonight_latest.png')}'>"
            f"<h3>Night so far — cumulative stack</h3>"
            f"<p class=muted>Every star trail, satellite and meteor caught since "
            f"{html.escape(d['first_ff'])} (through {html.escape(d['last_ff'])}). Live preview — "
            f"RMS builds the clean final stack at dawn. Overlay: <span style='color:#78c8ff'>stars</span>, "
            f"<span style='color:#ffd25a'>planets</span>, <span style='color:#fff'>Moon/NCP</span>, "
            f"<span style='color:#c878dc'>ecliptic</span>, <span style='color:#ff8c64'>N/E/S/W</span> — "
            f"labels mark positions at the latest frame (trail heads), via the live platepar.</p>"
            f"<img src='/tonight_stack.png?v={mt('tonight_stack.png')}'>")
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
            f"<p class=muted>Nearest GMN / UK-network stations to your camera ({d['lat']:.4f}, {d['lon']:.4f}). "
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

    def night(self, name):
        safe = real_within(ARCH_DET, name)
        if not safe or not os.path.isdir(safe):
            return self._send(404, "text/plain", b"no such night")
        parts = [f"<h2>{html.escape(name)}</h2>"]
        # Per-detection cards: thumbnail links to a detail page with the
        # FF-reconstruction clip; direct links to the clip and full-res frame.
        _, _, idx = det_index(name)
        if idx and idx.get("detections"):
            v = idx.get("src_mtime", 0)
            cards = []
            for d in idx["detections"]:
                qs = f"d={urllib.parse.quote(name)}&i={d['i']}&v={v}"
                img = (f"<img src='/detthumb?{qs}' loading=lazy style='height:130px;width:auto'>"
                       if d.get("has_ff") else "<div class=muted>FF not archived</div>")
                links = (f"<a href='/det?{qs}'>video</a> · <a href='/detfull?{qs}' target=_blank>full image</a>"
                         if d.get("has_ff") else "")
                cards.append(f"<div class=card><a href='/det?{qs}'>{img}</a>"
                             f"<div style='font-size:12px;margin-top:4px'>{html.escape(d['time'])} · "
                             f"{d['dur']:.2f}s · {d['nseg']} frames<br>{links}</div></div>")
            parts.append(f"<h3>Detections ({len(idx['detections'])}) "
                         f"<span class=muted style='font-size:12px'>— click a thumbnail for the video "
                         f"snippet (½ speed, rebuilt from the FF block)</span></h3>"
                         f"<div class=grid>{''.join(cards)}</div>")
        for label, suf in [("Detected meteors", "_DETECTED_thumbs.jpg"),
                           ("Detected meteors — night stack", "_meteors.jpg"),
                           ("Captured stack (whole night)", "_captured_stack.jpg"),
                           ("Captured thumbnails (sampled across the night, incl. dawn)", "_CAPTURED_thumbs.jpg"),
                           ("Field sums", "_fieldsums.png")]:
            f = find_suffix(safe, suf)
            if f:
                parts.append(f"<h3>{label}</h3><img src='{fileurl(os.path.join(safe, f))}'>")
        # timelapse
        tl = find_suffix(safe, "_timelapse.mp4")
        if tl:
            parts.append(f"<h3>Timelapse</h3><video controls style='max-width:100%'>"
                         f"<source src='{fileurl(os.path.join(safe, tl))}' type=video/mp4></video>")
        # observation summary text
        rep = find_suffix(safe, "_observation_summary.txt") or find_suffix(safe, ".txt")
        if rep:
            try:
                with open(os.path.join(safe, rep), "r", errors="replace") as fh:
                    txt = fh.read()[:20000]
                parts.append(f"<h3>{html.escape(rep)}</h3><pre>{html.escape(txt)}</pre>")
            except OSError:
                pass
        self.page(name, "".join(parts))

    def det(self, name, i_s):
        try:
            i = int(i_s)
        except ValueError:
            return self._send(404, "text/plain", b"bad detection index")
        _, _, idx = det_index(name)
        dets = (idx or {}).get("detections", [])
        if not (0 <= i < len(dets)):
            return self._send(404, "text/plain", b"no such detection")
        d = dets[i]
        v = idx.get("src_mtime", 0)
        qs = f"d={urllib.parse.quote(name)}&i={i}&v={v}"
        nav = " · ".join(
            [f"<a href='/det?d={urllib.parse.quote(name)}&i={i-1}&v={v}'>&larr; prev</a>"] * (i > 0)
            + [f"<a href='/det?d={urllib.parse.quote(name)}&i={i+1}&v={v}'>next &rarr;</a>"] * (i < len(dets) - 1))
        media = ("<p class=muted>FF file not archived for this detection — no media available.</p>"
                 if not d.get("has_ff") else
                 f"<video controls autoplay muted loop playsinline style='max-width:100%;background:#000'>"
                 f"<source src='/detvid?{qs}' type=video/mp4></video>"
                 f"<p class=muted>Clip rebuilt from the FF block (avepixel + maxpixel transients), "
                 f"played at ½ speed; first load takes a few seconds while it renders. "
                 f"<a href='/detfull?{qs}' target=_blank>full-resolution maxpixel frame</a></p>")
        body = (f"<h2><a href='/night?d={urllib.parse.quote(name)}'>{html.escape(name)}</a> — "
                f"detection {i+1} of {len(dets)}</h2>"
                f"<p>{html.escape(d['date'])} {html.escape(d['time'])} · {d['dur']:.2f} s · "
                f"{d['nseg']} frames · <code>{html.escape(d['ff'])}</code></p>"
                f"{media}<p>{nav}</p>")
        self.page(f"{name} · det {i+1}", body)

    def det_asset(self, name, i_s, kind):
        try:
            i = int(i_s)
        except ValueError:
            return self._send(404, "text/plain", b"bad detection index")
        p = det_media(name, i, kind)
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
