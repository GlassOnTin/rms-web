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
JSON_OUT   = os.path.join(HERE, "tonight.json")
STATE_NPY  = os.path.join(HERE, ".tonight_running.npy")
STATE_META = os.path.join(HERE, ".tonight_state.json")
INTERVAL = int(os.environ.get("TONIGHT_INTERVAL", "20"))

def newest_dir():
    ds = [d for d in glob.glob(os.path.join(CAP, "*")) if os.path.isdir(d)]
    return max(ds, key=os.path.basename) if ds else None

_stretch_stats = {}   # per-role EMA of (bg, nz), so display gain can't pump

def stretch(arr, floor_sigma=4.0, key="stack"):
    # crush the noise floor (a long maxpixel accumulates per-pixel noise peaks) so
    # only real stars/trails/events show, then asinh-stretch what remains.
    # The scale is anchored to sensor saturation (~220 ADU above floor -> white)
    # rather than an image percentile: a bright aircraft trail or the Moon used
    # to own the 99.6th percentile and dim every star to black, and per-frame
    # percentile jitter under drifting cloud made the brightness oscillate.
    a = arr.astype(np.float32)
    bg = float(np.median(a))
    nz = 1.4826 * float(np.median(np.abs(a - bg))) + 1e-3
    prev = _stretch_stats.get(key)
    if prev is not None:
        bg = 0.3 * bg + 0.7 * prev[0]
        nz = 0.3 * nz + 0.7 * prev[1]
    _stretch_stats[key] = (bg, nz)
    x = np.clip(a - (bg + floor_sigma * nz), 0, None)
    s = np.arcsinh(x / (4 * nz))
    hi = float(np.arcsinh(220.0 / (4 * nz)))
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
