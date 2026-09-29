"""Build building-stories/pools.json from NYC Health Department pool inspection records.

Usage:
  python tools/refresh_pools.py                 # download the latest records from NYC Open Data
  python tools/refresh_pools.py pools.csv       # use a CSV you downloaded yourself

pools.json maps each map lot (BBL) to [types, last inspected (YYYY-MM), facility names].
"""
import csv, io, json, os, re, sys, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "building-stories")
URL = ("https://data.cityofnewyork.us/resource/3kfa-rvez.csv?"
       "$select=bbl,facility_name,permit_type,max(inspection_date)%20as%20last_inspected"
       "&$where=bo='MA'&$group=bbl,facility_name,permit_type&$order=bbl&$limit=50000")

if len(sys.argv) > 1:
    rows = list(csv.DictReader(open(sys.argv[1], newline="")))
else:
    with urllib.request.urlopen(URL, timeout=120) as r:
        rows = list(csv.DictReader(io.StringIO(r.read().decode("utf-8"))))

B = json.load(open(os.path.join(SITE, "buildings.json")))
known = set(B["bbl"])
# Condo buildings are one "billing" lot (75xx) on the map but individual unit lots in some records.
billing = {}
for b in B["bbl"]:
    blk, lot = divmod(b, 10000)
    if 7500 <= lot <= 7599:
        billing.setdefault(blk, []).append(b)

def to_map_bbl(raw):
    try:
        b = int(float(raw))
    except (TypeError, ValueError):
        return None
    if b in known:
        return b
    blk, lot = divmod(b, 10000)
    if 1000 <= lot <= 6999 and len(billing.get(blk, [])) == 1:
        return billing[blk][0]
    return None

pools, missed = {}, 0
for r in rows:
    b = to_map_bbl(r.get("bbl"))
    if b is None:
        missed += 1
        continue
    p = pools.setdefault(b, [[], "", []])
    t = (r.get("permit_type") or "").strip()
    if t and t not in p[0]:
        p[0].append(t)
    last = (r.get("last_inspected") or "")[:7]
    if last > p[1]:
        p[1] = last
    name = " ".join((r.get("facility_name") or "").split())
    key = lambda n: re.sub(r"[^A-Z0-9]", "", n.upper())
    if name and key(name) not in [key(n) for n in p[2]]:
        p[2].append(name)
for p in pools.values():
    p[0].sort()
    p[2] = p[2][:2]

out = os.path.join(SITE, "pools.json")
json.dump({str(k): v for k, v in sorted(pools.items())}, open(out, "w"), separators=(",", ":"))
print(f"{len(pools)} Manhattan buildings with pools ({missed} records couldn't be matched to a map lot)")
