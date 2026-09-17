"""Regression tests for the 2026-09-14 robustness audit.

Every test here reproduces a bug that was live in production. Each docstring states what the
bug cost, because the whole point of this suite is that these failures are SILENT -- the run
goes green, the state file looks healthy, and a real posting never reaches the phone.
"""

from __future__ import annotations

import json

import pytest

import handlers
import poller
from poller import load_state, new_state, save_state

from tests.test_state import GH_CFG, WD_CFG, Args, _run_with, _seeded_state, job, result


# --------------------------------------------------------------------------------------
# Finding 1 -- the anomaly branch burned matched jobs
# --------------------------------------------------------------------------------------


def test_anomaly_never_records_a_matching_job_without_pushing_it(tmp_path, monkeypatch):
    """The worst bug found: matching internships marked seen and never sent.

    The old guard tripped on the count of ALL new jobs across all 61 boards, then wrote every
    job that had PASSED the filter into seen.json as "summarized" without pushing it. Because
    diff_new keys on seen, those roles could never resurface. One Workday board re-keying its
    externalPath would bury a genuine Varda posting that appeared in the same run.
    """
    p = _seeded_state(tmp_path, ["greenhouse:vardaspace", "workday:blueorigin:BlueOrigin"])
    # A flood of non-matching jobs, plus two real matches hiding inside it.
    flood = [job(source="workday:blueorigin:BlueOrigin", job_id=f"f{i}",
                 title="Mechanical Technician", company="Blue Origin") for i in range(400)]
    real = [job(source="greenhouse:vardaspace", job_id="real1",
                title="Software Engineer Intern, Summer 2027", company="Varda Space"),
            job(source="greenhouse:vardaspace", job_id="real2",
                title="Flight Software Intern, Summer 2027", company="Varda Space")]
    _, sent = _run_with(monkeypatch, tmp_path, p,
                        {"greenhouse:vardaspace": real,
                         "workday:blueorigin:BlueOrigin": flood},
                        [GH_CFG, WD_CFG])

    final, _ = load_state(p)
    for j in real:
        entry = final["seen"].get(poller.seen_key(j))
        if entry is not None:
            assert entry["outcome"] in ("notified", "summarized"), (
                f"{j['title']} was recorded as {entry['outcome']} without being pushed")
    assert {j["job_id"] for j in sent} >= {"real1", "real2"} or not sent, \
        "matching jobs must be pushed, or left unseen to retry -- never silently buried"


def test_a_flood_of_non_matching_jobs_does_not_suppress_the_few_real_ones(tmp_path, monkeypatch):
    """The anomaly guard should care about NOTIFIABLE volume, not raw new-job volume.

    400 junk postings appearing at once is a board doing board things; 2 matching internships
    in the same run still deserve their pushes.
    """
    p = _seeded_state(tmp_path, ["greenhouse:vardaspace", "workday:blueorigin:BlueOrigin"])
    flood = [job(source="workday:blueorigin:BlueOrigin", job_id=f"f{i}",
                 title="Mechanical Technician", company="Blue Origin") for i in range(400)]
    real = [job(source="greenhouse:vardaspace", job_id="real1",
                title="Software Engineer Intern, Summer 2027", company="Varda Space")]
    _, sent = _run_with(monkeypatch, tmp_path, p,
                        {"greenhouse:vardaspace": real,
                         "workday:blueorigin:BlueOrigin": flood},
                        [GH_CFG, WD_CFG])
    assert [j["job_id"] for j in sent] == ["real1"]


def test_failed_push_in_the_anomaly_path_leaves_jobs_unseen(tmp_path, monkeypatch):
    """If the digest cannot be delivered, nothing may be recorded as seen."""
    p = _seeded_state(tmp_path, ["greenhouse:vardaspace"])
    many = [job(source="greenhouse:vardaspace", job_id=f"m{i}",
                title=f"Software Engineer Intern {i}, Summer 2027", company="Varda Space")
            for i in range(30)]
    monkeypatch.setattr(poller.notify, "notify_summary", lambda *a, **k: False)
    monkeypatch.setattr(poller.notify, "notify_job", lambda *a, **k: False)
    monkeypatch.setattr(poller.notify, "notify_alert", lambda *a, **k: True)
    monkeypatch.setattr(poller, "load_sources", lambda *a, **k: [GH_CFG])
    monkeypatch.setattr(poller, "build_session", lambda: object())
    monkeypatch.setattr(poller, "fetch_source",
                        lambda cfg, ctx: result(cfg["source_id"], jobs=many))
    poller.run(Args(p))

    final, _ = load_state(p)
    buried = [k for k, v in final["seen"].items() if v["outcome"] in ("notified", "summarized")]
    assert buried == [], "a failed push must leave every job unseen so the next run retries"


# --------------------------------------------------------------------------------------
# Finding 2 -- a failed first poll bypassed backfill
# --------------------------------------------------------------------------------------


def test_a_source_whose_first_poll_fails_is_still_backfilled_next_run(tmp_path, monkeypatch):
    """update_health created a health entry even for a FAILED poll.

    That promoted the board to "established", so on its next (successful) run all ~2,000 of
    its jobs arrived as fresh rather than backfill -- tripping the anomaly guard and, via
    finding 1, burning every matching role. Not hypothetical: Anduril's 2.3MB board
    intermittently times out, which is exactly this scenario.
    """
    p = _seeded_state(tmp_path, ["greenhouse:vardaspace"])

    # Run 1: the new Workday board fails outright.
    monkeypatch.setattr(poller, "load_sources", lambda *a, **k: [GH_CFG, WD_CFG])
    monkeypatch.setattr(poller, "build_session", lambda: object())
    monkeypatch.setattr(poller.notify, "notify_job", lambda *a, **k: True)
    monkeypatch.setattr(poller.notify, "notify_summary", lambda *a, **k: True)
    monkeypatch.setattr(poller.notify, "notify_alert", lambda *a, **k: True)
    monkeypatch.setattr(poller, "fetch_source", lambda cfg, ctx: result(
        cfg["source_id"], ok=cfg["ats"] != "workday", jobs=[], error="ConnectTimeout"))
    poller.run(Args(p))

    # Run 2: it succeeds, with a big board full of matching roles.
    wd_jobs = [job(source="workday:blueorigin:BlueOrigin", job_id=str(i),
                   title="Software Engineer Intern, Summer 2027", company="Blue Origin")
               for i in range(300)]
    sent = []
    monkeypatch.setattr(poller.notify, "notify_job", lambda j, **k: sent.append(j) or True)
    monkeypatch.setattr(poller.notify, "notify_summary", lambda js, **k: sent.extend(js) or True)
    monkeypatch.setattr(poller, "fetch_source", lambda cfg, ctx: result(
        cfg["source_id"], jobs=wd_jobs if cfg["ats"] == "workday" else []))
    poller.run(Args(p))

    final, _ = load_state(p)
    outcomes = {v["outcome"] for k, v in final["seen"].items() if k.startswith("workday:")}
    assert outcomes == {"backfill"}, f"expected a clean backfill, got {outcomes}"
    assert sent == [], "a first successful poll of a board must not fire 300 pushes"


# --------------------------------------------------------------------------------------
# Finding 3 -- empty state silently re-bootstrapped
# --------------------------------------------------------------------------------------


def test_zero_byte_state_is_corrupt_not_a_fresh_start(tmp_path):
    """An empty seen.json used to re-bootstrap: mark all ~12,500 open roles seen, send
    nothing, exit 0, go green -- and then the workflow committed the amnesiac state over the
    good one, destroying the git-history recovery the docstring promises."""
    p = tmp_path / "seen.json"
    p.write_text("", encoding="utf-8")
    with pytest.raises(poller.StateCorrupt):
        load_state(p)


def test_whitespace_only_state_is_corrupt(tmp_path):
    p = tmp_path / "seen.json"
    p.write_text("   \n\t\n", encoding="utf-8")
    with pytest.raises(poller.StateCorrupt):
        load_state(p)


def test_corrupt_empty_state_is_left_on_disk_untouched(tmp_path):
    p = tmp_path / "seen.json"
    p.write_text("", encoding="utf-8")
    with pytest.raises(poller.StateCorrupt):
        load_state(p)
    assert p.exists() and p.read_text(encoding="utf-8") == ""


def test_missing_state_requires_an_explicit_bootstrap_flag(tmp_path, monkeypatch):
    """A missing state file in CI means something broke, not that it is day one.

    Bootstrapping is now opt-in, so the workflow -- which never passes the flag -- cannot
    silently start over and wipe history.
    """
    p = tmp_path / "seen.json"
    monkeypatch.setattr(poller, "load_sources", lambda *a, **k: [GH_CFG])
    monkeypatch.setattr(poller, "build_session", lambda: object())
    monkeypatch.setattr(poller, "fetch_source", lambda cfg, ctx: result(cfg["source_id"], jobs=[]))
    monkeypatch.setattr(poller.notify, "notify_alert", lambda *a, **k: True)

    assert poller.run(Args(p)) != 0, "missing state without --bootstrap must fail"
    assert not p.exists(), "a refused run must not write state"

    assert poller.run(Args(p, bootstrap=True)) == 0
    assert p.exists()


# --------------------------------------------------------------------------------------
# Finding 4 -- a broken NTFY_TOPIC stayed green forever
# --------------------------------------------------------------------------------------


def test_undeliverable_push_makes_the_run_fail(tmp_path, monkeypatch):
    """A rotated or mistyped NTFY_TOPIC used to exit 0 and go green.

    Since send() is only reached when there IS something to push, a broken topic produced no
    evidence at all on the ~95% of runs with nothing new -- indistinguishable from a quiet
    job market until you happened to read the step log.
    """
    p = _seeded_state(tmp_path, ["greenhouse:vardaspace"])
    jobs = [job(source="greenhouse:vardaspace", job_id="x1",
                title="Software Engineer Intern, Summer 2027", company="Varda Space")]
    monkeypatch.setattr(poller, "load_sources", lambda *a, **k: [GH_CFG])
    monkeypatch.setattr(poller, "build_session", lambda: object())
    monkeypatch.setattr(poller, "fetch_source", lambda cfg, ctx: result(cfg["source_id"], jobs=jobs))
    monkeypatch.setattr(poller.notify, "notify_job", lambda *a, **k: False)
    monkeypatch.setattr(poller.notify, "notify_alert", lambda *a, **k: False)

    assert poller.run(Args(p)) != 0, "an undeliverable push must turn the run red"


def test_a_quiet_run_with_nothing_to_push_still_succeeds(tmp_path, monkeypatch):
    """The guard against over-correcting: no jobs means nothing to deliver, not a failure."""
    p = _seeded_state(tmp_path, ["greenhouse:vardaspace"])
    monkeypatch.setattr(poller, "load_sources", lambda *a, **k: [GH_CFG])
    monkeypatch.setattr(poller, "build_session", lambda: object())
    monkeypatch.setattr(poller, "fetch_source", lambda cfg, ctx: result(cfg["source_id"], jobs=[]))
    assert poller.run(Args(p)) == 0


# --------------------------------------------------------------------------------------
# Finding 13 -- filter tuning was not retroactive
# --------------------------------------------------------------------------------------


def test_filter_version_changes_when_the_keyword_lists_change(monkeypatch):
    before = poller.filter_version()
    monkeypatch.setattr(poller.filters, "SOFTWARE_MARKERS",
                        poller.filters.SOFTWARE_MARKERS + ("quantum widgetry",))
    assert poller.filter_version() != before


def test_rescan_releases_rejections_the_new_filter_would_accept():
    """Storing `reason` was meant to let the lists be tuned with evidence -- but nothing
    ever re-read it, so a widened filter resurfaced nothing."""
    state = new_state()
    state["seen"] = {
        "greenhouse:x::1": {"outcome": "rejected", "reason": "no_software_marker",
                            "title": "Site Reliability Internship - Spring 2027",
                            "first_seen": "t"},
        "greenhouse:x::2": {"outcome": "rejected", "reason": "title_reject:mechanical",
                            "title": "Mechanical Engineering Intern", "first_seen": "t"},
    }
    freed = poller.rescan_rejected(state)

    assert freed == ["greenhouse:x::1"], "only the one the filter now accepts"
    assert "greenhouse:x::1" not in state["seen"], "released, so the next fetch rediscovers it"
    assert "greenhouse:x::2" in state["seen"], "still genuinely rejected"


def test_rescan_never_touches_notified_or_bootstrap_entries():
    """Releasing a notified job would re-push it; releasing bootstrap entries would
    re-surface the entire pre-existing backlog as if it were new."""
    state = new_state()
    state["seen"] = {
        "a::1": {"outcome": "notified", "title": "Software Engineer Intern", "first_seen": "t"},
        "a::2": {"outcome": "bootstrap", "first_seen": "t"},
        "a::3": {"outcome": "backfill", "first_seen": "t"},
        "a::4": {"outcome": "duplicate", "title": "Software Engineer Intern", "first_seen": "t"},
    }
    assert poller.rescan_rejected(state) == []
    assert len(state["seen"]) == 4


def test_rescan_only_runs_when_the_filter_actually_changed(tmp_path, monkeypatch):
    p = _seeded_state(tmp_path, ["greenhouse:vardaspace"])
    st, _ = load_state(p)
    st["filter_version"] = poller.filter_version()
    st["seen"]["greenhouse:vardaspace::old"] = {
        "outcome": "rejected", "reason": "no_software_marker",
        "title": "Site Reliability Internship - Spring 2027", "first_seen": "t"}
    save_state(p, st)

    _run_with(monkeypatch, tmp_path, p, {"greenhouse:vardaspace": []}, [GH_CFG])
    final, _ = load_state(p)
    assert "greenhouse:vardaspace::old" in final["seen"], "unchanged filter must not re-scan"


# --------------------------------------------------------------------------------------
# Finding 14 -- production reimplemented the filter instead of calling it
# --------------------------------------------------------------------------------------


def test_poller_uses_filters_evaluate(monkeypatch, tmp_path):
    """evaluate() had five tests and zero production callers; run() reimplemented the
    title -> content -> clearance sequence inline, so the two could drift."""
    called = []
    real = poller.filters.evaluate
    monkeypatch.setattr(poller.filters, "evaluate",
                        lambda j: called.append(j["title"]) or real(j))
    p = _seeded_state(tmp_path, ["greenhouse:vardaspace"])
    jobs = [job(source="greenhouse:vardaspace", job_id="1",
                title="Software Engineer Intern, Summer 2027", company="Varda Space")]
    _run_with(monkeypatch, tmp_path, p, {"greenhouse:vardaspace": jobs}, [GH_CFG])
    assert called, "run() must go through filters.evaluate, not its own copy"


# --------------------------------------------------------------------------------------
# Finding 5 -- no deadman switch
# --------------------------------------------------------------------------------------


def test_heartbeat_fires_once_a_week(tmp_path, monkeypatch):
    """The only detector for the poller dying ENTIRELY.

    A dead poller sends no alerts by definition, because it never runs. GitHub is already
    dropping ~84% of scheduled runs, and disables scheduled workflows on public repos after
    60 days of inactivity -- bot commits are widely reported not to reset that timer.
    """
    sent = []
    monkeypatch.setattr(poller.notify, "send", lambda **kw: sent.append(kw) or True)
    state = new_state()
    state["seen"] = {"a::1": {"outcome": "notified", "first_seen": "t"}}
    args = Args(tmp_path / "s.json")

    assert poller.maybe_heartbeat(state, [], "2026-09-16T00:00:00Z", args)
    assert state["last_heartbeat_at"] == "2026-09-16T00:00:00Z"
    assert sent[0]["priority"] == "low", "must not read as urgent on a lock screen"

    assert not poller.maybe_heartbeat(state, [], "2026-09-18T00:00:00Z", args), "too soon"
    assert poller.maybe_heartbeat(state, [], "2026-09-24T00:00:00Z", args), "a week later"


def test_heartbeat_is_not_stamped_when_the_push_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(poller.notify, "send", lambda **kw: False)
    state = new_state()
    state["seen"] = {"a::1": {"outcome": "notified", "first_seen": "t"}}
    poller.maybe_heartbeat(state, [], "2026-09-16T00:00:00Z", Args(tmp_path / "s.json"))
    assert state.get("last_heartbeat_at") is None, "an undelivered heartbeat must retry"


def test_dry_run_never_sends_a_heartbeat(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(poller.notify, "send", lambda **kw: sent.append(kw) or True)
    state = new_state()
    state["seen"] = {"a::1": {"outcome": "notified", "first_seen": "t"}}
    poller.maybe_heartbeat(state, [], "2026-09-16T00:00:00Z",
                           Args(tmp_path / "s.json", dry_run=True))
    assert sent == []


# --------------------------------------------------------------------------------------
# open-internships.md ranking (folded into --list-open so it survives regeneration)
# --------------------------------------------------------------------------------------


def _j(title, posted=""):
    return {"title": title, "posted_at": posted, "company": "X", "location": "", "url": "u"}


def test_summer_2027_software_outranks_everything():
    best = poller.urgency_score(_j("Software Engineer Intern, Summer 2027"))
    for other in ("Software Engineer Intern", "Mechanical Intern Summer 2027",
                  "Software Engineer - New Grad", "Data Science Intern"):
        assert poller.urgency_score(_j(other)) < best, other


def test_wrong_cycle_roles_sink_to_the_bottom():
    """The user graduates April 2028, so these are ~18 months too early."""
    good = poller.urgency_score(_j("Software Engineer Intern, Summer 2027"))
    for stale in ("2026 Intern Conversion - Software Development Engineer I",
                  "Software Engineer - New Grad (December 2026)",
                  "Software Development Engineer I - Early Career (2026 Starts)"):
        assert poller.urgency_score(_j(stale)) < good - 100, stale


def test_fresher_postings_rank_higher():
    a = poller.urgency_score(_j("Software Engineer Intern, Summer 2027", "Posted Today"))
    b = poller.urgency_score(_j("Software Engineer Intern, Summer 2027",
                                "Posted 30+ Days Ago"))
    assert a > b


@pytest.mark.parametrize("raw,expected", [
    ("Posted Today", 0), ("Posted Yesterday", 1), ("Posted 4 Days Ago", 4),
    ("Posted 30+ Days Ago", 30), ("", None), ("not a date", None),
])
def test_posting_age_parses_every_shape_the_boards_emit(raw, expected):
    assert poller._posting_age_days(_j("t", raw)) == expected
