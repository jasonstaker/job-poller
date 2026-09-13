"""Tests for handlers.py. Offline: every request goes through a FakeSession."""

from __future__ import annotations

import pytest
import requests

import handlers
from handlers import (
    GREENHOUSE_HOSTS,
    FetchContext,
    fetch_greenhouse,
    fetch_source,
    greenhouse_fetch_content,
    source_id,
)
from tests.conftest import FakeResponse

GH = GREENHOUSE_HOSTS["us"]
GH_EU = GREENHOUSE_HOSTS["eu"]


def gh_cfg(token="vardaspace", company="Varda Space"):
    cfg = {"ats": "greenhouse", "token": token, "company": company, "status": "verified"}
    cfg["source_id"] = source_id(cfg)
    return cfg


GREENHOUSE_PAYLOAD = {
    "jobs": [
        {
            "id": 7145321,  # int on the wire -- must come back as a str
            "title": "Software Engineer Intern, Flight Software",
            "absolute_url": "https://boards.greenhouse.io/vardaspace/jobs/7145321",
            "location": {"name": "El Segundo, CA"},
            "updated_at": "2026-09-10T18:04:00-04:00",
            "departments": [{"name": "Engineering"}],
            "offices": [{"name": "El Segundo"}],
        },
        {
            "id": 7145322,
            "title": "Propulsion Engineer",
            "absolute_url": "https://boards.greenhouse.io/vardaspace/jobs/7145322",
            "location": None,            # missing location must not crash
            "departments": [],
            "offices": [{"name": "Remote"}],
        },
    ]
}


# --------------------------------------------------------------------------------------
# source_id
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cfg,expected",
    [
        ({"ats": "greenhouse", "token": "vardaspace"}, "greenhouse:vardaspace"),
        ({"ats": "lever", "slug": "shieldai"}, "lever:shieldai"),
        ({"ats": "ashby", "board": "K2space"}, "ashby:K2space"),
        ({"ats": "workday", "tenant": "cadence", "site": "Univ_Careers"},
         "workday:cadence:Univ_Careers"),
    ],
)
def test_source_id(cfg, expected):
    assert source_id(cfg) == expected


def test_ashby_board_case_is_preserved():
    """Ashby board names are case-sensitive: 'Luminary' and 'K2space' have capitals."""
    assert source_id({"ats": "ashby", "board": "Luminary"}) == "ashby:Luminary"
    assert "::" not in source_id({"ats": "ashby", "board": "K2space"})


def test_the_two_k2_boards_get_distinct_ids():
    """Same company, two boards -- the ids must differ or the diff would collide."""
    gh = source_id({"ats": "greenhouse", "token": "k2spacecorporation"})
    ashby = source_id({"ats": "ashby", "board": "K2space"})
    assert gh != ashby


# --------------------------------------------------------------------------------------
# Greenhouse parsing
# --------------------------------------------------------------------------------------


def test_greenhouse_parses_and_normalizes(ctx_factory):
    cfg = gh_cfg()
    ctx, session = ctx_factory({f"{GH}/v1/boards/vardaspace/jobs":
                                FakeResponse(200, GREENHOUSE_PAYLOAD)})
    status, jobs = fetch_greenhouse(cfg, ctx)

    assert status == 200
    assert len(jobs) == 2
    job = jobs[0]
    assert job["job_id"] == "7145321"
    assert isinstance(job["job_id"], str), "int ids would cause silent duplicate pushes"
    assert job["source"] == "greenhouse:vardaspace"
    assert job["company"] == "Varda Space"
    assert job["title"] == "Software Engineer Intern, Flight Software"
    assert job["location"] == "El Segundo, CA"
    assert job["department"] == "Engineering"
    assert job["url"].endswith("/7145321")
    assert job["first_seen"] == "", "handlers must not invent first_seen"
    assert job["posted_at"] == "2026-09-10T18:04:00-04:00"


def test_greenhouse_falls_back_to_offices_when_location_missing(ctx_factory):
    cfg = gh_cfg()
    ctx, _ = ctx_factory({f"{GH}/v1/boards/vardaspace/jobs":
                          FakeResponse(200, GREENHOUSE_PAYLOAD)})
    _, jobs = fetch_greenhouse(cfg, ctx)
    assert jobs[1]["location"] == "Remote"
    assert jobs[1]["department"] == ""


def test_greenhouse_bulk_listing_omits_content_param(ctx_factory):
    """The bulk call must not use ?content=true -- that is tens of MB per run."""
    cfg = gh_cfg()
    ctx, session = ctx_factory({f"{GH}/v1/boards/vardaspace/jobs":
                                FakeResponse(200, GREENHOUSE_PAYLOAD)})
    fetch_greenhouse(cfg, ctx)
    assert all("content=true" not in url for _, url, _ in session.calls)


def test_greenhouse_empty_board_is_not_an_error(ctx_factory):
    cfg = gh_cfg("attaboticsish")
    ctx, _ = ctx_factory({f"{GH}/v1/boards/attaboticsish/jobs": FakeResponse(200, {"jobs": []})})
    status, jobs = fetch_greenhouse(cfg, ctx)
    assert status == 200 and jobs == []


# --------------------------------------------------------------------------------------
# Greenhouse EU fallback
# --------------------------------------------------------------------------------------


def test_eu_fallback_on_404_and_memoizes_host(ctx_factory):
    cfg = gh_cfg("physicsx", "PhysicsX")
    ctx, session = ctx_factory({
        f"{GH}/v1/boards/physicsx/jobs": FakeResponse(404, {}),
        f"{GH_EU}/v1/boards/physicsx/jobs": FakeResponse(200, {"jobs": []}),
    })
    status, jobs = fetch_greenhouse(cfg, ctx)

    assert status == 200
    assert cfg["_state_updates"] == {"greenhouse_host": "eu"}
    assert len(session.calls) == 2


def test_memoized_eu_host_skips_the_us_round_trip(ctx_factory):
    cfg = gh_cfg("physicsx", "PhysicsX")
    ctx, session = ctx_factory({f"{GH_EU}/v1/boards/physicsx/jobs":
                                FakeResponse(200, GREENHOUSE_PAYLOAD)})
    ctx.state_hints = {"greenhouse:physicsx": {"greenhouse_host": "eu"}}
    status, _ = fetch_greenhouse(cfg, ctx)

    assert status == 200
    assert len(session.calls) == 1, "a memoized host must not re-probe the other one"
    assert "_state_updates" not in cfg, "no update needed when the hint was already right"


def test_empty_200_does_not_trigger_eu_fallback(ctx_factory):
    """An empty board is legitimate; only a 404 means wrong host."""
    cfg = gh_cfg("ursamajor", "Ursa Major")
    ctx, session = ctx_factory({f"{GH}/v1/boards/ursamajor/jobs": FakeResponse(200, {"jobs": []})})
    fetch_greenhouse(cfg, ctx)
    assert len(session.calls) == 1


def test_404_on_both_hosts_raises(ctx_factory):
    cfg = gh_cfg("deadtoken")
    ctx, _ = ctx_factory({})
    with pytest.raises(requests.HTTPError):
        fetch_greenhouse(cfg, ctx)


# --------------------------------------------------------------------------------------
# Per-job content fetch
# --------------------------------------------------------------------------------------


def test_content_fetch_returns_body(ctx_factory):
    cfg = gh_cfg()
    ctx, _ = ctx_factory({f"{GH}/v1/boards/vardaspace/jobs/7145321":
                          FakeResponse(200, {"content": "&lt;p&gt;ITAR applies.&lt;/p&gt;"})})
    assert greenhouse_fetch_content(cfg, "7145321", ctx) == "&lt;p&gt;ITAR applies.&lt;/p&gt;"


def test_content_fetch_failure_returns_empty_not_raise(ctx_factory):
    """A missing description skips the clearance check; it must never drop the job."""
    cfg = gh_cfg()
    ctx, _ = ctx_factory({})
    assert greenhouse_fetch_content(cfg, "999", ctx) == ""


def test_content_fetch_uses_the_memoized_host(ctx_factory):
    cfg = gh_cfg("physicsx", "PhysicsX")
    ctx, session = ctx_factory({f"{GH_EU}/v1/boards/physicsx/jobs/42":
                                FakeResponse(200, {"content": "x"})})
    ctx.state_hints = {"greenhouse:physicsx": {"greenhouse_host": "eu"}}
    assert greenhouse_fetch_content(cfg, "42", ctx) == "x"
    assert session.calls[0][1].startswith(GH_EU)


# --------------------------------------------------------------------------------------
# fetch_source: the isolation boundary
# --------------------------------------------------------------------------------------


def test_fetch_source_success(ctx_factory):
    cfg = gh_cfg()
    ctx, _ = ctx_factory({f"{GH}/v1/boards/vardaspace/jobs":
                          FakeResponse(200, GREENHOUSE_PAYLOAD)})
    result = fetch_source(cfg, ctx)

    assert result.ok and result.status == 200
    assert len(result.jobs) == 2
    assert result.source == "greenhouse:vardaspace"
    assert result.error == ""


def test_fetch_source_swallows_http_error(ctx_factory):
    cfg = gh_cfg("deadtoken")
    ctx, _ = ctx_factory({})
    result = fetch_source(cfg, ctx)

    assert not result.ok
    assert result.status == 404
    assert result.jobs == []
    assert "404" in result.error


def test_fetch_source_swallows_arbitrary_exception(ctx_factory, monkeypatch):
    """One dead board must never abort the run (section 3 step 2)."""
    cfg = gh_cfg()
    ctx, _ = ctx_factory({})

    def boom(cfg, ctx):
        raise ValueError("malformed payload")

    monkeypatch.setitem(handlers.HANDLERS, "greenhouse", boom)
    result = fetch_source(cfg, ctx)

    assert not result.ok
    assert result.status is None
    assert "ValueError: malformed payload" in result.error


def test_fetch_source_swallows_timeout(ctx_factory, monkeypatch):
    cfg = gh_cfg()
    ctx, _ = ctx_factory({})

    def timeout(cfg, ctx):
        raise requests.ConnectTimeout("connect timed out")

    monkeypatch.setitem(handlers.HANDLERS, "greenhouse", timeout)
    result = fetch_source(cfg, ctx)
    assert not result.ok and "ConnectTimeout" in result.error


def test_fetch_source_reports_unknown_ats(ctx_factory):
    cfg = {"ats": "carrier_pigeon", "company": "X", "source_id": "carrier_pigeon:x"}
    ctx, _ = ctx_factory({})
    result = fetch_source(cfg, ctx)
    assert not result.ok and "no handler" in result.error


def test_fetch_source_propagates_state_updates(ctx_factory):
    cfg = gh_cfg("physicsx", "PhysicsX")
    ctx, _ = ctx_factory({
        f"{GH}/v1/boards/physicsx/jobs": FakeResponse(404, {}),
        f"{GH_EU}/v1/boards/physicsx/jobs": FakeResponse(200, {"jobs": []}),
    })
    result = fetch_source(cfg, ctx)
    assert result.state_updates == {"greenhouse_host": "eu"}


def test_fetch_source_does_not_leak_state_updates_between_runs(ctx_factory):
    cfg = gh_cfg()
    cfg["_state_updates"] = {"greenhouse_host": "eu"}   # stale leftover
    ctx, _ = ctx_factory({f"{GH}/v1/boards/vardaspace/jobs":
                          FakeResponse(200, GREENHOUSE_PAYLOAD)})
    result = fetch_source(cfg, ctx)
    assert result.state_updates == {}


# --------------------------------------------------------------------------------------
# Shared HTTP concerns
# --------------------------------------------------------------------------------------


def test_descriptive_user_agent_on_every_call(ctx_factory):
    cfg = gh_cfg()
    ctx, session = ctx_factory({f"{GH}/v1/boards/vardaspace/jobs":
                                FakeResponse(200, GREENHOUSE_PAYLOAD)})
    fetch_greenhouse(cfg, ctx)
    for _, _, hdrs in session.calls:
        assert hdrs["User-Agent"].startswith("job-poller/")
        assert "github.com/jasonstaker" in hdrs["User-Agent"]


def test_no_jitter_means_no_sleep(ctx_factory, monkeypatch):
    slept = []
    monkeypatch.setattr(handlers.time, "sleep", lambda s: slept.append(s))
    cfg = gh_cfg("physicsx")
    ctx, _ = ctx_factory({
        f"{GH}/v1/boards/physicsx/jobs": FakeResponse(404, {}),
        f"{GH_EU}/v1/boards/physicsx/jobs": FakeResponse(200, {"jobs": []}),
    })
    fetch_greenhouse(cfg, ctx)
    assert slept == []


def test_jitter_skips_the_first_request_then_applies(ctx_factory, monkeypatch):
    slept = []
    monkeypatch.setattr(handlers.time, "sleep", lambda s: slept.append(s))
    cfg = gh_cfg("physicsx")
    ctx, _ = ctx_factory({
        f"{GH}/v1/boards/physicsx/jobs": FakeResponse(404, {}),
        f"{GH_EU}/v1/boards/physicsx/jobs": FakeResponse(200, {"jobs": []}),
    })
    ctx.jitter = True
    fetch_greenhouse(cfg, ctx)

    assert len(slept) == 1, "first request unjittered, second jittered"
    assert 0.5 <= slept[0] <= 2.0


def test_every_request_carries_a_timeout(ctx_factory):
    """Section 11: every network call gets a timeout."""
    cfg = gh_cfg()
    captured = {}

    class RecordingSession:
        def request(self, method, url, json=None, headers=None, timeout=None):
            captured["timeout"] = timeout
            return FakeResponse(200, GREENHOUSE_PAYLOAD)

    ctx = FetchContext(session=RecordingSession(), jitter=False)
    fetch_greenhouse(cfg, ctx)
    assert captured["timeout"] == (5.0, 20.0)
