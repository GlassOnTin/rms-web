#!/usr/bin/env python3
"""
make_neighbours_map.py — render the 'neighbours' map for the RMS dashboard:
OSM basemap + this camera's ~100 km-altitude footprint + nearby GMN stations,
with a few real GMN 100 km field-of-view polygons drawn to show sky overlap,
and stations classified by triangulation baseline (the real discriminator).

Outputs next to this script: neighbours_map.png, neighbours.json
Data: uk_stations.csv (code,lat,lon = 25 km FOV centroid) + GMN <code>-100km.kml.
Re-run to refresh.
"""
import math, os, io, json, time, sys, re, urllib.request
from concurrent.futures import ThreadPoolExecutor
import matplotlib; matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Polygon as MplPoly
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
# Station location — override via env for other stations (defaults: Denmead, Hampshire).
MLAT = float(os.environ.get("STATION_LAT", "50.8987692"))
MLON = float(os.environ.get("STATION_LON", "-1.0586827"))
STATION_NAME = os.environ.get("STATION_NAME", "Denmead")
USER_R_KM = float(os.environ.get("STATION_FOOTPRINT_KM", "100.0"))
KML = "https://globalmeteornetwork.org/data/kml_fov/{}-100km.kml"
TILE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
UA = {"User-Agent": "rms-neighbours-map/1.0 (personal meteor station dashboard; +https://github.com/)"}
Z = 8
N_DRAW = 6      # neighbour FOV polygons to draw (readability)
PALETTE = ["#1c7ed6","#f76707","#ae3ec9","#0c8599","#e8590c","#5f3dc4"]

def hav(a,b,c,d):
    R=6371.0;p=math.radians
    x=math.sin(p(c-a)/2)**2+math.cos(p(a))*math.cos(p(c))*math.sin(p(d-b)/2)**2
    return 2*R*math.asin(math.sqrt(x))
def bearing(a,b,c,d):
    p=math.radians
    y=math.sin(p(d-b))*math.cos(p(c));x=math.cos(p(a))*math.sin(p(c))-math.sin(p(a))*math.cos(p(c))*math.cos(p(d-b))
    return ['N','NE','E','SE','S','SW','W','NW'][round(((math.degrees(math.atan2(y,x))+360)%360)/45)%8]
def dest(lat,lon,brg,dkm):
    R=6371.0;p=math.radians;d=dkm/R;la=p(lat);lo=p(lon);b=p(brg)
    la2=math.asin(math.sin(la)*math.cos(d)+math.cos(la)*math.sin(d)*math.cos(b))
    lo2=lo+math.atan2(math.sin(b)*math.sin(d)*math.cos(la),math.cos(d)-math.sin(la)*math.sin(la2))
    return math.degrees(la2),math.degrees(lo2)
def merc(lat,lon):
    R=6378137.0
    return math.radians(lon)*R, math.log(math.tan(math.pi/4+math.radians(max(min(lat,85),-85))/2))*R
def deg2tile(lat,lon,z):
    n=2**z; return (lon+180)/360*n,(1-math.log(math.tan(math.radians(lat))+1/math.cos(math.radians(lat)))/math.pi)/2*n
def tile2deg(xt,yt,z):
    n=2**z; return math.degrees(math.atan(math.sinh(math.pi*(1-2*yt/n)))), xt/n*360-180
def band(d):
    return "close" if d<30 else ("prime" if d<=150 else "far")
BAND_COL={"close":"#e8590c","prime":"#2f9e44","far":"#adb5bd"}

# --- optional: refresh UK station list from GMN (picks up new stations) -----
def refresh_stations():
    base = "https://globalmeteornetwork.org/data/kml_fov/"
    idx = urllib.request.urlopen(urllib.request.Request(base, headers=UA), timeout=60).read().decode("utf-8","replace")
    files = sorted(set(re.findall(r'(UK[0-9A-Za-z]+-25km\.kml)', idx)))
    def centroid(f):
        try:
            txt = urllib.request.urlopen(urllib.request.Request(base+f, headers=UA), timeout=15).read().decode("utf-8","replace")
            i = txt.find("<coordinates>"); j = txt.find("</coordinates>", i)
            if i < 0 or j < 0: return None
            lat = lon = 0.0; n = 0
            for tok in txt[i+13:j].split():
                p = tok.split(",")
                if len(p) >= 2:
                    try: lon += float(p[0]); lat += float(p[1]); n += 1
                    except: pass
            if n: return f"{f.split('-')[0]},{lat/n:.5f},{lon/n:.5f}"
        except Exception:
            return None
        return None
    with ThreadPoolExecutor(max_workers=16) as ex:
        rows = [r for r in ex.map(centroid, files) if r]
    if len(rows) >= 100:   # sanity: never clobber the good CSV with a partial/failed fetch
        with open(os.path.join(HERE, "uk_stations.csv"), "w") as fh:
            fh.write("\n".join(rows) + "\n")
        return len(rows)
    return 0

if "--refresh" in sys.argv:
    got = refresh_stations()
    print(f"refresh: {'updated' if got else 'SKIPPED (kept existing)'} uk_stations.csv ({got} stations)")

# --- stations ---------------------------------------------------------------
st=[]
for line in open(os.path.join(HERE,"uk_stations.csv")):
    try:
        c,la,lo=line.split(","); la=float(la); lo=float(lo)
        st.append({"code":c,"lat":la,"lon":lo,"dist":hav(MLAT,MLON,la,lo),
                   "brg":bearing(MLAT,MLON,la,lo)})
    except: pass
st.sort(key=lambda s:s["dist"])
for s in st: s["band"]=band(s["dist"])

# --- fetch first FOV ring for the nearest N_DRAW ----------------------------
def fetch_poly(code):
    try:
        req=urllib.request.Request(KML.format(code), headers=UA)
        txt=urllib.request.urlopen(req, timeout=15).read().decode("utf-8","replace")
        i=txt.find("<coordinates>"); j=txt.find("</coordinates>", i)
        if i<0 or j<0: return None
        block=txt[i+13:j]
        pts=[]
        for tok in block.split():
            p=tok.split(",")
            if len(p)>=2:
                try: pts.append((float(p[1]), float(p[0])))
                except: pass
        return pts if len(pts)>=3 else None
    except Exception:
        return None
draw=st[:N_DRAW]
with ThreadPoolExecutor(max_workers=8) as ex:
    for s,p in zip(draw, ex.map(fetch_poly,[s["code"] for s in draw])):
        s["poly"]=p

# --- OSM basemap ------------------------------------------------------------
lon_min,lon_max=MLON-3.2,MLON+3.2; lat_min,lat_max=MLAT-2.0,MLAT+2.0
x0,y0=deg2tile(lat_max,lon_min,Z); x1,y1=deg2tile(lat_min,lon_max,Z)
xt0,yt0=int(x0),int(y0); xt1,yt1=int(x1),int(y1)
os.makedirs(os.path.join(HERE,"osm_cache"), exist_ok=True)
def get_tile(tx,ty):
    cp=os.path.join(HERE,"osm_cache",f"{Z}_{tx}_{ty}.png")
    if os.path.exists(cp): return tx,ty,Image.open(cp).convert("RGB")
    try:
        req=urllib.request.Request(TILE.format(z=Z,x=tx,y=ty), headers=UA)
        data=urllib.request.urlopen(req, timeout=20).read(); open(cp,"wb").write(data); time.sleep(0.05)
        return tx,ty,Image.open(io.BytesIO(data)).convert("RGB")
    except Exception:
        return tx,ty,Image.new("RGB",(256,256),(235,235,235))
tiles=[(tx,ty) for tx in range(xt0,xt1+1) for ty in range(yt0,yt1+1)]
mosaic=Image.new("RGB",((xt1-xt0+1)*256,(yt1-yt0+1)*256),(235,235,235))
with ThreadPoolExecutor(max_workers=8) as ex:
    for tx,ty,img in ex.map(lambda t:get_tile(*t), tiles):
        mosaic.paste(img,((tx-xt0)*256,(ty-yt0)*256))
latNW,lonNW=tile2deg(xt0,yt0,Z); latSE,lonSE=tile2deg(xt1+1,yt1+1,Z)
xW,yN=merc(latNW,lonNW); xE,yS=merc(latSE,lonSE)

# --- plot -------------------------------------------------------------------
fig,ax=plt.subplots(figsize=(12,12))
ax.imshow(mosaic, extent=[xW,xE,yS,yN], origin="upper", zorder=0)
# fade basemap slightly for legibility
ax.add_patch(plt.Rectangle((xW,yS),xE-xW,yN-yS, color="white", alpha=0.18, zorder=1))
mx,my=merc(MLAT,MLON)

# your footprint
ring=[merc(*dest(MLAT,MLON,b,USER_R_KM)) for b in range(0,361,3)]
ax.fill([p[0] for p in ring],[p[1] for p in ring], color="#e23b3b", alpha=0.10, zorder=2)
ax.plot([p[0] for p in ring],[p[1] for p in ring], color="#e23b3b", lw=2.0, zorder=5,
        label=f"your sky at ~100 km alt (~{USER_R_KM:.0f} km radius)")

# a few neighbour FOV polygons to SHOW the overlap concretely
for k,s in enumerate(draw):
    if not s.get("poly"): continue
    pm=[merc(la,lo) for (la,lo) in s["poly"]]
    col=PALETTE[k%len(PALETTE)]
    ax.add_patch(MplPoly(pm, closed=True, facecolor=col, edgecolor=col, alpha=0.14, lw=1.4, zorder=3))

# range rings
for r in (50,100,150,200):
    pts=[merc(*dest(MLAT,MLON,b,r)) for b in range(0,361,3)]
    ax.plot([p[0] for p in pts],[p[1] for p in pts], ls=":", lw=0.8, color="#343a40", zorder=4)
    e=merc(*dest(MLAT,MLON,90,r)); ax.text(e[0],e[1],f"{r}km", fontsize=7, ha="center", zorder=6,
        bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="none", alpha=0.7))

# station markers by baseline band (nearest 14 labelled)
for s in st[:14]:
    px,py=merc(s["lat"],s["lon"])
    ax.scatter([px],[py], s=66, marker="o", c=BAND_COL[s["band"]], edgecolor="k", linewidth=0.6, zorder=6)
    ax.annotate(f"{s['code']} {s['dist']:.0f}km", (px,py), textcoords="offset points", xytext=(6,4),
                fontsize=8, fontweight="bold", zorder=7,
                bbox=dict(boxstyle="round,pad=0.1", fc="white", ec="none", alpha=0.7))
# home
ax.scatter([mx],[my], marker="*", s=470, c="#e23b3b", edgecolor="k", linewidth=0.9, zorder=8)
ax.annotate(STATION_NAME.upper(), (mx,my), textcoords="offset points", xytext=(9,-16), fontsize=11,
            fontweight="bold", color="#b81f1f", zorder=8,
            bbox=dict(boxstyle="round,pad=0.15", fc="white", ec="none", alpha=0.75))

n50=sum(1 for s in st if s["dist"]<=50); n100=sum(1 for s in st if s["dist"]<=100)
n200=sum(1 for s in st if s["dist"]<=200); nprime=sum(1 for s in st if s["band"]=="prime")
ax.set_xlim(xW,xE); ax.set_ylim(yS,yN); ax.set_aspect("equal"); ax.set_xticks([]); ax.set_yticks([])
ax.set_title(f"Meteor-network neighbours of {STATION_NAME} — your sky overlaps every UK station within ~200 km "
             f"({n200} of them); {nprime} at prime 30–150 km baseline", fontsize=12)
# band legend
from matplotlib.lines import Line2D
leg=[Line2D([0],[0],marker='*',color='w',markerfacecolor='#e23b3b',markersize=15,label=f'{STATION_NAME} (you)'),
     Line2D([0],[0],marker='o',color='w',markerfacecolor=BAND_COL['close'],markersize=9,label='<30 km (great overlap, weak parallax)'),
     Line2D([0],[0],marker='o',color='w',markerfacecolor=BAND_COL['prime'],markersize=9,label='30–150 km (prime triangulation)'),
     Line2D([0],[0],marker='o',color='w',markerfacecolor=BAND_COL['far'],markersize=9,label='>150 km'),
     Line2D([0],[0],color='#e23b3b',lw=2,label='your ~100 km-alt footprint'),
     Line2D([0],[0],color=PALETTE[0],lw=2,alpha=0.6,label='neighbour FOV (100 km alt) — overlap')]
ax.legend(handles=leg, loc="upper left", fontsize=8.5, framealpha=0.9)
ax.text(0.995,0.005,"© OpenStreetMap contributors · FOV data: Global Meteor Network",
        transform=ax.transAxes, ha="right", va="bottom", fontsize=7, color="#343a40",
        bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="none", alpha=0.75))
plt.tight_layout(); plt.savefig(os.path.join(HERE,"neighbours_map.png"), dpi=110)

out={"lat":MLAT,"lon":MLON,"user_footprint_km":USER_R_KM,"total_uk":len(st),
     "within_50":n50,"within_100":n100,"within_200":n200,"prime_30_150":nprime,
     "nearest":[{"code":s["code"],"dist":round(s["dist"],1),"brg":s["brg"],
                 "lat":round(s["lat"],4),"lon":round(s["lon"],4),"band":s["band"]} for s in st[:20]]}
json.dump(out, open(os.path.join(HERE,"neighbours.json"),"w"), indent=1)
print(f"done: within200={n200} prime={nprime}; wrote neighbours_map.png + neighbours.json")
