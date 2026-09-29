"""Refresh Manhattan Building Stories with Parallel's Task API.

Each run does two jobs, then writes stories.json and a review summary:
  1. Check existing stories for recent news (oldest-checked first).
  2. Research new buildings: NYC landmarks on the map that don't have a story yet.

Nothing publishes on its own. The GitHub Action turns the changes into a pull
request; merging it is what puts the new facts on the map.

Env:  PARALLEL_API_KEY (required unless --dry-run)
Usage: python tools/refresh_stories.py [--new 10] [--check 10] [--processor core] [--dry-run]
"""
import argparse, datetime as dt, json, os, re, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "building-stories")
STORIES = os.path.join(SITE, "stories.json")
BUILDINGS = os.path.join(SITE, "buildings.json")
RESEARCHED = os.path.join(SITE, "researched.json")  # lots checked that had no story
SUMMARY = os.path.join(ROOT, "story-review.md")
API = "https://api.parallel.ai/v1/tasks/runs"
TODAY = dt.date.today()
TAGS = ["history", "film", "lore", "residents"]

STYLE = ("Write each fact as one or two plain sentences, 35 words or fewer, in active voice. "
         "Do not use em dashes. Only include facts you can support with a reliable source "
         "(landmark designation reports, museums, major newspapers, Wikipedia, official sites). "
         "Prefer surprising, charming or little-known details over dry statistics. "
         "Never include information about current private residents.")

FACT = {"type": "object", "additionalProperties": False, "required": ["text", "source_url"],
        "properties": {"text": {"type": "string", "description": "The fact, following the style rules"},
                       "source_url": {"type": "string", "description": "URL of the source that supports this fact"}}}

NEW_SCHEMA = {"type": "object", "additionalProperties": False,
    "required": ["has_story", "name", "year_built", "tags", "facts"],
    "properties": {
        "has_story": {"type": "boolean", "description": "True only if the building has at least two well-sourced, interesting facts"},
        "name": {"type": "string", "description": "The building's common name, or its address if it has none"},
        "year_built": {"type": "integer", "description": "Year construction was completed, or 0 if unknown"},
        "tags": {"type": "array", "items": {"type": "string", "enum": TAGS}},
        "facts": {"type": "array", "items": FACT, "description": "Two to three facts"}}}

NEWS_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["facts"],
    "properties": {"facts": {"type": "array", "items": FACT,
        "description": "New developments since the given date. Empty if nothing notable happened."}}}


def call_parallel(prompt, schema, processor, key):
    body = json.dumps({"processor": processor, "input": prompt,
                       "task_spec": {"output_schema": {"type": "json", "json_schema": schema}}}).encode()
    hdr = {"x-api-key": key, "Content-Type": "application/json"}
    req = urllib.request.Request(API, data=body, headers=hdr, method="POST")
    run = json.load(urllib.request.urlopen(req, timeout=60))
    url = f"{API}/{run['run_id']}/result?timeout=600"
    for attempt in range(6):  # result call blocks up to 10 minutes; retry on 408
        try:
            res = json.load(urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=660))
            break
        except urllib.error.HTTPError as e:
            if e.code == 408:
                continue
            raise
    else:
        raise TimeoutError(run["run_id"])
    out = res.get("output", {})
    content = out.get("content")
    if isinstance(content, str):
        content = json.loads(content)
    conf = {b.get("field"): b.get("confidence") for b in out.get("basis", [])}
    return content, conf


def clean(facts, conf):
    """Keep facts with a real source URL and no low-confidence flag; enforce house style."""
    keep = []
    for k, f in enumerate(facts or []):
        text = re.sub(r"\s*[—–]\s*", ", ", (f.get("text") or "").strip())
        src = (f.get("source_url") or "").strip()
        low = any(v == "low" for fld, v in conf.items() if fld and (fld == "facts" or fld.startswith(f"facts.{k}")))
        if text and src.startswith("http") and not low:
            keep.append({"text": text, "source_url": src})
    return keep


def pretty_addr(a):
    words = a.title().split()
    return " ".join(words)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--new", type=int, default=10, help="new buildings to research per run")
    ap.add_argument("--check", type=int, default=10, help="existing stories to check for news per run")
    ap.add_argument("--processor", default="core")
    ap.add_argument("--dry-run", action="store_true", help="fake responses, no API calls")
    a = ap.parse_args()
    key = os.environ.get("PARALLEL_API_KEY", "")
    if not key and not a.dry_run:
        sys.exit("PARALLEL_API_KEY is not set. Add it as a repository secret.")

    stories = json.load(open(STORIES))
    B = json.load(open(BUILDINGS))
    researched = json.load(open(RESEARCHED)) if os.path.exists(RESEARCHED) else []
    have = {s.get("bbl") for s in stories if s.get("bbl")} | set(researched)

    # Job 1: oldest-checked stories first
    to_check = sorted(stories, key=lambda s: s.get("checked", "0000"))[:a.check]
    # Job 2: landmark lots without a story, oldest first (most likely to have history)
    cands = [i for i, b in enumerate(B["bbl"]) if B["lm"][i] and b not in have and B["ad"][i]]
    cands.sort(key=lambda i: B["yr"][i] or 9999)
    to_new = cands[:a.new]

    def run(job):
        kind, item = job
        if kind == "check":
            since = item.get("checked", (TODAY - dt.timedelta(days=90)).isoformat())
            prompt = (f"Building: {item['name']}, Manhattan, New York City. "
                      f"Find notable, verifiable developments about this building since {since}: "
                      f"landmark decisions, restorations, openings or closings, anniversaries, films, or news. "
                      f"Skip routine real estate listings and anything about private residents. {STYLE}")
            schema = NEWS_SCHEMA
        else:
            i = item
            prompt = (f"Building at {pretty_addr(B['ad'][i])}, Manhattan, New York City, NY {B['zip'][i]} "
                      f"(city records list it as built in {B['yr'][i] or 'an unknown year'}; it is an official NYC landmark). "
                      f"Research its history and find its most interesting stories: notable architects, "
                      f"famous past residents or visitors, film and TV appearances, legends and odd details. {STYLE}")
            schema = NEW_SCHEMA
        if a.dry_run:
            fake = {"text": "Example fact from a dry run — not real.", "source_url": "https://example.com"}
            return job, ({"facts": []} if kind == "check" else
                         {"has_story": True, "name": pretty_addr(B["ad"][item]), "year_built": 0, "tags": ["history"], "facts": [fake, fake]}), {}
        try:
            content, conf = call_parallel(prompt, schema, a.processor, key)
            return job, content, conf
        except Exception as e:  # one failure shouldn't sink the batch
            print(f"  ! {kind} failed: {e}", file=sys.stderr)
            return job, None, {}

    jobs = [("check", s) for s in to_check] + [("new", i) for i in to_new]
    print(f"Researching {len(to_check)} existing stories and {len(to_new)} new buildings with '{a.processor}'...")
    with ThreadPoolExecutor(max_workers=5) as ex:
        results = list(ex.map(run, jobs))

    added, updated, skipped = [], [], []
    for (kind, item), content, conf in results:
        if content is None:
            continue
        if kind == "check":
            item["checked"] = TODAY.isoformat()
            facts = clean(content.get("facts"), conf)
            if facts:
                item.setdefault("news", [])
                item["news"] = (facts + item["news"])[:4]
                updated.append((item["name"], facts))
        else:
            i = item
            bbl = B["bbl"][i]
            facts = clean(content.get("facts"), conf)
            if content.get("has_story") and len(facts) >= 2:
                st = {"bbl": bbl, "name": content.get("name") or pretty_addr(B["ad"][i]),
                      "tags": [t for t in content.get("tags", []) if t in TAGS] or ["history"],
                      "year": int(content.get("year_built") or 0),
                      "facts": [f["text"] for f in facts[:3]],
                      "sources": [f["source_url"] for f in facts[:3]],
                      "src": facts[0]["source_url"],
                      "added": TODAY.isoformat(), "checked": TODAY.isoformat(), "by": "parallel"}
                stories.append(st)
                added.append(st)
            else:
                researched.append(bbl)
                skipped.append(pretty_addr(B["ad"][i]))

    json.dump(stories, open(STORIES, "w"), ensure_ascii=False, indent=0)
    json.dump(sorted(set(researched)), open(RESEARCHED, "w"))

    lines = [f"# Story review, {TODAY:%B %-d, %Y}", "",
             "Read each fact and its source before merging. To reject a fact, edit `building-stories/stories.json` "
             "in this pull request and delete it. Merging publishes everything below to the map.", ""]
    lines += [f"## New stories ({len(added)})", ""]
    for st in added:
        lines.append(f"### {st['name']}")
        lines += [f"- {t} ([source]({u}))" for t, u in zip(st["facts"], st["sources"])]
        lines.append("")
    lines += [f"## News on existing stories ({len(updated)})", ""]
    for name, facts in updated:
        lines.append(f"### {name}")
        lines += [f"- {f['text']} ([source]({f['source_url']}))" for f in facts]
        lines.append("")
    if skipped:
        lines += [f"## Researched, no strong story found ({len(skipped)})", "", ", ".join(skipped), ""]
    open(SUMMARY, "w").write("\n".join(lines))
    print(f"Added {len(added)} stories, updated {len(updated)}, no story for {len(skipped)}.")
    # tell the workflow whether there is anything to review
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"changes={len(added) + len(updated)}\n")


if __name__ == "__main__":
    main()
