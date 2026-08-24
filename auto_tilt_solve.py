#!/home/ian/vRMS/bin/python
"""
auto_tilt_solve.py — durable one-shot platepar solver for the 30-deg north tilt.

Run by cron every 15 min. Self-guards so it's cheap when it can't act, and only
does real work on a genuinely clear window. On the first clear night after the
tilt it blind-solves with RMS autoFitPlatepar, validates the result against
strict sanity gates (pointing near the tilt, sane scale, enough matched stars),
and if it passes, backs up and deploys the platepar — then writes a marker so it
never solves again. End-of-night reprocessing picks up the deployed platepar
(getPlatepar reads config_file_path), so NO capture restart is needed.

This is STAGE-1 (fast coarse pointing). auto_refit_distortion.py is STAGE-2: once a full night
has been captured & archived it refits the distortion on a multi-frame star union and redeploys
(this single-frame coarse fit has under-constrained distortion, so per-FF recalibration won't
converge until stage-2 runs). Stage-2 writes its own marker ~/.tilt_refit_done.

To re-enable BOTH stages after a future camera move:
    rm ~/.tilt_autosolve_done ~/.tilt_refit_done
Logs to /mnt/nvme/RMS_data/logs/auto_tilt_solve.log
"""
import os, sys, glob, shutil, re, time, datetime, subprocess
sys.path.insert(0, "/home/ian/source/RMS")

MARKER  = "/home/ian/.tilt_autosolve_done"
SOLVING = "/home/ian/.tilt_solving"         # present while a forced (UI-triggered) solve runs
FORCE   = os.environ.get("FORCE") == "1"     # UI "Re-solve platepar" button: bypass the auto guards
LOG     = "/mnt/nvme/RMS_data/logs/auto_tilt_solve.log"
LIVE_PP = "/home/ian/source/RMS/platepar_cmn2010.cal"
CONFIG  = "/home/ian/source/RMS/.config"
WORK    = "/home/ian/platesolve_4mm"
CAP     = "/mnt/nvme/RMS_data/CapturedFiles"
STAR_MIN = 70               # lowered from 85 (2026-08-22): clear night, live count ~68-70 after the intensity_threshold fix
ALT_RANGE, AZ_TOL = (50.0, 70.0), 45.0      # 30-deg north tilt -> alt~60, az within 45 of north
FSCALE_RANGE, MATCH_MIN = (17.0, 22.0), 40  # 4mm lens ~19.67 px/deg; enough matched stars

def log(m):
    s = "%s %s" % (datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), m)
    print(s)
    try:
        open(LOG, "a").write(s + "\n")
    except OSError:
        pass

def recent_star_max():
    """Cheap clear-check from the newest capture log's recent detections."""
    logs = sorted(glob.glob("/mnt/nvme/RMS_data/logs/log_UK00DY_*.log"), key=os.path.getmtime)
    if not logs:
        return 0
    out = subprocess.run(["tail", "-500", logs[-1]], capture_output=True, text=True).stdout
    counts = [int(m) for m in re.findall(r"Detected stars: (\d+)", out)]
    return max(counts[-25:]) if counts else 0

def main():
    if os.path.exists(MARKER) and not FORCE:
        return                                  # already solved & deployed; disarmed (FORCE overrides)

    # must be actively capturing (newest FF fresh) — i.e. night, camera live
    nights = sorted(glob.glob(os.path.join(CAP, "UK00DY_*/")), key=os.path.getmtime)
    if not nights:
        return
    night = nights[-1]
    ffs = sorted(glob.glob(os.path.join(night, "FF_*.fits")), key=os.path.getmtime)
    if not ffs or (time.time() - os.path.getmtime(ffs[-1]) > 900):
        return                                  # not capturing

    if not FORCE and recent_star_max() < STAR_MIN:
        return                                  # not clear enough; retry next tick (quiet)

    log("%s (recent max %d stars) — attempting blind solve"
        % ("FORCED solve (UI button)" if FORCE else "clear window", recent_star_max()))

    import numpy as np
    import RMS.ConfigReader as cr
    from RMS.Formats import FFfile
    from scipy.ndimage import maximum_filter
    from RMS.Astrometry.AutoPlatepar import autoFitPlatepar, loadCatalogStars
    from RMS.Astrometry.Conversions import raDec2AltAz

    # stage the richest of the last few frames + a 64x50 fov config, extract stars
    if os.path.isdir(WORK):
        shutil.rmtree(WORK)
    os.makedirs(WORK)
    def crude(ff):
        img = ff.avepixel.astype(float); d = img[:, img.shape[1]//2:]
        bg = np.median(d); n = max(1.4826*np.median(np.abs(d - bg)), 1.0)
        pk = (img == maximum_filter(img, 7)) & (img > bg + 6*n)
        return int(pk[6:-6, 6:-6].sum())
    best = max(ffs[-8:], key=lambda f: crude(FFfile.read(os.path.dirname(f), os.path.basename(f))))
    shutil.copy(best, WORK)
    txt = open(CONFIG).read()
    txt = re.sub(r'(?im)^(\s*fov_w\s*:\s*)\S+', r'\g<1>64', txt)
    txt = re.sub(r'(?im)^(\s*fov_h\s*:\s*)\S+', r'\g<1>50', txt)
    open(os.path.join(WORK, ".config"), "w").write(txt)
    os.system("cd /home/ian/source/RMS && /home/ian/vRMS/bin/python -m RMS.ExtractStars "
              "%s -c %s/.config >/dev/null 2>&1" % (WORK, WORK))

    config = cr.parse(os.path.join(WORK, ".config"))
    cat = loadCatalogStars(config, 6.5)
    try:
        pp, matched, _ = autoFitPlatepar(WORK, config, cat, fov_w_hint=64, wide_fov_search=True, verbose=False)
    except Exception as e:
        log("autoFitPlatepar error: %s" % e); return
    if pp is None:
        log("solve: no match on best frame %s" % os.path.basename(best)); return

    az, alt = raDec2AltAz(pp.RA_d, pp.dec_d, pp.JD, pp.lat, pp.lon)
    nmatch = len(matched) if matched is not None else 0
    az_off = min(abs(az % 360), abs((az % 360) - 360))
    ok = (ALT_RANGE[0] <= alt <= ALT_RANGE[1] and az_off <= AZ_TOL
          and FSCALE_RANGE[0] <= pp.F_scale <= FSCALE_RANGE[1] and nmatch >= MATCH_MIN)
    log("solve: az=%.2f alt=%.2f F_scale=%.2f px/deg matched=%d -> %s"
        % (az, alt, pp.F_scale, nmatch, "PASS" if ok else "REJECT (failed sanity gate)"))
    if not ok:
        return                                  # don't deploy a dubious solve; try again next clear window

    stamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    shutil.copy(LIVE_PP, LIVE_PP + ".bak-autotilt-" + stamp)
    pp.write(LIVE_PP)
    open(MARKER, "w").write("deployed %s UTC  az=%.2f alt=%.2f F_scale=%.3f matched=%d ref=%s\n"
                            % (stamp, az, alt, pp.F_scale, nmatch, os.path.basename(best)))
    log("DEPLOYED tilt platepar (backup %s.bak-autotilt-%s). "
        "Applies at end-of-night reprocessing. Auto-solve disarmed (marker written)."
        % (os.path.basename(LIVE_PP), stamp))

if __name__ == "__main__":
    if FORCE:
        try:
            open(SOLVING, "w").write(datetime.datetime.utcnow().isoformat())
        except OSError:
            pass
        try:
            main()
        finally:
            try:
                os.remove(SOLVING)
            except OSError:
                pass
    else:
        main()
