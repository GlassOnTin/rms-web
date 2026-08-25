#!/home/ian/vRMS/bin/python
"""auto_refit_distortion.py — durable stage-2 of the tilt auto-solve: distortion refit.

Companion to auto_tilt_solve.py (stage-1, the fast coarse pointing solve on the first clear
window). A single-frame coarse platepar has under-constrained distortion (corners tens of px
off), so per-FF recalibration rejects every frame and meteor astrometry is coarse/diverged.
This stage runs LATER — once a full night has been captured & archived after the coarse solve
— and refits the distortion on a dense multi-frame star union (see refit_distortion.py), then
redeploys. Recalibration then converges (measured: 16/72 -> 62/72 detection-FFs, 2026-08-23).

Run by cron every 15 min alongside auto_tilt_solve.py. Cheap no-op until its guards pass;
runs once, redeploys, disarms. Self-heals across nights: if a night refits poorly (cloud),
it does NOT deploy or disarm, and retries on the next night's archive.

Markers:
  ~/.tilt_autosolve_done  (stage-1, written by auto_tilt_solve.py — pointing solved)
  ~/.tilt_refit_done      (stage-2, written here — distortion refined; disarms this stage)
To re-enable BOTH stages after a future camera move: rm ~/.tilt_autosolve_done ~/.tilt_refit_done
Logs to /mnt/nvme/RMS_data/logs/auto_refit_distortion.log
"""
import os
import sys
import glob
import shutil
import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))   # for refit_distortion + calib_lock (siblings)
sys.path.insert(0, "/home/ian/source/RMS")
import calib_lock

COARSE_MARKER = "/home/ian/.tilt_autosolve_done"
REFIT_MARKER  = "/home/ian/.tilt_refit_done"
REFITTING     = "/home/ian/.tilt_refitting"      # present while a forced (UI-triggered) refit runs
FORCE         = os.environ.get("FORCE") == "1"    # UI "Refit distortion" button: bypass the auto guards
LOG           = "/mnt/nvme/RMS_data/logs/auto_refit_distortion.log"
LIVE_PP       = "/home/ian/source/RMS/platepar_cmn2010.cal"
CONFIG        = "/home/ian/source/RMS/.config"
ARCHIVE       = "/mnt/nvme/RMS_data/ArchivedFiles"

MIN_DARK_FRAMES = 30       # dark-window CALSTARS frames needed to attempt a refit
MAX_DEPLOY_RMSD = 0.60     # px; don't deploy a still-poor fit (cloudy night) -> retry next night


def log(m):
    s = "%s %s" % (datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), m)
    print(s)
    try:
        open(LOG, "a").write(s + "\n")
    except OSError:
        pass


def main():
    if os.path.exists(REFIT_MARKER) and not FORCE:
        return                                  # already refined & deployed; disarmed (FORCE overrides)
    if not os.path.exists(COARSE_MARKER):
        return                                  # need a coarse pointing solve first (even when forced)

    import logging
    logging.disable(logging.CRITICAL)
    import RMS.ConfigReader as cr
    from RMS.Formats import CALSTARS
    from RMS.Formats.Platepar import Platepar
    from refit_distortion import refit_night, sane_refit, find_dark_frames

    # Pick the RICHEST dark-frame archive captured AFTER the coarse solve (never refit
    # pre-move data). Richest-not-newest so a short post-restart fragment doesn't win over
    # the main night, and cloudy/empty-CALSTARS nights are naturally skipped.
    marker_mtime = os.path.getmtime(COARSE_MARKER)
    best_n, night = 0, None
    for cand in glob.glob(os.path.join(ARCHIVE, "UK00DY_*/")):
        if os.path.getmtime(cand) <= marker_mtime:
            continue
        csf = [f for f in os.listdir(cand) if f.startswith("CALSTARS")]
        if not csf:
            continue
        try:
            cs_list, _ = CALSTARS.readCALSTARS(cand, csf[0])
        except Exception:
            continue
        # uncapped dark-window count so the main night beats a short fragment unambiguously
        # (find_dark_frames caps its RETURN at n_frames; here we want the true window size)
        n_dark = len(find_dark_frames([ff for ff, _ in cs_list], n_frames=10**9, min_frames=8))
        if n_dark > best_n:
            best_n, night = n_dark, cand

    floor = 8 if FORCE else MIN_DARK_FRAMES      # forced refit accepts a thinner night (still gated on RMSD)
    if night is None or best_n < floor:
        return                                  # no post-move night with enough dark frames yet

    log("refit attempt on %s (%d dark frames)" % (os.path.basename(night.rstrip("/")), best_n))

    config = cr.parse(CONFIG)
    base = Platepar()
    base.read(LIVE_PP)

    try:
        new, stats = refit_night(night, LIVE_PP, config, verbose=False)
    except Exception as e:
        log("refit_night error: %s" % e)
        return

    if new is None:
        log("refit produced no platepar: %s" % stats.get("error", stats))
        return

    log("refit: BASE %.3f px -> REFIT %.3f px (median %.3f, %d stars, %d frames) F_scale %.4f->%.4f"
        % (stats["base_rmsd"], stats["refit_rmsd"], stats["refit_median"], stats["n_union"],
           stats["n_frames_used"], base.F_scale, new.F_scale))

    if not sane_refit(base, new, stats):
        log("REJECT: failed sanity gate (improved/F_scale/pointing) — not deploying, will retry")
        return
    if stats["refit_rmsd"] > MAX_DEPLOY_RMSD:
        log("REJECT: refit RMSD %.3f px > %.2f (poor night) — not deploying, will retry next night"
            % (stats["refit_rmsd"], MAX_DEPLOY_RMSD))
        return

    stamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    shutil.copy(LIVE_PP, LIVE_PP + ".bak-autorefit-" + stamp)
    new.write(LIVE_PP)
    open(REFIT_MARKER, "w").write(
        "deployed %s UTC  refit_rmsd=%.3f px  base=%.3f  stars=%d frames=%d night=%s\n"
        % (stamp, stats["refit_rmsd"], stats["base_rmsd"], stats["n_union"],
           stats["n_frames_used"], os.path.basename(night.rstrip("/"))))
    log("DEPLOYED refit platepar (backup %s.bak-autorefit-%s). "
        "End-of-night reprocessing adopts it; recalibration will now converge. Stage-2 disarmed."
        % (os.path.basename(LIVE_PP), stamp))


if __name__ == "__main__":
    # Shared lock so no two calibration scripts (solve/refit/bump) — or the cron firing
    # mid-run — ever race on the live platepar. Idempotent: extra clicks are no-ops.
    if not calib_lock.acquire():
        if FORCE:
            log("a calibration is already running — skipping this click")
        sys.exit(0)
    try:
        if FORCE:
            try:
                open(REFITTING, "w").write(datetime.datetime.utcnow().isoformat())
            except OSError:
                pass
        main()
    finally:
        if FORCE:
            try:
                os.remove(REFITTING)
            except OSError:
                pass
        calib_lock.release()
