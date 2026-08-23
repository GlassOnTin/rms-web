#!/home/ian/vRMS/bin/python
"""rebuild_tonight_stack.py — CLI/headless rebuild of TONIGHT's cumulative stack from ALL of
tonight's captured FFs (every segment of the dusk-anchored observing night), applying the
clear-sky gate. Recovers frames missed before a capture restart/reboot or a gate change.

The /tonight "Rebuild stack" button does the same thing in-process (via a flag the preview
daemon picks up). This is the equivalent for headless/scripted use — run it with the preview
daemon STOPPED (both write the same files). It reuses make_tonight_preview.rebuild_running()
so the gate and stacking logic have a single source of truth.
"""
import os, sys, glob, json, datetime
import numpy as np
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, "/home/ian/source/RMS")
from make_tonight_preview import (rebuild_running, night_segments, night_key, stretch,
                                  ff_time, STACK_PNG, STATE_NPY, STATE_META, JSON_OUT, CAP)
try:
    from sky_overlay import annotate
except Exception:
    annotate = None

all_ffs = [os.path.basename(x) for x in glob.glob(os.path.join(CAP, "*", "FF_*.fits"))]
if not all_ffs:
    sys.exit("no FFs found")
cur_night = night_key(max(all_ffs))

running, latest_mp, total, first, last = rebuild_running(cur_night)
if running is None:
    sys.exit("no clear frames passed the gate — nothing to rebuild")
print("observing night %s: stacked from %d FF blocks across %d segment(s)"
      % (cur_night, total, len(night_segments(cur_night))))

# render with the daemon's exact stretch + overlay, labelled at the last FF's time
p = last.split("_")
last_dt = datetime.datetime.strptime(p[2] + p[3], "%Y%m%d%H%M%S")
img = Image.fromarray(stretch(running, floor_sigma=6.0, key="stack"))
if annotate is not None:
    try:
        img = annotate(img, last_dt)
    except Exception:
        pass
img.save(STACK_PNG)
np.save(STATE_NPY, running)

# update persisted meta so the daemon resumes cleanly from the rebuilt base
meta = json.load(open(STATE_META)) if os.path.isfile(STATE_META) else {}
meta.update({
    "dir": os.path.basename(night_segments(cur_night)[-1]), "last": last, "count": total,
    "night": cur_night, "first_ff_name": first, "frames": total * 256,
    "duration_min": round(total * 256 / 25.0 / 60.0, 1),
    "first_ff": ff_time(first), "last_ff": ff_time(last),
    "stack_built": True, "segments": len(night_segments(cur_night)),
})
json.dump(meta, open(STATE_META, "w"))
json.dump(meta, open(JSON_OUT, "w"))
print("rebuilt %s + state; daemon resumes from %s" % (os.path.basename(STACK_PNG), last))
