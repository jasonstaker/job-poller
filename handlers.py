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

# Which sources.json field carries the board identifier, per ATS. The spec uses a
# different key name for each, so this is the one place that difference is resolved.
ID_FIELDS: dict[str, tuple[str, ...]] = {
    "greenhouse": ("token",),
    "lever": ("slug",),
    "ashby": ("board",),
    "workday": ("tenant", "site"),
    "workable": ("subdomain",),
    "bamboohr": ("subdomain",),
}

# Present in sources.json but with no handler yet (build order step 9).
SKIP_ATS = frozenset({"scrape_later"})


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
    timeout: tuple[float, float] = (5.0, 20.0)   # (connect, read)
    user_agent: str = UA_DEFAULT
    jitter: bool = True
    log: logging.Logger = log
    # Per-source memoized hints read back from state/seen.json, keyed by source id.
    state_hints: dict[str, dict] = field(default_factory=dict)
    requests_made: int = 0

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

    last_status = None
    for host in order:
        url = f"{GREENHOUSE_HOSTS[host]}/v1/boards/{token}/jobs"
        resp = http_request("GET", url, ctx=ctx)
        last_status = resp.status_code
        if resp.status_code == 404:
            ctx.log.debug("greenhouse %s 404 on %s host", token, host)
            continue
        resp.raise_for_status()
        jobs = _greenhouse_parse(cfg, resp.json())
        if host != preferred:
            # Memoize so later runs skip the wasted round trip forever.
            cfg.setdefault("_state_updates", {})["greenhouse_host"] = host
        return resp.status_code, jobs

    resp.raise_for_status()  # every host 404'd; surface it as a real failure
    return last_status, []   # pragma: no cover - raise_for_status always fires above


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
# Dispatch
# --------------------------------------------------------------------------------------

Handler = Callable[[dict, FetchContext], "tuple[int, list[dict]]"]

HANDLERS: dict[str, Handler] = {
    "greenhouse": fetch_greenhouse,
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
