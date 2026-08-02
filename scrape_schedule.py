#!/usr/bin/env python3
"""
Builds the DASH class-schedule feed from the De Anza class schedule.

For every term listed on deanza.edu/schedule (Summer/Fall/Winter/Spring)
and every subject in the department dropdown, the listings page
(listings.html?dept=XXXX&t=TERM) is fetched and its class table parsed.

Output, committed by the monthly GitHub Action:

  terms.json           — small index the app fetches first:
                         {"updated": ..., "terms": [{"code": "F2026",
                          "name": "Fall 2026", "file": "classes-F2026.json",
                          "count": 2412}, ...]}
  classes-<CODE>.json  — one file per term with every section:
                         {"term": "F2026", "name": "Fall 2026",
                          "updated": ..., "classes": [...]}

Each class:

  {"crn": "00471", "course": "CIS 3", "section": "02Z",
   "title": "Business Information Systems", "instructor": "Mahesh Pakala",
   "meetings": [
      {"days": ["Tue"], "start": "18:00", "end": "18:50",
       "location": "ONLINE", "tba": false},
      {"days": [], "start": null, "end": null, "location": null,
       "tba": true, "type": "LAB"}
   ]}

Times are 24-hour "HH:MM" campus (Pacific) local. A meeting whose times
read "TBA-TBA" (online/arranged classes) has tba=true — the app files
those under its unscheduled list instead of the timetable grid.

Reliability follows the events relay:
  - deanza.edu sits behind Cloudflare, which fingerprints the TLS
    handshake — plain urllib/curl get 403 no matter the headers. The
    fetches go through curl_cffi impersonating Chrome (the workflow
    pip-installs it); urllib remains only as a long-shot fallback.
  - Every term is isolated: if a term's scrape fails or comes back
    empty, the previous run's file for that term is kept as-is rather
    than overwritten with nothing.
  - Cancelled sections are skipped.

Offline testing: set DASH_FIXTURES to a directory of saved pages and the
fetchers read files (schedule.html, listings-<TERM>-<DEPT>.html) instead
of the network.
"""

import html as htmllib
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

try:
    from curl_cffi import requests as cffi_requests
except ImportError:
    cffi_requests = None

ROOT = os.path.dirname(os.path.abspath(__file__))
SITE = "https://www.deanza.edu"

HEADERS = {
    # Cloudflare rejects default urllib/bot user agents.
    "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                   "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                   "Version/17.5 Safari/605.1.15"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Pause between listing fetches — ~240 pages per run; stay polite and
# under any rate limiting.
FETCH_DELAY = 0.4

# The days column is seven fixed positions: M T W R F S U,
# e.g. "M·W····" = Monday + Wednesday, "···R···" = Thursday.
DAY_POSITIONS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def fetch(url, fixture=None, retries=2):
    fixtures = os.environ.get("DASH_FIXTURES")
    if fixtures and fixture:
        with open(os.path.join(fixtures, fixture), encoding="utf-8",
                  errors="replace") as f:
            return f.read()
    last = None
    for attempt in range(retries + 1):
        try:
            if cffi_requests is not None:
                # Chrome TLS impersonation — the only client Cloudflare
                # lets through here.
                resp = cffi_requests.get(url, impersonate="chrome", timeout=30)
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                return resp.text
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as exc:
            last = exc
            time.sleep(3 * (attempt + 1))
    raise last


def strip_tags(fragment):
    text = re.sub(r"<!--.*?-->", " ", fragment, flags=re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = htmllib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


# ---- landing page: terms and subjects ----------------------------------------

def parse_landing(page):
    """Term buttons (value=\"F2026\", label \"Fall 2026\") and the subject
    codes from the department dropdown."""
    terms = []
    for value, label in re.findall(
            r'<button[^>]*class="[^"]*btn-term[^"]*"[^>]*value="([A-Z]\d{4})"[^>]*>(.*?)</button>',
            page, re.S):
        terms.append({"code": value, "name": strip_tags(label)})
    depts = []
    select = re.search(r'(?is)<select[^>]*id="dept-select".*?</select>', page)
    if select:
        for value in re.findall(r'<option value="([^"]+)"', select.group(0)):
            depts.append(value)
    return terms, depts


# ---- listings page ------------------------------------------------------------

TIME_RANGE_RE = re.compile(
    r"(\d{1,2}):(\d{2})\s*([AP])\.?M\.?\s*-\s*(\d{1,2}):(\d{2})\s*([AP])\.?M\.?", re.I)


def parse_days(cell_html):
    """'M·W····' (letters at fixed positions, middots elsewhere) → day names."""
    text = htmllib.unescape(re.sub(r"<[^>]+>", "", cell_html)).strip()
    days = []
    for i, ch in enumerate(text):
        if i < len(DAY_POSITIONS) and ch not in "· .":
            days.append(DAY_POSITIONS[i])
    return days


def parse_meeting(days_html, times_html, loc_html, label=None):
    days = parse_days(days_html)
    times_text = strip_tags(times_html)
    location = strip_tags(loc_html) or None
    match = TIME_RANGE_RE.search(times_text)
    if match and days:
        h1, m1, mer1, h2, m2, mer2 = match.groups()
        def to24(h, mer):
            h = int(h) % 12
            return h + 12 if mer.upper() == "P" else h
        meeting = {
            "days": days,
            "start": f"{to24(h1, mer1):02d}:{m1}",
            "end": f"{to24(h2, mer2):02d}:{m2}",
            "location": location,
            "tba": False,
        }
    else:
        # "TBA-TBA" or no listed days: an online/arranged meeting.
        meeting = {"days": days, "start": None, "end": None,
                   "location": location, "tba": True}
    if label:
        meeting["type"] = label
    return meeting


def parse_listings(page):
    """The class table: CRN | Course | Sec | Seats | Title | Days | Times |
    Instructor | Loc | Info. A section's first row carries rowspan'd CRN/
    Course/Sec cells; extra meetings (labs) follow as short rows."""
    table = re.search(r'(?is)<th class="th-crn">.*?<tbody>(.*?)</tbody>', page)
    if not table:
        return []
    sections = []
    current = None
    for row_html in re.findall(r"(?is)<tr[^>]*>(.*?)</tr>", table.group(1)):
        cells = re.findall(r"(?is)<td[^>]*>(.*?)</td>", row_html)
        if len(cells) >= 10:
            crn = strip_tags(cells[0])
            if not crn.isdigit():
                continue
            seats = strip_tags(cells[3]).lower()
            title_m = re.search(r"(?is)<a[^>]*>(.*?)</a>", cells[4])
            current = {
                "crn": crn,
                "course": strip_tags(cells[1]),
                "section": strip_tags(cells[2]),
                "title": strip_tags(title_m.group(1)) if title_m else strip_tags(cells[4])[:80],
                "instructor": strip_tags(cells[7]),
                "meetings": [parse_meeting(cells[5], cells[6], cells[8])],
            }
            if "cancel" in seats:
                current = None       # skip cancelled sections entirely
                continue
            sections.append(current)
        elif len(cells) == 5 and current is not None:
            # Continuation row: label (LAB/CLAS/...), days, times, inst, loc.
            label = strip_tags(cells[0]) or None
            current["meetings"].append(
                parse_meeting(cells[1], cells[2], cells[4], label))
    return sections


def scrape_term(term, depts):
    classes = []
    for dept in depts:
        url = (f"{SITE}/schedule/listings.html?"
               f"dept={urllib.parse.quote(dept, safe='')}&t={term['code']}")
        fixture = f"listings-{term['code']}-{dept.replace('/', '_')}.html"
        try:
            page = fetch(url, fixture=fixture)
        except Exception as exc:
            print(f"  {term['code']} {dept}: FAILED — {exc}", file=sys.stderr)
            continue
        found = parse_listings(page)
        if found:
            print(f"  {term['code']} {dept}: {len(found)}")
        classes.extend(found)
        if not os.environ.get("DASH_FIXTURES"):
            time.sleep(FETCH_DELAY)
    return classes


# ---- assembly ------------------------------------------------------------------

def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def main():
    updated = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    page = fetch(f"{SITE}/schedule/", fixture="schedule.html")
    terms, depts = parse_landing(page)
    if not terms or not depts:
        # Nothing to iterate — leave every previously committed file alone.
        print("Could not read terms/departments from the schedule page; "
              "keeping previous feed.", file=sys.stderr)
        sys.exit(1)
    print(f"{len(terms)} terms × {len(depts)} subjects")

    index = []
    for term in terms:
        out_path = os.path.join(ROOT, f"classes-{term['code']}.json")
        classes = scrape_term(term, depts)
        if classes:
            with open(out_path, "w", encoding="utf-8") as f:
                json.dump({"term": term["code"], "name": term["name"],
                           "updated": updated, "classes": classes},
                          f, separators=(",", ":"))
            count = len(classes)
        else:
            # Empty scrape (site hiccup): keep the previous run's file.
            prior = load_json(out_path, {})
            count = len(prior.get("classes", []))
            print(f"  {term['code']}: empty — kept previous file "
                  f"({count} classes)", file=sys.stderr)
            if not count:
                continue
        index.append({"code": term["code"], "name": term["name"],
                      "file": f"classes-{term['code']}.json", "count": count})
        print(f"{term['name']}: {count} classes")

    with open(os.path.join(ROOT, "terms.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": updated, "terms": index}, f, indent=2)
    print(f"Wrote terms.json with {len(index)} terms")


if __name__ == "__main__":
    main()
