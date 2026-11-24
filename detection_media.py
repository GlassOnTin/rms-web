#!/usr/bin/env python3
"""
detection_media.py — per-detection, FR-cutout, and FF-block media & metadata
for the rms-web night study pages.

Run under the RMS venv (~/vRMS/bin/python3) by rms_web.py; the web server itself
stays stdlib-only. Reads the night's canonical FTPdetectinfo, unfiltered KHT
candidates (_unfiltered.txt), radiant associations (_radiants.txt), FR*.bin
raw fireball/bright-event cutouts, and archived/captured FF files, writing into
a per-night cache directory:

  index      <night_dir> <cache_dir>
  full       <night_dir> <cache_dir> <group> <idx>
  ann        <night_dir> <cache_dir> <group> <idx>
  resid      <night_dir> <cache_dir> <group> <idx>
  video      <night_dir> <cache_dir> <group> <idx>
  cropvideo  <night_dir> <cache_dir> <group> <idx>
  frfull     <night_dir> <cache_dir> <fr_idx>
  frvid      <night_dir> <cache_dir> <fr_idx>
  frcropvid  <night_dir> <cache_dir> <fr_idx>
  blockthumb <dir>       <cache_dir> <ff_name>
  blockfull  <dir>       <cache_dir> <ff_name>
  blockann   <dir>       <cache_dir> <ff_name>
  blockresid <dir>       <cache_dir> <ff_name>
  blockvideo <dir>       <cache_dir> <ff_name>

Backwards-compatible 4-arg calls (`full`/`video` `<night_dir> <cache_dir> <i_int>`)
default `<group>` to `"detections"`.
"""
import glob
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timedelta

# Editable-install RMS resolves from anywhere, but be explicit to dodge the
# ~/source namespace-dir shadowing gotcha (see rms_ml_crops.py). RMS_SOURCE
# overrides it for non-default clones (e.g. the fireball 2nd instance).
sys.path.insert(0, os.environ.get("RMS_SOURCE", os.path.expanduser("~/source/RMS")))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from RMS.Astrometry import ApplyAstrometry
from RMS.Astrometry.Conversions import datetime2JD, raDec2AltAz
from RMS.Formats import FRbin, Platepar
from RMS.Formats.FFfile import filenameToDatetime, read as readFF
from RMS.Formats.FTPdetectinfo import readFTPdetectinfo

INDEX_VERSION = 2
PAD_FRAMES = 15       # context frames either side of the detection in the clip
VIDEO_FPS = 12.5      # half real speed so short meteors are visible
THUMB_H = 160         # thumbnail height, px
THUMB_MARGIN = 60     # crop margin around the detection's pixel track
CROP_MARGIN = 96      # margin around track for zoomed video study


def _getfont(size=13):
    try:
        return ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", size)
    except Exception:
        return ImageFont.load_default()


def capturedCounterpart(night_dir):
    """Sibling CapturedFiles/<night> directory for an ArchivedFiles/<night> path."""
    norm = os.path.normpath(night_dir)
    parent = os.path.dirname(norm)
    if os.path.basename(parent) == "ArchivedFiles":
        cand = os.path.join(os.path.dirname(parent), "CapturedFiles", os.path.basename(norm))
        if os.path.isdir(cand):
            return cand
    return None


def resolveFileDir(night_dir, filename):
    """Return directory containing `filename` (checking night_dir, then CapturedFiles/<night>)."""
    if not filename:
        return None
    if os.path.isfile(os.path.join(night_dir, filename)):
        return night_dir
    cap = capturedCounterpart(night_dir)
    if cap and os.path.isfile(os.path.join(cap, filename)):
        return cap
    return None


def findFTPdetectinfo(night_dir, unfiltered=False):
    """Canonical FTPdetectinfo or _unfiltered.txt for night_dir."""
    base = os.path.basename(os.path.normpath(night_dir))
    suffix = "_unfiltered.txt" if unfiltered else ".txt"
    path = os.path.join(night_dir, "FTPdetectinfo_{}{}".format(base, suffix))
    return path if os.path.isfile(path) else None


def stretchLUT(ff):
    """One linear stretch per FF, shared by thumb/full/video frames so they agree.
    Anchored on the avepixel floor (raw-pipeline nights sit at ~1-3 ADU) with a
    guaranteed span so a dark, clean frame doesn't amplify to pure noise."""
    lo = float(np.percentile(ff.avepixel, 1.0))
    hi = max(float(np.percentile(ff.maxpixel, 99.8)), lo + 40.0)
    lut = np.clip((np.arange(256, dtype=np.float32) - lo) * 255.0 / (hi - lo), 0, 255)
    return lut.astype(np.uint8)


def residualLUT(resid):
    lo = float(np.percentile(resid, 50.0))
    hi = max(float(np.percentile(resid, 99.9)), lo + 20.0)
    return np.clip((np.arange(256, dtype=np.float32) - lo) * 255.0 / (hi - lo), 0, 255).astype(np.uint8)


def blockResidual(ff):
    """maxpixel-avepixel: the transient (meteor) signal, star-drift removed."""
    return np.clip(ff.maxpixel.astype(np.int16) - ff.avepixel.astype(np.int16), 0, 255).astype(np.uint8)


def loadPlateparLookup(night_dir):
    """Return (default_pp, recal_dict) from night_dir if available."""
    recal = {}
    for jname in ("platepars_all_recalibrated.json", "platepars_flux_recalibrated.json"):
        jp = os.path.join(night_dir, jname)
        if os.path.isfile(jp):
            try:
                d = json.load(open(jp))
                if isinstance(d, dict) and d:
                    recal.update(d)
            except Exception:
                pass
    default_pp = None
    pp_path = os.path.join(night_dir, "platepar_cmn2010.cal")
    if not os.path.isfile(pp_path):
        pp_path = os.environ.get("RMS_PLATEPAR", "")
    if pp_path and os.path.isfile(pp_path):
        try:
            pp = Platepar.Platepar()
            pp.read(pp_path, use_flat=None)
            default_pp = pp
        except Exception:
            default_pp = None
    return default_pp, recal


def plateparForFF(ff_name, default_pp, recal):
    if ff_name and ff_name in recal:
        try:
            pp = Platepar.Platepar()
            pp.loadFromDict(recal[ff_name], use_flat=None)
            return pp, "recalibrated"
        except Exception:
            pass
    if default_pp is not None:
        return default_pp, "night_platepar"
    return None, "none"


def parseRadiants(night_dir):
    """Parse *_radiants.txt in night_dir into a list of dicts for matching detections."""
    base = os.path.basename(os.path.normpath(night_dir))
    p = os.path.join(night_dir, "{}_radiants.txt".format(base))
    if not os.path.isfile(p):
        cands = glob.glob(os.path.join(night_dir, "*_radiants.txt"))
        p = cands[0] if cands else None
    if not p or not os.path.isfile(p):
        return []
    out = []
    try:
        with open(p, "r", errors="replace") as fh:
            for line in fh:
                s = line.strip()
                if not s or s.startswith("#"):
                    continue
                parts = [c.strip() for c in s.split(",")]
                if len(parts) < 15:
                    continue
                try:
                    dt = datetime.strptime(parts[0], "%Y%m%d %H:%M:%S.%f")
                except ValueError:
                    continue
                code = parts[3]
                shower = "spo" if code in ("...", "", "None") else code
                def _f(idx):
                    if idx >= len(parts) or parts[idx] in ("None", "", "..."):
                        return None
                    try:
                        return round(float(parts[idx]), 2)
                    except ValueError:
                        return None
                out.append({
                    "dt": dt,
                    "shower": shower,
                    "ra_beg": _f(4),
                    "dec_beg": _f(5),
                    "ra_end": _f(6),
                    "dec_end": _f(7),
                    "ra_rad": _f(8),
                    "dec_rad": _f(9),
                    "app_mag": _f(14),
                    "abs_mag": _f(15),
                    "rad_elev": _f(16),
                })
    except OSError:
        pass
    return out


def matchRadiant(t_start, ra0, radiants):
    best, best_score = None, 999.0
    for r in radiants:
        dt_s = abs((r["dt"] - t_start).total_seconds())
        if dt_s > 2.0:
            continue
        dra = 0.0
        if ra0 is not None and r["ra_beg"] is not None:
            dra = min(abs(ra0 - r["ra_beg"]), 360.0 - abs(ra0 - r["ra_beg"])) * 0.05
        score = dt_s + dra
        if score < best_score:
            best_score = score
            best = r
    return best


def angularSepDeg(ra0, dec0, ra1, dec1):
    r0, d0 = math.radians(ra0), math.radians(dec0)
    r1, d1 = math.radians(ra1), math.radians(dec1)
    c = math.sin(d0) * math.sin(d1) + math.cos(d0) * math.cos(d1) * math.cos(r1 - r0)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


def compassOctant(az):
    dirs = ["N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
            "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW"]
    return dirs[int(((az % 360.0) + 11.25) // 22.5) % 16]


def computeTrackKinematics(frames, xs, ys):
    """Return (cross_residuals_px_list, step_speeds_px_fr_list, lin_rms_px)."""
    n = len(xs)
    if n == 0:
        return [], [], 0.0
    step_spd = [0.0]
    for k in range(1, n):
        df = frames[k] - frames[k - 1]
        dist = math.hypot(xs[k] - xs[k - 1], ys[k] - ys[k - 1])
        step_spd.append(round(dist / df, 2) if df > 1e-4 else 0.0)
    if n >= 2:
        step_spd[0] = step_spd[1]
    if n < 3:
        return [0.0] * n, step_spd, 0.0
    pts = np.column_stack([np.asarray(xs, dtype=np.float64), np.asarray(ys, dtype=np.float64)])
    centered = pts - pts.mean(axis=0)
    _, _, vt = np.linalg.svd(centered, full_matrices=False)
    # Ensure direction vector points from start toward end
    direction = vt[0]
    if np.dot(pts[-1] - pts[0], direction) < 0:
        direction = -direction
    normal = np.array([-direction[1], direction[0]])
    cross = centered @ normal
    rms = float(np.sqrt(np.mean(cross ** 2)))
    return [round(float(c), 3) for c in cross], step_spd, round(rms, 3)


def buildDetectionDict(entry, idx_num, prefix, night_dir, default_pp, recal, radiants, fr_by_stem):
    ff_name, cam, meteor_no, n_segments, fps, hnr, mle, binn, pxfm, rho, phi, meas = entry
    fps = float(fps or 25.0)
    frames = [float(m[1]) for m in meas]
    xs = [float(m[2]) for m in meas]
    ys = [float(m[3]) for m in meas]
    ras = [float(m[4]) for m in meas]
    decs = [float(m[5]) for m in meas]
    azs = [float(m[6]) for m in meas]
    els = [float(m[7]) for m in meas]
    intens = [float(m[8]) if len(m) > 8 else 0.0 for m in meas]
    mags = [float(m[9]) if len(m) > 9 else 0.0 for m in meas]
    bcknds = [float(m[10]) if len(m) > 10 else 0.0 for m in meas]
    snrs = [float(m[11]) if len(m) > 11 else 0.0 for m in meas]
    nsat = [int(round(float(m[12]))) if len(m) > 12 else 0 for m in meas]

    fmin, fmax = min(frames), max(frames)
    t0_ff = filenameToDatetime(ff_name)
    t_start = t0_ff + timedelta(seconds=fmin / fps)
    t_end = t0_ff + timedelta(seconds=fmax / fps)

    has_cal = any(r != 0.0 or d != 0.0 for r, d in zip(ras, decs))
    has_mag = any(mg != 0.0 for mg in mags) and has_cal
    cal_source = "ftpdetectinfo" if has_cal else "none"

    if not has_cal and frames:
        pp, pp_src = plateparForFF(ff_name, default_pp, recal)
        if pp is not None:
            try:
                times = [t0_ff + timedelta(seconds=fr_n / fps) for fr_n in frames]
                jds = [datetime2JD(t) for t in times]
                lvls = [max(1.0, iv) for iv in intens]
                _, ra_c, dec_c, mag_c = ApplyAstrometry.xyToRaDecPP(
                    jds, xs, ys, lvls, pp, extinction_correction=True, measurement=True, jd_time=True
                )
                ras = [float(v) for v in ra_c]
                decs = [float(v) for v in dec_c]
                az_l, el_l = [], []
                for jd_v, ra_v, dec_v in zip(jds, ras, decs):
                    az_v, el_v = raDec2AltAz(ra_v, dec_v, jd_v, pp.lat, pp.lon)
                    az_l.append(float(az_v))
                    el_l.append(float(el_v))
                azs, els = az_l, el_l
                if getattr(pp, "mag_lev", None) and any(iv > 0 for iv in intens):
                    mags = [float(v) for v in mag_c]
                    has_mag = True
                has_cal = True
                cal_source = pp_src
            except Exception:
                pass

    cross_px, step_spd, lin_rms = computeTrackKinematics(frames, xs, ys)
    dur_s = max(0.0, (fmax - fmin) / fps)
    px_len = math.hypot(xs[-1] - xs[0], ys[-1] - ys[0]) if len(xs) >= 2 else 0.0
    ang_len = angularSepDeg(ras[0], decs[0], ras[-1], decs[-1]) if (has_cal and len(ras) >= 2) else None
    ang_vel = round(ang_len / dur_s, 2) if (ang_len is not None and dur_s > 0.02) else None

    valid_mags = [mg for mg, iv in zip(mags, intens) if has_mag and iv > 0 and -15.0 < mg < 12.0]
    peak_mag = round(min(valid_mags), 2) if valid_mags else None
    peak_snr = round(max(snrs), 2) if snrs else None
    peak_inten = int(round(max(intens))) if intens else 0
    total_inten = int(round(sum(intens))) if intens else 0
    max_sat = max(nsat) if nsat else 0

    # Peak frame index
    if valid_mags and has_mag:
        peak_idx = int(np.argmin([mg if (iv > 0 and -15.0 < mg < 12.0) else 99.0 for mg, iv in zip(mags, intens)]))
    elif intens:
        peak_idx = int(np.argmax(intens))
    else:
        peak_idx = 0
    peak_frame = round(frames[peak_idx], 2) if frames else fmin

    rad = matchRadiant(t_start, ras[0] if has_cal else None, radiants) if prefix == "det" else None
    if rad and peak_mag is None and rad.get("app_mag") is not None:
        peak_mag = rad["app_mag"]

    segments = []
    for k in range(len(frames)):
        tk = t0_ff + timedelta(seconds=frames[k] / fps)
        segments.append({
            "fr": round(frames[k], 4),
            "ut": tk.strftime("%H:%M:%S.%f")[:-3],
            "x": round(xs[k], 2),
            "y": round(ys[k], 2),
            "ra": round(ras[k], 4) if has_cal else None,
            "dec": round(decs[k], 4) if has_cal else None,
            "az": round(azs[k], 2) if has_cal else None,
            "el": round(els[k], 2) if has_cal else None,
            "inten": int(round(intens[k])),
            "mag": round(mags[k], 2) if (has_mag and intens[k] > 0 and -15.0 < mags[k] < 12.0) else None,
            "bck": int(round(bcknds[k])),
            "snr": round(snrs[k], 2),
            "sat": nsat[k],
            "cross_px": cross_px[k],
            "spd_px": step_spd[k],
        })

    ff_dir = resolveFileDir(night_dir, ff_name)
    stem = ff_name[3:-5] if (ff_name.startswith("FF_") and ff_name.endswith(".fits")) else ""
    fr_match = fr_by_stem.get(stem)

    tag = "{}{:03d}".format(prefix, idx_num)
    return {
        "i": idx_num,
        "group": "detections" if prefix == "det" else "unfiltered",
        "ff": ff_name,
        "ff_source": ("archived" if ff_dir == night_dir else "captured" if ff_dir else None),
        "cam": str(cam),
        "no": int(meteor_no),
        "time": t_start.strftime("%H:%M:%S.%f")[:-4] + " UT",
        "time_end": t_end.strftime("%H:%M:%S.%f")[:-4] + " UT",
        "date": t_start.strftime("%Y-%m-%d"),
        "dur": round(dur_s, 2),
        "nseg": int(n_segments),
        "fmin": round(fmin, 3),
        "fmax": round(fmax, 3),
        "peak_frame": peak_frame,
        "fps": round(fps, 3),
        "pxfm": round(float(pxfm), 2) if pxfm is not None else None,
        "hnr": round(float(hnr), 2) if hnr is not None else None,
        "mle": round(float(mle), 2) if mle is not None else None,
        "rho": round(float(rho), 2) if rho is not None else None,
        "phi": round(float(phi), 2) if phi is not None else None,
        "px_len": round(px_len, 1),
        "lin_rms_px": lin_rms,
        "has_cal": has_cal,
        "has_mag": has_mag,
        "cal_source": cal_source,
        "ra_beg": round(ras[0], 3) if has_cal else None,
        "dec_beg": round(decs[0], 3) if has_cal else None,
        "ra_end": round(ras[-1], 3) if has_cal else None,
        "dec_end": round(decs[-1], 3) if has_cal else None,
        "az_beg": round(azs[0], 1) if has_cal else None,
        "el_beg": round(els[0], 1) if has_cal else None,
        "az_end": round(azs[-1], 1) if has_cal else None,
        "el_end": round(els[-1], 1) if has_cal else None,
        "compass_beg": compassOctant(azs[0]) if has_cal else None,
        "compass_end": compassOctant(azs[-1]) if has_cal else None,
        "ang_len_deg": round(ang_len, 2) if ang_len is not None else None,
        "ang_vel_deg_s": ang_vel,
        "peak_mag": peak_mag,
        "peak_snr": peak_snr,
        "peak_inten": peak_inten,
        "total_inten": total_inten,
        "max_sat_px": max_sat,
        "shower": rad["shower"] if rad else None,
        "abs_mag": rad["abs_mag"] if rad else None,
        "rad_elev": rad["rad_elev"] if rad else None,
        "ra_rad": rad["ra_rad"] if rad else None,
        "dec_rad": rad["dec_rad"] if rad else None,
        "bbox": [round(min(xs), 1), round(min(ys), 1), round(max(xs), 1), round(max(ys), 1)],
        "has_ff": ff_dir is not None,
        "fr_idx": fr_match["i"] if fr_match is not None else None,
        "fr_name": fr_match["fr"] if fr_match is not None else None,
        "thumb": "{}_thumb.jpg".format(tag),
        "video": "{}.mp4".format(tag),
        "cropvideo": "{}_crop.mp4".format(tag),
        "full": "{}_full.jpg".format(tag),
        "ann": "{}_ann.jpg".format(tag),
        "resid": "{}_resid.jpg".format(tag),
        "segments": segments,
    }


def loadFRClips(night_dir, default_pp, recal, fps_default=25.0):
    """Discover FR_*.bin files in night_dir (and CapturedFiles/<night> if any)."""
    seen = {}
    for d in [night_dir, capturedCounterpart(night_dir)]:
        if not d or not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if fn.startswith("FR_") and fn.endswith(".bin") and fn not in seen:
                seen[fn] = d
    clips = []
    for fn in sorted(seen.keys()):
        d = seen[fn]
        try:
            fr = FRbin.read(d, fn)
        except Exception:
            continue
        if getattr(fr, "lines", 0) <= 0:
            continue
        all_t, all_x, all_y, all_sz = [], [], [], []
        by_frame = {}
        for ln in range(fr.lines):
            for z in range(int(fr.frameNum[ln])):
                t_i = int(fr.t[ln][z])
                xc_i = int(fr.xc[ln][z])
                yc_i = int(fr.yc[ln][z])
                sz_i = int(fr.size[ln][z])
                patch = fr.frames[ln][z]
                all_t.append(t_i)
                all_x.append(xc_i)
                all_y.append(yc_i)
                all_sz.append(sz_i)
                bck = float(np.percentile(patch, 25.0))
                flx = float(np.sum(np.maximum(0.0, patch.astype(np.float32) - bck)))
                pk = int(patch.max())
                sat = int(np.sum(patch >= 254))
                prev = by_frame.get(t_i)
                if prev is None or flx > prev["flux"]:
                    by_frame[t_i] = {
                        "fr": t_i, "xc": xc_i, "yc": yc_i, "size": sz_i,
                        "flux": int(round(flx)), "peak_adu": pk, "bck": int(round(bck)), "sat": sat,
                    }
        if not all_t:
            continue
        fmin, fmax = min(all_t), max(all_t)
        stem = fn[3:-4]
        ff_name = "FF_" + stem + ".fits"
        ff_dir = resolveFileDir(night_dir, ff_name)
        try:
            t0_ff = filenameToDatetime(ff_name)
        except Exception:
            t0_ff = datetime(2000, 1, 1)
        fps = fps_default
        t_start = t0_ff + timedelta(seconds=fmin / fps)
        t_end = t0_ff + timedelta(seconds=fmax / fps)
        dur_s = max(0.0, (fmax - fmin) / fps)

        ordered = [by_frame[k] for k in sorted(by_frame.keys())]
        x0_c, y0_c = ordered[0]["xc"], ordered[0]["yc"]
        x1_c, y1_c = ordered[-1]["xc"], ordered[-1]["yc"]
        max_half = max(all_sz) // 2
        bbox = [min(all_x) - max_half, min(all_y) - max_half,
                max(all_x) + max_half, max(all_y) + max_half]

        az_beg = el_beg = az_end = el_end = ang_len = ang_vel = None
        compass_beg = compass_end = None
        pp, _ = plateparForFF(ff_name, default_pp, recal)
        if pp is not None:
            try:
                jds = [datetime2JD(t_start), datetime2JD(t_end)]
                _, ra_c, dec_c, _ = ApplyAstrometry.xyToRaDecPP(
                    jds, [float(x0_c), float(x1_c)], [float(y0_c), float(y1_c)], [100.0, 100.0],
                    pp, extinction_correction=True, measurement=True, jd_time=True
                )
                az0, el0 = raDec2AltAz(float(ra_c[0]), float(dec_c[0]), jds[0], pp.lat, pp.lon)
                az1, el1 = raDec2AltAz(float(ra_c[1]), float(dec_c[1]), jds[1], pp.lat, pp.lon)
                az_beg, el_beg = round(float(az0), 1), round(float(el0), 1)
                az_end, el_end = round(float(az1), 1), round(float(el1), 1)
                compass_beg, compass_end = compassOctant(az_beg), compassOctant(az_end)
                ang_len = round(angularSepDeg(float(ra_c[0]), float(dec_c[0]), float(ra_c[1]), float(dec_c[1])), 2)
                if dur_s > 0.02:
                    ang_vel = round(ang_len / dur_s, 2)
            except Exception:
                pass

        idx_num = len(clips)
        peak_entry = max(ordered, key=lambda r: r["flux"])
        tag = "fr{:03d}".format(idx_num)
        clips.append({
            "i": idx_num,
            "fr": fn,
            "fr_source": "archived" if d == night_dir else "captured",
            "ff": ff_name,
            "has_ff": ff_dir is not None,
            "lines": int(fr.lines),
            "nframes": len(ordered),
            "fmin": fmin,
            "fmax": fmax,
            "peak_frame": peak_entry["fr"],
            "fps": round(fps, 3),
            "time": t_start.strftime("%H:%M:%S.%f")[:-4] + " UT",
            "time_end": t_end.strftime("%H:%M:%S.%f")[:-4] + " UT",
            "date": t_start.strftime("%Y-%m-%d"),
            "dur": round(dur_s, 2),
            "cutout_px": max(all_sz),
            "peak_adu": max(r["peak_adu"] for r in ordered),
            "peak_flux": peak_entry["flux"],
            "max_sat_px": max(r["sat"] for r in ordered),
            "az_beg": az_beg,
            "el_beg": el_beg,
            "az_end": az_end,
            "el_end": el_end,
            "compass_beg": compass_beg,
            "compass_end": compass_end,
            "ang_len_deg": ang_len,
            "ang_vel_deg_s": ang_vel,
            "bbox": bbox,
            "thumb": "{}_thumb.jpg".format(tag),
            "full": "{}_full.jpg".format(tag),
            "video": "{}.mp4".format(tag),
            "cropvideo": "{}_crop.mp4".format(tag),
            "frames": ordered,
        })
    return clips


def readFFcached(night_dir, ff_name, cache={}):
    """FFs repeat across detections (several meteors per block); read each once."""
    if ff_name not in cache:
        d = resolveFileDir(night_dir, ff_name)
        if not d:
            raise FileNotFoundError(ff_name)
        cache[ff_name] = readFF(d, ff_name)
    return cache[ff_name]


def evenBox(x0, y0, x1, y1, w, h, margin, min_side=200):
    """Padded crop box within (0..w, 0..h) guaranteed to have even width & height >= 2."""
    cx = 0.5 * (x0 + x1)
    cy = 0.5 * (y0 + y1)
    bw = max(min_side, int(math.ceil(x1 - x0)) + 2 * margin)
    bh = max(min_side, int(math.ceil(y1 - y0)) + 2 * margin)
    bw = min(w - (w % 2), bw + (bw % 2))
    bh = min(h - (h % 2), bh + (bh % 2))
    ix0 = max(0, min(w - bw, int(round(cx - bw / 2.0))))
    iy0 = max(0, min(h - bh, int(round(cy - bh / 2.0))))
    return ix0, iy0, ix0 + bw, iy0 + bh


def saveDetThumb(ff, bbox, out_path):
    lut = stretchLUT(ff)
    h, w = ff.maxpixel.shape
    x0, y0, x1, y1 = bbox
    x0 = max(0, int(x0) - THUMB_MARGIN)
    y0 = max(0, int(y0) - THUMB_MARGIN)
    x1 = min(w, int(x1) + THUMB_MARGIN)
    y1 = min(h, int(y1) + THUMB_MARGIN)
    if x1 <= x0 or y1 <= y0:
        crop = lut[ff.maxpixel]
    else:
        crop = lut[ff.maxpixel[y0:y1, x0:x1]]
    img = Image.fromarray(crop)
    img = img.resize((max(1, int(img.width * THUMB_H / max(1, img.height))), THUMB_H), Image.LANCZOS)
    img.save(out_path, quality=88)


def pasteFRFrame(base_img, fr, pairs):
    """Paste list of (line, z) cutouts from `fr` onto `base_img` safely clipped to image bounds."""
    h, w = base_img.shape
    img = base_img.copy()
    for ln, z in pairs:
        yc = int(fr.yc[ln][z])
        xc = int(fr.xc[ln][z])
        sz = int(fr.size[ln][z])
        half = sz // 2
        y0_i, y1_i = yc - half, yc - half + sz
        x0_i, x1_i = xc - half, xc - half + sz
        cy0 = max(0, y0_i)
        cy1 = min(h, y1_i)
        cx0 = max(0, x0_i)
        cx1 = min(w, x1_i)
        if cy1 > cy0 and cx1 > cx0:
            py0, py1 = cy0 - y0_i, cy1 - y0_i
            px0, px1 = cx0 - x0_i, cx1 - x0_i
            img[cy0:cy1, cx0:cx1] = fr.frames[ln][z][py0:py1, px0:px1]
    return img


def buildFRCompositeMax(night_dir, fr_entry):
    """Return (full_max_uint8, lut, w, h) combining FR raw max pixels with parent FF if available."""
    fr_dir = resolveFileDir(night_dir, fr_entry["fr"])
    fr = FRbin.read(fr_dir, fr_entry["fr"])
    ff_dir = resolveFileDir(night_dir, fr_entry["ff"])
    if ff_dir:
        ff = readFF(ff_dir, fr_entry["ff"])
        base = ff.avepixel.copy()
        lut = stretchLUT(ff)
    else:
        max_x = max(max(fr.xc[i]) + max(fr.size[i]) // 2 for i in range(fr.lines)) + 32
        max_y = max(max(fr.yc[i]) + max(fr.size[i]) // 2 for i in range(fr.lines)) + 32
        w = max(640, max_x + (max_x % 2))
        h = max(480, max_y + (max_y % 2))
        base = np.zeros((h, w), dtype=np.uint8)
        lut = np.arange(256, dtype=np.uint8)
    comp = base.copy()
    for ln in range(fr.lines):
        for z in range(int(fr.frameNum[ln])):
            patched = pasteFRFrame(base, fr, [(ln, z)])
            comp = np.maximum(comp, patched)
    return comp, base, fr, lut


def writeIndex(night_dir, cache_dir):
    default_pp, recal = loadPlateparLookup(night_dir)
    radiants = parseRadiants(night_dir)

    # Read canonical FTPdetectinfo first to get fps if present
    src = findFTPdetectinfo(night_dir, unfiltered=False)
    canon_entries = readFTPdetectinfo(night_dir, os.path.basename(src)) if src else []
    fps_default = float(canon_entries[0][4]) if (canon_entries and canon_entries[0][4]) else 25.0

    fr_clips = loadFRClips(night_dir, default_pp, recal, fps_default=fps_default)
    fr_by_stem = {}
    for fc in fr_clips:
        stem = fc["fr"][3:-4]
        fr_by_stem[stem] = fc

    dets = []
    canon_keys = set()
    for entry in canon_entries:
        d = buildDetectionDict(entry, len(dets), "det", night_dir, default_pp, recal, radiants, fr_by_stem)
        dets.append(d)
        canon_keys.add((d["ff"], int(round(d["fmin"] / 3.0))))

    unf_src = findFTPdetectinfo(night_dir, unfiltered=True)
    unf_entries = readFTPdetectinfo(night_dir, os.path.basename(unf_src)) if unf_src else []
    unfiltered = []
    for entry in unf_entries:
        ff_name = entry[0]
        meas = entry[11]
        if not meas:
            continue
        fmin_cand = min(float(m[1]) for m in meas)
        # Skip candidates that survived into canonical detections (within ±3 frames on same FF)
        if any(cd["ff"] == ff_name and abs(cd["fmin"] - fmin_cand) <= 4.0 for cd in dets):
            continue
        u = buildDetectionDict(entry, len(unfiltered), "unf", night_dir, default_pp, recal, radiants, fr_by_stem)
        unfiltered.append(u)

    # Link FR clips back to matching detection/unfiltered indices
    for d in dets:
        if d["fr_idx"] is not None:
            fr_clips[d["fr_idx"]]["det_idx"] = d["i"]
    for u in unfiltered:
        if u["fr_idx"] is not None and "det_idx" not in fr_clips[u["fr_idx"]]:
            fr_clips[u["fr_idx"]]["unf_idx"] = u["i"]

    os.makedirs(cache_dir, exist_ok=True)
    ff_cache = {}
    for group_list in (dets, unfiltered):
        for d in group_list:
            if not d["has_ff"]:
                continue
            try:
                ff = readFFcached(night_dir, d["ff"], ff_cache)
                saveDetThumb(ff, d["bbox"], os.path.join(cache_dir, d["thumb"]))
            except Exception:
                pass

    for fc in fr_clips:
        try:
            comp, _, _, lut = buildFRCompositeMax(night_dir, fc)
            h, w = comp.shape
            x0, y0, x1, y1 = fc["bbox"]
            x0 = max(0, int(x0) - 40)
            y0 = max(0, int(y0) - 40)
            x1 = min(w, int(x1) + 40)
            y1 = min(h, int(y1) + 40)
            crop = lut[comp[y0:y1, x0:x1]] if (x1 > x0 and y1 > y0) else lut[comp]
            img = Image.fromarray(crop)
            img = img.resize((max(1, int(img.width * THUMB_H / max(1, img.height))), THUMB_H), Image.LANCZOS)
            img.save(os.path.join(cache_dir, fc["thumb"]), quality=88)
        except Exception:
            pass

    # Extra archived FFs (e.g. reference clear-sky blocks saved when detections == 0)
    det_ffs = {d["ff"] for d in dets}
    extra_ffs = []
    for fn in sorted(os.listdir(night_dir)):
        if fn.startswith("FF_") and fn.endswith(".fits") and fn not in det_ffs:
            try:
                dt = filenameToDatetime(fn)
                tm = dt.strftime("%H:%M:%S UT")
            except Exception:
                tm = fn
            extra_ffs.append({"ff": fn, "time": tm})

    cap_dir = capturedCounterpart(night_dir)
    cap_count, first_ff_utc, last_ff_utc = 0, None, None
    if cap_dir:
        cap_ffs = sorted(f for f in os.listdir(cap_dir) if f.startswith("FF_") and f.endswith(".fits"))
        cap_count = len(cap_ffs)
        if cap_ffs:
            try:
                first_ff_utc = filenameToDatetime(cap_ffs[0]).strftime("%H:%M:%S")
                last_ff_utc = (filenameToDatetime(cap_ffs[-1]) + timedelta(seconds=10)).strftime("%H:%M:%S")
            except Exception:
                pass

    mtimes = [int(os.path.getmtime(p)) for p in (src, unf_src) if p and os.path.isfile(p)]
    out = {
        "version": INDEX_VERSION,
        "night": os.path.basename(os.path.normpath(night_dir)),
        "src": os.path.basename(src) if src else None,
        "src_mtime": max(mtimes) if mtimes else 0,
        "captured_available": cap_count > 0,
        "captured_ff_count": cap_count,
        "first_ff_utc": first_ff_utc,
        "last_ff_utc": last_ff_utc,
        "detections": dets,
        "unfiltered": unfiltered,
        "fr_clips": fr_clips,
        "extra_ffs": extra_ffs,
    }
    tmp = os.path.join(cache_dir, "detections.json.tmp")
    with open(tmp, "w") as f:
        json.dump(out, f, indent=1)
    os.replace(tmp, os.path.join(cache_dir, "detections.json"))


def loadIndexGroupEntry(cache_dir, group, det_index):
    with open(os.path.join(cache_dir, "detections.json")) as f:
        idx = json.load(f)
    return idx[group][det_index]


def applySkyOverlay(night_dir, gray_or_rgb_pil, dt):
    """Annotate image with stars/planets/cardinals via sky_overlay using night's platepar."""
    try:
        import sky_overlay
        pp_night = os.path.join(night_dir, "platepar_cmn2010.cal")
        if os.path.isfile(pp_night):
            sky_overlay.PLATEPAR_PATH = pp_night
            sky_overlay._pp_mtime = 0
        return sky_overlay.annotate(gray_or_rgb_pil, dt)
    except Exception:
        return gray_or_rgb_pil.convert("RGB")


def annotateDetectionFrame(night_dir, ff, d):
    lut = stretchLUT(ff)
    t0 = filenameToDatetime(d["ff"]) + timedelta(seconds=d["fmin"] / d["fps"])
    img = applySkyOverlay(night_dir, Image.fromarray(lut[ff.maxpixel]), t0)
    dr = ImageDraw.Draw(img)
    font = _getfont(13)
    W, H = img.size

    segs = d.get("segments", [])
    if segs:
        x0, y0 = segs[0]["x"], segs[0]["y"]
        x1, y1 = segs[-1]["x"], segs[-1]["y"]
        # Draw subtle crop reticle around the event without touching streak pixels
        bx0, by0, bx1, by1 = d["bbox"]
        rx0 = max(2, int(bx0) - 28)
        ry0 = max(2, int(by0) - 28)
        rx1 = min(W - 3, int(bx1) + 28)
        ry1 = min(H - 3, int(by1) + 28)
        corn = 14
        col_box = (125, 180, 255)
        for cx, cy, sx, sy in [(rx0, ry0, 1, 1), (rx1, ry0, -1, 1), (rx0, ry1, 1, -1), (rx1, ry1, -1, -1)]:
            dr.line([cx, cy, cx + sx * corn, cy], fill=col_box, width=2)
            dr.line([cx, cy, cx, cy + sy * corn], fill=col_box, width=2)

        # Start marker (green ring) & end marker (amber ring) outside pixel core
        dr.ellipse([x0 - 9, y0 - 9, x0 + 9, y0 + 9], outline=(126, 226, 154), width=2)
        dr.ellipse([x1 - 9, y1 - 9, x1 + 9, y1 + 9], outline=(255, 190, 92), width=2)
        lbl = "{} ({:.2f}s)".format(d["time"], d["dur"])
        if d.get("shower"):
            lbl += " · " + d["shower"]
        if d.get("peak_mag") is not None:
            lbl += " · {:+.1f}m".format(d["peak_mag"])
        lx = max(6, min(W - 8 * len(lbl) - 6, rx0))
        ly = ry0 - 20 if ry0 > 24 else min(H - 20, ry1 + 6)
        dr.text((lx + 1, ly + 1), lbl, fill=(0, 0, 0), font=font)
        dr.text((lx, ly), lbl, fill=(126, 226, 154), font=font)
    return img


def writeFull(night_dir, cache_dir, group, det_index):
    d = loadIndexGroupEntry(cache_dir, group, det_index)
    ff = readFFcached(night_dir, d["ff"], {})
    lut = stretchLUT(ff)
    Image.fromarray(lut[ff.maxpixel]).save(os.path.join(cache_dir, d["full"]), quality=90)


def writeAnn(night_dir, cache_dir, group, det_index):
    d = loadIndexGroupEntry(cache_dir, group, det_index)
    ff = readFFcached(night_dir, d["ff"], {})
    img = annotateDetectionFrame(night_dir, ff, d)
    img.save(os.path.join(cache_dir, d["ann"]), quality=90)


def writeResid(night_dir, cache_dir, group, det_index):
    d = loadIndexGroupEntry(cache_dir, group, det_index)
    ff = readFFcached(night_dir, d["ff"], {})
    resid = blockResidual(ff)
    lut = residualLUT(resid)
    Image.fromarray(lut[resid]).save(os.path.join(cache_dir, d["resid"]), quality=90)


def encodeGrayFrames(frames_iter, w, h, fps, out_path, upscale_min=0):
    vf = []
    if upscale_min and max(w, h) < upscale_min:
        scale = int(math.ceil(upscale_min / float(max(w, h))))
        nw, nh = w * scale, h * scale
        nw += nw % 2
        nh += nh % 2
        vf = ["-vf", "scale={}:{}:flags=neighbor".format(nw, nh)]
    elif (w % 2) or (h % 2):
        vf = ["-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2"]
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pixel_format", "gray",
        "-video_size", "{}x{}".format(w, h), "-framerate", str(fps), "-i", "-",
        *vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "19",
        "-pix_fmt", "yuv420p", "-movflags", "+faststart", out_path + ".tmp.mp4",
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)
    for frm in frames_iter:
        proc.stdin.write(frm.tobytes())
    proc.stdin.close()
    if proc.wait() != 0:
        raise RuntimeError("ffmpeg failed for {}".format(out_path))
    os.replace(out_path + ".tmp.mp4", out_path)


def writeVideo(night_dir, cache_dir, group, det_index, cropped=False):
    d = loadIndexGroupEntry(cache_dir, group, det_index)
    ff = readFFcached(night_dir, d["ff"], {})
    lut = stretchLUT(ff)
    n = ff.nframes if getattr(ff, "nframes", 0) else 256
    f0 = max(0, int(d["fmin"]) - PAD_FRAMES)
    f1 = min(n - 1, int(math.ceil(d["fmax"])) + PAD_FRAMES)
    h, w = ff.maxpixel.shape
    if cropped:
        bx0, by0, bx1, by1 = d["bbox"]
        x0, y0, x1, y1 = evenBox(bx0, by0, bx1, by1, w, h, CROP_MARGIN, min_side=240)
        out = os.path.join(cache_dir, d["cropvideo"])
        cw, ch = x1 - x0, y1 - y0
        def _gen():
            for i in range(f0, f1 + 1):
                frame = ff.avepixel[y0:y1, x0:x1].copy()
                m = ff.maxframe[y0:y1, x0:x1] == i
                frame[m] = ff.maxpixel[y0:y1, x0:x1][m]
                yield lut[frame]
        encodeGrayFrames(_gen(), cw, ch, VIDEO_FPS, out, upscale_min=360)
    else:
        out = os.path.join(cache_dir, d["video"])
        def _gen():
            for i in range(f0, f1 + 1):
                frame = ff.avepixel.copy()
                m = ff.maxframe == i
                frame[m] = ff.maxpixel[m]
                yield lut[frame]
        encodeGrayFrames(_gen(), w, h, VIDEO_FPS, out)


# ---- Raw FR_*.bin media ----------------------------------------------------

def writeFRFull(night_dir, cache_dir, fr_index):
    fc = loadIndexGroupEntry(cache_dir, "fr_clips", fr_index)
    comp, _, _, lut = buildFRCompositeMax(night_dir, fc)
    Image.fromarray(lut[comp]).save(os.path.join(cache_dir, fc["full"]), quality=90)


def writeFRVideo(night_dir, cache_dir, fr_index, cropped=False):
    fc = loadIndexGroupEntry(cache_dir, "fr_clips", fr_index)
    _, base, fr, lut = buildFRCompositeMax(night_dir, fc)
    h, w = base.shape
    clips = {}
    for ln in range(fr.lines):
        for z in range(int(fr.frameNum[ln])):
            t_i = int(fr.t[ln][z])
            clips.setdefault(t_i, []).append((ln, z))
    t0, t1 = int(fc["fmin"]), int(fc["fmax"])

    if cropped:
        bx0, by0, bx1, by1 = fc["bbox"]
        x0, y0, x1, y1 = evenBox(bx0, by0, bx1, by1, w, h, 48, min_side=200)
        out = os.path.join(cache_dir, fc["cropvideo"])
        cw, ch = x1 - x0, y1 - y0
        def _gen():
            for t_i in range(t0, t1 + 1):
                img = pasteFRFrame(base, fr, clips.get(t_i, []))
                yield lut[img[y0:y1, x0:x1]]
        encodeGrayFrames(_gen(), cw, ch, VIDEO_FPS, out, upscale_min=360)
    else:
        out = os.path.join(cache_dir, fc["video"])
        def _gen():
            for t_i in range(t0, t1 + 1):
                img = pasteFRFrame(base, fr, clips.get(t_i, []))
                yield lut[img]
        encodeGrayFrames(_gen(), w, h, VIDEO_FPS, out)


# ---- per-BLOCK media for /tonight and /ff block inspector ------------------

def blockBase(ff_name):
    """Stable cache basename for a flagged block, e.g. 'blk_20260821_231033_504'."""
    stem = ff_name[3:] if ff_name.startswith("FF_") else ff_name
    parts = stem.replace(".fits", "").split("_")
    # Strip leading station code (e.g. UK00DY_, UK00E4_) so cache names stay uniform
    if len(parts) >= 4 and len(parts[1]) == 8 and parts[1].isdigit():
        core = "_".join(parts[1:])
    else:
        core = stem.replace(".fits", "")
    return "blk_" + core[:20]


def writeBlockThumb(cap_dir, cache_dir, ff_name, thumb_h=160):
    d = resolveFileDir(cap_dir, ff_name) or cap_dir
    ff = readFF(d, ff_name)
    resid = blockResidual(ff)
    lut = residualLUT(resid)
    img = Image.fromarray(lut[resid])
    os.makedirs(cache_dir, exist_ok=True)
    img = img.resize((max(1, int(img.width * thumb_h / img.height)), thumb_h), Image.LANCZOS)
    img.save(os.path.join(cache_dir, blockBase(ff_name) + "_thumb.jpg"), quality=88)


def writeBlockFull(cap_dir, cache_dir, ff_name):
    d = resolveFileDir(cap_dir, ff_name) or cap_dir
    ff = readFF(d, ff_name)
    lut = stretchLUT(ff)
    os.makedirs(cache_dir, exist_ok=True)
    Image.fromarray(lut[ff.maxpixel]).save(
        os.path.join(cache_dir, blockBase(ff_name) + "_full.jpg"), quality=90)


def writeBlockAnn(night_dir, cache_dir, ff_name):
    d = resolveFileDir(night_dir, ff_name) or night_dir
    ff = readFF(d, ff_name)
    lut = stretchLUT(ff)
    dt = filenameToDatetime(ff_name)
    img = applySkyOverlay(night_dir, Image.fromarray(lut[ff.maxpixel]), dt)
    os.makedirs(cache_dir, exist_ok=True)
    img.save(os.path.join(cache_dir, blockBase(ff_name) + "_ann.jpg"), quality=90)


def writeBlockResid(night_dir, cache_dir, ff_name):
    d = resolveFileDir(night_dir, ff_name) or night_dir
    ff = readFF(d, ff_name)
    resid = blockResidual(ff)
    lut = residualLUT(resid)
    os.makedirs(cache_dir, exist_ok=True)
    Image.fromarray(lut[resid]).save(
        os.path.join(cache_dir, blockBase(ff_name) + "_resid.jpg"), quality=90)


def writeBlockVideo(cap_dir, cache_dir, ff_name):
    d = resolveFileDir(cap_dir, ff_name) or cap_dir
    ff = readFF(d, ff_name)
    lut = stretchLUT(ff)
    n = ff.nframes if getattr(ff, "nframes", 0) else 256
    h, w = ff.maxpixel.shape
    out = os.path.join(cache_dir, blockBase(ff_name) + ".mp4")
    os.makedirs(cache_dir, exist_ok=True)
    def _gen():
        for i in range(n):
            frame = ff.avepixel.copy()
            m = ff.maxframe == i
            frame[m] = ff.maxpixel[m]
            yield lut[frame]
    encodeGrayFrames(_gen(), w, h, VIDEO_FPS, out)


def _parseGroupIdx(args):
    if len(args) == 1:
        return "detections", int(args[0])
    return args[0], int(args[1])


if __name__ == "__main__":
    mode, night_dir, cache_dir = sys.argv[1], sys.argv[2], sys.argv[3]
    rest = sys.argv[4:]
    if mode == "index":
        writeIndex(night_dir, cache_dir)
    elif mode == "full":
        grp, idx_n = _parseGroupIdx(rest)
        writeFull(night_dir, cache_dir, grp, idx_n)
    elif mode == "ann":
        grp, idx_n = _parseGroupIdx(rest)
        writeAnn(night_dir, cache_dir, grp, idx_n)
    elif mode == "resid":
        grp, idx_n = _parseGroupIdx(rest)
        writeResid(night_dir, cache_dir, grp, idx_n)
    elif mode == "video":
        grp, idx_n = _parseGroupIdx(rest)
        writeVideo(night_dir, cache_dir, grp, idx_n, cropped=False)
    elif mode == "cropvideo":
        grp, idx_n = _parseGroupIdx(rest)
        writeVideo(night_dir, cache_dir, grp, idx_n, cropped=True)
    elif mode == "frfull":
        writeFRFull(night_dir, cache_dir, int(rest[0]))
    elif mode == "frvid":
        writeFRVideo(night_dir, cache_dir, int(rest[0]), cropped=False)
    elif mode == "frcropvid":
        writeFRVideo(night_dir, cache_dir, int(rest[0]), cropped=True)
    elif mode == "blockthumb":
        writeBlockThumb(night_dir, cache_dir, rest[0])
    elif mode == "blockfull":
        writeBlockFull(night_dir, cache_dir, rest[0])
    elif mode == "blockann":
        writeBlockAnn(night_dir, cache_dir, rest[0])
    elif mode == "blockresid":
        writeBlockResid(night_dir, cache_dir, rest[0])
    elif mode == "blockvideo":
        writeBlockVideo(night_dir, cache_dir, rest[0])
    else:
        sys.exit("unknown mode: " + mode)
