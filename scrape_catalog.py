#!/usr/bin/env python3
"""
Builds the DASH course-catalog feed (catalog.json) from the De Anza online
catalog, so the app's long-press "course detail" sheet can show a course's
units, prerequisites, advisories, and description — none of which live in the
class-schedule feed.

Source pages (same Cloudflare-fronted site as the schedule):

  /catalog/courses/
        The catalog landing page. Its <select id="ddlDepts"> lists every
        department as <option value="135">Accounting Department (ACCT)</option>
        — a numeric department id plus the subject code in parentheses.

  /_resources/php/catalog/dept-course-list.php?dept=<id>
        The AJAX fragment the page loads when a department is picked: a table
        of Course Number | Course Title | Units, where each title links to
        …/_course_details_display.php?course=<detailId>.

  /catalog/courses/utilities/_course_details_display.php?course=<detailId>
        One course's full detail: <h3>Course Description</h3> paragraph and a
        <dl> of Prerequisite(s) / Advisory(ies).

Output, committed by the monthly GitHub Action:

  catalog.json — one object per course, matching CourseCatalogService's schema:
      [{"course": "ACCT 1A", "title": "Financial Accounting I", "units": "5",
        "prerequisites": "…", "advisories": "…", "description": "…"}, ...]
    Every field except "course" is optional and omitted when blank. The course
    code is printed exactly as the catalog shows it (e.g. "ACCT 1A"), which is
    the same format the schedule feed and the app use.

Efficiency: the dept tables give code/title/units for every course in one fetch
per department (~50). Fetching each course's detail page is the expensive part
(~thousands of pages), so by default details are pulled only for courses that
are actually offered in the committed classes-<TERM>.json files — the only
courses the app's long-press can reach. Set CATALOG_ALL=1 to fetch details for
every catalog course regardless.

Reliability mirrors scrape_schedule.py: it reuses that module's fetch() (Chrome
TLS impersonation via curl_cffi, with a headless-Chromium/Playwright fallback on
Cloudflare 403), and a run that comes back with far fewer courses than the last
one keeps the previous catalog.json rather than overwriting it with a bad scrape.

Offline testing: set DASH_FIXTURES to a directory of saved pages and the fetches
read files (catalog-index.html, catalog-dept-<id>.html, catalog-detail-<id>.html)
instead of the network.
"""

import glob
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

# Reuse the schedule scraper's fetch pipeline and helpers so the Cloudflare
# handling stays in one place.
from scrape_schedule import fetch, strip_tags, SITE, FETCH_DELAY

ROOT = os.path.dirname(os.path.abspath(__file__))

DEPT_LIST = SITE + "/_resources/php/catalog/dept-course-list.php?dept={id}"
DETAIL = SITE + "/catalog/courses/utilities/_course_details_display.php?course={id}"

# A run that collapses to a small fraction of the previous catalog is treated
# as a bad scrape, and the previous file is kept.
MIN_KEEP_RATIO = 0.5


def normalize(course):
    """Course code as the app keys it: spaces removed, uppercased."""
    return course.upper().replace(" ", "")


# ---- landing page: department ids --------------------------------------------

def parse_departments(page):
    """[(id, subjectCode)] from the ddlDepts dropdown; skips 'Please Select'."""
    select = re.search(r'(?is)<select[^>]*id="ddlDepts".*?</select>', page)
    if not select:
        return []
    depts = []
    for value, label in re.findall(r'<option value="(\d+)"[^>]*>(.*?)</option>',
                                   select.group(0), re.S):
        if value == "0":
            continue
        text = strip_tags(label)
        code = re.search(r'\(([^)]+)\)\s*$', text)
        depts.append((value, code.group(1) if code else text))
    return depts


# ---- department course table -------------------------------------------------

ROW_RE = re.compile(
    r'(?is)<tr>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>')
DETAIL_ID_RE = re.compile(r'course=(\d+)')


def parse_dept_table(fragment):
    """[{course, title, units, detail}] for one department's table."""
    rows = []
    for code_html, title_html, units_html in ROW_RE.findall(fragment):
        course = strip_tags(code_html)
        if not course or course.lower() == "course number":
            continue
        detail = DETAIL_ID_RE.search(title_html)
        rows.append({
            "course": course,
            "title": strip_tags(title_html),
            "units": strip_tags(units_html),
            "detail": detail.group(1) if detail else None,
        })
    return rows


# ---- course detail page ------------------------------------------------------

def _dl_field(page, label):
    """Joined text of every <dd> under the <dt> whose text starts with `label`
    (e.g. 'Advisory' matches 'Advisory(ies)'), up to the next <dt> or </dl>."""
    block = re.search(r'(?is)<dt[^>]*>\s*' + label + r'.*?</dt>(.*?)(?=<dt|</dl>)', page)
    if not block:
        return None
    parts = [strip_tags(dd) for dd in re.findall(r'(?is)<dd[^>]*>(.*?)</dd>', block.group(1))]
    parts = [p for p in parts if p]
    return "; ".join(parts) if parts else None


def parse_detail(page):
    """(description, prerequisites, advisories) from a course detail page."""
    desc = None
    m = re.search(r'(?is)<h3>\s*Course Description\s*</h3>\s*<p>(.*?)</p>', page)
    if m:
        desc = strip_tags(m.group(1))
    prereq = _dl_field(page, "Prerequisite")
    advisory = _dl_field(page, "Advisor")
    return desc, prereq, advisory


# ---- offered-course set (to bound detail fetches) ----------------------------

def load_offered_codes():
    """Normalized course codes present in the committed classes-<TERM>.json
    files — the courses the app can actually look up."""
    offered = set()
    for path in glob.glob(os.path.join(ROOT, "classes-*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        for section in data.get("classes", []):
            course = section.get("course")
            if course:
                offered.add(normalize(course))
    return offered


# ---- assembly ----------------------------------------------------------------

def load_json(path, default):
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    return default


def main():
    updated = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    out_path = os.path.join(ROOT, "catalog.json")

    index_page = fetch(SITE + "/catalog/courses/", fixture="catalog-index.html")
    depts = parse_departments(index_page)
    if not depts:
        print("Could not read departments from the catalog page; keeping "
              "previous catalog.json.", file=sys.stderr)
        sys.exit(1)

    offered = load_offered_codes()
    fetch_all = os.environ.get("CATALOG_ALL") == "1" or not offered
    # Course descriptions change ~yearly, so by default a course whose detail
    # was already captured is reused without re-fetching — that keeps the
    # monthly run cheap (only genuinely new courses hit the slow detail pages).
    # CATALOG_REFRESH=1 re-fetches every detail.
    refresh = os.environ.get("CATALOG_REFRESH") == "1"
    prior = load_json(out_path, [])
    prior_by_key = {normalize(e["course"]): e for e in prior}
    DETAIL_KEYS = ("description", "prerequisites", "advisories")
    print(f"{len(depts)} departments; "
          f"{'all courses' if fetch_all else f'{len(offered)} offered courses'} "
          f"get detail pages"
          f"{' (reusing known ones)' if prior and not refresh else ''}",
          file=sys.stderr)

    entries = []
    seen = set()
    for dept_id, code in depts:
        try:
            fragment = fetch(DEPT_LIST.format(id=dept_id),
                             fixture=f"catalog-dept-{dept_id}.html")
        except Exception as exc:
            print(f"  dept {code} ({dept_id}): FAILED — {exc}", file=sys.stderr)
            continue
        rows = parse_dept_table(fragment)
        detailed = 0
        for row in rows:
            key = normalize(row["course"])
            if key in seen:
                continue
            seen.add(key)
            entry = {"course": row["course"]}
            if row["title"]:
                entry["title"] = row["title"]
            if row["units"]:
                entry["units"] = row["units"]
            want_detail = row["detail"] and (fetch_all or key in offered)
            existing = prior_by_key.get(key)
            if want_detail and existing and not refresh \
                    and any(k in existing for k in DETAIL_KEYS):
                # Already captured on a previous run — carry it over, no fetch.
                for k in DETAIL_KEYS:
                    if existing.get(k):
                        entry[k] = existing[k]
            elif want_detail:
                try:
                    detail_page = fetch(DETAIL.format(id=row["detail"]),
                                        fixture=f"catalog-detail-{row['detail']}.html")
                    desc, prereq, advisory = parse_detail(detail_page)
                    if desc:
                        entry["description"] = desc
                    if prereq:
                        entry["prerequisites"] = prereq
                    if advisory:
                        entry["advisories"] = advisory
                    detailed += 1
                except Exception as exc:
                    print(f"    {row['course']}: detail FAILED — {exc}", file=sys.stderr)
                if not os.environ.get("DASH_FIXTURES"):
                    time.sleep(FETCH_DELAY)
            entries.append(entry)
        print(f"  {code}: {len(rows)} courses ({detailed} detailed)", file=sys.stderr)
        if not os.environ.get("DASH_FIXTURES"):
            time.sleep(FETCH_DELAY)

    if len(entries) < max(20, len(prior) * MIN_KEEP_RATIO):
        print(f"Only {len(entries)} courses (previous had {len(prior)}); "
              f"keeping previous catalog.json.", file=sys.stderr)
        sys.exit(1 if not prior else 0)

    entries.sort(key=lambda e: e["course"])
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, separators=(",", ":"))
    withdesc = sum(1 for e in entries if "description" in e)
    print(f"Wrote catalog.json with {len(entries)} courses "
          f"({withdesc} with descriptions). Updated {updated}.")


if __name__ == "__main__":
    main()
