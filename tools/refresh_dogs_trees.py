"""Build building-stories/dogs.json (licensed dogs by ZIP) and building-stories/trees.json (street trees).

Usage:
  python tools/refresh_dogs_trees.py                          # download both from NYC Open Data
  python tools/refresh_dogs_trees.py DOGS.csv TREES.csv [YEAR] # use files you downloaded yourself

Dogs:  NYC Dog Licensing Dataset (nu7n-tubp), latest yearly snapshot, Manhattan ZIPs.
Trees: Forestry Tree Points (hn5i-inap), living street trees. Each tree is linked to the
       nearest building within 45 meters, which stands in for "the trees out front".
Needs: scipy
"""
import csv, io, json, math, os, re, sys, urllib.request
from collections import Counter, defaultdict
from scipy.spatial import cKDTree

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "building-stories")
API = "https://data.cityofnewyork.us/resource/"

def get_csv(url):
    with urllib.request.urlopen(url, timeout=300) as r:
        return list(csv.DictReader(io.StringIO(r.read().decode("utf-8"))))

dog_year = sys.argv[3] if len(sys.argv) > 3 else None
if len(sys.argv) > 2:
    dogs = list(csv.DictReader(open(sys.argv[1], newline="")))
    trees = list(csv.DictReader(open(sys.argv[2], newline="")))
else:
    yr = dog_year = get_csv(API + "nu7n-tubp.csv?$select=max(extract_year)%20as%20y")[0]["y"]
    dogs = get_csv(API + "nu7n-tubp.csv?$select=zipcode,animalname,breedname,animalgender"
                   f"&$where=extract_year='{yr}'%20AND%20zipcode%20between%20'10001'%20and%20'10282'&$limit=200000")
    trees = get_csv(API + "hn5i-inap.csv?$select=genusspecies,dbh,location"
                    "&$where=tpstructure='Full'%20AND%20within_box(location,40.882,-74.03,40.68,-73.906)&$limit=400000")

# ---------- dogs ----------
BAD = {"UNKNOWN", "NAME NOT PROVIDED", "NAME", "NONE", "N/A", "NA", "NO NAME", "UNKNOWED", "DOG", ""}
def nice_name(n):
    n = " ".join((n or "").split()).upper()
    if n in BAD or not re.search(r"[A-Z]", n):
        return None
    return n.title()
def nice_breed(b):
    b = " ".join((b or "").split())
    if not b or b.lower().startswith("unknown"):
        return None
    b = re.sub(r"\s*Crossbreed$", " mix", b)
    b = b.replace("American Pit Bull Mix / Pit Bull Mix", "Pit bull mix").replace("American Pit Bull Terrier/Pit Bull", "Pit bull")
    return b
names, breeds, total = defaultdict(Counter), defaultdict(Counter), Counter()
for r in dogs:
    z = (r.get("zipcode") or "").strip()[:5]
    if not z.isdigit():
        continue
    total[z] += 1
    n, b = nice_name(r.get("animalname")), nice_breed(r.get("breedname"))
    if n: names[z][n] += 1
    if b: breeds[z][b] += 1
all_names = Counter(); [all_names.update(c) for c in names.values()]
all_breeds = Counter(); [all_breeds.update(c) for c in breeds.values()]
N = sum(total.values())
out = {}
for z in sorted(total):
    if total[z] < 25:
        continue
    # "Unusually popular here": the name most over-represented in this ZIP vs all of Manhattan
    cand = [((c / total[z]) / (all_names[n] / N), n) for n, c in names[z].items() if c >= 4]
    cand = [(l, n) for l, n in cand if l >= 3]
    sig = max(cand)[1] if cand else None
    out[z] = {"dogs": total[z], "names": [n for n, _ in names[z].most_common(3)],
              "breed": breeds[z].most_common(1)[0][0] if breeds[z] else None, "signature": sig}
json.dump({"year": dog_year,
           "manhattan": {"dogs": N, "names": [n for n, _ in all_names.most_common(3)], "breed": all_breeds.most_common(1)[0][0]},
           "zips": out}, open(os.path.join(SITE, "dogs.json"), "w"), separators=(",", ":"))
print(f"Dogs: {N:,} licensed dogs across {len(out)} ZIPs; top names {', '.join(n for n,_ in all_names.most_common(3))}")

# ---------- trees ----------
B = json.load(open(os.path.join(SITE, "buildings.json")))
LAT0, LNG0, KX, KY = B["origin"]
kd = cKDTree(list(zip(B["x"], B["y"])))
species, sp_idx = [], {}
def common(gs):
    c = (gs or "").split(" - ")[-1].strip()
    c = c[:1].upper() + c[1:] if c else "Unknown tree"
    return c
tx, ty, ts, td = [], [], [], []
for r in trees:
    m = re.search(r"POINT \(([-\d.]+) ([-\d.]+)\)", r.get("location") or "")
    if not m:
        continue
    lng, lat = float(m.group(1)), float(m.group(2))
    x, y = (lng - LNG0) * KX, (lat - LAT0) * KY
    d, i = kd.query((x, y))
    if d > 60:            # not next to a Manhattan building: across the river, or deep in a park
        continue
    name = common(r.get("genusspecies"))
    if name not in sp_idx:
        sp_idx[name] = len(species); species.append(name)
    try:
        dbh = int(float(r.get("dbh") or 0))
    except ValueError:
        dbh = 0
    if dbh > 90: dbh = 0  # obvious data-entry errors (e.g. 999)
    tx.append(round(x)); ty.append(round(y)); ts.append(sp_idx[name]); td.append(dbh)
# Trees "out front": within a radius that grows with the lot's size, so big buildings reach their own curb.
tk = cKDTree(list(zip(tx, ty)))
la = B.get("la") or [0] * len(B["x"])
offsets, flat = [0], []
for i in range(len(B["x"])):
    side = math.sqrt(max(la[i], 0) * 0.0929)          # sq ft -> m, then the side of a square lot
    rad = min(100, 22 + side * 0.7)
    near = sorted(tk.query_ball_point((B["x"][i], B["y"][i]), rad))
    flat.extend(near); offsets.append(len(flat))
json.dump({"species": species, "x": tx, "y": ty, "s": ts, "d": td, "o": offsets, "l": flat},
          open(os.path.join(SITE, "trees.json"), "w"), separators=(",", ":"))
print(f"Trees: {len(tx):,} street trees next to Manhattan buildings, {len(species)} species; "
      f"{os.path.getsize(os.path.join(SITE, 'trees.json'))//1024} KB")
