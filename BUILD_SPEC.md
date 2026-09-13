# Build Spec: Aerospace SWE Internship Poller

**Read this whole file before writing code.** It contains every endpoint, token, and filter rule
you need. Do not research or guess ATS tokens — they are all listed below with verification status.

---

## 1. What this builds

A GitHub Actions cron job that polls ~55 company job boards every 30 minutes, detects newly-posted
software engineering internship/co-op requisitions, and sends a push notification to the user's
phone via ntfy.

Targeting **Summer 2027** software internships primarily and **Fall 2027 / Winter 2028 /
8-month co-op** terms secondarily. Latency target is minutes-to-an-hour, not seconds.
The candidate clears ITAR and export-control requirements but holds no security
clearance -- see section 6.

---

## 2. Repository layout

```
job-poller/
├── .github/workflows/poll.yml
├── sources.json          # company list — data, not code
├── poller.py             # entry point
├── handlers.py           # one fetch function per ATS type
├── filters.py            # include/exclude logic
├── notify.py             # ntfy push
├── state/seen.json       # committed state, gitignore nothing here
├── requirements.txt
└── README.md
```

Python 3.11+. Keep dependencies minimal: `requests` is sufficient. Do not add a database, a web
framework, or an ORM.

---

## 3. Core loop

1. Load `sources.json` and `state/seen.json`.
2. For each source, call the handler for its `ats` type. Wrap every call in try/except — one
   dead board must never abort the run.
3. Normalize every returned job into a common dict:
   `{source, company, job_id, title, location, department, url, first_seen}`.
4. Compute the set of `(source, job_id)` pairs not present in `seen.json`.
5. Apply the filters in section 6 to the new jobs.
6. Send one ntfy notification per surviving job (see section 7).
7. Write all newly-seen job IDs — **including ones the filter rejected** — into `seen.json`, so a
   rejected job is never re-evaluated and cannot re-notify.
8. Commit `state/seen.json` back to the repo.

**First run bootstrap:** if `seen.json` is empty or missing, populate it with everything currently
on every board and send **no** notifications. Otherwise the first run fires several hundred pushes.
Log the count instead.

---

## 4. ATS handlers

Write one function per ATS. Four handlers cover the large majority of sources.

### 4.1 Greenhouse
```
GET https://boards-api.greenhouse.io/v1/boards/{token}/jobs?content=true
```
- No auth. Returns everything in one call, no pagination.
- Response: top-level key `jobs` (array).
- Fields: `title`, `id`, `location.name`, `updated_at`, `absolute_url`,
  `departments[].name`, `offices[].name`.
- Drop `?content=true` if response size becomes a problem — you only need titles for filtering.
- EU-hosted boards use `boards-api.eu.greenhouse.io`. Only `physicsx` may need this; try the
  `.io` host first and fall back.

### 4.2 Lever
```
GET https://api.lever.co/v0/postings/{slug}?mode=json
```
- No auth. Returns a **bare top-level array**, not an object. Handle that difference.
- Fields: `text` (this is the title), `id`, `categories.location`, `categories.team`,
  `categories.commitment` (e.g. `"Intern"`), `createdAt` (epoch **milliseconds**), `hostedUrl`.
- Optional server-side filter: append `&commitment=Intern`. Do **not** rely on this alone —
  many companies mislabel commitment. Filter client-side too.

### 4.3 Ashby
```
GET https://api.ashbyhq.com/posting-api/job-board/{boardName}
```
- No auth. **Board name is case-sensitive** (`Luminary`, `K2space` — note the capitals).
- Response: top-level key `jobs` (array).
- Fields: `title`, `id`, `location`, `department`, `team`, `employmentType`, `publishedAt`,
  `updatedAt`, `jobUrl`.

### 4.4 Workday
```
POST https://{tenant}.{dc}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
Content-Type: application/json
Accept: application/json
User-Agent: <realistic browser UA string>
Body: {"appliedFacets":{}, "limit":20, "offset":0, "searchText":"intern"}
```
- The `User-Agent` and JSON headers are **required** — Workday returns 404 or empty without them.
- Response: `jobPostings` (array), plus `total` (int).
- Fields: `title`, `externalPath`, `locationsText`, `postedOn`, `bulletFields[0]` (usually req ID).
- Apply URL = `https://{tenant}.{dc}.myworkdayjobs.com/en-US/{site}` + `externalPath`.
- **Paginate**: increment `offset` by `limit` until `offset >= total`. Max 20 per page.
- Poll Workday sources at a lower cadence than the others (see section 8).

### 4.5 Workable
```
GET https://apply.workable.com/api/v3/accounts/{subdomain}/jobs
```
Only used by GHGSat. Low priority.

### 4.6 BambooHR
```
GET https://{subdomain}.bamboohr.com/careers/list
```
Response has a `result` array. Only used by Sedaro.

### 4.7 RSS / scrape sources
Build these **last**, after everything above works. For each, fetch the page, extract the job list,
and diff on a stable identifier. Firefly has a real RSS feed and should use a feed parser.
If a scrape handler is more than ~40 lines, drop that source into a `manual_check` list in the
README instead of building it.

---

## 5. sources.json

Verification status: `verified` means the token was confirmed against a live careers page or API
call. `probe` means it needs a one-time confirmation run before being trusted — **on the first run,
log a warning for any `probe` source returning zero jobs.**

```json
{
  "greenhouse": [
    {"company": "Anduril Industries",   "token": "andurilindustries",     "status": "verified"},
    {"company": "Stoke Space",          "token": "stokespacetechnologies","status": "verified"},
    {"company": "Zipline",              "token": "flyzipline",            "status": "verified"},
    {"company": "Astranis",             "token": "astranis",              "status": "verified"},
    {"company": "K2 Space",             "token": "k2spacecorporation",    "status": "verified"},
    {"company": "Vast",                 "token": "vast",                  "status": "verified"},
    {"company": "True Anomaly",         "token": "trueanomalyinc",        "status": "verified"},
    {"company": "Rocket Lab",           "token": "rocketlab",             "status": "verified"},
    {"company": "Skydio",               "token": "skydio",                "status": "verified"},
    {"company": "Applied Intuition",    "token": "appliedintuition",      "status": "verified"},
    {"company": "Muon Space",           "token": "muonspace",             "status": "verified"},
    {"company": "SpaceX",               "token": "spacex",                "status": "verified"},
    {"company": "Planet Labs",          "token": "planetlabs",            "status": "verified"},
    {"company": "Relativity Space",     "token": "relativity",            "status": "verified"},
    {"company": "Relativity (interns)", "token": "rsinternboard",         "status": "verified",
      "note": "SEPARATE intern board — must poll alongside 'relativity'"},
    {"company": "Whisper Aero",         "token": "whisperaero",           "status": "verified"},
    {"company": "Ursa Major",           "token": "ursamajor",             "status": "probe"},
    {"company": "Varda Space",          "token": "vardaspace",            "status": "verified"},
    {"company": "Capella Space",        "token": "capellaspace",          "status": "probe"},
    {"company": "Archer Aviation",      "token": "archer56",              "status": "probe"},
    {"company": "Chaos Industries",     "token": "chaosindustries",       "status": "probe"},
    {"company": "Epirus",               "token": "epirus",                "status": "probe"},
    {"company": "Vannevar Labs",        "token": "vannevarlabs",          "status": "probe"},
    {"company": "Scale AI",             "token": "scaleai",               "status": "probe"},
    {"company": "Neros Technologies",   "token": "nerostechnologies",     "status": "probe"},
    {"company": "Vatn Systems",         "token": "vatnsystems",           "status": "probe"},
    {"company": "Pyka",                 "token": "pyka",                  "status": "probe"},
    {"company": "Figure AI",            "token": "figureai",              "status": "probe"},
    {"company": "Apptronik",            "token": "apptronik",             "status": "verified"},
    {"company": "Nuro",                 "token": "nuro",                  "status": "verified"},
    {"company": "Torc Robotics",        "token": "torcrobotics",          "status": "probe"},
    {"company": "Kodiak Robotics",      "token": "kodiak",                "status": "probe"},
    {"company": "Agility Robotics",     "token": "agilityrobotics",       "status": "probe"},
    {"company": "IonQ",                 "token": "ionq",                  "status": "probe"},
    {"company": "Schrodinger",          "token": "schrdinger",            "status": "probe",
      "note": "token has NO letter 'o' — this is not a typo"},
    {"company": "PhysicsX",             "token": "physicsx",              "status": "probe",
      "note": "may require boards-api.eu.greenhouse.io"}
  ],
  "lever": [
    {"company": "Shield AI",            "slug": "shieldai",     "status": "verified"},
    {"company": "Loft Orbital",         "slug": "loftorbital",  "status": "verified"},
    {"company": "Kepler Communications","slug": "kepler",       "status": "verified",
      "note": "NOT 'keplergroup' (different company) and NOT Ashby 'kepler-ai'"},
    {"company": "Zoox",                 "slug": "zoox",         "status": "probe"},
    {"company": "Merlin Labs",          "slug": "merlinlabs",   "status": "probe"},
    {"company": "Elroy Air",            "slug": "elroyair",     "status": "probe"},
    {"company": "Palantir",             "slug": "palantir",     "status": "probe"},
    {"company": "Sanctuary AI",         "slug": "sanctuary",    "status": "probe"},
    {"company": "Telesat",              "slug": "telesat",      "status": "probe"},
    {"company": "Attabotics",           "slug": "attabotics",   "status": "probe",
      "note": "board may be legitimately empty — do not treat as an error"}
  ],
  "ashby": [
    {"company": "Reliable Robotics",  "board": "reliable-robotics", "status": "verified",
      "note": "Lever slug 'reliable' exists but is empty — use Ashby"},
    {"company": "Saronic",            "board": "saronic",           "status": "verified",
      "note": "its Lever board is empty — use Ashby"},
    {"company": "K2 Space",           "board": "K2space",           "status": "probe",
      "note": "secondary board; Greenhouse is primary. Dedupe against it."},
    {"company": "Mach Industries",    "board": "mach",              "status": "probe",
      "note": "NOT Greenhouse 'machindustries' (empty)"},
    {"company": "HavocAI",            "board": "havocai",           "status": "probe"},
    {"company": "Gecko Robotics",     "board": "gecko-robotics",    "status": "probe"},
    {"company": "Luminary Cloud",     "board": "Luminary",          "status": "probe",
      "note": "capital L — case-sensitive"},
    {"company": "SkyWatch",           "board": "skywatch",          "status": "probe"},
    {"company": "Applied Intuition",  "board": "applied",           "status": "probe",
      "note": "second board; dedupe against Greenhouse 'appliedintuition'"}
  ],
  "workday": [
    {"company": "Blue Origin",     "tenant": "blueorigin",     "dc": "wd5",   "site": "BlueOrigin",                 "status": "verified"},
    {"company": "Wisk Aero",       "tenant": "wisk",           "dc": "wd108", "site": "Wisk_Careers",               "status": "verified"},
    {"company": "CAE",             "tenant": "cae",            "dc": "wd3",   "site": "career",                     "status": "verified"},
    {"company": "Boston Dynamics", "tenant": "bostondynamics", "dc": "wd1",   "site": "Boston_Dynamics",            "status": "probe"},
    {"company": "NVIDIA",          "tenant": "nvidia",         "dc": "wd5",   "site": "NVIDIAExternalCareerSite",   "status": "probe"},
    {"company": "Cadence",         "tenant": "cadence",        "dc": "wd1",   "site": "Univ_Careers",               "status": "probe",
      "note": "Univ_Careers is the student board; External_Careers is the main one"}
  ],
  "workable": [
    {"company": "GHGSat", "subdomain": "ghgsat", "status": "probe"}
  ],
  "bamboohr": [
    {"company": "Sedaro", "subdomain": "sedarotech", "status": "probe"}
  ],
  "scrape_later": [
    {"company": "Firefly Aerospace", "method": "rss",  "host": "firefly.hrmdirect.com"},
    {"company": "MDA Space",         "method": "html", "note": "UltiPro tenant MAC5000MCDW, co-op board GUID 7667adcc-47ae-477a-9183-0d8ef8bc0748"},
    {"company": "Xona Space",        "method": "html", "note": "Paylocity, JS-rendered"},
    {"company": "Impulse Space",     "method": "html", "note": "Pinpoint; try impulsespace.pinpointhq.com/postings.json first"},
    {"company": "NordSpace",         "method": "html"},
    {"company": "Mission Control",   "method": "html"},
    {"company": "Canadensys",        "method": "html"}
  ]
}
```

---

## 6. Filtering

Apply to the job title, case-insensitive. Both lists must pass.

**REQUIRE at least one internship marker:**
`intern`, `internship`, `co-op`, `coop`, `early career`, `student`, `new grad`,
`summer 2027`, `fall 2027`, `winter 2028`

**REQUIRE at least one software marker:**
`software`, `swe`, `flight software`, `ground software`, `embedded`, `firmware`, `autonomy`,
`simulation`, `backend`, `full stack`, `full-stack`, `platform`, `infrastructure`, `devops`,
`robotics software`, `perception`, `data engineer`, `machine learning`, `computer vision`

**REJECT if any of these appear** (these override — reject wins over any include match):
`gnc`, `guidance navigation`, `guidance, navigation`, `flight dynamics`, `controls engineer`,
`astrodynamics`, `mechanical`, `propulsion`, `structures`, `thermal`, `avionics hardware`,
`manufacturing`, `rf engineer`, `payload`, `technician`, `machinist`, `welder`, `composites`,
`quality engineer`, `supply chain`, `recruiter`, `sales`, `marketing`, `finance`, `legal`,
`phd`, `doctoral`

**REJECT on clearance language** — check the description body when available (`content=true`
on Greenhouse gives you this). Reject if it contains any of:
`active security clearance`, `active secret`, `active top secret`, `ts/sci`,
`must possess a clearance`, `currently hold a clearance`, `interim secret`.
Do **not** reject on `ITAR`, `U.S. Person`, `export control`, or `ability to obtain a clearance` —
the user clears those. This distinction matters; get it right.

Make all four lists constants at the top of `filters.py` so they are trivially editable.

---

## 7. Notification (ntfy)

The topic name comes from the environment variable `NTFY_TOPIC`, set as a GitHub Actions secret.
Never hardcode it.

```
POST https://ntfy.sh/{NTFY_TOPIC}
Body:    <plain text message>
Headers: Title: <company> — <job title>
         Priority: default
         Tags: rocket
         Click: <apply url>
```

Message body should contain: job title, company, location, and the apply URL. Keep it short enough
to read on a lock screen.

If more than 8 jobs fire in a single run, send one summary notification instead of 8 pushes, with
the count and the company names, and write the full list to the Actions log.

---

## 8. Scheduling

`.github/workflows/poll.yml`:

```yaml
on:
  schedule:
    - cron: '*/30 * * * *'
  workflow_dispatch:
```

- `workflow_dispatch` is required so the user can trigger a manual test run.
- GitHub Actions cron is best-effort and can lag several minutes under load. That is acceptable.
- Poll Greenhouse, Lever, Ashby, Workable, BambooHR on every run.
- Poll **Workday sources only every 4th run** (roughly every 2 hours) — it throttles more
  aggressively. Track this with a simple run counter in `seen.json` or by checking the current
  hour. Do not add a second workflow file for this.
- Add 0.5–2s of jitter between requests. Set a descriptive `User-Agent` on every call.

Workflow steps: checkout → setup-python → pip install → run poller → commit `state/seen.json` if
changed. Use `permissions: contents: write` and commit with the built-in `GITHUB_TOKEN`.

---

## 9. Failure visibility

This is the part most likely to be skipped and most likely to matter. A silently broken fetcher
means missed postings.

- Any source raising an exception: log it, continue the run, and include it in a run summary.
- If **any** source throws on 3 consecutive runs, send an ntfy notification with priority `high`
  saying which source is broken.
- If a source that previously returned jobs returns **zero** for 5 consecutive runs, send an ntfy
  warning — this is the token-drift signature (a company renamed its board and the poller is now
  silently polling nothing). Track per-source consecutive-zero counts in `seen.json`.
- Write a one-line-per-source summary to the Actions log every run: source, HTTP status, job
  count, new count.

---

## 10. Build order

1. `filters.py` + unit tests against a handful of hand-written fake titles, including the
   clearance-language edge cases. This is the piece most likely to be subtly wrong.
2. Greenhouse handler. Test against `vardaspace` and `stokespacetechnologies` locally.
3. Lever + Ashby handlers.
4. Diff logic, state file, bootstrap path.
5. ntfy notification.
6. Workflow YAML. Test via `workflow_dispatch` before enabling cron.
7. Workday handler.
8. Workable / BambooHR.
9. Scrape sources, only if the rest is solid.

Run a one-off script early that hits every `probe` token and prints
`token → HTTP status, job count`. Fix or remove dead tokens before wiring notifications. Report
the results to the user rather than silently deleting entries.

---

## 11. Constraints

- Private repo.
- No secrets in code or in `sources.json`.
- No `localStorage`, no database, no server.
- Every network call gets a timeout and a try/except.
- `seen.json` must never be rewritten from scratch in a way that loses history — append and update
  only.
