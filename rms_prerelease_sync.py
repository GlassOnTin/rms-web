#!/usr/bin/env python3
"""
rms_prerelease_sync.py — daytime & cloudy-night safe tracker/updater for
CroatianMeteorNetwork/RMS `prerelease`.

Policy:
  - NEVER updates or restarts StartCapture during clear/partly-clear night skies
    (matched/detected stars > 3 or recent meteor detections) or while post-night
    reprocessing / calibration (RMS.Reprocess, ukmonPostProc, iStream) is running.
  - Allows `--apply` updates ONLY when `behind_count > 0` AND either:
      a) `safe_daytime`: solar elevation > +5.0° at station (lat, lon) and no
         `FF_*.fits` written in the last 45 minutes, OR
      b) `safe_cloudy_night`: night capture is active under an auto-restarting
         systemd service (`Restart=always`), >= 3 recent FF blocks & star-count
         readings over the last 20 minutes confirm overcast sky (median stars <= 3,
         last-3 max <= 3, and 0 meteors in the last 5 minutes).
  - Preserves station-owned files (`.config`, `mask.bmp`, `camera_settings.json`,
    `flat.bmp`, `dark.bmp`, `platepar_cmn2010.cal`) byte-for-byte, 3-way re-applies
    any tracked local code patches (e.g. `RMS/BufferedCapture.py`, `Utils/Flux.py`),
    rebuilds Cython `.so` extensions onto fresh inodes (preventing live `SIGBUS`),
    verifies imports, and rolls back atomically on any merge conflict or build error.

Stdlib-only control plane; invokes `<vRMS>/bin/python` for Cython build & smoke test.
"""
import argparse
import datetime
import errno
import glob
import json
import math
import os
import re
import shutil
import signal
import statistics
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
UPSTREAM_REPO_SUBSTR = "CroatianMeteorNetwork/RMS"
DEFAULT_BRANCH = "prerelease"

STATION_OWNED_FILES = (".config", "mask.bmp", "camera_settings.json", "flat.bmp", "dark.bmp")
EXTRA_BACKUP_FILES = ("platepar_cmn2010.cal",)
BLOCKING_PROCS = (
    "RMS.Reprocess",
    "RMS/Reprocess.py",
    "ukmonPostProc",
    "iStream",
    "auto_tilt_solve.py",
    "auto_refit_distortion.py",
    "bump_solve.py",
)

DAYTIME_MIN_SUN_DEG = 5.0
DAYTIME_QUIET_FF_S = 45 * 60
CLOUDY_MAX_STARS = 3
CLOUDY_MIN_SAMPLES = 3
LOCK_STALE_S = 30 * 60


def read_rms_cfg(cfg_path, key, default=None):
    try:
        with open(cfg_path, "r", errors="replace") as fh:
            for line in fh:
                line = line.split(";", 1)[0]
                if line.strip().startswith(key + ":"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return default


def resolve_paths():
    home = os.path.expanduser("~")
    rms_src = os.path.expanduser(os.environ.get("RMS_SOURCE", os.path.join(home, "source", "RMS")))
    rms_cfg = os.path.expanduser(os.environ.get("RMS_CONFIG", os.path.join(rms_src, ".config")))
    raw_data = os.environ.get("RMS_DATA") or read_rms_cfg(rms_cfg, "data_dir") or "~/RMS_data"
    if raw_data.startswith("~/"):
        data_dir = os.path.join(home, raw_data[2:])
    else:
        data_dir = os.path.expanduser(raw_data)
    venv_py = os.path.expanduser(os.environ.get("RMS_WEB_VENV_PY", os.path.join(home, "vRMS", "bin", "python3")))
    if not os.path.isfile(venv_py):
        alt = os.path.join(os.path.dirname(venv_py), "python")
        if os.path.isfile(alt):
            venv_py = alt
    cap_svc = os.environ.get("RMS_CAPTURE_SERVICE", "rms-capture")
    state_dir = os.path.expanduser(os.environ.get("RMS_SYNC_STATE_DIR", HERE))
    station = os.environ.get("STATION_NAME") or read_rms_cfg(rms_cfg, "stationID") or "RMS"
    lat_s = os.environ.get("STATION_LAT") or read_rms_cfg(rms_cfg, "latitude")
    lon_s = os.environ.get("STATION_LON") or read_rms_cfg(rms_cfg, "longitude")
    lat = float(lat_s) if lat_s is not None else None
    lon = float(lon_s) if lon_s is not None else None
    return {
        "rms_src": rms_src,
        "rms_cfg": rms_cfg,
        "data_dir": data_dir,
        "venv_py": venv_py,
        "capture_service": cap_svc,
        "state_dir": state_dir,
        "station": station,
        "lat": lat,
        "lon": lon,
    }


def solar_elevation_deg(utc_dt, lat_deg, lon_deg):
    """Astronomical geometric solar altitude (deg) at naive UTC `utc_dt` and `(lat_deg, lon_deg)`.
    Meeus low-precision solar coordinates; accurate to ~0.02° without external ephemeris packages."""
    jd = (utc_dt - datetime.datetime(2000, 1, 1, 12, 0, 0)).total_seconds() / 86400.0 + 2451545.0
    t = (jd - 2451545.0) / 36525.0
    l0 = (280.46646 + t * (36000.76983 + 0.0003032 * t)) % 360.0
    m = 357.52911 + t * (35999.05029 - 0.0001537 * t)
    mr = math.radians(m)
    c = ((1.914602 - t * (0.004817 + 0.000014 * t)) * math.sin(mr)
         + (0.019993 - 0.000101 * t) * math.sin(2.0 * mr)
         + 0.000289 * math.sin(3.0 * mr))
    sun_lon = l0 + c
    omega = 125.04 - 1934.136 * t
    lam = math.radians(sun_lon - 0.00569 - 0.00478 * math.sin(math.radians(omega)))
    eps0 = 23.0 + (26.0 + (21.448 - t * (46.8150 + t * (0.00059 - t * 0.001813))) / 60.0) / 60.0
    eps = math.radians(eps0 + 0.00256 * math.cos(math.radians(omega)))

    ra = math.atan2(math.cos(eps) * math.sin(lam), math.cos(lam))
    dec = math.asin(math.sin(eps) * math.sin(lam))

    gmst_deg = (280.46061837 + 360.98564736629 * (jd - 2451545.0)
                + 0.000387933 * t * t - (t * t * t) / 38710000.0) % 360.0
    ha = math.radians((gmst_deg + lon_deg) - math.degrees(ra))
    lat_r = math.radians(lat_deg)
    sin_alt = math.sin(lat_r) * math.sin(dec) + math.cos(lat_r) * math.cos(dec) * math.cos(ha)
    return math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))


def list_running_processes():
    try:
        out = subprocess.run(["ps", "-eo", "args"], capture_output=True, text=True, timeout=10).stdout
        return out.splitlines()
    except Exception:
        return []


def check_supervisor(capture_service):
    """Inspect systemd capture unit supervision state (`Restart=always` + `ActiveState`)."""
    svc = capture_service if capture_service.endswith(".service") else capture_service + ".service"
    try:
        r = subprocess.run(
            ["systemctl", "show", "-p", "LoadState,ActiveState,SubState,Restart,MainPID,User", svc],
            capture_output=True, text=True, timeout=8,
        )
        kv = {}
        for ln in r.stdout.splitlines():
            if "=" in ln:
                k, v = ln.split("=", 1)
                kv[k.strip()] = v.strip()
        loaded = kv.get("LoadState") == "loaded"
        active = kv.get("ActiveState") == "active"
        auto_restart = kv.get("Restart", "") in ("always", "on-success", "on-failure")
        pid = int(kv.get("MainPID") or "0")
        return {
            "unit": svc,
            "loaded": loaded,
            "active": active,
            "restart_policy": kv.get("Restart", ""),
            "supervised": bool(loaded and active and auto_restart and pid > 1),
            "main_pid": pid,
            "user": kv.get("User", ""),
        }
    except Exception:
        return {"unit": svc, "loaded": False, "active": False, "supervised": False, "main_pid": 0}


def read_recent_ff_activity(data_dir, now_ts=None):
    """Return `(newest_ff_age_s or None, count_last_20m, newest_night_name)`."""
    now_ts = time.time() if now_ts is None else now_ts
    cap_root = os.path.join(data_dir, "CapturedFiles")
    if not os.path.isdir(cap_root):
        return None, 0, None
    rx = re.compile(r"_(\d{8}_\d{6})_")
    nights = sorted(
        (d for d in os.listdir(cap_root) if os.path.isdir(os.path.join(cap_root, d))),
        key=lambda n: (rx.search(n).group(1) if rx.search(n) else n),
        reverse=True,
    )
    if not nights:
        return None, 0, None
    for night in nights[:2]:
        ndir = os.path.join(cap_root, night)
        newest_m = 0.0
        cnt_20m = 0
        try:
            for fn in os.listdir(ndir):
                if fn.startswith("FF_") and fn.endswith(".fits"):
                    mt = os.path.getmtime(os.path.join(ndir, fn))
                    if mt > newest_m:
                        newest_m = mt
                    if now_ts - mt <= 20 * 60:
                        cnt_20m += 1
        except OSError:
            continue
        if newest_m > 0:
            return max(0.0, now_ts - newest_m), cnt_20m, night
    return None, 0, nights[0]


_STAR_RX = re.compile(r"^(\d{4})/(\d\d)/(\d\d) (\d\d):(\d\d):(\d\d).*?Detected stars: (\d+)")
_MET_RX = re.compile(r"^(\d{4})/(\d\d)/(\d\d) (\d\d):(\d\d):(\d\d).*?FF_\S+\.fits detected meteors: (\d+)")


def read_recent_sky_telemetry(data_dir, utc_now, lookback_min=20, state_dir=None):
    """Read star-extraction & meteor counts from recent RMS logs over `lookback_min` minutes,
    plus optional `wx_wrf.json` cloud cover."""
    logs_dir = os.path.join(data_dir, "logs")
    star_vals = []
    met_5m = 0
    if os.path.isdir(logs_dir):
        log_files = sorted(
            (os.path.join(logs_dir, f) for f in os.listdir(logs_dir)
             if f.startswith("log_") and f.endswith(".log")),
            key=os.path.getmtime,
        )[-2:]
        t_cut = utc_now - datetime.timedelta(minutes=lookback_min)
        t_met_cut = utc_now - datetime.timedelta(minutes=5)
        t_hi = utc_now + datetime.timedelta(minutes=2)
        for lf in log_files:
            try:
                sz = os.path.getsize(lf)
                with open(lf, "rb") as fh:
                    if sz > 768 * 1024:
                        fh.seek(sz - 768 * 1024)
                        fh.readline()
                    chunk = fh.read().decode("utf-8", "replace")
            except OSError:
                continue
            for ln in chunk.splitlines():
                if "Detected stars:" in ln:
                    m = _STAR_RX.match(ln)
                    if m:
                        try:
                            dt = datetime.datetime(*(int(x) for x in m.groups()[:6]))
                            if t_cut <= dt <= t_hi:
                                star_vals.append(int(m.group(7)))
                        except ValueError:
                            pass
                elif "detected meteors:" in ln:
                    m = _MET_RX.match(ln)
                    if m:
                        try:
                            dt = datetime.datetime(*(int(x) for x in m.groups()[:6]))
                            if t_met_cut <= dt <= t_hi:
                                met_5m += int(m.group(7))
                        except ValueError:
                            pass

    wx_cloud_pct = None
    if state_dir:
        wx_p = os.path.join(state_dir, "wx_wrf.json")
        if os.path.isfile(wx_p):
            try:
                wx = json.load(open(wx_p))
                best_dt, best_cov = 9e9, None
                for fr in (wx.get("frames") or {}).values():
                    vdt = datetime.datetime.fromisoformat(fr["valid_utc"].replace("Z", "+00:00")).replace(tzinfo=None)
                    diff = abs((vdt - utc_now).total_seconds())
                    if diff < best_dt and diff <= 5400:
                        best_dt = diff
                        best_cov = max(int(fr.get("low", 0)), int(fr.get("mid", 0)), int(fr.get("high", 0)))
                wx_cloud_pct = best_cov
            except Exception:
                pass

    return {
        "star_samples_20m": len(star_vals),
        "median_stars_20m": round(float(statistics.median(star_vals)), 1) if star_vals else None,
        "max_stars_last3": max(star_vals[-3:]) if star_vals else None,
        "recent_meteors_5m": met_5m,
        "wx_cloud_pct": wx_cloud_pct,
    }


def evaluate_safety_gate(data_dir, lat, lon, utc_now=None, now_ts=None,
                         proc_lines=None, supervisor_info=None, state_dir=None):
    """Evaluate whether updating RMS & restarting StartCapture is safe right now."""
    utc_now = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None) if utc_now is None else utc_now
    now_ts = time.time() if now_ts is None else now_ts
    proc_lines = list_running_processes() if proc_lines is None else proc_lines

    # 1. Block on post-night reprocessing / calibration
    for line in proc_lines:
        for pat in BLOCKING_PROCS:
            if pat in line and "rms_prerelease_sync" not in line:
                return {
                    "allow": False,
                    "window": "deferred",
                    "sky_state": "postprocessing",
                    "solar_elev_deg": round(solar_elevation_deg(utc_now, lat, lon), 2) if (lat is not None and lon is not None) else None,
                    "reason": f"deferred: post-processing or calibration active ({pat})",
                }

    ff_age_s, ff_cnt_20m, cur_night = read_recent_ff_activity(data_dir, now_ts=now_ts)
    sun_el = round(solar_elevation_deg(utc_now, lat, lon), 2) if (lat is not None and lon is not None) else None
    sky = read_recent_sky_telemetry(data_dir, utc_now, lookback_min=20, state_dir=state_dir)

    # 2. Daytime idle gate: solar elevation > +5° and no FF written in last 45 min
    if sun_el is not None and sun_el > DAYTIME_MIN_SUN_DEG:
        if ff_age_s is None or ff_age_s > DAYTIME_QUIET_FF_S:
            return {
                "allow": True,
                "window": "safe_daytime",
                "sky_state": "daytime",
                "solar_elev_deg": sun_el,
                "newest_ff_age_s": round(ff_age_s, 1) if ff_age_s is not None else None,
                "recent_ff_count_20m": ff_cnt_20m,
                **sky,
                "reason": f"safe_daytime: solar elevation {sun_el:+.1f}° (> +{DAYTIME_MIN_SUN_DEG:.1f}°), capture idle",
            }
        return {
            "allow": False,
            "window": "deferred",
            "sky_state": "daytime_active_ff",
            "solar_elev_deg": sun_el,
            "newest_ff_age_s": round(ff_age_s, 1),
            "recent_ff_count_20m": ff_cnt_20m,
            **sky,
            "reason": f"deferred: FF written {ff_age_s/60.0:.1f} min ago (< 45 min quiet threshold)",
        }

    # 3. Night active capture: allow ONLY on confirmed cloudy/overcast sky with clean systemd supervisor
    if ff_age_s is not None and ff_age_s <= 900:
        sup = supervisor_info or {"supervised": False}
        if not sup.get("supervised"):
            return {
                "allow": False,
                "window": "deferred",
                "sky_state": "night_unsupervised",
                "solar_elev_deg": sun_el,
                "newest_ff_age_s": round(ff_age_s, 1),
                "recent_ff_count_20m": ff_cnt_20m,
                **sky,
                "reason": "deferred: night capture active without auto-restarting systemd supervisor",
            }
        n_samp = sky["star_samples_20m"]
        med_st = sky["median_stars_20m"]
        max_3 = sky["max_stars_last3"]
        met_5m = sky["recent_meteors_5m"]
        if ff_cnt_20m < CLOUDY_MIN_SAMPLES or n_samp < CLOUDY_MIN_SAMPLES or med_st is None:
            return {
                "allow": False,
                "window": "deferred",
                "sky_state": "startup",
                "solar_elev_deg": sun_el,
                "newest_ff_age_s": round(ff_age_s, 1),
                "recent_ff_count_20m": ff_cnt_20m,
                **sky,
                "reason": f"deferred: capture warming up ({ff_cnt_20m} FFs / {n_samp} star readings in last 20m)",
            }
        if med_st <= CLOUDY_MAX_STARS and max_3 <= CLOUDY_MAX_STARS and met_5m == 0:
            wx_note = f", wx cloud {sky['wx_cloud_pct']}%" if sky.get("wx_cloud_pct") is not None else ""
            return {
                "allow": True,
                "window": "safe_cloudy_night",
                "sky_state": "cloudy_night",
                "solar_elev_deg": sun_el,
                "newest_ff_age_s": round(ff_age_s, 1),
                "recent_ff_count_20m": ff_cnt_20m,
                **sky,
                "reason": (f"safe_cloudy_night: overcast ({n_samp} blocks/20m, "
                           f"median stars {med_st:.0f} <= {CLOUDY_MAX_STARS}, 0 recent meteors{wx_note})"),
            }
        return {
            "allow": False,
            "window": "deferred",
            "sky_state": "clear_night",
            "solar_elev_deg": sun_el,
            "newest_ff_age_s": round(ff_age_s, 1),
            "recent_ff_count_20m": ff_cnt_20m,
            **sky,
            "reason": (f"deferred: clear/partly-clear night sky "
                       f"(median stars {med_st:.0f}, recent max {max_3}, meteors/5m {met_5m})"),
        }

    return {
        "allow": False,
        "window": "deferred",
        "sky_state": "twilight",
        "solar_elev_deg": sun_el,
        "newest_ff_age_s": round(ff_age_s, 1) if ff_age_s is not None else None,
        "recent_ff_count_20m": ff_cnt_20m,
        **sky,
        "reason": f"deferred: twilight/night transition (solar elevation {sun_el if sun_el is not None else '?'}°)",
    }


def _git(rms_src, *args, check=True, timeout=60):
    return subprocess.run(
        ["git", *args], cwd=rms_src, capture_output=True, text=True, check=check, timeout=timeout
    )


def resolve_upstream_remote(rms_src):
    """Pick the git remote tracking CroatianMeteorNetwork/RMS (preferring `upstream`, then `origin`),
    and ensure its fetch refspec covers all branches (`+refs/heads/*:refs/remotes/<rem>/*`)."""
    r = _git(rms_src, "remote", "-v", check=False)
    remotes = {}
    for ln in r.stdout.splitlines():
        parts = ln.split()
        if len(parts) >= 2 and "(fetch)" in ln:
            remotes[parts[0]] = parts[1]
    chosen = None
    if "upstream" in remotes and UPSTREAM_REPO_SUBSTR in remotes["upstream"]:
        chosen = "upstream"
    elif "origin" in remotes and UPSTREAM_REPO_SUBSTR in remotes["origin"]:
        chosen = "origin"
    elif "upstream" in remotes:
        chosen = "upstream"
    elif "origin" in remotes:
        chosen = "origin"
    elif remotes:
        chosen = sorted(remotes.keys())[0]
    if chosen:
        fspec = _git(rms_src, "config", "--get-all", f"remote.{chosen}.fetch", check=False).stdout
        wildcard = f"+refs/heads/*:refs/remotes/{chosen}/*"
        if wildcard not in fspec.splitlines():
            _git(rms_src, "config", f"remote.{chosen}.fetch", wildcard, check=False)
    return chosen


def inspect_git_state(rms_src, branch=DEFAULT_BRANCH, do_fetch=False, fetch_if_stale_s=None):
    """Inspect current branch, HEAD, upstream SHA, behind/ahead counts, and dirty files."""
    if not os.path.isdir(os.path.join(rms_src, ".git")):
        return {"error": f"not a git repository: {rms_src}"}
    remote = resolve_upstream_remote(rms_src)
    if not remote:
        return {"error": "no git remote configured"}

    fetch_head = os.path.join(rms_src, ".git", "refs", "remotes", remote, branch)
    need_fetch = do_fetch
    if not need_fetch and fetch_if_stale_s is not None:
        if not os.path.isfile(fetch_head) or (time.time() - os.path.getmtime(fetch_head) > fetch_if_stale_s):
            need_fetch = True
    fetch_err = None
    if need_fetch:
        fr = _git(rms_src, "fetch", "--quiet", remote, branch, check=False, timeout=45)
        if fr.returncode != 0:
            fetch_err = fr.stderr.strip()[-200:]

    cur_branch = _git(rms_src, "rev-parse", "--abbrev-ref", "HEAD", check=False).stdout.strip()
    head_sha = _git(rms_src, "rev-parse", "HEAD", check=False).stdout.strip()
    head_ts = _git(rms_src, "show", "-s", "--format=%ci", "HEAD", check=False).stdout.strip()
    ref_name = f"{remote}/{branch}"
    up_r = _git(rms_src, "rev-parse", ref_name, check=False)
    upstream_sha = up_r.stdout.strip() if up_r.returncode == 0 else None

    behind, ahead = None, None
    if upstream_sha:
        cnt_r = _git(rms_src, "rev-list", "--left-right", "--count", f"HEAD...{ref_name}", check=False)
        if cnt_r.returncode == 0:
            parts = cnt_r.stdout.split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])

    dirty_r = _git(rms_src, "diff", "--name-only", "HEAD", check=False)
    dirty_all = [x.strip() for x in dirty_r.stdout.splitlines() if x.strip()]
    station_dirty = [f for f in dirty_all if f in STATION_OWNED_FILES]
    code_patches = [f for f in dirty_all if f not in STATION_OWNED_FILES]

    return {
        "rms_src": rms_src,
        "branch": cur_branch,
        "target_branch": branch,
        "remote": remote,
        "head_sha": head_sha,
        "head_short": head_sha[:8],
        "head_commit_utc": head_ts,
        "upstream_sha": upstream_sha,
        "upstream_short": upstream_sha[:8] if upstream_sha else None,
        "behind_count": behind,
        "ahead_count": ahead,
        "station_modified": station_dirty,
        "code_patches": code_patches,
        "fetch_error": fetch_err,
    }


def _default_build(rms_src, venv_py):
    """Unlink old Cython .so files first (prevents SIGBUS in running processes) and rebuild in-place."""
    removed = []
    for root, _dirs, files in os.walk(os.path.join(rms_src, "RMS")):
        for fn in files:
            if fn.endswith(".so"):
                p = os.path.join(root, fn)
                try:
                    os.unlink(p)
                    removed.append(p)
                except OSError:
                    pass
    r = subprocess.run(
        ["nice", "-n", "15", venv_py, "setup.py", "build_ext", "--inplace"],
        cwd=rms_src, capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        return False, (r.stderr or r.stdout)[-500:]
    return True, f"rebuilt Cython extensions ({len(removed)} old .so unlinked)"


def _default_smoke(rms_src, venv_py):
    env = dict(os.environ, PYTHONPATH=rms_src)
    r = subprocess.run(
        [venv_py, "-c", "import RMS.StartCapture, RMS.Astrometry.CyFunctions, RMS.Routines.Grouping3Dcy, Utils.Flux"],
        cwd=rms_src, env=env, capture_output=True, text=True, timeout=45,
    )
    if r.returncode != 0:
        return False, (r.stderr or r.stdout)[-400:]
    return True, "smoke imports passed"


def _default_restart(capture_service):
    sup = check_supervisor(capture_service)
    if not sup.get("active"):
        return True, "capture unit not active (idle); new code ready for next start"
    if not sup.get("supervised"):
        return True, "capture not under Restart=always systemd unit; left running untouched"
    old_pid = sup["main_pid"]
    if os.geteuid() == 0:
        r = subprocess.run(["systemctl", "restart", sup["unit"]], capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False, f"systemctl restart failed: {r.stderr.strip()}"
    else:
        try:
            os.kill(old_pid, signal.SIGINT)
        except OSError as e:
            return False, f"SIGINT to MainPID {old_pid} failed: {e}"
    # Wait up to 30 s for systemd Restart=always to launch a fresh MainPID
    deadline = time.time() + 30.0
    while time.time() < deadline:
        time.sleep(1.5)
        cur = check_supervisor(capture_service)
        if cur.get("active") and cur.get("main_pid", 0) > 1 and cur["main_pid"] != old_pid:
            return True, f"restarted {sup['unit']} ({old_pid} -> {cur['main_pid']})"
    return True, f"signaled {sup['unit']} (PID {old_pid}) for systemd restart"


def _save_status(state_dir, payload):
    os.makedirs(state_dir, exist_ok=True)
    sp = os.path.join(state_dir, ".rms_prerelease_status.json")
    prev = {}
    if os.path.isfile(sp):
        try:
            prev = json.load(open(sp))
        except Exception:
            prev = {}
    merged = dict(prev)
    merged.update(payload)
    tmp = sp + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(merged, fh, indent=1)
    os.replace(tmp, sp)
    return merged


def _restore_station_files(rms_src, backup_dir, saved_files):
    for rel in saved_files:
        src_p = os.path.join(backup_dir, "station_files", rel)
        dst_p = os.path.join(rms_src, rel)
        if os.path.isfile(src_p):
            os.makedirs(os.path.dirname(dst_p), exist_ok=True)
            shutil.copy2(src_p, dst_p)


def perform_update(rms_src, state_dir, branch=DEFAULT_BRANCH, venv_py=None,
                   capture_service="rms-capture", build_fn=None, smoke_fn=None, restart_fn=None):
    """Execute a safe fast-forward update of `rms_src` to `<remote>/<branch>`, preserving
    station-owned files and 3-way re-applying tracked code patches with full rollback on error."""
    now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    g = inspect_git_state(rms_src, branch=branch, do_fetch=True)
    if g.get("error"):
        return _save_status(state_dir, {"checked_utc": now_iso, "last_result": "error", "last_detail": g["error"]})
    if g["branch"] != branch:
        msg = f"working tree is on branch '{g['branch']}', expected '{branch}'"
        return _save_status(state_dir, {**g, "checked_utc": now_iso, "last_result": "skipped_branch", "last_detail": msg})
    if not g["upstream_sha"]:
        msg = f"could not resolve {g['remote']}/{branch}"
        return _save_status(state_dir, {**g, "checked_utc": now_iso, "last_result": "fetch_failed", "last_detail": msg})
    if g["behind_count"] == 0:
        return _save_status(state_dir, {**g, "checked_utc": now_iso, "last_result": "up_to_date",
                                        "last_detail": f"already at {g['head_short']}"})
    if (g["ahead_count"] or 0) > 0:
        msg = f"local branch is {g['ahead_count']} commit(s) ahead of {g['remote']}/{branch}; refusing non-FF merge"
        return _save_status(state_dir, {**g, "checked_utc": now_iso, "last_result": "diverged", "last_detail": msg})

    pre_sha = g["head_sha"]
    target_sha = g["upstream_sha"]
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(state_dir, "rms_update_backups", f"{stamp}_{pre_sha[:8]}")
    os.makedirs(os.path.join(backup_dir, "station_files"), exist_ok=True)

    # 1. Backup station-owned files + platepar + full & code-only diffs
    saved_station_files = []
    for rel in (*STATION_OWNED_FILES, *EXTRA_BACKUP_FILES):
        p = os.path.join(rms_src, rel)
        if os.path.isfile(p):
            dst = os.path.join(backup_dir, "station_files", rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(p, dst)
            if rel in STATION_OWNED_FILES:
                saved_station_files.append(rel)

    full_diff = _git(rms_src, "diff", "--binary", "HEAD").stdout
    open(os.path.join(backup_dir, "full_working_tree.diff"), "wb").write(full_diff.encode("utf-8", "surrogateescape"))

    code_patches = g["code_patches"]
    patch_path = os.path.join(backup_dir, "code_patches.diff")
    if code_patches:
        cp_r = subprocess.run(["git", "diff", "--binary", "HEAD", "--", *code_patches],
                              cwd=rms_src, capture_output=True, check=True)
        open(patch_path, "wb").write(cp_r.stdout)

    def _rollback(reason_code, detail):
        _git(rms_src, "reset", "--hard", pre_sha, check=False)
        if code_patches and os.path.isfile(patch_path):
            subprocess.run(["git", "apply", "--whitespace=nowarn", patch_path], cwd=rms_src, capture_output=True)
        _restore_station_files(rms_src, backup_dir, (*saved_station_files, *EXTRA_BACKUP_FILES))
        if reason_code == "build_failed" and build_fn is None and venv_py:
            _default_build(rms_src, venv_py)
        post_g = inspect_git_state(rms_src, branch=branch, do_fetch=False)
        return _save_status(state_dir, {
            **post_g,
            "checked_utc": now_iso,
            "last_attempt_utc": now_iso,
            "last_result": reason_code,
            "last_detail": detail,
            "failed_target_sha": target_sha,
            "backup_dir": backup_dir,
        })

    # 2. Reset index and tracked modifications cleanly to HEAD before fast-forward merge
    _git(rms_src, "reset", "--hard", "HEAD")

    ff_r = _git(rms_src, "merge", "--ff-only", target_sha, check=False)
    if ff_r.returncode != 0:
        return _rollback("ff_failed", f"git merge --ff-only failed: {ff_r.stderr.strip()[-300:]}")

    # 3. Re-apply tracked local code patches via 3-way apply, failing safe on any conflict
    if code_patches and os.path.isfile(patch_path) and os.path.getsize(patch_path) > 0:
        ap_r = subprocess.run(
            ["git", "apply", "--3way", "--whitespace=nowarn", patch_path],
            cwd=rms_src, capture_output=True, text=True,
        )
        unmerged = _git(rms_src, "ls-files", "-u", check=False).stdout.strip()
        if ap_r.returncode != 0 or unmerged:
            err = (ap_r.stderr or ap_r.stdout or "unmerged index entries").strip()[-300:]
            return _rollback("conflict", f"local patch conflict on {', '.join(code_patches)}: {err}")
        # Keep local patches unstaged in the working tree (git apply --3way stages clean merges)
        _git(rms_src, "reset", "--quiet", "HEAD", check=False)

    # 4. Restore station-owned config/mask files byte-for-byte
    _restore_station_files(rms_src, backup_dir, saved_station_files)

    # 5. Rebuild Cython extensions & run smoke test
    req_changed = bool(_git(rms_src, "diff", "--name-only", pre_sha, target_sha, "--", "requirements.txt", check=False).stdout.strip())
    do_build = build_fn or (lambda src: _default_build(src, venv_py))
    ok_b, msg_b = do_build(rms_src)
    if not ok_b:
        return _rollback("build_failed", f"Cython build failed: {msg_b}")

    do_smoke = smoke_fn or (lambda src: _default_smoke(src, venv_py))
    ok_s, msg_s = do_smoke(rms_src)
    if not ok_s:
        return _rollback("build_failed", f"smoke check failed: {msg_s}")

    # 6. Restart capture service cleanly if supervised
    do_restart = restart_fn or (lambda: _default_restart(capture_service))
    ok_r, msg_r = do_restart()

    post_g = inspect_git_state(rms_src, branch=branch, do_fetch=False)
    detail = f"updated {pre_sha[:8]} -> {target_sha[:8]} ({msg_b}; {msg_r})"
    if req_changed:
        detail += " [note: requirements.txt changed upstream]"
    return _save_status(state_dir, {
        **post_g,
        "checked_utc": now_iso,
        "last_update_utc": now_iso,
        "prev_sha": pre_sha,
        "last_result": "updated" if ok_r else "updated_restart_warn",
        "last_detail": detail,
        "requirements_changed": req_changed,
        "backup_dir": backup_dir,
    })


def acquire_lock(state_dir):
    os.makedirs(state_dir, exist_ok=True)
    lp = os.path.join(state_dir, ".rms_prerelease_sync.lock")
    try:
        fd = os.open(lp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, f"{os.getpid()} {datetime.datetime.now(datetime.timezone.utc).isoformat()}\n".encode())
        os.close(fd)
        return lp
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
        try:
            if time.time() - os.path.getmtime(lp) > LOCK_STALE_S:
                os.unlink(lp)
                return acquire_lock(state_dir)
        except OSError:
            pass
        return None


def main():
    ap = argparse.ArgumentParser(description="Daytime & cloudy-night safe RMS prerelease tracker and updater.")
    grp = ap.add_mutually_exclusive_group()
    grp.add_argument("--status", "--dry-run", "--check-only", dest="mode", action="store_const",
                     const="status", default="status", help="Inspect git + sky safety gate without modifying RMS (default)")
    grp.add_argument("--apply", dest="mode", action="store_const", const="apply",
                     help="Fetch and fast-forward RMS prerelease if behind > 0 and safety gate allows")
    ap.add_argument("--no-fetch", action="store_true", help="Skip network git fetch in --status mode")
    ap.add_argument("--fetch-if-stale", type=int, default=None, metavar="SEC",
                    help="In --status mode, fetch only if remote ref is older than SEC seconds")
    ap.add_argument("--branch", default=DEFAULT_BRANCH, help="Target upstream branch (default: prerelease)")
    ap.add_argument("--json", action="store_true", help="Emit JSON output")
    args = ap.parse_args()

    cfg = resolve_paths()
    sup = check_supervisor(cfg["capture_service"])
    gate = evaluate_safety_gate(
        data_dir=cfg["data_dir"], lat=cfg["lat"], lon=cfg["lon"],
        supervisor_info=sup, state_dir=cfg["state_dir"],
    )

    if args.mode == "status":
        do_fetch = (not args.no_fetch) and (args.fetch_if_stale is None)
        g = inspect_git_state(
            cfg["rms_src"], branch=args.branch,
            do_fetch=do_fetch, fetch_if_stale_s=(None if args.no_fetch else args.fetch_if_stale),
        )
        now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        saved = _save_status(cfg["state_dir"], {
            **g,
            "station": cfg["station"],
            "checked_utc": now_iso,
            "supervisor": sup,
            "gate": gate,
        })
        if args.json:
            print(json.dumps(saved, indent=2))
        else:
            print(f"[{cfg['station']}] {g.get('branch')} @ {g.get('head_short')} "
                  f"(upstream {g.get('remote')}/{args.branch}: {g.get('upstream_short')}, "
                  f"behind={g.get('behind_count')}, ahead={g.get('ahead_count')})")
            print(f"  station files : {', '.join(g.get('station_modified') or ['(clean)'])}")
            print(f"  code patches  : {', '.join(g.get('code_patches') or ['(none)'])}")
            print(f"  supervisor    : {sup.get('unit')} active={sup.get('active')} pid={sup.get('main_pid')} restart={sup.get('restart_policy')}")
            print(f"  safety gate   : allow={gate['allow']} ({gate['reason']})")
            if saved.get("last_result"):
                print(f"  last action   : {saved['last_result']} — {saved.get('last_detail', '')}")
        return 0

    # --apply mode
    lp = acquire_lock(cfg["state_dir"])
    if not lp:
        print("rms_prerelease_sync: another sync holds the lock; exiting.")
        return 0
    try:
        g = inspect_git_state(cfg["rms_src"], branch=args.branch, do_fetch=True)
        now_iso = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        if g.get("error"):
            st = _save_status(cfg["state_dir"], {"checked_utc": now_iso, "gate": gate, "last_result": "error", "last_detail": g["error"]})
            print(f"Error: {g['error']}")
            return 1
        if g.get("behind_count") == 0:
            st = _save_status(cfg["state_dir"], {
                **g, "station": cfg["station"], "checked_utc": now_iso,
                "supervisor": sup, "gate": gate,
                "last_result": "up_to_date", "last_detail": f"up to date at {g['head_short']}",
            })
            if args.json:
                print(json.dumps(st, indent=2))
            else:
                print(f"[{cfg['station']}] already up to date at {g['head_short']} ({gate['reason']}).")
            return 0
        if not gate["allow"]:
            st = _save_status(cfg["state_dir"], {
                **g, "station": cfg["station"], "checked_utc": now_iso,
                "supervisor": sup, "gate": gate,
                "last_result": "deferred", "last_detail": gate["reason"],
            })
            if args.json:
                print(json.dumps(st, indent=2))
            else:
                print(f"[{cfg['station']}] {g['behind_count']} commit(s) behind {g['remote']}/{args.branch} — {gate['reason']}")
            return 0

        res = perform_update(
            rms_src=cfg["rms_src"], state_dir=cfg["state_dir"], branch=args.branch,
            venv_py=cfg["venv_py"], capture_service=cfg["capture_service"],
        )
        res = _save_status(cfg["state_dir"], {"station": cfg["station"], "supervisor": check_supervisor(cfg["capture_service"]), "gate": gate})
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(f"[{cfg['station']}] {res.get('last_result')}: {res.get('last_detail')}")
        return 0 if res.get("last_result") in ("updated", "up_to_date", "deferred") else 2
    finally:
        try:
            os.unlink(lp)
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
