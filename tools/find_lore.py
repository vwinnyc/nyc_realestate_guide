"""Grow the map's whimsical stamp categories with Parallel.

Categories (the stamp filters on the map):
  oncewas    Once was: buildings that started life as something else
  song       Song lines: places named in songs
  written    Written here: where famous books, poems or plays were written
  tradition  Odd bylaws & traditions: publicly reported quirks and customs

Each run:
  1. Merges tools/lore_seed.json into stories.json (only adds what's missing, never removes).
  2. For the chosen categories, asks Parallel's FindAll API for a list of places, then runs one
     Task per new place to get its address, year and sourced facts.
  3. Matches each place to a map lot (by address, then by coordinates), adds or updates stories,
     and appends a section to story-review.md. Nothing is published until you merge the pull request.

Cost (Parallel's published rates): FindAll "core" is $2 per list plus $0.15 per match, and each
Task on "core" is $0.025. One category with 10 matches is about $3.75. By default the script
does one category per month, rotating; use --category all for everything at once.

Env:   PARALLEL_API_KEY (not needed for --dry-run)
Usage: python tools/find_lore.py [--category oncewas|song|written|tradition|all|auto|seed] [--limit 10] [--dry-run]
       --category seed only adds the starter entries, with no Parallel calls.
"""
import argparse, datetime as dt, json, math, os, re, sys, time, urllib.error, urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "building-stories")
STORIES = os.path.join(SITE, "stories.json")
BUILDINGS = os.path.join(SITE, "buildings.json")
SEEN = os.path.join(SITE, "lore_seen.json")
SEED = os.path.join(ROOT, "tools", "lore_seed.json")
SUMMARY = os.path.join(ROOT, "story-review.md")
API = "https://api.parallel.ai"
TODAY = dt.date.today()

STYLE = ("Write each fact as one or two plain sentences, 35 words or fewer, in active voice. Do not use em dashes. "
         "Only include facts supported by a reliable source (newspapers, magazines, books, museums, landmark reports, "
         "official sites, Wikipedia). Never include information about current private residents, and never name a "
         "private person who is not a public figure.")

CATS = {
  "oncewas": dict(label="Once was",
    objective=("FindAll buildings in Manhattan, New York City, that were built for one use and now serve a clearly "
               "different one, for example a firehouse turned into a home, a church turned into a nightclub or condos, "
               "a bank turned into a store, or a factory turned into apartments."),
    conditions=[("in_manhattan", "The building stands in the borough of Manhattan, New York City, today."),
                ("conversion_documented", "A reliable source documents the building's original use and a later, different use.")],
    ask="Explain what the building was built as, what it became, and roughly when it changed."),
  "song": dict(label="Song lines",
    objective=("FindAll specific Manhattan places (buildings, addresses, bridges, intersections or venues) that are named "
               "in the title or lyrics of a well-known published song."),
    conditions=[("in_manhattan", "The place is in the borough of Manhattan, New York City."),
                ("named_in_song", "A reliable source confirms the place is named in a published song's title or lyrics.")],
    ask="Name the song, the artist and the year, and say how the place appears in it. Do not quote more than a few words of lyrics."),
  "written": dict(label="Written here",
    objective=("FindAll buildings in Manhattan, New York City, still standing today, where a famous book, poem, play or "
               "essay was written, according to biographies, literary guides or historic plaques."),
    conditions=[("in_manhattan", "The building stands in the borough of Manhattan, New York City, today."),
                ("writing_documented", "A reliable source says a specific well-known work was written, at least in part, at this building.")],
    ask="Name the writer, the work and the year, and anything charming about how it was written there."),
  "tradition": dict(label="Odd bylaws & traditions",
    objective=("FindAll buildings or long-running businesses in Manhattan, New York City, known for a quirky, publicly "
               "reported tradition, custom or house rule, such as a rooftop Halloween party, a lobby holiday display, "
               "a resident animal, an unusual co-op bylaw, or a decades-old ritual."),
    conditions=[("in_manhattan", "The building or business is in the borough of Manhattan, New York City."),
                ("publicly_reported", "The tradition or rule was reported by a newspaper, magazine, book or the building's own public materials."),
                ("about_the_place", "The tradition is about the building or business itself, not about a named private resident.")],
    ask="Describe the tradition or house rule, when it started if known, and how it works today."),
}
ORDER = ["oncewas", "song", "written", "tradition"]

FACT = {"type": "object", "additionalProperties": False, "required": ["text", "source_url"],
        "properties": {"text": {"type": "string"}, "source_url": {"type": "string"}}}
PLACE_SCHEMA = {"type": "object", "additionalProperties": False,
  "required": ["in_manhattan", "still_standing", "name", "street_address", "latitude", "longitude", "year_built", "facts"],
  "properties": {
    "in_manhattan": {"type": "boolean", "description": "True only if the place is in the borough of Manhattan"},
    "still_standing": {"type": "boolean", "description": "True if the building or place exists today"},
    "name": {"type": "string", "description": "Common name of the building or place, or its address if it has none"},
    "street_address": {"type": "string", "description": "House number and street, e.g. '171 Fifth Avenue'. For an intersection or bridge, give its name."},
    "latitude": {"type": "number"}, "longitude": {"type": "number"},
    "year_built": {"type": "integer", "description": "Year the building was completed, or 0 if unknown or not a building"},
    "facts": {"type": "array", "items": FACT, "description": "One to three facts about this category"}}}


# ---------------------------------------------------------------- Parallel calls
def http(method, path, key, body=None, timeout=120):
    req = urllib.request.Request(API + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"x-api-key": key, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))

def findall(cat, limit, key):
    c = CATS[cat]
    run = http("POST", "/v1beta/findall/runs", key, {
        "objective": c["objective"], "entity_type": "places",
        "match_conditions": [{"name": n, "description": d} for n, d in c["conditions"]],
        "generator": "core", "match_limit": limit})
    fid = run["findall_id"]
    for _ in range(120):  # up to about 40 minutes
        st = http("GET", f"/v1beta/findall/runs/{fid}", key)["status"]
        if not st.get("is_active", True):
            break
        time.sleep(20)
    res = http("GET", f"/v1beta/findall/runs/{fid}/result", key)
    return [{"name": x.get("name", ""), "description": x.get("description", ""), "url": x.get("url", "")}
            for x in res.get("candidates", []) if x.get("match_status") == "matched"]

def task(prompt, key):
    run = http("POST", "/v1/tasks/runs", key, {"processor": "core", "input": prompt,
        "task_spec": {"output_schema": {"type": "json", "json_schema": PLACE_SCHEMA}}})
    for _ in range(6):
        try:
            res = http("GET", f"/v1/tasks/runs/{run['run_id']}/result?timeout=600", key, timeout=660)
            break
        except urllib.error.HTTPError as e:
            if e.code != 408:
                raise
    else:
        raise TimeoutError(run["run_id"])
    out = res.get("output", {})
    content = out.get("content")
    if isinstance(content, str):
        content = json.loads(content)
    low = any(b.get("confidence") == "low" and (b.get("field") or "").startswith("facts") for b in out.get("basis", []))
    return content, low


# ---------------------------------------------------------------- matching to map lots
ORD = {"FIRST": "1", "SECOND": "2", "THIRD": "3", "FOURTH": "4", "FIFTH": "5", "SIXTH": "6",
       "SEVENTH": "7", "EIGHTH": "8", "NINTH": "9", "TENTH": "10", "ELEVENTH": "11", "TWELFTH": "12"}
ABBR = {"ST": "STREET", "AVE": "AVENUE", "AV": "AVENUE", "PL": "PLACE", "W": "WEST", "E": "EAST",
        "BLVD": "BOULEVARD", "DR": "DRIVE", "SQ": "SQUARE", "TER": "TERRACE", "PKWY": "PARKWAY"}
def norm(addr):
    a = re.sub(r"[.,#]", " ", (addr or "").upper())
    a = re.sub(r"\b(NEW YORK|NY|MANHATTAN)\b.*$", "", a)
    t = a.split()
    if not t or not re.match(r"^\d", t[0]):
        return []
    t[0] = re.split(r"[-–/]", t[0])[0]
    out = []
    for i, w in enumerate(t):
        if w in ORD: w = ORD[w]
        w = re.sub(r"^(\d+)(ST|ND|RD|TH)$", r"\1", w)
        if w in ABBR and not (w == "ST" and i + 1 < len(t) and i == 1):  # "ST" right after the number = Saint
            w = ABBR[w]
        out.append(w)
    s = " ".join(out)
    alts = {s, s.replace("6 AVENUE", "AVENUE OF THE AMERICAS"), s.replace("AVENUE OF THE AMERICAS", "6 AVENUE")}
    return [x for x in alts if x]

class Lots:
    def __init__(self, B):
        self.B = B
        self.LAT0, self.LNG0, self.KX, self.KY = B["origin"]
        self.by_addr = {}
        for i, ad in enumerate(B["ad"]):
            self.by_addr.setdefault(" ".join(ad.upper().split()), i)
    def xy(self, lat, lng):
        return (lng - self.LNG0) * self.KX, (lat - self.LAT0) * self.KY
    def match(self, address, lat, lng):
        for a in norm(address):
            if a in self.by_addr:
                return self.by_addr[a], "address"
        if lat and lng and 40.68 < lat < 40.89 and -74.05 < lng < -73.9:
            x, y = self.xy(lat, lng)
            best, bd = None, 1e18
            for i in range(len(self.B["x"])):
                d = (self.B["x"][i] - x) ** 2 + (self.B["y"][i] - y) ** 2
                if d < bd: best, bd = i, d
            if math.sqrt(bd) <= 35:
                return best, "coordinates"
        return None, None


# ---------------------------------------------------------------- stories helpers
def clean_facts(facts, low):
    if low:
        return []
    keep = []
    for f in facts or []:
        t = re.sub(r"\s*[—–]\s*", ", ", (f.get("text") or "").strip())
        u = (f.get("source_url") or "").strip()
        if t and u.startswith("http"):
            keep.append({"text": t, "source_url": u})
    return keep[:3]

def add_to_story(s, cat, facts):
    changed = False
    if cat not in s["tags"]:
        s["tags"].append(cat); changed = True
    for f in facts:
        if f["text"] not in s["facts"]:
            s.setdefault("sources", [s.get("src", "")] * len(s["facts"]))
            s["facts"].append(f["text"]); s["sources"].append(f["source_url"]); changed = True
    return changed

def merge_seed(stories):
    if not os.path.exists(SEED):
        return 0
    seed = json.load(open(SEED)); by = {s["name"]: s for s in stories}; n = 0
    for u in seed.get("updates", []):
        s = by.get(u["name"])
        if not s: continue
        facts = [{"text": u["fact"], "source_url": u.get("src") or s.get("src", "")}] if u.get("fact") else []
        for t in u["tags"]:
            n += add_to_story(s, t, facts if t == u["tags"][-1] else [])
    for s in seed.get("new", []):
        if s["name"] not in by:
            stories.append(dict(s, added=TODAY.isoformat(), by="seed")); n += 1
    return n


# ---------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--category", default="auto", help="oncewas, song, written, tradition, all, or auto (rotate monthly)")
    ap.add_argument("--limit", type=int, default=10, help="places to find per category")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    key = os.environ.get("PARALLEL_API_KEY", "")
    if a.category == "seed":
        cats = []   # only merge tools/lore_seed.json; no Parallel calls, no cost
    elif not key and not a.dry_run:
        sys.exit("PARALLEL_API_KEY is not set. Add it as a repository secret.")
    else:
        cats = ORDER if a.category == "all" else [ORDER[TODAY.month % 4]] if a.category == "auto" else [a.category]

    stories = json.load(open(STORIES))
    seen = set(json.load(open(SEEN))) if os.path.exists(SEEN) else set()
    lots = Lots(json.load(open(BUILDINGS)))
    seeded = merge_seed(stories)
    by_bbl = {s["bbl"]: s for s in stories if s.get("bbl")}
    by_name = {s["name"].lower(): s for s in stories}

    report = {c: {"new": [], "updated": [], "skipped": []} for c in cats}
    for cat in cats:
        print(f"Finding '{CATS[cat]['label']}' places...")
        if a.dry_run:
            cands = [{"name": "Example Firehouse (dry run)", "description": "", "url": ""}]
        else:
            try:
                cands = findall(cat, a.limit, key)
            except Exception as e:
                print(f"  ! FindAll failed for {cat}: {e}", file=sys.stderr); continue
        cands = [c for c in cands if f"{cat}:{c['name'].lower()}" not in seen]
        print(f"  {len(cands)} new places to research")

        def research(c):
            prompt = (f"Place: {c['name']}, Manhattan, New York City. Context: {c['description']} {c['url']}\n"
                      f"Topic: {CATS[cat]['label']}. {CATS[cat]['ask']} Give its street address and coordinates. {STYLE}")
            if a.dry_run:
                return c, {"in_manhattan": True, "still_standing": True, "name": c["name"], "street_address": "87 Lafayette Street",
                           "latitude": 0, "longitude": 0, "year_built": 1895,
                           "facts": [{"text": "Example fact from a dry run, not real.", "source_url": "https://example.com"}]}, False
            try:
                content, low = task(prompt, key)
                return c, content, low
            except Exception as e:
                print(f"  ! Task failed for {c['name']}: {e}", file=sys.stderr)
                return c, None, False

        with ThreadPoolExecutor(max_workers=5) as ex:
            results = list(ex.map(research, cands))

        for c, r, low in results:
            if r is None:
                continue
            seen.add(f"{cat}:{c['name'].lower()}")
            facts = clean_facts(r.get("facts"), low)
            if not r.get("in_manhattan") or not r.get("still_standing") or not facts:
                report[cat]["skipped"].append(r.get("name") or c["name"]); continue
            i, how = lots.match(r.get("street_address"), r.get("latitude"), r.get("longitude"))
            bbl = lots.B["bbl"][i] if i is not None else None
            existing = by_bbl.get(bbl) if bbl else by_name.get((r.get("name") or "").lower())
            if existing:
                if add_to_story(existing, cat, facts):
                    report[cat]["updated"].append((existing["name"], facts))
                continue
            st = {"name": r.get("name") or c["name"], "tags": [cat], "year": int(r.get("year_built") or 0),
                  "facts": [f["text"] for f in facts], "sources": [f["source_url"] for f in facts],
                  "src": facts[0]["source_url"], "added": TODAY.isoformat(), "by": "parallel"}
            if bbl:
                st["bbl"] = bbl
            elif r.get("latitude") and r.get("longitude"):
                st["lat"], st["lng"] = round(r["latitude"], 6), round(r["longitude"], 6)  # a bridge or intersection
            else:
                report[cat]["skipped"].append(st["name"] + " (couldn't place it on the map)"); continue
            stories.append(st)
            if bbl: by_bbl[bbl] = st
            by_name[st["name"].lower()] = st
            report[cat]["new"].append(st)

    json.dump(stories, open(STORIES, "w"), ensure_ascii=False, indent=0)
    json.dump(sorted(seen), open(SEEN, "w"), ensure_ascii=False, indent=0)

    lines = ["", "---", "", f"# Whimsy stamps, {TODAY:%B %-d, %Y}", ""]
    if seeded:
        lines += [f"Added {seeded} starter entries from `tools/lore_seed.json`.", ""]
    changes = seeded
    for cat in cats:
        rep = report[cat]; changes += len(rep["new"]) + len(rep["updated"])
        lines += [f"## {CATS[cat]['label']}", ""]
        for st in rep["new"]:
            lines.append(f"### New: {st['name']}")
            lines += [f"- {t} ([source]({u}))" for t, u in zip(st["facts"], st["sources"])] + [""]
        for name, facts in rep["updated"]:
            lines.append(f"### Added to: {name}")
            lines += [f"- {f['text']} ([source]({f['source_url']}))" for f in facts] + [""]
        if rep["skipped"]:
            lines += [f"Researched but not added: {', '.join(rep['skipped'])}", ""]
        if not (rep["new"] or rep["updated"] or rep["skipped"]):
            lines += ["Nothing new this time.", ""]
    with open(SUMMARY, "a") as fh:
        fh.write("\n".join(lines))
    print(f"Done: {changes} changes.")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"changes={changes}\n")

if __name__ == "__main__":
    main()
