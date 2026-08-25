#!/home/ian/vRMS/bin/python
"""
bump_solve.py — recover the platepar after a camera BUMP (a knock / PSU-swap nudge),
using the correspondence method that actually works on this wide fisheye and on
partly-cloudy data. Triggered by the /tonight "Fix camera bump" button (POST /bump_solve).

Unlike auto_tilt_solve (astrometry.net blind solve — which CANNOT solve this cheap wide
fisheye: its quad-matcher finds no solution even at ~100 stars, and cloud noise makes it
worse) this recovers a KNOWN-approximate pointing after a small move:

  1. GRID-SEARCH a small (dRA, dDec, dRot) reseat of the CURRENT platepar, scoring
     catalog<->detection matches on the richest (clearest) frame. Robust to cloud noise:
     random noise won't cohere to the catalog pattern, a real star field will.
  2. Fit pointing+roll only (fit_only_pointing, fixed_scale) — a bump is a rigid rotation,
     it changes where the camera looks and its roll, NOT the lens distortion, so the good
     pre-bump distortion is kept.
  3. Multi-frame UNION distortion refit over the richest (clearest) frames to restore the
     sub-0.5 px distortion that per-FF recalibration needs to converge.

Sanity-gated: deploys (with backup) only if the union is large, the RMSD is good and
improved, the scale is sane, and the move is plausibly a bump. Capture is untouched —
getPlatepar re-reads the platepar at the next night's capture start / next reprocess.

IDEMPOTENT: an atomic O_EXCL lock (~/.tilt_bumpsolving) makes rapid or repeated clicks a
no-op — only the first proceeds; the rest exit immediately. A stale lock (>20 min, from a
crashed run) is reclaimed. The UI also hides the button while the lock is present.

On success it writes the auto markers (~/.tilt_autosolve_done + ~/.tilt_refit_done) so the
cron auto-solve stays disarmed. Re-arm after a future move: rm those two markers.
Logs to /mnt/nvme/RMS_data/logs/bump_solve.log
"""
import os, sys, glob, copy, errno, datetime, time
sys.path.insert(0, "/home/ian/source/RMS")
import numpy as np

LOCK    = "/home/ian/.tilt_bumpsolving"          # atomic run-lock (idempotency)
LOCK_STALE_S = 20 * 60
AUTOSOLVE_MARKER = "/home/ian/.tilt_autosolve_done"
REFIT_MARKER     = "/home/ian/.tilt_refit_done"
LIVE_PP = "/home/ian/source/RMS/platepar_cmn2010.cal"
CONFIG  = "/home/ian/source/RMS/.config"
LOG     = "/mnt/nvme/RMS_data/logs/bump_solve.log"
ARCHIVED = "/mnt/nvme/RMS_data/ArchivedFiles"
CAPTURED = "/mnt/nvme/RMS_data/CapturedFiles"

# --- sanity gates (this touches the LIVE science platepar) ---
MIN_PEAK_STARS = 45        # richest frame must have this many detections to attempt a solve
GRID_NBRIGHT   = 60        # brightest detections used to score the grid / seed the fit
UNION_FRAMES   = 15        # richest frames unioned for the distortion refit
G_MIN_UNION    = 120       # union must have >= this many matched stars to trust a radial7 fit
G_MAX_RMSD     = 1.0       # px; refit RMSD must be at/below this
G_MAX_P90      = 2.0       # px; refit 90th-percentile residual ceiling
G_FSCALE       = (17.0, 22.0)
G_MAX_MOVE_DEG = 20.0      # a bump is small; reject a wild (probably wrong) solution
G_MIN_MATCH    = 25        # refit must match >= this many stars within 2 px (proves the solve is real)
G_MATCH_IMPROVE = 8        # ...and beat the current platepar by >= this many stars (else nothing to fix:
                           # re-running when already solved is a correct no-op, and small bumps pass)


def log(m):
    s = "%s %s" % (datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"), m)
    print(s)
    try:
        open(LOG, "a").write(s + "\n")
    except OSError:
        pass


def acquire_lock():
    """Atomic O_EXCL create; reclaim a stale lock from a crashed run. Returns True if held."""
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, datetime.datetime.utcnow().isoformat().encode())
        os.close(fd)
        return True
    except OSError as e:
        if e.errno != errno.EEXIST:
            raise
        try:
            if time.time() - os.path.getmtime(LOCK) > LOCK_STALE_S:
                log("reclaiming stale lock (>%dmin)" % (LOCK_STALE_S // 60))
                os.remove(LOCK)
                return acquire_lock()
        except OSError:
            pass
        return False


def find_night(config):
    """Newest Captured/Archived dir with a CALSTARS whose richest frame clears MIN_PEAK_STARS."""
    from RMS.Formats import CALSTARS
    dirs = glob.glob(os.path.join(ARCHIVED, "UK00DY_*")) + glob.glob(os.path.join(CAPTURED, "UK00DY_*"))
    # newest by CAPTURE START (the UK00DY_YYYYMMDD_HHMMSS dirname sorts chronologically) — not by
    # mtime, which reflects reprocessing and can rank an earlier segment "newer". Newest-first keeps
    # temporal correctness: a bump's fix must come from post-bump (latest) data, not stale pointing.
    for d in sorted(dirs, key=os.path.basename, reverse=True):
        if not os.path.isdir(d):                 # glob also catches *_metadata.tar.bz2 etc.
            continue
        csf = [f for f in os.listdir(d) if f.startswith("CALSTARS")]
        if not csf:
            continue
        per = dict(CALSTARS.readCALSTARS(d, csf[0])[0])
        if not per:
            continue
        peak = max(len(v) for v in per.values())
        if peak >= MIN_PEAK_STARS:
            return d, per, peak
        log("skip %s (peak %d < %d)" % (os.path.basename(d), peak, MIN_PEAK_STARS))
    return None, None, 0


def main():
    import RMS.ConfigReader as cr
    from RMS.Formats import StarCatalog
    from RMS.Formats.FFfile import getMiddleTimeFF
    from RMS.Formats.Platepar import Platepar
    from RMS.Astrometry.Conversions import date2JD, raDec2AltAz, altAz2RADec
    from RMS.Astrometry.ApplyAstrometry import raDecToXYPP

    config = cr.parse(CONFIG)
    W, H = config.width, config.height
    lat, lon = config.latitude, config.longitude

    night, per, peak = find_night(config)
    if night is None:
        log("ABORT: no night with a CALSTARS clearing %d stars — wait for a clearer sky" % MIN_PEAK_STARS)
        return
    ranked = sorted(per, key=lambda k: -len(per[k]))
    log("night %s  peak %d stars  (%d frames)" % (os.path.basename(night), peak, len(per)))

    live = Platepar(); live.read(LIVE_PP)

    cat, _, _ = StarCatalog.readStarCatalog(
        config.star_catalog_path, config.star_catalog_file,
        lim_mag=6.5, mag_band_ratios=config.star_catalog_band_ratios)
    cra, cdec, cmag = cat[:, 0], cat[:, 1], cat[:, 2]

    def fxy(ff, nbright=None):
        a = np.array(per[ff], float)
        c0, c1 = a[:, 0], a[:, 1]
        ys, xs = (c0, c1) if c1.max() > c0.max() else (c1, c0)   # CALSTARS row is [y, x, intensity, ...]
        if nbright is not None:
            idx = np.argsort(-a[:, 2])[:nbright]
            xs, ys = xs[idx], ys[idx]
        tk = getMiddleTimeFF(ff, config.fps, ret_milliseconds=True, dt_obj=True)
        jd = date2JD(tk.year, tk.month, tk.day, tk.hour, tk.minute, tk.second, tk.microsecond / 1000.)
        return jd, xs, ys

    def project(pp, jd):
        px, py = raDecToXYPP(cra, cdec, jd, pp)
        m = (px > 0) & (px < W) & (py > 0) & (py < H)
        return px[m], py[m], cra[m], cdec[m], cmag[m]

    def count_match(pp, jd, xs, ys, tol):
        px, py, _, _, _ = project(pp, jd)
        if len(px) == 0:
            return 0
        return int(sum(1 for x, y in zip(xs, ys) if np.min((px - x) ** 2 + (py - y) ** 2) < tol * tol))

    # ---- 1. grid-search the reseat that best matches the richest frame ----
    ref = ranked[0]
    jd0, gx, gy = fxy(ref, GRID_NBRIGHT)
    base_match = count_match(live, jd0, gx, gy, 4.0)

    def score(dRA, dDec, dRot, tol=4.0):
        pp = copy.deepcopy(live)
        pp.RA_d = live.RA_d + dRA; pp.dec_d = live.dec_d + dDec
        pp.pos_angle_ref = live.pos_angle_ref + dRot
        return count_match(pp, jd0, gx, gy, tol)

    best = (base_match, 0.0, 0.0, 0.0)
    for dRA in np.arange(-12, 12.01, 2.0):
        for dDec in np.arange(-12, 12.01, 2.0):
            for dRot in np.arange(-10, 10.01, 2.0):
                s = score(dRA, dDec, dRot)
                if s > best[0]:
                    best = (s, dRA, dDec, dRot)
    s0, rA, rD, rR = best
    for dRA in np.arange(rA - 2, rA + 2.01, 0.5):
        for dDec in np.arange(rD - 2, rD + 2.01, 0.5):
            for dRot in np.arange(rR - 2, rR + 2.01, 0.5):
                s = score(dRA, dDec, dRot)
                if s > best[0]:
                    best = (s, dRA, dDec, dRot)
    s, rA, rD, rR = best
    log("grid: baseline %d/%d -> best %d/%d matches at dRA=%.1f dDec=%.1f dRot=%.1f"
        % (base_match, len(gx), s, len(gx), rA, rD, rR))
    if s < G_MIN_MATCH:
        log("ABORT: no coherent shift found (best %d < %d matches) — not enough real stars "
            "(too cloudy), wait for a clearer sky" % (s, G_MIN_MATCH))
        return

    # ---- 2. pointing+roll fit (keep distortion & scale) ----
    cand = copy.deepcopy(live)
    cand.RA_d += rA; cand.dec_d += rD; cand.pos_angle_ref += rR
    px, py, pra, pdec, pmag = project(cand, jd0)
    _, xs0, ys0 = fxy(ref)
    img, cats = [], []
    for x, y in zip(xs0, ys0):
        d2 = (px - x) ** 2 + (py - y) ** 2
        j = int(np.argmin(d2))
        if d2[j] < 6.0 ** 2:
            img.append((x, y, 1.0)); cats.append((pra[j], pdec[j], pmag[j]))
    if len(img) < 8:
        log("ABORT: only %d pairs for the pointing fit" % len(img))
        return
    cand.fitAstrometry(jd0, np.array(img), np.array(cats), fit_only_pointing=True, fixed_scale=True)

    # ---- 3. union distortion refit over the richest (clearest) frames ----
    jd_ref, _, _ = fxy(ref)
    img_all, cat_all, used = [], [], 0
    for ff in ranked[:UNION_FRAMES]:
        jd, xs, ys = fxy(ff)
        cx, cy, cr, cd, cm = project(cand, jd)
        matched = 0
        for x, y in zip(xs, ys):
            d2 = (cx - x) ** 2 + (cy - y) ** 2
            j = int(np.argmin(d2))
            if d2[j] < 2.5 ** 2:
                az, alt = raDec2AltAz(cr[j], cd[j], jd, lat, lon)       # true pos @ frame time
                ra_f, dec_f = altAz2RADec(az, alt, jd_ref, lat, lon)   # -> same pixel @ ref time
                img_all.append([x, y, 1.0]); cat_all.append([ra_f, dec_f, cm[j]]); matched += 1
        if matched:
            used += 1
    if len(img_all) < G_MIN_UNION:
        log("ABORT: union too small (%d matched stars < %d)" % (len(img_all), G_MIN_UNION))
        return
    img_all = np.array(img_all); cat_all = np.array(cat_all)

    def rmsd(pp):
        qx, qy = raDecToXYPP(cat_all[:, 0], cat_all[:, 1], jd_ref, pp)
        dd = np.hypot(qx - img_all[:, 0], qy - img_all[:, 1])
        return dd.mean(), np.median(dd), np.percentile(dd, 90)

    reseat_rmsd = rmsd(cand)[0]
    refit = copy.deepcopy(cand)
    refit.distortion_type = "radial7-odd"
    refit.fitAstrometry(jd_ref, img_all, cat_all, fixed_scale=True)
    r_mean, r_med, r_p90 = rmsd(refit)

    # physical move (great-circle old->new pointing at the same instant)
    la = raDec2AltAz(live.RA_d, live.dec_d, jd_ref, lat, lon)
    na = raDec2AltAz(refit.RA_d, refit.dec_d, jd_ref, lat, lon)
    move = np.degrees(np.arccos(np.clip(
        np.sin(np.radians(la[1])) * np.sin(np.radians(na[1])) +
        np.cos(np.radians(la[1])) * np.cos(np.radians(na[1])) * np.cos(np.radians(la[0] - na[0])), -1, 1)))
    refit_match = count_match(refit, jd0, *fxy(ref)[1:], tol=2.0)

    log("refit: union %d stars/%d frames | RMSD %.3f->%.3f px (med %.3f p90 %.3f) | "
        "F=%.4f | move %.2f deg | match %d/%d (was %d)"
        % (len(img_all), used, reseat_rmsd, r_mean, r_med, r_p90, refit.F_scale, move,
           refit_match, len(gx), base_match))

    # ---- sanity gates ----
    fails = []
    if r_mean > G_MAX_RMSD:                 fails.append("RMSD %.3f > %.2f" % (r_mean, G_MAX_RMSD))
    if r_mean >= reseat_rmsd:               fails.append("union did not improve RMSD")
    if r_p90 > G_MAX_P90:                   fails.append("p90 %.3f > %.2f" % (r_p90, G_MAX_P90))
    if not (G_FSCALE[0] < refit.F_scale < G_FSCALE[1]):
        fails.append("F_scale %.3f out of %s" % (refit.F_scale, G_FSCALE))
    if move > G_MAX_MOVE_DEG:               fails.append("move %.1f > %.0f deg" % (move, G_MAX_MOVE_DEG))
    if refit_match < G_MIN_MATCH:           fails.append("refit match %d < %d (not real)" % (refit_match, G_MIN_MATCH))
    if refit_match < base_match + G_MATCH_IMPROVE:
        fails.append("no improvement over current platepar (%d vs %d) — not bumped, nothing to fix"
                     % (refit_match, base_match))
    if fails:
        log("DECLINED (no deploy): " + "; ".join(fails))
        return

    # ---- deploy: backup + atomic replace + disarm markers ----
    stamp = datetime.datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    bak = LIVE_PP + ".bak-bumpsolve-" + stamp
    import shutil
    shutil.copy2(LIVE_PP, bak)
    tmp = LIVE_PP + ".tmp-bumpsolve"
    refit.write(tmp)
    os.replace(tmp, LIVE_PP)                 # atomic; never leaves a half-written live platepar
    note = ("deployed %s UTC  RA=%.3f Dec=%.3f F=%.4f  RMSD=%.3fpx  move=%.2fdeg  from %s\n"
            % (stamp, refit.RA_d, refit.dec_d, refit.F_scale, r_mean, move, os.path.basename(night)))
    for mk in (AUTOSOLVE_MARKER, REFIT_MARKER):
        try:
            open(mk, "w").write(note)
        except OSError:
            pass
    log("DEPLOYED bump-corrected platepar (backup %s). Reprocess/next-night capture will adopt it."
        % os.path.basename(bak))


if __name__ == "__main__":
    if not acquire_lock():
        log("already running (lock held) — skipping this click")
        sys.exit(0)
    try:
        main()
    except Exception:
        import traceback
        log("ERROR: " + traceback.format_exc().replace("\n", " | ")[-800:])
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass
