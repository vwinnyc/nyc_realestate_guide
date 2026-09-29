"""Build building-stories/markets.json from the city's farmers market list.

Usage:
  python tools/refresh_markets.py               # download the latest list from NYC Open Data
  python tools/refresh_markets.py markets.csv   # use a CSV you downloaded yourself

Keeps only the most recent year in the data, so markets that stopped running drop off.
"""
import csv, io, json, os, sys, urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "building-stories")
URL = ("https://data.cityofnewyork.us/resource/8vwk-6iz2.csv?"
       "$where=borough='Manhattan'&$order=year%20DESC&$limit=5000")

if len(sys.argv) > 1:
    rows = list(csv.DictReader(open(sys.argv[1], newline="")))
else:
    with urllib.request.urlopen(URL, timeout=120) as r:
        rows = list(csv.DictReader(io.StringIO(r.read().decode("utf-8"))))

def yr(r):
    try:
        return int(float(r.get("year") or 0))
    except ValueError:
        return 0

latest = max(yr(r) for r in rows)
out = []
for r in rows:
    if yr(r) != latest:
        continue
    try:
        lat, lng = float(r["latitude"]), float(r["longitude"])
    except (KeyError, ValueError):
        continue
    clean = lambda k: " ".join((r.get(k) or "").split())
    out.append({
        "name": clean("marketname"), "addr": clean("streetaddress"),
        "days": clean("daysoperation"), "hours": clean("hoursoperations"),
        "season": clean("season_begin"),
        "ebt": clean("accepts_ebt").lower() == "yes",
        "yearRound": clean("open_year_round").lower() == "yes",
        "lat": round(lat, 6), "lng": round(lng, 6),
    })
out.sort(key=lambda m: m["name"])
json.dump({"year": latest, "markets": out}, open(os.path.join(SITE, "markets.json"), "w"),
          ensure_ascii=False, separators=(",", ":"))
print(f"{len(out)} Manhattan farmers markets for {latest}")
