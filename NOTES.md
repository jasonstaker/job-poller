# Engineering notes

Detail that would clutter the README. All findings are from live probing; dates are
when the endpoint was actually hit.

## Manual check

Six companies have no pollable job board. Each is listed in `sources.json` under
`manual_check` with a dated note recording exactly what was probed, so the data file is the
single source of truth and this table just transcribes it.

The rule is stricter than the spec's "~40 lines": **no new dependencies** (the spec limits
this to `requests`), so anything needing a headless browser or an HTML parser is checked by
hand rather than built as a scraper that breaks silently.

| Company | Why | Where to look |
|---|---|---|
| Firefly Aerospace | `rss.php` returns 442 bytes with no `<item>` elements — the RSS feed doesn't exist | firefly.hrmdirect.com |
| MDA Space | UltiPro endpoint 404s on both hosts | mdacorporation careers |
| Xona Space | Paylocity page is a JS shell; needs a headless browser | recruiting.paylocity.com |
| NordSpace | Webflow HTML, no JSON endpoint | nordspace.com/careers |
| Mission Control | `/careers` 404s, no board located | missioncontrolspaceservices.com |
| Canadensys | `/careers/` 404s, no board located | canadensys.com |

## Corrections to BUILD_SPEC.md

The spec is kept as the original contract; corrections are logged here rather than edited
into it silently. All measured 2026-09-14.

- **§4.4 — the dangerous one.** "Increment `offset` by `limit` until `offset >= total`" is
  correct *only* if `total` is read once from the first page. Workday returns `total` only
  at `offset=0` and reports `0` on every later page, so re-reading it per page truncates
  every board to 40 jobs — with HTTP 200, no error, and a plausible log line. Blue Origin
  would yield 40 of 1,632.
- **§4.4** — "max 20 per page" is stronger than a max: `limit > 20` returns *zero* postings
  and `total: null`, rather than clamping.
- **§4.4** — "The `User-Agent` and JSON headers are **required** — Workday returns 404 or
  empty without them" is false for all six tenants. They answer identically with the plain
  descriptive UA, and two answer even with `requests`' default UA and no JSON headers. No
  browser spoofing is needed, which also dissolves the apparent conflict with §8.
- **§4.5** — the Workable path is right but the **method** is wrong: it is a `POST`, not a
  `GET`. As a GET it 404s for every slug, which is why it looked dead.
- **§4.7** — "Firefly has a real RSS feed" is false (442 bytes, no items).
- **§4.7 / §5** — Impulse Space is filed under scrape sources but is a clean JSON API; it is
  now a first-class `pinpoint` source.
- **§8** — the every-4th-run Workday cadence assumed `*/30`. GitHub actually delivers ~1 run
  per 3.5h, which would leave Workday ~14h stale, so every source is polled every run.
  ~180 rapid probe requests drew no throttling.
- **§5** — seven board tokens had drifted by 2026-09-12; each repointed entry carries a dated note in `sources.json`.
