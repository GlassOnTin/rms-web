#!/usr/bin/env python3
"""
make_tonight_preview.py — live preview of the CURRENT night's capture for the
RMS dashboard. Runs as a small daemon: it incrementally builds a cumulative
maxpixel stack of tonight's FF files (reading each FF exactly once, persisting
state so restarts resume cheaply) and writes:

  tonight_stack.png   — cumulative stack of the night so far (shows every event)
  tonight_latest.png  — the most recent FF's maxpixel (the live sky ~now)
  tonight.json        — stats (night id, FF count, frames, duration, freshness)

Needs the RMS venv (RMS.Formats, numpy, pillow). One iteration per ~20 s.
"""
import os, glob, json, time, sys, datetime
import numpy as np
from PIL import Image
from RMS.Formats import FFfile
try:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from sky_overlay import annotate                # star/planet/Moon labels via the live platepar
except Exception:
    annotate = None                                 # overlay is optional — previews must never break
try:
    from sat_overlay import annotate_sats           # satellite streaks (latest image only) + pass log
except Exception:
    annotate_sats = None
try:
    from adsb_overlay import annotate_aircraft, annotate_stack_aircraft  # ADS-B streaks + pass log
except Exception:
    annotate_aircraft = annotate_stack_aircraft = None

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("RMS_DATA", "/mnt/nvme/RMS_data")
CAP  = os.path.join(DATA, "CapturedFiles")
STACK_PNG  = os.path.join(HERE, "tonight_stack.png")
LATEST_PNG = os.path.join(HERE, "tonight_latest.png")
DET_PNG    = os.path.join(HERE, "tonight_detected.png")
JSON_OUT   = os.path.join(HERE, "tonight.json")
STATE_NPY  = os.path.join(HERE, ".tonight_running.npy")
STATE_DET_NPY = os.path.join(HERE, ".tonight_det.npy")
STATE_META = os.path.join(HERE, ".tonight_state.json")
INTERVAL = int(os.environ.get("TONIGHT_INTERVAL", "20"))

def newest_dir():
    ds = [d for d in glob.glob(os.path.join(CAP, "*")) if os.path.isdir(d)]
    return max(ds, key=os.path.basename) if ds else None

_stretch_stats = {}   # per-role EMA of (bg, nz, hi), so display gain can't pump

def stretch(arr, floor_sigma=4.0, key="stack"):
    # crush the noise floor (a long maxpixel accumulates per-pixel noise peaks) so
    # only real stars/trails/events show, then asinh-stretch what remains.
    # The normalizer is the 99.6th percentile CLAMPED to the star-brightness
    # regime (25..220 ADU above floor): unclamped, a bright aircraft trail or
    # the Moon owns the percentile and dims every star to black, while a dark
    # clear frame drives it so low that pure saturation-anchoring renders the
    # single-frame preview black. The normalizer is EMA-smoothed across calls so
    # per-frame jitter under drifting cloud can't visibly pump the display gain
    # (the floor stays per-frame: it must adapt immediately when the sky changes).
    a = arr.astype(np.float32)
    bg = float(np.median(a))
    nz = 1.4826 * float(np.median(np.abs(a - bg))) + 1e-3
    x = np.clip(a - (bg + floor_sigma * nz), 0, None)
    s = np.arcsinh(x / (4 * nz))
    hi = float(np.percentile(s, 99.6))
    hi = min(max(hi, float(np.arcsinh(25.0 / (4 * nz)))),
             float(np.arcsinh(220.0 / (4 * nz))))
    prev = _stretch_stats.get(key)
    if prev is not None:
        hi = 0.3 * hi + 0.7 * prev[2]
    _stretch_stats[key] = (bg, nz, hi)
    return np.clip(s / hi * 255, 0, 255).astype("uint8")

def ff_time(name):
    # FF_XX0001_YYYYMMDD_HHMMSS_mmm_NNNNNNN.fits
    try:
        p = name.split("_"); return f"{p[2][:4]}-{p[2][4:6]}-{p[2][6:8]} {p[3][:2]}:{p[3][2:4]}:{p[3][4:6]} UTC"
    except Exception:
        return "?"

def night_key(ff_name):
    """Dusk-anchored night id, so every capture segment of one observing night
    (which spans UTC midnight, and survives capture restarts that spawn a new
    directory) shares a key. Reset the stack only when this changes."""
    try:
        p = ff_name.split("_")
        t = datetime.datetime.strptime(p[2] + p[3], "%Y%m%d%H%M%S")
        if t.hour < 12:                       # after-midnight capture belongs to the prior evening
            t -= datetime.timedelta(days=1)
        return t.strftime("%Y%m%d")
    except Exception:
        return ff_name[:16]

import re as _re
import subprocess as _sp

def detection_ffs():
    """FF names the real-time detector flagged tonight ('detected meteors: N',
    N>0), from RMS logs touched in the last 16 h (capture restarts split logs)."""
    out = set()
    logs = glob.glob(os.path.join(DATA, "logs", "log_*.log"))
    cutoff = time.time() - 16 * 3600
    for lg in logs:
        try:
            if os.path.getmtime(lg) < cutoff:
                continue
            txt = _sp.run(["grep", "-F", "detected meteors:", lg],
                          capture_output=True, text=True, timeout=20).stdout
        except Exception:
            continue
        for m in _re.finditer(r"(FF_\S+\.fits) detected meteors: ([1-9]\d*)", txt):
            out.add(m.group(1))
    return out


def load_state():
    try:
        meta = json.load(open(STATE_META))
        run = np.load(STATE_NPY)
        return meta, run
    except Exception:
        return None, None

def save_state(meta, run):
    try:
        np.save(STATE_NPY, run); json.dump(meta, open(STATE_META, "w"))
    except Exception:
        pass

def main():
    meta, running = load_state()
    cur = meta.get("dir") if meta else None
    last = meta.get("last", "") if meta else ""
    cur_night = meta.get("night") if meta else None
    total = meta.get("count", 0) if meta else 0            # cumulative FF blocks across the night
    det_merged = set(meta.get("det_ffs", [])) if meta else set()
    try:
        det_running = np.load(STATE_DET_NPY) if det_merged else None
    except Exception:
        det_running, det_merged = None, set()
    first_ff_name = meta.get("first_ff_name", "") if meta else ""
    latest_mp = None
    while True:
        d = newest_dir()
        if d is None:
            time.sleep(INTERVAL); continue
        name = os.path.basename(d)
        ffs = sorted(os.path.basename(f) for f in glob.glob(os.path.join(d, "FF_*.fits")))
        if not ffs:
            time.sleep(INTERVAL); continue
        nk = night_key(ffs[-1])
        if nk != cur_night:                   # genuinely new observing night -> reset the stack
            cur_night, running, latest_mp, total, first_ff_name = nk, None, None, 0, ""
            cur, last = name, ""
            det_running, det_merged = None, set()
        elif name != cur:                     # same night, new capture segment (restart) -> keep the stack
            cur, last = name, ""
        now = time.time()
        fresh = [f for f in ffs if f > last]
        added = 0
        for f in fresh:
            fp = os.path.join(d, f)
            if now - os.path.getmtime(fp) < 2:   # skip a file RMS may still be writing
                continue
            try:
                ff = FFfile.read(d, f)
                mp = ff.maxpixel
            except Exception:
                continue                          # partial/unreadable -> retry next loop
            # A running max is unforgiving: one bright moonlit-cloud frame stamps
            # its glow onto every pixel for the rest of the night (measured: stack
            # bg ~145/255 after one such passage). Gate on the MAXPIXEL median:
            # drifting haze can keep the avepixel median low while still writing
            # bright values into the max (each pixel only needs one lit moment in
            # 10 s). Clear-sky maxpixel median measured ~25-28; haze far above.
            # The "latest" image still shows skipped frames; detection unaffected.
            clear = (float(np.median(ff.avepixel)) <= 8
                     and float(np.median(mp)) <= 34)
            if clear:
                running = mp.astype(np.uint8) if running is None else np.maximum(running, mp)
            latest_mp = mp; last = f; added += 1; total += 1
            if not first_ff_name:
                first_ff_name = f
        # Detected-meteors stack: running max of ONLY the FF blocks the real-time
        # detector flagged. No cloud gate — a candidate block is included even in
        # poor sky, since showing the candidate is the whole point.
        det_new = 0
        for f in sorted(detection_ffs() - det_merged):
            if night_key(f) != cur_night:
                continue
            hitpaths = glob.glob(os.path.join(CAP, "*", f))
            if not hitpaths:
                continue
            try:
                dmp = FFfile.read(os.path.dirname(hitpaths[0]), f).maxpixel
            except Exception:
                continue
            det_running = dmp.astype(np.uint8) if det_running is None else np.maximum(det_running, dmp)
            det_merged.add(f); det_new += 1
        if det_new and det_running is not None:
            try:
                det_img = Image.fromarray(stretch(det_running, floor_sigma=4.0, key="det"))
                if annotate is not None and last:
                    p = last.split("_")
                    dt = datetime.datetime.strptime(p[2] + p[3], "%Y%m%d%H%M%S")
                    det_img = annotate(det_img, dt)
                det_img.save(DET_PNG)
                np.save(STATE_DET_NPY, det_running)
            except Exception:
                pass

        if added:
            stack_img = Image.fromarray(stretch(running, floor_sigma=6.0, key="stack"))
            latest_img = Image.fromarray(stretch(latest_mp, floor_sigma=3.5, key="latest"))
            if annotate is not None:
                try:
                    # label at the LAST FF's time: latest sky is exact; on the stack
                    # the labels ride the trail heads (current positions). Overlays key
                    # on the night id so labels survive same-night capture restarts.
                    p = last.split("_")
                    dt = datetime.datetime.strptime(p[2] + p[3], "%Y%m%d%H%M%S")
                    stack_img = annotate(stack_img, dt)
                    if annotate_stack_aircraft is not None:
                        stack_img = annotate_stack_aircraft(stack_img, night=cur_night)
                    latest_img = annotate(latest_img, dt)
                    if annotate_sats is not None:
                        latest_img = annotate_sats(latest_img, dt, night=cur_night)
                    if annotate_aircraft is not None:
                        latest_img = annotate_aircraft(latest_img, dt, night=cur_night)
                except Exception:
                    pass                              # bad platepar/ephemeris -> plain images
            stack_img.save(STACK_PNG)
            latest_img.save(LATEST_PNG)
            count = total
            frames = count * 256
            meta = {"dir": cur, "last": last, "count": count, "night": cur_night,
                    "first_ff_name": first_ff_name,
                    "frames": frames, "duration_min": round(frames / 25.0 / 60.0, 1),
                    "first_ff": ff_time(first_ff_name) if first_ff_name else "?",
                    "last_ff": ff_time(last),
                    "det_ffs": sorted(det_merged), "det_blocks": len(det_merged),
                    "updated": int(now), "last_added": int(now)}
            json.dump(meta, open(JSON_OUT, "w"))
            save_state(meta, running)
        else:
            # keep freshness flag current even when idle (capture may be paused/day)
            if os.path.isfile(JSON_OUT):
                try:
                    m = json.load(open(JSON_OUT)); m["updated"] = int(now); json.dump(m, open(JSON_OUT, "w"))
                except Exception:
                    pass
        time.sleep(INTERVAL)

if __name__ == "__main__":
    main()
