#!/usr/bin/env python3
"""
sky_overlay.py — annotate tonight-preview images with bright star names, the
Moon and naked-eye planets, projected through the live RMS platepar (so labels
land exactly where the calibration pipeline puts those objects — if a label
drifts off its star, the astrometry has shifted).

Stars: embedded table of IAU-named bright stars (J2000, Vmag <= ~2.5) — no
catalog files needed. Moon/planets: pyephem, topocentric, converted to J2000
to match RMS's convention (raDecToXYPP precesses J2000 -> epoch internally).

Note a zenith camera at 50.9N rarely sees planets (ecliptic tops out ~62 deg
altitude here, just inside the FOV's long axis) and the Moon only at high
declination — they're included and simply don't draw when outside the frame.
"""
import os, sys
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, "/home/ian/source/RMS")
import ephem
from RMS.Astrometry.ApplyAstrometry import raDecToXYPP
from RMS.Astrometry.Conversions import date2JD, altAz2RADec, raDec2AltAz
from RMS.Formats import Platepar

OBLIQUITY = np.radians(23.4393)          # J2000 mean obliquity, for the ecliptic line

PLATEPAR_PATH = os.environ.get("RMS_PLATEPAR", "/home/ian/source/RMS/platepar_cmn2010.cal")

# (name, RA J2000 deg, Dec J2000 deg, Vmag) — IAU names, mag <= ~2.5, southern
# never-risers left out. Projection clips to the FOV so extras are harmless.
STARS = [
    ("Sirius",     101.287, -16.716, -1.46), ("Arcturus",  213.915,  19.182, -0.05),
    ("Vega",       279.234,  38.784,  0.03), ("Capella",    79.172,  45.998,  0.08),
    ("Rigel",       78.634,  -8.202,  0.13), ("Procyon",   114.826,   5.225,  0.34),
    ("Betelgeuse",  88.793,   7.407,  0.50), ("Altair",    297.696,   8.868,  0.77),
    ("Aldebaran",   68.980,  16.509,  0.85), ("Spica",     201.298, -11.161,  0.97),
    ("Pollux",     116.329,  28.026,  1.14), ("Deneb",     310.358,  45.280,  1.25),
    ("Regulus",    152.093,  11.967,  1.35), ("Castor",    113.650,  31.888,  1.58),
    ("Elnath",      81.573,  28.608,  1.65), ("Alioth",    193.507,  55.960,  1.77),
    ("Dubhe",      165.932,  61.751,  1.79), ("Mirfak",     51.081,  49.861,  1.80),
    ("Alkaid",     206.885,  49.313,  1.86), ("Menkalinan", 89.882,  44.947,  1.90),
    ("Alhena",      99.428,  16.399,  1.92), ("Polaris",    37.955,  89.264,  1.98),
    ("Hamal",       31.793,  23.462,  2.00), ("Mirach",     17.433,  35.621,  2.05),
    ("Alpheratz",    2.097,  29.090,  2.06), ("Rasalhague",263.734,  12.560,  2.07),
    ("Kochab",     222.676,  74.156,  2.08), ("Algol",      47.042,  40.956,  2.12),
    ("Denebola",   177.265,  14.572,  2.13), ("Sadr",      305.557,  40.257,  2.23),
    ("Eltanin",    269.152,  51.489,  2.23), ("Schedar",    10.127,  56.537,  2.24),
    ("Alphecca",   233.672,  26.715,  2.24), ("Almach",     30.975,  42.330,  2.26),
    ("Mizar",      200.981,  54.926,  2.27), ("Caph",        2.295,  59.150,  2.27),
    ("Merak",      165.460,  56.383,  2.37), ("Enif",      326.046,   9.875,  2.39),
    ("Scheat",     345.944,  28.083,  2.42), ("Phecda",    178.458,  53.695,  2.44),
    ("Alderamin",  319.645,  62.585,  2.46), ("Markab",    346.190,  15.205,  2.49),
]

PLANETS = [  # (label, ephem body factory)
    ("Moon",    ephem.Moon),
    ("Venus",   ephem.Venus),
    ("Mars",    ephem.Mars),
    ("Jupiter", ephem.Jupiter),
    ("Saturn",  ephem.Saturn),
]

STAR_COL, PLANET_COL, MOON_COL = (120, 200, 255), (255, 210, 90), (255, 255, 255)
LINE_COL, CARD_COL = (200, 120, 220), (255, 140, 100)

_pp, _pp_mtime = None, 0
def _platepar():
    """Live platepar, reloaded if RMS recalibration rewrites it."""
    global _pp, _pp_mtime
    m = os.path.getmtime(PLATEPAR_PATH)
    if _pp is None or m != _pp_mtime:
        _pp = Platepar.Platepar()
        _pp.read(PLATEPAR_PATH, use_flat=None)
        _pp_mtime = m
    return _pp

_font = None
def _getfont():
    global _font
    if _font is None:
        try:
            _font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 15)
        except Exception:
            _font = ImageFont.load_default()
    return _font

def _bodies_j2000(dt, pp):
    """Topocentric positions of Moon+planets at dt (UTC), converted to J2000."""
    obs = ephem.Observer()
    obs.lat, obs.lon = str(pp.lat), str(pp.lon)
    obs.elevation = float(pp.elev)
    obs.date = dt.strftime("%Y/%m/%d %H:%M:%S")
    obs.pressure = 0                     # no refraction here; the platepar model applies its own
    out = []
    for label, factory in PLANETS:
        b = factory()
        b.compute(obs)
        if float(b.alt) < 0.6:           # radians; skip anything below ~35 deg — nowhere near a zenith FOV
            continue
        eq = ephem.Equatorial(b.ra, b.dec, epoch=obs.date)      # topocentric, of-date
        eq2 = ephem.Equatorial(eq, epoch=ephem.J2000)           # -> J2000 for raDecToXYPP
        out.append((label, np.degrees(float(eq2.ra)), np.degrees(float(eq2.dec))))
    return out

def annotate(img, dt):
    """Return an RGB copy of `img` (PIL, L or RGB) labelled for UTC time `dt`."""
    pp = _platepar()
    jd = date2JD(dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second)
    img = img.convert("RGB")
    W, H = img.size
    dr = ImageDraw.Draw(img)
    font = _getfont()

    def mark(x, y, label, col, r=7, big=False):
        if not (r < x < W - r and r < y < H - r):
            return
        dr.ellipse([x - r, y - r, x + r, y + r], outline=col, width=2 if big else 1)
        tx, ty = x + r + 3, y - 9
        if tx + 8 * len(label) > W:      # keep text inside the right edge
            tx = x - r - 3 - 8 * len(label)
        dr.text((tx + 1, ty + 1), label, fill=(0, 0, 0), font=font)   # shadow for contrast
        dr.text((tx, ty), label, fill=col, font=font)

    # altitude-filter BEFORE projecting: the distortion model happily back-projects
    # below-horizon stars into frame coordinates (Rigel in August, etc.)
    names = [s[0] for s in STARS]
    ra = np.array([s[1] for s in STARS]); dec = np.array([s[2] for s in STARS])
    _, alt = raDec2AltAz(ra, dec, jd, pp.lat, pp.lon)
    up = alt > 40.0                      # zenith FOV bottoms out ~50 deg; margin below
    xs, ys = raDecToXYPP(ra[up], dec[up], jd, pp)
    for n, x, y in zip([n for n, u in zip(names, up) if u], xs, ys):
        mark(float(x), float(y), n, STAR_COL)

    for label, bra, bdec in _bodies_j2000(dt, pp):
        x, y = raDecToXYPP(np.array([bra]), np.array([bdec]), jd, pp)
        col = MOON_COL if label == "Moon" else PLANET_COL
        mark(float(x[0]), float(y[0]), label, col, r=14 if label == "Moon" else 9, big=True)

    # --- ecliptic: dotted line, exact (J2000 ecliptic -> equatorial; projection precesses)
    lam = np.radians(np.arange(0.0, 360.0, 1.5))
    e_ra = np.degrees(np.arctan2(np.sin(lam) * np.cos(OBLIQUITY), np.cos(lam))) % 360.0
    e_dec = np.degrees(np.arcsin(np.sin(lam) * np.sin(OBLIQUITY)))
    _, e_alt = raDec2AltAz(e_ra, e_dec, jd, pp.lat, pp.lon)
    e_ra, e_dec = e_ra[e_alt > 25.0], e_dec[e_alt > 25.0]   # same back-projection guard
    ex, ey = raDecToXYPP(e_ra, e_dec, jd, pp)
    pts = [(float(a), float(b)) for a, b in zip(ex, ey) if -50 < a < W + 50 and -50 < b < H + 50]
    for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
        if (x2 - x1) ** 2 + (y2 - y1) ** 2 < 90 ** 2:      # skip discontinuities/wraps
            dr.line([x1, y1, x2, y2], fill=LINE_COL, width=1)
    if pts:
        mid = pts[len(pts) // 2]
        dr.text((mid[0] + 4, mid[1] + 4), "ecliptic", fill=LINE_COL, font=font)

    # --- north celestial pole (feed the J2000 pole; of-date offset is ~3 px here)
    px, py = raDecToXYPP(np.array([0.0]), np.array([90.0]), jd, pp)
    x, y = float(px[0]), float(py[0])
    if 10 < x < W - 10 and 10 < y < H - 10:
        dr.line([x - 8, y, x + 8, y], fill=MOON_COL, width=1)
        dr.line([x, y - 8, x, y + 8], fill=MOON_COL, width=1)
        dr.text((x + 10, y - 9), "NCP", fill=MOON_COL, font=font)

    # --- cardinal directions: rays from the zenith toward az 0/90/180/270, lettered
    # at the frame edge. altAz2RADec returns of-date coords; fed as J2000 that's a
    # ~0.36 deg (~7 px) slant — invisible at letter scale.
    zra, zdec = altAz2RADec(0.0, 90.0, jd, pp.lat, pp.lon)
    zx, zy = raDecToXYPP(np.array([zra]), np.array([zdec]), jd, pp)
    zx, zy = float(zx[0]), float(zy[0])
    for az, letter in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
        cra, cdec = altAz2RADec(float(az), 70.0, jd, pp.lat, pp.lon)
        cx, cy = raDecToXYPP(np.array([cra]), np.array([cdec]), jd, pp)
        dx, dy = float(cx[0]) - zx, float(cy[0]) - zy
        n = (dx * dx + dy * dy) ** 0.5
        if n < 1:
            continue
        dx, dy = dx / n, dy / n
        # walk the ray to the frame border, keep a margin for the letter
        ts = []
        if dx: ts += [((W - 24) - zx) / dx, (24 - zx) / dx]
        if dy: ts += [((H - 24) - zy) / dy, (24 - zy) / dy]
        t = min(t for t in ts if t > 0)
        lx, ly = zx + dx * t, zy + dy * t
        dr.text((lx + 1, ly + 1), letter, fill=(0, 0, 0), font=font)
        dr.text((lx, ly), letter, fill=CARD_COL, font=font)
    return img
