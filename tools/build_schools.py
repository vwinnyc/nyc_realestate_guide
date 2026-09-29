"""Build building-stories/schools.json: Manhattan school zones and each building's zoned schools.

Usage:
  python tools/build_schools.py ELEMENTARY.geojson MIDDLE.geojson SCHOOLS.csv [ZONE_YEAR]

Inputs come from NYC Open Data (Department of Education):
  elementary zones  cmjf-yawu (2024-25)   middle zones  t26j-jbq7 (2024-25)
  school names      wg9x-4ke6
The DOE publishes a new dataset each school year, so rerun this when new zones come out.
Needs: pip install shapely
"""
import csv, json, math, os, re, sys
from shapely.geometry import shape, Point
from shapely.ops import unary_union, transform
from shapely.strtree import STRtree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "building-stories")
es_path, ms_path, names_path = sys.argv[1:4]
year = sys.argv[4] if len(sys.argv) > 4 else "2024–25"

B = json.load(open(os.path.join(SITE, "buildings.json")))
LAT0, LNG0, KX, KY = B["origin"]
to_m = lambda g: transform(lambda x, y, z=None: ((x - LNG0) * KX, (y - LAT0) * KY), g)

names = {}
for r in csv.DictReader(open(names_path, newline="")):
    names.setdefault(r["system_code"].strip(), (r["location_name"].strip(),
        re.sub(r"(\d)(St|Nd|Rd|Th)\b", lambda m: m.group(1) + m.group(2).lower(), " ".join(r["primary_address_line_1"].split()).title())))

def short(name, dbn):
    m = re.match(r"^(P\.?S\.?|I\.?S\.?|M\.?S\.?|J\.?H\.?S\.?)\s*(?:/\s*I\.?S\.?\s*)?M?0*(\d+)", name, re.I)
    if m:
        kind = re.sub(r"[^A-Z]", "", m.group(1).upper())
        kind = {"PS": "P.S.", "IS": "I.S.", "MS": "M.S.", "JHS": "J.H.S."}.get(kind, kind)
        return f"{kind} {int(m.group(2))}"
    return name if len(name) <= 26 else name[:24].rstrip() + "…"

def clean_remark(t):
    if not t:
        return ""
    t = re.sub(r"\s*Contact [\d-]+ for more information\.?", "", t).strip()
    return t

def zones(path, level):
    feats = json.load(open(path))["features"]
    out = []
    for f in feats:
        p = f["properties"]
        g = shape(f["geometry"]).buffer(0)
        if g.is_empty:
            continue
        dbns = [d.strip() for d in (p.get("dbn") or "").split(",") if d.strip()]
        found = [(d, *names[d]) for d in dbns if d in names]
        dbn = ",".join(dbns)
        nm = " and ".join(f[1] for f in found)
        addr = " and ".join(f[2] for f in found) if len(found) == 1 else ""
        schools = [{"dbn": f[0], "name": f[1], "addr": f[2]} for f in found]
        remark = clean_remark(p.get("remarks"))
        dist = (p.get("zoned_dist") or "").split(".")[0]
        if found:
            label = " / ".join(short(f[1], f[0]) for f in found)
        elif "No zoned school" in remark or "Choice" in remark:
            label = f"District {dist} choice"
        else:
            label = remark or ""
        gm = to_m(g).simplify(4, preserve_topology=True)
        polys = gm.geoms if hasattr(gm, "geoms") else [gm]
        rings = []
        for pg in polys:
            for ring in [pg.exterior, *pg.interiors]:
                rings.append([int(round(v)) for xy in ring.coords for v in xy])
        rp = gm.representative_point()
        out.append({"level": level, "dbn": dbn, "schools": schools, "label": label,
                    "remark": remark, "district": dist, "rings": rings,
                    "lx": int(rp.x), "ly": int(rp.y), "_g": g})
    return out

Z = zones(es_path, "es") + zones(ms_path, "ms")

def assign(level):
    zs = [z for z in Z if z["level"] == level]
    tree = STRtree([z["_g"] for z in zs])
    idx = [Z.index(z) for z in zs]
    res = []
    for i in range(len(B["x"])):
        lng = B["x"][i] / KX + LNG0
        lat = B["y"][i] / KY + LAT0
        pt = Point(lng, lat)
        hit = -1
        for j in tree.query(pt):
            if zs[j]["_g"].covers(pt):
                hit = idx[j]
                break
        res.append(hit)
    return res

es, ms = assign("es"), assign("ms")
for z in Z:
    del z["_g"]
out = {"year": year, "zones": Z, "es": es, "ms": ms}
path = os.path.join(SITE, "schools.json")
json.dump(out, open(path, "w"), separators=(",", ":"), ensure_ascii=False)
print(f"{sum(z['level']=='es' for z in Z)} elementary and {sum(z['level']=='ms' for z in Z)} middle zones; "
      f"{sum(e >= 0 for e in es)}/{len(es)} buildings in an elementary zone, {sum(m >= 0 for m in ms)} in a middle zone; "
      f"{os.path.getsize(path)//1024} KB")
missing = sorted({d for z in Z for d in z["dbn"].split(",") if d and d not in names})
if missing:
    print("Zone codes with no school name found:", ", ".join(missing))
