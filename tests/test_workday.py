"""Tests for the Workday handler.

Docstrings quote the live measurements taken 2026-09-14 against all six tenants, because
several of these behaviours contradict BUILD_SPEC.md and the evidence is the only reason
the code looks the way it does.
"""

from __future__ import annotations

import handlers
from handlers import fetch_source
from tests.conftest import FakeResponse

WD_URL = "https://blueorigin.wd5.myworkdayjobs.com/wday/cxs/blueorigin/BlueOrigin/jobs"


def wd_cfg(tenant="blueorigin", dc="wd5", site="BlueOrigin", company="Blue Origin"):
    cfg = {"ats": "workday", "tenant": tenant, "dc": dc, "site": site, "company": company}
    cfg["source_id"] = handlers.source_id(cfg)
    return cfg


def wd_posting(i):
    return {
        "title": f"Software Engineer Intern {i}",
        "externalPath": f"/job/Kent-WA/Software-Engineer-Intern-{i}_R{1000 + i}",
        "locationsText": "Kent, WA",
        "postedOn": "Posted 3 Days Ago",
        "bulletFields": [f"R{1000 + i}"],
    }


def wd_board(postings, total, total_later=0):
    """A fake that serves by offset and mimics the real `total` behaviour.

    MEASURED: only offset=0 carries a real total; every later page reports 0.
    """
    def route(method, url, body, headers):
        off, lim = body["offset"], body["limit"]
        return FakeResponse(200, {
            "jobPostings": postings[off:off + lim],
            "total": total if off == 0 else total_later,
        })
    return route


# --------------------------------------------------------------------------------------
# Pagination
# --------------------------------------------------------------------------------------


def test_reads_total_only_from_the_first_page(ctx_factory):
    """THE regression test for the most dangerous sentence in the spec.

    Section 4.4 says "increment offset by limit until offset >= total". Workday returns
    `total` only at offset=0 and reports 0 on every later page, so re-reading it per page
    stops after 40 jobs -- with HTTP 200, no exception, and a plausible log line. Blue
    Origin would silently yield 40 of its 1632.
    """
    cfg = wd_cfg()
    ctx, session = ctx_factory({WD_URL: wd_board([wd_posting(i) for i in range(100)], 100)})
    status, jobs = handlers.fetch_workday(cfg, ctx)

    assert status == 200
    assert len(jobs) == 100, "re-reading `total` each page truncates this to 40"
    assert len(session.calls) == 5


def test_sends_increasing_offsets(ctx_factory):
    cfg = wd_cfg()
    ctx, session = ctx_factory({WD_URL: wd_board([wd_posting(i) for i in range(45)], 45)})
    handlers.fetch_workday(cfg, ctx)
    assert [c.body["offset"] for c in session.calls] == [0, 20, 40]


def test_limit_is_always_twenty(ctx_factory):
    """MEASURED: limit above 20 returns ZERO postings and total=null. It does not clamp."""
    cfg = wd_cfg()
    ctx, session = ctx_factory({WD_URL: wd_board([wd_posting(i) for i in range(45)], 45)})
    handlers.fetch_workday(cfg, ctx)
    assert all(c.body["limit"] == 20 for c in session.calls)


def test_sends_no_facets(ctx_factory):
    """The workerSubType "Intern (Fixed Term)" facet looks cheaper and is wrong.

    MEASURED at Blue Origin: 29 jobs returned, but 6 of the 10 that pass our title filter
    are missing -- including "2026 Intern Conversion - Software Development Engineer I"
    and "Software Development Engineer I - Early Career (2027 Starts)", which Workday
    classifies as Regular rather than Intern.
    """
    cfg = wd_cfg()
    ctx, session = ctx_factory({WD_URL: wd_board([wd_posting(0)], 1)})
    handlers.fetch_workday(cfg, ctx)
    assert all(c.body["appliedFacets"] == {} for c in session.calls)


def test_search_text_is_intern(ctx_factory):
    """MEASURED: full-text over descriptions, and it misses zero title-filter-passing
    roles (10/10 Blue Origin, 3/3 Cadence) at a third of the request cost. "co-op" is
    useless as a second pass -- it matches every posting on the board."""
    cfg = wd_cfg()
    ctx, session = ctx_factory({WD_URL: wd_board([wd_posting(0)], 1)})
    handlers.fetch_workday(cfg, ctx)
    assert all(c.body["searchText"] == "intern" for c in session.calls)


def test_max_pages_does_not_truncate_nvidia():
    """NVIDIA needs 51 pages for its 1019 hits. Never tune the cap below that."""
    assert handlers.WORKDAY_MAX_PAGES >= 60


def test_stops_on_an_empty_page(ctx_factory):
    """The terminator that holds even when `total` lies."""
    cfg = wd_cfg()
    ctx, session = ctx_factory({WD_URL: wd_board([wd_posting(i) for i in range(20)], 500)})
    _, jobs = handlers.fetch_workday(cfg, ctx)
    assert len(jobs) == 20 and len(session.calls) == 2


def test_missing_total_still_terminates(ctx_factory):
    cfg = wd_cfg()

    def route(method, url, body, headers):
        off = body["offset"]
        return FakeResponse(200, {
            "jobPostings": [wd_posting(i) for i in range(off, min(off + 20, 30))]
        })

    ctx, _ = ctx_factory({WD_URL: route})
    _, jobs = handlers.fetch_workday(cfg, ctx)
    assert len(jobs) == 30


def test_runaway_cap_stops_and_warns(ctx_factory, caplog):
    cfg = wd_cfg()

    def route(method, url, body, headers):
        return FakeResponse(200, {
            "total": 999999 if body["offset"] == 0 else 0,
            "jobPostings": [wd_posting(i) for i in range(20)],
        })

    ctx, session = ctx_factory({WD_URL: route})
    with caplog.at_level("WARNING"):
        handlers.fetch_workday(cfg, ctx)
    assert len(session.calls) == handlers.WORKDAY_MAX_PAGES
    assert "page cap" in caplog.text


def test_warns_when_short_of_total(ctx_factory, caplog):
    """The operational tripwire: 40 of 477 is exactly what the pagination bug looks like."""
    cfg = wd_cfg()
    ctx, _ = ctx_factory({WD_URL: wd_board([wd_posting(i) for i in range(40)], 477)})
    with caplog.at_level("WARNING"):
        handlers.fetch_workday(cfg, ctx)
    assert "collected 40 of 477" in caplog.text


def test_small_shortfall_does_not_warn(ctx_factory, caplog):
    """A board dropping one posting mid-pagination is ordinary churn, not a bug."""
    cfg = wd_cfg()
    ctx, _ = ctx_factory({WD_URL: wd_board([wd_posting(i) for i in range(19)], 20)})
    with caplog.at_level("WARNING"):
        handlers.fetch_workday(cfg, ctx)
    assert "truncated" not in caplog.text


def test_http_error_mid_pagination_raises_rather_than_returning_partial(ctx_factory):
    """Fail loud, never partial.

    A partial return would look like success and silence the section 9 alert. Nothing is
    lost by raising: unfetched jobs were never recorded as seen, so the next good poll
    diffs and notifies them normally.
    """
    cfg = wd_cfg()

    def route(method, url, body, headers):
        if body["offset"] == 0:
            return FakeResponse(200, {"total": 100,
                                      "jobPostings": [wd_posting(i) for i in range(20)]})
        return FakeResponse(500, {})

    ctx, _ = ctx_factory({WD_URL: route})
    result = fetch_source(cfg, ctx)
    assert not result.ok and result.status == 500 and result.jobs == []


def test_empty_board_is_not_an_error(ctx_factory):
    cfg = wd_cfg()
    ctx, _ = ctx_factory({WD_URL: wd_board([], 0)})
    status, jobs = handlers.fetch_workday(cfg, ctx)
    assert status == 200 and jobs == []


def test_reports_pages(ctx_factory):
    cfg = wd_cfg()
    ctx, _ = ctx_factory({WD_URL: wd_board([wd_posting(i) for i in range(45)], 45)})
    assert fetch_source(cfg, ctx).pages == 3


# --------------------------------------------------------------------------------------
# Normalization
# --------------------------------------------------------------------------------------

WISK_URL = "https://wisk.wd108.myworkdayjobs.com/wday/cxs/wisk/Wisk_Careers/jobs"

# The real Wisk posting: the title was changed at some point but externalPath still reads
# Avionics-Software-Architect. Direct evidence that externalPath is minted once and
# survives a retitle, which is why it is the id.
WISK_RETITLED = {
    "title": "Sr. Staff Software Development Engineer",
    "externalPath": "/job/Mountain-View-CA/Avionics-Software-Architect_JR100285",
    "locationsText": "Mountain View, CA",
    "postedOn": "Posted 30+ Days Ago",
    "bulletFields": ["JR100285"],
}


def test_job_id_is_external_path_not_bulletfield(ctx_factory):
    cfg = wd_cfg("wisk", "wd108", "Wisk_Careers", "Wisk Aero")
    ctx, _ = ctx_factory({WISK_URL: wd_board([WISK_RETITLED], 1)})
    _, jobs = handlers.fetch_workday(cfg, ctx)

    assert jobs[0]["job_id"] == "/job/Mountain-View-CA/Avionics-Software-Architect_JR100285"
    assert jobs[0]["job_id"] != "JR100285"


def test_title_change_does_not_change_the_job_id(ctx_factory):
    """The never-re-notify invariant, stated positively."""
    cfg = wd_cfg("wisk", "wd108", "Wisk_Careers", "Wisk Aero")
    ids = []
    for payload in (WISK_RETITLED, dict(WISK_RETITLED, title="Completely Different Title")):
        ctx, _ = ctx_factory({WISK_URL: wd_board([payload], 1)})
        _, jobs = handlers.fetch_workday(cfg, ctx)
        ids.append(jobs[0]["job_id"])
    assert ids[0] == ids[1]


def test_builds_the_apply_url(ctx_factory):
    cfg = wd_cfg()
    ctx, _ = ctx_factory({WD_URL: wd_board([wd_posting(7)], 1)})
    _, jobs = handlers.fetch_workday(cfg, ctx)
    assert jobs[0]["url"] == (
        "https://blueorigin.wd5.myworkdayjobs.com/en-US/BlueOrigin"
        "/job/Kent-WA/Software-Engineer-Intern-7_R1007"
    )
    assert "//job" not in jobs[0]["url"].replace("https://", "")


def test_missing_posted_on_is_empty_not_a_crash(ctx_factory):
    """MEASURED: Boston Dynamics omits postedOn entirely."""
    cfg = wd_cfg("bostondynamics", "wd1", "Boston_Dynamics", "Boston Dynamics")
    url = ("https://bostondynamics.wd1.myworkdayjobs.com"
           "/wday/cxs/bostondynamics/Boston_Dynamics/jobs")
    raw = {"title": "Software Engineering Intern", "externalPath": "/job/Waltham-MA/X_R1",
           "locationsText": "Waltham, MA", "bulletFields": ["R1"]}
    ctx, _ = ctx_factory({url: wd_board([raw], 1)})
    _, jobs = handlers.fetch_workday(cfg, ctx)
    assert jobs[0]["posted_at"] == ""
    assert jobs[0]["title"] == "Software Engineering Intern"


def test_locations_text_passes_through_verbatim(ctx_factory):
    """CAE returns the literal string "2 Locations". location is display-only -- nothing
    filters or dedupes on it, and blanking it would discard the multi-site fact."""
    cfg = wd_cfg()
    ctx, _ = ctx_factory({WD_URL: wd_board([dict(wd_posting(1), locationsText="2 Locations")], 1)})
    _, jobs = handlers.fetch_workday(cfg, ctx)
    assert jobs[0]["location"] == "2 Locations"


def test_posting_without_external_path_is_skipped(ctx_factory):
    """MEASURED: 2 of NVIDIA's 1019 postings carry no title, path, location or date."""
    cfg = wd_cfg()
    ctx, _ = ctx_factory({WD_URL: wd_board([wd_posting(1), {}, wd_posting(2)], 3)})
    _, jobs = handlers.fetch_workday(cfg, ctx)
    assert len(jobs) == 2


def test_falls_back_to_bulletfield_when_path_is_missing(ctx_factory):
    cfg = wd_cfg()
    raw = {"title": "Software Intern", "locationsText": "Kent, WA", "bulletFields": ["R99"]}
    ctx, _ = ctx_factory({WD_URL: wd_board([raw], 1)})
    _, jobs = handlers.fetch_workday(cfg, ctx)
    assert jobs[0]["job_id"] == "R99"


def test_job_id_contains_no_key_separator(ctx_factory):
    """seen_key is "{source}::{job_id}"; a "::" in the id would break the split."""
    cfg = wd_cfg()
    ctx, _ = ctx_factory({WD_URL: wd_board([wd_posting(i) for i in range(5)], 5)})
    _, jobs = handlers.fetch_workday(cfg, ctx)
    assert all("::" not in j["job_id"] for j in jobs)


# --------------------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------------------


def test_uses_post_with_json_headers(ctx_factory):
    cfg = wd_cfg()
    ctx, session = ctx_factory({WD_URL: wd_board([wd_posting(0)], 1)})
    handlers.fetch_workday(cfg, ctx)
    call = session.calls[0]
    assert call.method == "POST"
    assert call.headers["Content-Type"] == "application/json"
    assert call.headers["Accept"] == "application/json"


def test_uses_the_descriptive_user_agent_not_a_browser_one(ctx_factory):
    """Section 4.4 says a browser User-Agent is required or Workday 404s.

    MEASURED 2026-09-14: false for all six tenants. They answer identically with the
    descriptive UA, and Blue Origin and NVIDIA answer even with the requests default and
    no JSON headers. So section 8 is honoured literally and nothing spoofs a browser.
    """
    cfg = wd_cfg()
    ctx, session = ctx_factory({WD_URL: wd_board([wd_posting(0)], 1)})
    handlers.fetch_workday(cfg, ctx)
    ua = session.calls[0].headers["User-Agent"]
    assert ua.startswith("job-poller/")
    assert "Mozilla" not in ua


def test_pagination_is_jittered(ctx_factory, monkeypatch):
    """Documents the cost model: 24 pages of Blue Origin is 23 jittered sleeps."""
    slept = []
    monkeypatch.setattr(handlers.time, "sleep", lambda s: slept.append(s))
    cfg = wd_cfg()
    ctx, _ = ctx_factory({WD_URL: wd_board([wd_posting(i) for i in range(45)], 45)})
    ctx.jitter = True
    handlers.fetch_workday(cfg, ctx)
    assert len(slept) == 2, "first request unjittered, then one sleep per extra page"
