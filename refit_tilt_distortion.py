#!/home/ian/vRMS/bin/python
"""refit_tilt_distortion.py — one-off headless distortion refit of the tilt platepar.

Thin wrapper around refit_distortion.refit_night (the shared, validated core). The deployed
tilt platepar was fit on a single 77-star twilight frame, so its radial7-odd distortion is
under-constrained (RMSD ~2.68'/0.87px) and per-FF recalibration's tight goodness check
rejects every frame. This refits the distortion on a DENSE multi-frame star union.

Writes the candidate to platepar_refit.cal (does NOT deploy). Reports RMSD before/after.
Deploy + recalib-convergence test are a separate step. Set ARCH via env or edit below.
"""
import os
import sys
import logging

sys.path.insert(0, "/home/ian/source/RMS")
logging.disable(logging.CRITICAL)

import RMS.ConfigReader as cr
from refit_distortion import refit_night, sane_refit
from RMS.Formats.Platepar import Platepar

ARCH = os.environ.get("ARCH", "/mnt/nvme/RMS_data/ArchivedFiles/UK00DY_20260822_204209_537644")
DISTORTION = os.environ.get("DIST", "radial7-odd")
FIXED_SCALE = os.environ.get("FIXSCALE", "0") == "1"
OUT = "/home/ian/platepar_refit.cal"

config = cr.parse("/home/ian/source/RMS/.config")

base = Platepar()
base.read(os.path.join(ARCH, "platepar_cmn2010.cal"))

new, stats = refit_night(ARCH, os.path.join(ARCH, "platepar_cmn2010.cal"), config,
                         distortion=DISTORTION, fixed_scale=FIXED_SCALE, verbose=True)

if new is None:
    sys.exit("refit failed: %s" % stats.get("error", stats))

print("RA %.3f->%.3f Dec %.3f->%.3f F_scale %.4f->%.4f (%s, fixed_scale=%s)"
      % (base.RA_d, new.RA_d, base.dec_d, new.dec_d, base.F_scale, new.F_scale,
         DISTORTION, FIXED_SCALE))

if sane_refit(base, new, stats):
    new.write(OUT)
    print("SAVED candidate -> %s  (RMSD %.3f px = %.2f')"
          % (OUT, stats["refit_rmsd"], stats["refit_rmsd"]/new.F_scale*60))
else:
    print("NOT SAVED (failed sanity gate)")
