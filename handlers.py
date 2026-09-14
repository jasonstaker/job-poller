"""One fetch function per ATS, plus the HTTP concerns they all share.

Every handler has the same signature -- ``(cfg, ctx) -> (http_status, jobs)`` -- and every
handler *raises* on failure. Isolation happens in exactly one place, `fetch_source`, which
is the only entry point `poller.py` calls. That is what makes BUILD_SPEC.md section 3
step 2 true: one dead board never aborts the run.

Handlers return the normalized job dict described in the plan. Every key is always present
with an empty-string default rather than None, so `filters.py` needs no None-guards.
"""

from __future__ import annotations

import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, NamedTuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

log = logging.getLogger("poller.handlers")

# A descriptive UA on every call, per section 8. Workday is the one exception -- see
# fetch_workday when it lands in build order step 7.
UA_DEFAULT = "job-poller/1.0 (+https://github.com/jasonstaker/job-poller)"

GREENHOUSE_HOSTS = {
    "us": "https://boards-api.greenhouse.io",
    "eu": "https://boards-api.eu.greenhouse.io",
}

# --- Workday -------------------------------------------------------------------------
# MEASURED 2026-09-14 against all six tenants; each constant records the evidence.

# limit > 20 returns ZERO postings and total=null -- it does not clamp. Section 4.4's
# "max 20 per page" is exact, not advisory.
WORKDAY_PAGE_SIZE = 20

# searchText is a FULL-TEXT search over descriptions, not titles. "intern" narrows Blue
# Origin 1632->477 and NVIDIA 2000->1019, and against full-board enumeration it misses
# ZERO title-filter-passing roles (10/10 Blue Origin, 3/3 Cadence) for a third of the
# requests. Do not add "co-op" as a second pass: it matches every posting on the board.
WORKDAY_SEARCH_TEXT = "intern"

# Runaway guard only. NVIDIA, the largest measured board, needs 51 pages -- never tune
# this below that or NVIDIA silently truncates.
WORKDAY_MAX_PAGES = 120

# How far short of `total` a board may land before it looks like the pagination bug rather
# than ordinary churn. Absolute, not a ratio, so small boards (Wisk has 3) never trip it.
WORKDAY_SHORTFALL_TOLERANCE = 5

# Which sources.json field carries the board identifier, per ATS. The spec uses a
# different key name for each, so this is the one place that difference is resolved.
ID_FIELDS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("token",),
    "lever": ("slug",),
    "ashby": ("board",),
    "workday": ("tenant", "site"),
    "workable": ("subdomain",),
    "bamboohr": ("subdomain",),
    "pinpoint": ("subdomain",),
}

# Present in sources.json but deliberately never polled. `manual_check` holds companies
# whose boards need a headless browser or an HTML parser -- section 2 limits dependencies
# to `requests`, so those are checked by hand rather than built as brittle scrapers. Each
# entry carries a dated note recording exactly what was probed. `scrape_later` is kept for
# backward compatibility with older state files even though the block is gone.
SKIP_ATS = frozenset({"scrape_later", "manual_check"})


def source_id(cfg: dict) -> str:
    """Stable unique id for a *board*, e.g. "greenhouse:vardaspace".

    Deliberately not the company name. The same company can appear on two boards --
    K2 Space is on both Greenhouse and Ashby, Applied Intuition likewise, and Relativity
    has two separate Greenhouse boards -- and those need distinct ids while sharing a
    display name. See `poller.dedupe`.
    """
    ats = cfg["ats"]
    parts = [cfg[f] for f in ID_FIELDS[ats]]
    return ":".join([ats, *parts])


class FetchResult(NamedTuple):
    """Everything the section 9 per-source log line and health counters need."""

    source: str
    company: str
    ok: bool
    status: int | None          # None when the request never completed at all
    jobs: list[dict]
    error: str
    elapsed_ms: int
    pages: int
    state_updates: dict         # hints to persist, e.g. {"greenhouse_host": "eu"}


@dataclass
class FetchContext:
    """Shared per-run HTTP state. Mutable: it counts requests to schedule jitter."""

    session: requests.Session
    # (connect, read). The read half is generous because the largest boards are big:
    # Anduril is a 2.3MB payload with 2,274 postings, and it intermittently takes
    # 25-40s when Greenhouse is throttling. A 20s read timeout dropped it entirely.
    timeout: tuple[float, float] = (5.0, 45.0)
    user_agent: str = UA_DEFAULT
    jitter: bool = True
    log: logging.Logger = log
    # Per-source memoized hints read back from state/seen.json, keyed by source id.
    state_hints: dict[str, dict] = field(default_factory=dict)
    requests_made: int = 0
    # Set once per run if the Greenhouse EU host turns out not to resolve, so the
    # remaining 404ing boards do not each pay a DNS timeout.
    greenhouse_eu_unavailable: bool = False

    def hint(self, sid: str, key: str, default: Any = None) -> Any:
        return self.state_hints.get(sid, {}).get(key, default)


def build_session() -> requests.Session:
    """A session that retries once on transient failures.

    Not in the spec, but without it a single network blip marches an otherwise healthy
    source toward the section 9 three-strike alert. 404 is deliberately absent from the
    status list: that is a dead token, not a transient error, and retrying it just doubles
    the cost of the failure.
    """
    retry = Retry(
        total=1,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),  # POST matters: Workday is POST-only
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


def http_request(
    method: str,
    url: str,
    *,
    ctx: FetchContext,
    json_body: dict | None = None,
    headers: dict | None = None,
    user_agent: str | None = None,
) -> requests.Response:
    """Single choke point for timeouts, User-Agent, and jitter."""
    if ctx.jitter and ctx.requests_made > 0:
        # Skipped on the run's very first request, and entirely under --no-jitter, so the
        # test suite never sleeps.
        time.sleep(random.uniform(0.5, 2.0))
    ctx.requests_made += 1

    hdrs = {"User-Agent": user_agent or ctx.user_agent, "Accept": "application/json"}
    if headers:
        hdrs.update(headers)

    return ctx.session.request(
        method, url, json=json_body, headers=hdrs, timeout=ctx.timeout
    )


# --------------------------------------------------------------------------------------
# Normalization helpers
# --------------------------------------------------------------------------------------


def _clean(value: Any) -> str:
    """Everything reaching the normalized dict is a stripped string, never None."""
    if value is None:
        return ""
    return " ".join(str(value).split())


def _iso(dt: datetime | None) -> str:
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _epoch_ms_to_iso(ms: Any) -> str:
    try:
        return _iso(datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc))
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def normalize_job(
    cfg: dict,
    *,
    job_id: Any,
    title: Any,
    url: Any,
    location: Any = "",
    department: Any = "",
    posted_at: str = "",
    content: str = "",
) -> dict:
    """Build the normalized job dict.

    `job_id` is coerced to str here rather than at the call site because Greenhouse
    returns integers, JSON object keys are strings, and 4711234 != "4711234" would produce
    silent duplicate notifications forever.

    `first_seen` is deliberately left empty. Section 3 lists it in the step-3 normalized
    dict, but it is unknowable until the step-4 diff, so the poller fills it in. The
    board's own posted date lives in `posted_at` so the two never get conflated.
    """
    return {
        "source": cfg["source_id"],
        "company": cfg["company"],
        "job_id": _clean(job_id),
        "title": _clean(title),
        "location": _clean(location),
        "department": _clean(department),
        "url": _clean(url),
        "first_seen": "",
        "content": content,
        "posted_at": posted_at,
        "ats": cfg["ats"],
    }


# --------------------------------------------------------------------------------------
# Greenhouse
# --------------------------------------------------------------------------------------


def _greenhouse_parse(cfg: dict, payload: dict) -> list[dict]:
    jobs = []
    for raw in payload.get("jobs") or []:
        offices = [o.get("name") for o in raw.get("offices") or [] if o.get("name")]
        departments = [d.get("name") for d in raw.get("departments") or [] if d.get("name")]
        location = (raw.get("location") or {}).get("name") or "; ".join(offices)
        jobs.append(
            normalize_job(
                cfg,
                job_id=raw.get("id"),
                title=raw.get("title"),
                url=raw.get("absolute_url"),
                location=location,
                department="; ".join(departments),
                posted_at=_clean(raw.get("updated_at")),
                content=raw.get("content") or "",
            )
        )
    return jobs


def fetch_greenhouse(cfg: dict, ctx: FetchContext) -> tuple[int, list[dict]]:
    """GET /v1/boards/{token}/jobs

    Note the absence of ?content=true. Across 36 boards -- SpaceX alone carries well over
    a thousand postings -- that flag is tens of megabytes every 30 minutes. Section 4.1
    offers to drop it, but dropping it outright would disable the section 6 clearance
    filter, so descriptions are instead fetched per-job by `greenhouse_fetch_content` for
    the handful of jobs that are both new and already past the title filter.

    EU fallback is generic rather than special-cased to physicsx: any board that 404s on
    the .io host is retried against the EU host. An empty-but-200 response is *not* a
    fallback trigger -- an empty board is legitimate.
    """
    sid = cfg["source_id"]
    token = cfg["token"]
    preferred = ctx.hint(sid, "greenhouse_host", "us")
    order = [preferred] + [h for h in ("us", "eu") if h != preferred]

    primary_resp: requests.Response | None = None
    primary_exc: Exception | None = None

    for index, host in enumerate(order):
        is_primary = index == 0
        # The EU host does not resolve from every network -- confirmed NXDOMAIN during the
        # step 3 probe. Once that is known for a run, skip it rather than paying a DNS
        # timeout for every 404ing board.
        if not is_primary and host == "eu" and ctx.greenhouse_eu_unavailable:
            break

        url = f"{GREENHOUSE_HOSTS[host]}/v1/boards/{token}/jobs"
        try:
            resp = http_request("GET", url, ctx=ctx)
        except requests.RequestException as exc:
            if is_primary:
                primary_exc = exc
                continue
            if host == "eu":
                ctx.greenhouse_eu_unavailable = True
            ctx.log.debug("greenhouse %s: fallback host %s unreachable: %s", token, host, exc)
            break

        if resp.status_code == 404:
            if is_primary:
                primary_resp = resp
                continue
            break

        resp.raise_for_status()
        jobs = _greenhouse_parse(cfg, resp.json())
        if host != preferred:
            # Memoize so later runs skip the wasted round trip forever.
            cfg.setdefault("_state_updates", {})["greenhouse_host"] = host
        return resp.status_code, jobs

    # Report the PREFERRED host's outcome. A fallback that is merely unreachable must
    # never mask the real answer -- otherwise a plain 404 (a dead token, actionable) gets
    # reported as a DNS failure (a network blip, not actionable).
    if primary_resp is not None:
        primary_resp.raise_for_status()
    raise primary_exc if primary_exc else RuntimeError(f"greenhouse {token}: no response")


def greenhouse_fetch_content(cfg: dict, job_id: str, ctx: FetchContext) -> str:
    """GET /v1/boards/{token}/jobs/{job_id} -- the description body for one job.

    Called only for jobs that are new AND passed the title filter, so this is typically
    0-5 requests per run. Returns "" on any failure: a missing description means the
    clearance check is skipped, never that the job is dropped.
    """
    sid = cfg["source_id"]
    host = ctx.hint(sid, "greenhouse_host", "us")
    url = f"{GREENHOUSE_HOSTS[host]}/v1/boards/{cfg['token']}/jobs/{job_id}"
    try:
        resp = http_request("GET", url, ctx=ctx)
        resp.raise_for_status()
        return resp.json().get("content") or ""
    except Exception as exc:
        ctx.log.warning("content fetch failed for %s::%s: %s", sid, job_id, exc)
        return ""


# --------------------------------------------------------------------------------------
# Lever
# --------------------------------------------------------------------------------------


def fetch_lever(cfg: dict, ctx: FetchContext) -> tuple[int, list[dict]]:
    """GET /v0/postings/{slug}?mode=json

    Two shapes to watch. The response is a BARE TOP-LEVEL ARRAY, not an object wrapping a
    jobs key, and the title field is `text`, not `title`.

    `&commitment=Intern` is deliberately not sent. Section 4.2 warns that many companies
    mislabel commitment, and a server-side filter can only ever lose jobs -- the client-side
    title filter is the real gate either way.
    """
    slug = cfg["slug"]
    url = f"https://api.lever.co/v0/postings/{slug}?mode=json"
    resp = http_request("GET", url, ctx=ctx)
    resp.raise_for_status()

    data = resp.json()
    if not isinstance(data, list):
        raise ValueError(f"lever {slug}: expected a top-level array, got {type(data).__name__}")

    jobs = []
    for raw in data:
        categories = raw.get("categories") or {}
        jobs.append(
            normalize_job(
                cfg,
                job_id=raw.get("id"),
                title=raw.get("text"),          # NOT "title"
                url=raw.get("hostedUrl") or raw.get("applyUrl"),
                location=categories.get("location"),
                department=categories.get("team") or categories.get("department"),
                posted_at=_epoch_ms_to_iso(raw.get("createdAt")),   # epoch MILLISECONDS
                content=raw.get("descriptionPlain") or raw.get("description") or "",
            )
        )
    return resp.status_code, jobs


# --------------------------------------------------------------------------------------
# Ashby
# --------------------------------------------------------------------------------------


def fetch_ashby(cfg: dict, ctx: FetchContext) -> tuple[int, list[dict]]:
    """GET /posting-api/job-board/{boardName}

    The board name is CASE-SENSITIVE -- sources.json carries `Luminary` and `K2space` with
    capitals, and lowercasing either one 404s. Nothing here may casefold it.
    """
    board = cfg["board"]
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
    resp = http_request("GET", url, ctx=ctx)
    resp.raise_for_status()

    payload = resp.json()
    jobs = []
    for raw in payload.get("jobs") or []:
        jobs.append(
            normalize_job(
                cfg,
                job_id=raw.get("id"),
                title=raw.get("title"),
                url=raw.get("jobUrl") or raw.get("applyUrl"),
                location=raw.get("location"),
                department=raw.get("department") or raw.get("team"),
                posted_at=_clean(raw.get("publishedAt") or raw.get("updatedAt")),
                content=raw.get("descriptionPlain") or "",
            )
        )
    return resp.status_code, jobs


# --------------------------------------------------------------------------------------
# Workday
# --------------------------------------------------------------------------------------


def _workday_host(cfg: dict) -> str:
    return f"https://{cfg['tenant']}.{cfg['dc']}.myworkdayjobs.com"


def _workday_parse(cfg: dict, postings: list[dict]) -> list[dict]:
    base = f"{_workday_host(cfg)}/en-US/{cfg['site']}"
    jobs = []
    for raw in postings:
        # externalPath over bulletFields[0] as the id. Both are stable, but externalPath is
        # a schema field rather than a display array, and it IS the url -- so id and link
        # can never disagree. Wisk proves it survives a retitle: a posting titled "Sr. Staff
        # Software Development Engineer" still has externalPath
        # ".../Avionics-Software-Architect_JR100285".
        path = _clean(raw.get("externalPath"))
        bullets = raw.get("bulletFields") or []
        job_id = path or _clean(bullets[0] if bullets else "")
        if not job_id:
            # MEASURED: 2 of NVIDIA's 1019 postings carry no title, externalPath,
            # locationsText or postedOn at all. Skip them -- deriving an id from the title
            # would re-notify on every title edit, the one thing this must never do.
            log.debug("%s: skipping a posting with no externalPath or bulletFields",
                      cfg["source_id"])
            continue
        jobs.append(
            normalize_job(
                cfg,
                job_id=job_id,
                title=raw.get("title"),
                url=base + "/" + path.lstrip("/") if path else base,
                # Verbatim, including the literal "2 Locations" that CAE returns.
                # location is display-only: nothing filters or dedupes on it, and blanking
                # it would throw away the fact that the role is multi-site.
                location=raw.get("locationsText"),
                # Prose ("Posted 3 Days Ago"), not a date, and Boston Dynamics omits it
                # entirely. Nothing consumes posted_at, so store it and do not parse it.
                posted_at=_clean(raw.get("postedOn")),
            )
        )
    return jobs


def fetch_workday(cfg: dict, ctx: FetchContext) -> tuple[int, list[dict]]:
    """POST /wday/cxs/{tenant}/{site}/jobs, paginated.

    The one thing to get right here: **`total` is returned only on the FIRST page.** Every
    page after offset=0 reports `total: 0`. Section 4.4 says "increment offset by limit
    until offset >= total", which read as a per-page instruction truncates every board to
    40 jobs -- with a 200, no exception, and a plausible-looking log line. Blue Origin
    would yield 40 of 1632.

    Section 4.4 also says a browser User-Agent and JSON headers are required or Workday
    404s. MEASURED 2026-09-14: not true for any of the six tenants -- all six return
    identical results with the plain descriptive UA, and Blue Origin and NVIDIA answer even
    with requests' default UA and no JSON headers. So no browser spoofing; section 8's
    descriptive-UA rule is honoured literally. The JSON headers are still sent explicitly,
    as cheap insurance for a tenant that might enforce them.
    """
    url = f"{_workday_host(cfg)}/wday/cxs/{cfg['tenant']}/{cfg['site']}/jobs"
    jobs: list[dict] = []
    offset = 0
    page = 0
    total: int | None = None
    status = 0

    while True:
        resp = http_request(
            "POST", url, ctx=ctx,
            json_body={"appliedFacets": {}, "limit": WORKDAY_PAGE_SIZE,
                       "offset": offset, "searchText": WORKDAY_SEARCH_TEXT},
            headers={"Content-Type": "application/json"},
        )
        resp.raise_for_status()
        payload = resp.json()
        page += 1
        if page == 1:
            # Positional, NOT `if total is None`. If page 1 ever omits the key, the `is
            # None` form latches page 2's total:0 and truncates the board to 40.
            status = resp.status_code
            total = payload.get("total")

        postings = payload.get("jobPostings") or []
        if not postings:
            # The real terminator -- true for every board regardless of what total says,
            # and what makes a missing total safe.
            break

        jobs.extend(_workday_parse(cfg, postings))
        offset += WORKDAY_PAGE_SIZE
        # offset >= total, not len(jobs) >= total: a short-but-nonempty page (or a skipped
        # malformed posting) would make the len form loop forever.
        if total and offset >= total:
            break
        if page >= WORKDAY_MAX_PAGES:
            log.warning("%s: hit the %d page cap at %d jobs; board may be truncated",
                        cfg["source_id"], WORKDAY_MAX_PAGES, len(jobs))
            break

    if total and page < WORKDAY_MAX_PAGES and len(jobs) < total - WORKDAY_SHORTFALL_TOLERANCE:
        # The operational tripwire for the total-only-on-page-1 bug. Warn, never raise: a
        # board that drops one posting mid-pagination is normal churn and must not cost the
        # other 476.
        log.warning("%s: collected %d of %d -- pagination may be truncated",
                    cfg["source_id"], len(jobs), total)

    cfg["_pages"] = page
    return status, jobs


# --------------------------------------------------------------------------------------
# BambooHR
# --------------------------------------------------------------------------------------


def _bamboo_location(raw: dict) -> str:
    """city/state, falling back to atsLocation, then to Remote.

    Both location objects can be present-but-all-null, so this cannot just truthiness-check
    the dict -- it has to look at the parts.
    """
    for key in ("location", "atsLocation"):
        blob = raw.get(key) or {}
        parts = [_clean(blob.get(f)) for f in ("city", "state", "province", "country")]
        joined = ", ".join(p for p in parts if p)
        if joined:
            return joined + (" (Remote)" if raw.get("isRemote") else "")
    return "Remote" if raw.get("isRemote") else ""


def fetch_bamboohr(cfg: dict, ctx: FetchContext) -> tuple[int, list[dict]]:
    """GET /careers/list -- a `result` array (section 4.6).

    The payload carries NO url of any kind, so the apply link is constructed. Verified live
    2026-09-14 that https://{subdomain}.bamboohr.com/careers/{id} returns 200.

    employmentType is ignored for the same reason Lever commitment is: companies mislabel
    it, and the title filter is the real gate.
    """
    sub = cfg["subdomain"]
    resp = http_request("GET", f"https://{sub}.bamboohr.com/careers/list", ctx=ctx)
    resp.raise_for_status()

    payload = resp.json()
    result = payload.get("result")
    if result is None:
        result = []
    if not isinstance(result, list):
        raise ValueError(f"bamboohr {sub}: expected `result` to be an array, "
                         f"got {type(result).__name__}")

    jobs = []
    for raw in result:
        job_id = _clean(raw.get("id"))
        if not job_id:
            continue
        jobs.append(
            normalize_job(
                cfg,
                job_id=job_id,
                title=raw.get("jobOpeningName"),
                url=raw.get("jobOpeningShareUrl")
                    or f"https://{sub}.bamboohr.com/careers/{job_id}",
                location=_bamboo_location(raw),
                department=raw.get("departmentLabel"),
            )
        )
    return resp.status_code, jobs


# --------------------------------------------------------------------------------------
# Pinpoint
# --------------------------------------------------------------------------------------


def _pinpoint_location(raw: dict) -> str:
    """The field is a dict on Impulse but may be a plain string elsewhere."""
    loc = raw.get("location")
    if isinstance(loc, str):
        return _clean(loc)
    if not isinstance(loc, dict):
        return ""
    parts = [_clean(loc.get(f)) for f in ("city", "province", "country")]
    joined = ", ".join(p for p in parts if p)
    return joined or _clean(loc.get("name"))


def fetch_pinpoint(cfg: dict, ctx: FetchContext) -> tuple[int, list[dict]]:
    """GET /postings.json.

    Section 4.7 files Impulse Space under scrape sources, but it is not a scrape at all --
    this is a clean JSON board API that nobody had checked. Note the payload key is `data`,
    not `jobs`.

    Descriptions ship inline, so unlike Lever/Ashby/Workday these jobs get the section 6
    clearance check for free -- which matters for a US space company.
    """
    sub = cfg["subdomain"]
    resp = http_request("GET", f"https://{sub}.pinpointhq.com/postings.json", ctx=ctx)
    resp.raise_for_status()

    payload = resp.json()
    data = payload.get("data")
    if data is None:
        data = []
    if not isinstance(data, list):
        raise ValueError(f"pinpoint {sub}: expected `data` to be an array, "
                         f"got {type(data).__name__}")

    jobs = []
    for raw in data:
        job_id = _clean(raw.get("id"))
        if not job_id:
            continue
        url = _clean(raw.get("url"))
        if url and not url.startswith("http"):
            url = f"https://{sub}.pinpointhq.com/{url.lstrip('/')}"
        jobs.append(
            normalize_job(
                cfg,
                job_id=job_id,
                title=raw.get("title"),
                url=url,
                location=_pinpoint_location(raw),
                department=(raw.get("department") or {}).get("name")
                           if isinstance(raw.get("department"), dict)
                           else raw.get("department"),
                posted_at=_clean(raw.get("published_at") or raw.get("created_at")),
                content=raw.get("description") or "",
            )
        )
    return resp.status_code, jobs


# --------------------------------------------------------------------------------------
# Workable
# --------------------------------------------------------------------------------------


def fetch_workable(cfg: dict, ctx: FetchContext) -> tuple[int, list[dict]]:
    """POST /api/v3/accounts/{subdomain}/jobs -- a `results` array.

    Section 4.5 documents this path as a GET, which is why it 404s: the path is right, the
    METHOD is wrong. Verified live 2026-09-14 that the same url answers 200 to a POST with
    an empty JSON body.

    The payload carries no apply url, so it is constructed from `shortcode`; verified that
    https://apply.workable.com/{subdomain}/j/{shortcode}/ returns 200. shortcode is
    preferred over the numeric id for the same reason Workday uses externalPath: it is the
    url, so id and link can never disagree.
    """
    sub = cfg["subdomain"]
    resp = http_request(
        "POST", f"https://apply.workable.com/api/v3/accounts/{sub}/jobs",
        ctx=ctx, json_body={}, headers={"Content-Type": "application/json"},
    )
    resp.raise_for_status()

    payload = resp.json()
    results = payload.get("results")
    if results is None:
        results = []
    if not isinstance(results, list):
        raise ValueError(f"workable {sub}: expected `results` to be an array, "
                         f"got {type(results).__name__}")

    jobs = []
    for raw in results:
        job_id = _clean(raw.get("shortcode") or raw.get("id"))
        if not job_id:
            continue
        location = raw.get("location") or {}
        # department is a LIST here, unlike every other ATS.
        dept = raw.get("department")
        if isinstance(dept, list):
            dept = "; ".join(_clean(d) for d in dept if d)
        jobs.append(
            normalize_job(
                cfg,
                job_id=job_id,
                title=raw.get("title"),
                url=f"https://apply.workable.com/{sub}/j/{job_id}/",
                location=location.get("display") if isinstance(location, dict) else location,
                department=dept,
                posted_at=_clean(raw.get("published")),
            )
        )
    return resp.status_code, jobs


# --------------------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------------------

Handler = Callable[[dict, FetchContext], "tuple[int, list[dict]]"]

HANDLERS: dict[str, Handler] = {
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "workday": fetch_workday,
    "bamboohr": fetch_bamboohr,
    "pinpoint": fetch_pinpoint,
    "workable": fetch_workable,
}


def fetch_source(cfg: dict, ctx: FetchContext) -> FetchResult:
    """The isolation boundary. Never raises.

    The broad `except Exception` is the single most important line for section 3 step 2.
    KeyboardInterrupt and SystemExit derive from BaseException, so they still propagate.
    """
    sid = cfg["source_id"]
    company = cfg.get("company", "")
    started = time.monotonic()
    cfg.pop("_state_updates", None)
    cfg.pop("_pages", None)

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    handler = HANDLERS.get(cfg["ats"])
    if handler is None:
        return FetchResult(sid, company, False, None, [], f"no handler for ats {cfg['ats']!r}",
                           elapsed(), 0, {})

    try:
        status, jobs = handler(cfg, ctx)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else None
        return FetchResult(sid, company, False, status, [], f"HTTP {status}", elapsed(), 0, {})
    except Exception as exc:
        ctx.log.debug("fetch failed for %s", sid, exc_info=True)
        return FetchResult(sid, company, False, None, [], f"{type(exc).__name__}: {exc}",
                           elapsed(), 0, {})

    return FetchResult(sid, company, True, status, jobs, "", elapsed(),
                       cfg.pop("_pages", 1), cfg.pop("_state_updates", {}))
