#!/home/ian/vRMS/bin/python
"""refit_distortion.py — shared multi-frame distortion-refit core.

Extracted from refit_tilt_distortion.py (2026-08-23) so both the one-off script and the
auto-solve stage (auto_refit_distortion.py) use the SAME validated logic.

A platepar fit on a single frame under-constrains the radial distortion (corners can be
tens of px off), so per-FF recalibration's tight goodness check rejects every frame. This
refits distortion on a DENSE multi-frame star union: stars drift across the night so the
union samples the whole distorted field. Matched pairs from many dark FFs are rotation-
frozen to one reference time (via alt/az, keeping the TRUE catalog positions), then
Platepar.fitAstrometry refits pose+distortion on the union.

Result on the 2026-08-22 tilt data: union RMSD 1.40 px -> 0.371 px; recalibration then
converges on 62/72 detection-FFs (vs 16/72 on the coarse single-frame fit).
"""
import os
import copy
import numpy as np

from RMS.Formats import CALSTARS, StarCatalog
from RMS.Formats.Platepar import Platepar
from RMS.Formats.FFfile import getMiddleTimeFF, filenameToDatetime
from RMS.Astrometry.Conversions import date2JD, raDec2AltAz, altAz2RADec
from RMS.Astrometry.ApplyAstrometry import raDecToXYPP
from RMS.Astrometry.CheckFit import matchStarsResiduals


# Years since J2000 for catalog proper-motion (2026.6). Kept as a module constant so the
# one-off script and the auto stage agree; refresh loosely each year (sub-arcsec effect).
CATALOG_EPOCH_YEARS = 26.6


def _rmsd(pp, img, catf, jd_ref):
    """RMS/median/p90 pixel residual of a platepar against a matched (img, catalog) union."""
    x, y = raDecToXYPP(catf[:, 0], catf[:, 1], jd_ref, pp)
    d = np.hypot(x - img[:, 0], y - img[:, 1])
    return np.sqrt(np.mean(d**2)), np.median(d), np.percentile(d, 90)


def find_dark_frames(calstars, n_frames=30, min_frames=8):
    """Pick up to n_frames CALSTARS FF names spread across the darkest UTC window, parsing
    the real timestamp from each FF name (NOT substring matching — the zero-padded frame
    counter false-matches hour strings like "_00"). Season-adaptive: prefer 23-01 UTC, then
    widen (22-02, 21-03, ...) until at least min_frames are available; fall back to all.
    Even sub-sampling across the window maximises the star-drift field coverage that
    constrains distortion."""
    hours = {}
    for ff in calstars:
        try:
            hours[ff] = filenameToDatetime(ff).hour
        except Exception:
            continue
    for lo, hi in ((23, 1), (22, 2), (21, 3), (20, 4), (19, 5)):
        # window wraps past midnight: hour >= lo OR hour <= hi
        window = sorted(ff for ff, h in hours.items() if h >= lo or h <= hi)
        if len(window) >= min_frames:
            break
    else:
        window = sorted(calstars)
    step = max(1, len(window)//n_frames)
    return window[::step][:n_frames]


def refit_night(night_dir, base_pp_path, config, n_frames=30, match_radius=2.0,
                distortion="radial7-odd", fixed_scale=False, verbose=True):
    """Refit the distortion of base_pp_path on a multi-frame star union from night_dir.

    Arguments:
        night_dir:    [str] dir containing a CALSTARS file for the night.
        base_pp_path: [str] platepar to refit (pose is kept; distortion re-solved).
        config:       [Config] RMS config (lat/lon/elev, catalog paths, fps).

    Keyword arguments:
        n_frames:     [int] max dark FFs to union.
        match_radius: [float] px; tight to admit only real matches (excludes CALSTARS noise).
        distortion:   [str] distortion model to fit.
        fixed_scale:  [bool] hold F_scale during the fit.
        verbose:      [bool] print progress.

    Return:
        (new_platepar, stats) where stats = dict(n_union, n_frames_used, base_rmsd,
        refit_rmsd, base_median, refit_median, refit_p90, jd_ref). new_platepar is None if
        no usable union could be built.
    """
    lat, lon = config.latitude, config.longitude

    base = Platepar()
    base.read(base_pp_path)
    base.lat, base.lon, base.elev = lat, lon, config.elevation

    csf = [f for f in os.listdir(night_dir) if f.startswith("CALSTARS")]
    if not csf:
        return None, {"error": "no CALSTARS file in %s" % night_dir}
    cs_list, _ = CALSTARS.readCALSTARS(night_dir, csf[0])
    calstars = {ff: np.array(st) for ff, st in cs_list}

    frames = find_dark_frames(calstars, n_frames=n_frames)
    if len(frames) < 3:
        return None, {"error": "only %d dark frames available" % len(frames)}
    if verbose:
        print("union over %d dark frames (of %d), match radius %.1f px"
              % (len(frames), len(calstars), match_radius))

    cat, _, config.star_catalog_band_ratios = StarCatalog.readStarCatalog(
        config.star_catalog_path, config.star_catalog_file,
        years_from_J2000=CATALOG_EPOCH_YEARS, lim_mag=6.5,
        mag_band_ratios=config.star_catalog_band_ratios)

    # reference time = middle frame
    tmid = getMiddleTimeFF(frames[len(frames)//2], config.fps, ret_milliseconds=True)
    jd_ref = date2JD(*tmid)

    img_all, cat_all = [], []
    used = 0
    for ff in frames:
        t = getMiddleTimeFF(ff, config.fps, ret_milliseconds=True)
        jd_k = date2JD(*t)
        n, _, _, matched = matchStarsResiduals(
            config, base, cat, {jd_k: calstars[ff]}, match_radius, ret_nmatch=True)
        if jd_k not in matched:
            continue
        img_stars, cat_stars, _ = matched[jd_k]
        if len(img_stars) == 0:
            continue
        used += 1
        for istar, cstar in zip(img_stars, cat_stars):
            ra, dec, mag = cstar
            az, alt = raDec2AltAz(ra, dec, jd_k, lat, lon)        # true pos @ frame time
            ra_f, dec_f = altAz2RADec(az, alt, jd_ref, lat, lon)  # -> same pixel @ ref time
            # matchStarsResiduals image_stars are [y, x, intensity, ...]; fitAstrometry and
            # raDecToXYPP want [x, y, intensity]
            img_all.append([istar[1], istar[0], istar[2]])
            cat_all.append([ra_f, dec_f, mag])

    if len(img_all) < 20:
        return None, {"error": "union too small (%d matched stars)" % len(img_all)}
    img_all = np.array(img_all)
    cat_all = np.array(cat_all)

    r0 = _rmsd(base, img_all, cat_all, jd_ref)
    new = copy.deepcopy(base)
    new.distortion_type = distortion
    new.fitAstrometry(jd_ref, img_all, cat_all, fixed_scale=fixed_scale)
    r1 = _rmsd(new, img_all, cat_all, jd_ref)

    stats = dict(n_union=len(img_all), n_frames_used=used,
                 base_rmsd=r0[0], base_median=r0[1],
                 refit_rmsd=r1[0], refit_median=r1[1], refit_p90=r1[2], jd_ref=jd_ref)
    if verbose:
        print("BASE  RMSD %.3f px | REFIT %.3f px (median %.3f, p90 %.3f) | %d stars, %d frames"
              % (r0[0], r1[0], r1[1], r1[2], len(img_all), used))
    return new, stats


def sane_refit(base_pp, new_pp, stats):
    """Deploy-safety gate shared by the one-off and auto scripts: the refit must improve
    RMSD, keep a sane 4mm F_scale, and not move the pointing more than a few degrees."""
    return (new_pp is not None
            and stats.get("refit_rmsd", 9e9) < stats.get("base_rmsd", 0)
            and 17.0 < new_pp.F_scale < 22.0
            and abs(new_pp.dec_d - base_pp.dec_d) < 3.0
            and abs(new_pp.RA_d - base_pp.RA_d) < 3.0)
