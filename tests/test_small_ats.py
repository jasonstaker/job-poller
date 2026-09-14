"""Tests for the three small-ATS handlers: BambooHR, Workable, Pinpoint.

Shapes are taken from live responses captured 2026-09-14. Each handler has at least one
quirk that an idealized hand-written payload would not have caught.
"""

from __future__ import annotations

import pytest

import handlers
from tests.conftest import FakeResponse

# --------------------------------------------------------------------------------------
# BambooHR
# --------------------------------------------------------------------------------------

BAMBOO_URL = "https://sedarotech.bamboohr.com/careers/list"

# Real shape: id is a STRING, departmentLabel and isRemote are null, and there is no url
# field of any kind anywhere in the payload.
BAMBOO_PAYLOAD = {
    "meta": {"totalCount": 2},
    "result": [
        {"id": "60", "jobOpeningName": "Lead Software Engineer, DevOps",
         "departmentId": None, "departmentLabel": None, "employmentStatusLabel": "Full-Time",
         "employmentType": None, "location": {"city": "Arlington", "state": "Virginia"},
         "atsLocation": {"country": None, "state": None, "province": None, "city": None},
         "isRemote": None, "locationType": "0"},
        {"id": "78", "jobOpeningName": "Software Engineering Intern",
         "departmentLabel": "Engineering",
         "location": {"city": None, "state": None}, "isRemote": True},
    ],
}


def bamboo_cfg():
    cfg = {"ats": "bamboohr", "subdomain": "sedarotech", "company": "Sedaro"}
    cfg["source_id"] = handlers.source_id(cfg)
    return cfg


def test_bamboohr_parses(ctx_factory):
    ctx, _ = ctx_factory({BAMBOO_URL: FakeResponse(200, BAMBOO_PAYLOAD)})
    status, jobs = handlers.fetch_bamboohr(bamboo_cfg(), ctx)

    assert status == 200 and len(jobs) == 2
    assert jobs[0]["job_id"] == "60"
    assert jobs[0]["title"] == "Lead Software Engineer, DevOps"
    assert jobs[0]["location"] == "Arlington, Virginia"
    assert jobs[0]["department"] == ""          # departmentLabel is null on the wire


def test_bamboohr_constructs_the_apply_url(ctx_factory):
    """The payload has no url field at all; verified live that this form returns 200."""
    ctx, _ = ctx_factory({BAMBOO_URL: FakeResponse(200, BAMBOO_PAYLOAD)})
    _, jobs = handlers.fetch_bamboohr(bamboo_cfg(), ctx)
    assert jobs[0]["url"] == "https://sedarotech.bamboohr.com/careers/60"


def test_bamboohr_all_null_location_falls_back_to_remote(ctx_factory):
    """Both location objects can be present but entirely null."""
    ctx, _ = ctx_factory({BAMBOO_URL: FakeResponse(200, BAMBOO_PAYLOAD)})
    _, jobs = handlers.fetch_bamboohr(bamboo_cfg(), ctx)
    assert jobs[1]["location"] == "Remote"


def test_bamboohr_non_list_result_raises(ctx_factory):
    ctx, _ = ctx_factory({BAMBOO_URL: FakeResponse(200, {"result": {"nope": 1}})})
    with pytest.raises(ValueError, match="array"):
        handlers.fetch_bamboohr(bamboo_cfg(), ctx)


def test_bamboohr_empty_board_is_not_an_error(ctx_factory):
    ctx, _ = ctx_factory({BAMBOO_URL: FakeResponse(200, {"result": []})})
    status, jobs = handlers.fetch_bamboohr(bamboo_cfg(), ctx)
    assert status == 200 and jobs == []


# --------------------------------------------------------------------------------------
# Workable
# --------------------------------------------------------------------------------------

WORKABLE_URL = "https://apply.workable.com/api/v3/accounts/ghgsat/jobs"

WORKABLE_PAYLOAD = {
    "total": 1,
    "results": [
        {"id": 6065668, "shortcode": "D278C951E3", "title": "Senior Backend Developer",
         "remote": False,
         "location": {"country": "Canada", "city": "Montreal", "region": "Quebec",
                      "display": "Montreal, Quebec, Canada"},
         "state": "published", "published": "2026-08-29T00:00:00.000Z",
         "department": ["Operations"]},      # a LIST, unlike every other ATS
    ],
}


def workable_cfg():
    cfg = {"ats": "workable", "subdomain": "ghgsat", "company": "GHGSat"}
    cfg["source_id"] = handlers.source_id(cfg)
    return cfg


def test_workable_uses_post_not_get(ctx_factory):
    """Section 4.5 documents a GET, which 404s. The path is right; the method is wrong."""
    ctx, session = ctx_factory({WORKABLE_URL: FakeResponse(200, WORKABLE_PAYLOAD)})
    handlers.fetch_workable(workable_cfg(), ctx)
    assert session.calls[0].method == "POST"


def test_workable_parses(ctx_factory):
    ctx, _ = ctx_factory({WORKABLE_URL: FakeResponse(200, WORKABLE_PAYLOAD)})
    status, jobs = handlers.fetch_workable(workable_cfg(), ctx)

    assert status == 200 and len(jobs) == 1
    assert jobs[0]["job_id"] == "D278C951E3", "shortcode, not the numeric id"
    assert jobs[0]["location"] == "Montreal, Quebec, Canada"
    assert jobs[0]["department"] == "Operations", "department is a list on the wire"
    assert jobs[0]["url"] == "https://apply.workable.com/ghgsat/j/D278C951E3/"


def test_workable_non_list_results_raises(ctx_factory):
    ctx, _ = ctx_factory({WORKABLE_URL: FakeResponse(200, {"results": "nope"})})
    with pytest.raises(ValueError, match="array"):
        handlers.fetch_workable(workable_cfg(), ctx)


# --------------------------------------------------------------------------------------
# Pinpoint
# --------------------------------------------------------------------------------------

PINPOINT_URL = "https://impulsespace.pinpointhq.com/postings.json"

PINPOINT_PAYLOAD = {
    "data": [
        {"id": "290785", "title": "Ground Software Engineering Intern (Summer 2027)",
         "url": "https://impulsespace.pinpointhq.com/en/postings/3ab1fb8a",
         "location": {"id": "6679", "city": "Redondo Beach", "province": "California",
                      "name": "Redondo Beach "},
         "department": None, "published_at": None, "created_at": None,
         "description": "<p>Must be a U.S. Person under ITAR.</p>"},
    ],
}


def pinpoint_cfg():
    cfg = {"ats": "pinpoint", "subdomain": "impulsespace", "company": "Impulse Space"}
    cfg["source_id"] = handlers.source_id(cfg)
    return cfg


def test_pinpoint_reads_the_data_key_not_jobs(ctx_factory):
    """The payload key is `data`. Same class of trap as Lever's bare top-level array."""
    ctx, _ = ctx_factory({PINPOINT_URL: FakeResponse(200, PINPOINT_PAYLOAD)})
    status, jobs = handlers.fetch_pinpoint(pinpoint_cfg(), ctx)
    assert status == 200 and len(jobs) == 1


def test_pinpoint_parses(ctx_factory):
    ctx, _ = ctx_factory({PINPOINT_URL: FakeResponse(200, PINPOINT_PAYLOAD)})
    _, jobs = handlers.fetch_pinpoint(pinpoint_cfg(), ctx)
    assert jobs[0]["job_id"] == "290785"
    assert jobs[0]["location"] == "Redondo Beach, California"
    assert jobs[0]["url"].startswith("https://impulsespace.pinpointhq.com/")


def test_pinpoint_supplies_content_for_the_clearance_check(ctx_factory):
    """Descriptions ship inline, so Impulse gets the section 6 clearance check for free."""
    ctx, _ = ctx_factory({PINPOINT_URL: FakeResponse(200, PINPOINT_PAYLOAD)})
    _, jobs = handlers.fetch_pinpoint(pinpoint_cfg(), ctx)
    assert "ITAR" in jobs[0]["content"]


def test_pinpoint_string_location_also_works(ctx_factory):
    """Observed as a dict at Impulse, but the field may be a plain string elsewhere."""
    payload = {"data": [dict(PINPOINT_PAYLOAD["data"][0], location="Remote")]}
    ctx, _ = ctx_factory({PINPOINT_URL: FakeResponse(200, payload)})
    _, jobs = handlers.fetch_pinpoint(pinpoint_cfg(), ctx)
    assert jobs[0]["location"] == "Remote"


def test_pinpoint_relative_url_is_absolutized(ctx_factory):
    payload = {"data": [dict(PINPOINT_PAYLOAD["data"][0], url="/en/postings/abc")]}
    ctx, _ = ctx_factory({PINPOINT_URL: FakeResponse(200, payload)})
    _, jobs = handlers.fetch_pinpoint(pinpoint_cfg(), ctx)
    assert jobs[0]["url"] == "https://impulsespace.pinpointhq.com/en/postings/abc"


def test_pinpoint_non_list_data_raises(ctx_factory):
    ctx, _ = ctx_factory({PINPOINT_URL: FakeResponse(200, {"data": {}})})
    with pytest.raises(ValueError, match="array"):
        handlers.fetch_pinpoint(pinpoint_cfg(), ctx)


# --------------------------------------------------------------------------------------
# Registry invariant
# --------------------------------------------------------------------------------------


def test_every_sources_json_key_has_a_handler_or_is_skipped():
    """Derived invariant, so it maintains itself as handlers come and go.

    Catches the two dangerous mistakes: a source block with no handler (polled and failing
    forever, marching toward the section 9 three-strike alert), and a handler that was
    written but never registered (its sources silently never polled).
    """
    import json
    import pathlib

    root = pathlib.Path(handlers.__file__).parent
    raw = json.loads((root / "sources.json").read_text(encoding="utf-8"))
    for ats in raw:
        assert ats in handlers.HANDLERS or ats in handlers.SKIP_ATS, (
            f"sources.json key {ats!r} is neither handled nor explicitly skipped"
        )
