# dash-schedule — the DASH class-schedule relay

This repo generates the class-schedule feed the DASH app uses to suggest
classes on the timetable's **Add Class** sheet. A GitHub Action scrapes
the De Anza class schedule on the **first day of every month** and
commits the JSON; the app fetches it from this repo's raw URL, so one
monthly update reaches every user with no App Store release.

It is the same relay pattern as `dash-events`.

## One-time setup

1. Create a **public** GitHub repo named `dash-schedule` and put these
   files in it (`scrape_schedule.py`,
   `.github/workflows/update-schedule.yml`).
2. In the repo: **Settings → Actions → General → Workflow permissions →
   Read and write permissions** (lets the Action commit the files).
3. Run it once: **Actions → Update class schedule feed → Run workflow**.
   This creates `terms.json` plus one `classes-<TERM>.json` per term
   (takes a few minutes — it walks ~86 subjects × 4 terms politely).
4. The app's `DeAnza/Model/ClassScheduleService.swift` already points at
   `https://raw.githubusercontent.com/Nakajima24/dash-schedule/main/` —
   if the repo lives elsewhere, update `baseURL` there.

## What gets scraped

`deanza.edu/schedule/` lists the open terms (e.g. Summer 2026 … Spring
2027) and the department dropdown. For each term × department the
scraper fetches `listings.html?dept=XXXX&t=TERM` and parses the class
table (CRN, course, section, title, days, times, instructor, location).
Cancelled sections are skipped.

- `terms.json` — tiny index: which terms exist and which file holds each
  term's classes. The app fetches this first and then only the term(s)
  the user's timetable quarters actually map to.
- `classes-F2026.json` etc. — every section for that term.

Meetings whose times read `TBA-TBA` on the site (fully online or
arranged-hours classes, and online lab halves of hybrid classes) are
kept with `"tba": true` — the app shows those in a separate
"no scheduled time" list under the timetable instead of on the grid.

## Reliability

- deanza.edu sits behind Cloudflare, which fingerprints the TLS
  handshake **and** scores the source IP. The scraper tries
  **curl_cffi** impersonating Chrome first (enough from residential
  networks); if that's 403'd — as happens from GitHub's datacenter
  runners — it switches to **real headless Chromium via Playwright**,
  which executes Cloudflare's browser challenge and reuses the
  clearance cookie for the rest of the run. The workflow installs
  both. If even the browser gets challenged with a CAPTCHA someday,
  the fallback is running this script from a normal home network
  (it works unchanged) and pushing the JSON.
- Each term is isolated: if a term's scrape fails or returns nothing,
  its previous `classes-<TERM>.json` is left untouched rather than
  replaced with an empty file.
- If the landing page itself can't be read, the run exits nonzero and
  nothing is committed — the app keeps serving last month's feed.

## Testing without network access

Save page snapshots (`schedule.html`,
`listings-<TERM>-<DEPT>.html`, e.g. `listings-F2026-MATH.html`) into a
directory and run:

```
DASH_FIXTURES=/path/to/fixtures python3 scrape_schedule.py
```

## Feed shape

```json
{
  "term": "F2026",
  "name": "Fall 2026",
  "updated": "2026-08-01T09:00:00Z",
  "classes": [
    {
      "crn": "00471",
      "course": "CIS 3",
      "section": "02Z",
      "title": "Business Information Systems",
      "instructor": "Mahesh Pakala",
      "meetings": [
        {"days": ["Tue"], "start": "18:00", "end": "18:50",
         "location": "ONLINE", "tba": false},
        {"days": [], "start": null, "end": null, "location": null,
         "tba": true, "type": "LAB"}
      ]
    }
  ]
}
```

`days` uses `Mon`–`Sun`; `start`/`end` are 24-hour campus (Pacific)
times; `location` is a room code (`S43`, `AT203`), `ONLINE`, or null.
