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

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.environ.get("RMS_DATA", "/mnt/nvme/RMS_data")
CAP  = os.path.join(DATA, "CapturedFiles")
STACK_PNG  = os.path.join(HERE, "tonight_stack.png")
LATEST_PNG = os.path.join(HERE, "tonight_latest.png")
JSON_OUT   = os.path.join(HERE, "tonight.json")
STATE_NPY  = os.path.join(HERE, ".tonight_running.npy")
STATE_META = os.path.join(HERE, ".tonight_state.json")
INTERVAL = int(os.environ.get("TONIGHT_INTERVAL", "20"))

def newest_dir():
    ds = [d for d in glob.glob(os.path.join(CAP, "*")) if os.path.isdir(d)]
    return max(ds, key=os.path.basename) if ds else None

def stretch(arr, floor_sigma=4.0):
    # crush the noise floor (a long maxpixel accumulates per-pixel noise peaks) so
    # only real stars/trails/events show, then asinh-stretch what remains.
    a = arr.astype(np.float32)
    bg = float(np.median(a))
    nz = 1.4826 * float(np.median(np.abs(a - bg))) + 1e-3
    x = np.clip(a - (bg + floor_sigma * nz), 0, None)
    s = np.arcsinh(x / (4 * nz))
    hi = np.percentile(s, 99.6) or 1.0
    return np.clip(s / hi * 255, 0, 255).astype("uint8")

def ff_time(name):
    # FF_XX0001_YYYYMMDD_HHMMSS_mmm_NNNNNNN.fits
    try:
        p = name.split("_"); return f"{p[2][:4]}-{p[2][4:6]}-{p[2][6:8]} {p[3][:2]}:{p[3][2:4]}:{p[3][4:6]} UTC"
    except Exception:
        return "?"

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
    latest_mp = None
    while True:
        d = newest_dir()
        if d is None:
            time.sleep(INTERVAL); continue
        name = os.path.basename(d)
        if name != cur:                       # new night -> reset
            cur, last, running, latest_mp = name, "", None, None
        ffs = sorted(os.path.basename(f) for f in glob.glob(os.path.join(d, "FF_*.fits")))
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
            running = mp.astype(np.uint8) if running is None else np.maximum(running, mp)
            latest_mp = mp; last = f; added += 1
        if added:
            stack_img = Image.fromarray(stretch(running, floor_sigma=6.0))
            latest_img = Image.fromarray(stretch(latest_mp, floor_sigma=3.5))
            if annotate is not None:
                try:
                    # label at the LAST FF's time: latest sky is exact; on the stack
                    # the labels ride the trail heads (current positions).
                    p = last.split("_")
                    dt = datetime.datetime.strptime(p[2] + p[3], "%Y%m%d%H%M%S")
                    stack_img = annotate(stack_img, dt)
                    latest_img = annotate(latest_img, dt)
                except Exception:
                    pass                              # bad platepar/ephemeris -> plain images
            stack_img.save(STACK_PNG)
            latest_img.save(LATEST_PNG)
            count = ffs.index(last) + 1 if last in ffs else 0
            frames = count * 256
            meta = {"dir": cur, "last": last, "count": count,
                    "frames": frames, "duration_min": round(frames / 25.0 / 60.0, 1),
                    "first_ff": ff_time(ffs[0]) if ffs else "?",
                    "last_ff": ff_time(last),
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
