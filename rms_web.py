#!/usr/bin/env python3
"""
rms_web.py — tiny read-only LAN dashboard for browsing RMS_data.

Serves the nightly RMS output (captured stacks, detection thumbnails, reports,
timelapses) over HTTP so they can be viewed in a browser without SSH. Read-only
(GET only), stdlib-only, path-sanitised to stay within RMS_data. NOT for exposure
to the public internet — LAN use only.
"""
import os, re, html, urllib.parse, mimetypes, subprocess
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.environ.get("RMS_DATA", "/mnt/nvme/RMS_data")
ARCHIVED = os.path.join(ROOT, "CapturedFiles")   # nights live here; ArchivedFiles holds detections
ARCH_DET = os.path.join(ROOT, "ArchivedFiles")
PORT = int(os.environ.get("RMS_WEB_PORT", "8080"))
ASSET_DIR = os.path.dirname(os.path.abspath(__file__))   # neighbours_map.png / neighbours.json live here
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".gif", ".mp4", ".txt", ".log"}

STATION_NAME = os.environ.get("STATION_NAME", "Denmead")

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
    return cap, cur

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
.on{{background:#123a1e;color:#7ee29a}} .off{{background:#3a1414;color:#e28b8b}}
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
<span class="pill {capcls}">capture: {cap}</span>
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
        station = f"{STATION_NAME} ({code})" if code and code != "XX0001" else STATION_NAME
        htmlout = PAGE.format(title=html.escape(title), station=html.escape(station),
                              cap=html.escape(cap), capcls=("on" if cap == "active" else "off"),
                              cur=html.escape(cur), host=html.escape(host), body=body, refresh=refresh)
        self._send(200, "text/html; charset=utf-8", htmlout.encode())

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        if u.path == "/":
            return self.home()
        if u.path == "/night":
            return self.night(q.get("d", [""])[0])
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
        live = stale < 120 and cap == "active"
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
            dethtml = (f"<h3>Detections tonight</h3>"
                       f"<p class=muted>Real-time detector candidates ({nproc} FF blocks screened so far). "
                       f"The ML filter and astrometry refine these at dawn — expect the final count to be lower.</p>"
                       f"<table style='border-spacing:12px 2px'><tr class=muted>"
                       f"<th align=left>time</th><th align=left>n</th><th align=left>FF block</th></tr>{rows}</table>")
        else:
            dethtml = (f"<h3>Detections tonight</h3>"
                       f"<p class=muted>No meteor candidates yet ({nproc} FF blocks screened so far).</p>")
        # satellite pass log (written by the preview daemon's TLE propagation)
        sathtml = ""
        try:
            sp = _json.load(open(os.path.join(ASSET_DIR, "sat_passes.json")))
            if sp.get("dir") == d["dir"] and sp.get("passes"):
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
            if ap.get("dir") == d["dir"] and ap.get("passes"):
                rows = "".join(f"<tr><td>{html.escape(p['t'])} UTC</td>"
                               f"<td>{html.escape(p['name'])}</td>"
                               f"<td>{p.get('alt','?'):,} ft</td>"
                               f"<td class=muted>{html.escape(p.get('path',''))}</td></tr>"
                               for p in ap["passes"][-20:][::-1])
                airhtml = (f"<h3>Aircraft through the FOV</h3>"
                           f"<p class=muted>Live ADS-B (adsb.fi) — beaded/strobed streaks at these "
                           f"times are aircraft.</p>"
                           f"<table style='border-spacing:12px 2px'><tr class=muted>"
                           f"<th align=left>time</th><th align=left>flight</th>"
                           f"<th align=left>altitude</th><th align=left>path</th></tr>{rows}</table>")
        except Exception:
            pass
        body = (
            f"<h2>Tonight &nbsp;{badge}</h2>"
            f"<p class=muted>Night <b>{html.escape(d['dir'])}</b> — auto-refreshes every 20&nbsp;s. "
            + (f"Last frame {stale}s ago." if live else
               "Capture not currently active; showing the most recent built preview.") + "</p>"
            f"<div style='display:flex;gap:16px;flex-wrap:wrap;margin:10px 0'>"
            f"<div class=card><b>{d['count']}</b><br><span class=muted>FF blocks (256 fr each)</span></div>"
            f"<div class=card><b>{d['frames']:,}</b><br><span class=muted>frames</span></div>"
            f"<div class=card><b>{d['duration_min']:.0f} min</b><br><span class=muted>captured tonight</span></div>"
            f"<div class=card><b>{ncand}</b><br><span class=muted>meteor candidates (pre-ML)</span></div>"
            f"</div>"
            + dethtml + sathtml + airhtml +
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
