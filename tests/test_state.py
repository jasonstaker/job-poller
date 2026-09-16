"""Tests for state handling, diff, dedupe, and the section 9 health counters."""

from __future__ import annotations

import json

import pytest

import handlers
import poller
from poller import (
    ALERT_COOLDOWN_HOURS,
    StateCorrupt,
    dedupe,
    diff_new,
    load_state,
    new_state,
    record_seen,
    save_state,
    seen_key,
    should_poll,
    update_health,
)


def job(source="greenhouse:vardaspace", job_id="1", title="Software Engineer Intern",
        company="Varda Space", **kw):
    base = {
        "source": source, "company": company, "job_id": job_id, "title": title,
        "location": "", "department": "", "url": f"https://example.com/{job_id}",
        "first_seen": "", "content": "", "posted_at": "",
        "ats": source.split(":", 1)[0],
    }
    base.update(kw)
    return base


def result(source, ok=True, jobs=(), status=200, error="", state_updates=None):
    return handlers.FetchResult(source, "Co", ok, status, list(jobs), error, 10, 1,
                                state_updates or {})


# --------------------------------------------------------------------------------------
# Bootstrap detection
# --------------------------------------------------------------------------------------


def test_missing_file_is_bootstrap(tmp_path):
    state, is_bootstrap = load_state(tmp_path / "seen.json")
    assert is_bootstrap and state["seen"] == {}


def test_empty_file_is_corrupt_not_a_fresh_start(tmp_path):
    """Behaviour changed deliberately 2026-09-16.

    An empty seen.json used to re-bootstrap silently: mark every open role as seen, send
    nothing, exit 0, then commit the amnesiac state over the good one. See the audit
    regression suite in tests/test_robustness.py.
    """
    p = tmp_path / "seen.json"
    p.write_text("   \n", encoding="utf-8")
    with pytest.raises(StateCorrupt):
        load_state(p)


def test_populated_state_is_not_bootstrap(tmp_path):
    p = tmp_path / "seen.json"
    state = new_state()
    state["bootstrapped_at"] = "2026-09-12T00:00:00Z"
    state["seen"] = {"greenhouse:x::1": {"first_seen": "...", "outcome": "bootstrap"}}
    save_state(p, state)
    _, is_bootstrap = load_state(p)
    assert not is_bootstrap


def test_empty_seen_with_sentinel_is_NOT_bootstrap(tmp_path):
    """The whole reason for the sentinel.

    A poller whose every board happens to be empty must not be mistaken for a first run --
    that would re-flood the user with hundreds of pushes the moment a board came back.
    """
    p = tmp_path / "seen.json"
    state = new_state()
    state["bootstrapped_at"] = "2026-09-12T00:00:00Z"
    save_state(p, state)

    loaded, is_bootstrap = load_state(p)
    assert loaded["seen"] == {}
    assert not is_bootstrap


# --------------------------------------------------------------------------------------
# Corruption: never overwrite, never re-bootstrap
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("content", [
    "{not json",
    '["a", "list"]',
    '{"seen": "not a dict"}',
    '{"seen": {}, "sources": []}',
    '{"seen": {}, "sources": {}, "schema_version": 99}',
])
def test_corrupt_state_raises(tmp_path, content):
    p = tmp_path / "seen.json"
    p.write_text(content, encoding="utf-8")
    with pytest.raises(StateCorrupt):
        load_state(p)


def test_corrupt_state_is_left_untouched(tmp_path):
    p = tmp_path / "seen.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(StateCorrupt):
        load_state(p)
    assert p.read_text(encoding="utf-8") == "{not json", "must not overwrite"


# --------------------------------------------------------------------------------------
# Atomic write
# --------------------------------------------------------------------------------------


def test_save_is_atomic_and_leaves_no_temp(tmp_path):
    p = tmp_path / "seen.json"
    save_state(p, new_state())
    save_state(p, new_state())          # os.rename would raise FileExistsError here
    assert p.exists()
    assert not (tmp_path / "seen.json.tmp").exists()


def test_stale_temp_is_discarded_never_promoted(tmp_path):
    p = tmp_path / "seen.json"
    save_state(p, new_state())
    stale = tmp_path / "seen.json.tmp"
    stale.write_text('{"seen": {"truncated": ', encoding="utf-8")

    load_state(p)
    assert not stale.exists(), "a half-written temp file must be discarded"


def test_saved_json_is_sorted_and_lf(tmp_path):
    p = tmp_path / "seen.json"
    state = new_state()
    state["seen"] = {"b::2": {"outcome": "bootstrap"}, "a::1": {"outcome": "bootstrap"}}
    save_state(p, state)

    raw = p.read_bytes()
    assert b"\r\n" not in raw, "CRLF would rewrite the whole file on every CI run"
    assert raw.decode().index('"a::1"') < raw.decode().index('"b::2"')


def test_round_trip_preserves_history(tmp_path):
    p = tmp_path / "seen.json"
    state = new_state()
    state["bootstrapped_at"] = "2026-09-12T00:00:00Z"
    record_seen(state, job(job_id="1"), "notified", now="2026-09-12T00:00:00Z")
    save_state(p, state)

    loaded, _ = load_state(p)
    record_seen(loaded, job(job_id="2"), "rejected", reason="title_reject:gnc",
                now="2026-09-12T01:00:00Z")
    save_state(p, loaded)

    final, _ = load_state(p)
    assert set(final["seen"]) == {"greenhouse:vardaspace::1", "greenhouse:vardaspace::2"}
    assert final["seen"]["greenhouse:vardaspace::2"]["reason"] == "title_reject:gnc"


# --------------------------------------------------------------------------------------
# record_seen
# --------------------------------------------------------------------------------------


def test_bootstrap_entries_omit_title():
    """Bootstrap writes thousands of rows at once; those titles have no debugging value."""
    state = new_state()
    record_seen(state, job(), "bootstrap", now="t")
    assert "title" not in state["seen"]["greenhouse:vardaspace::1"]


def test_non_bootstrap_entries_keep_title_and_reason():
    state = new_state()
    record_seen(state, job(), "rejected", reason="title_reject:payload", now="t")
    entry = state["seen"]["greenhouse:vardaspace::1"]
    assert entry["title"] == "Software Engineer Intern"
    assert entry["reason"] == "title_reject:payload"


# --------------------------------------------------------------------------------------
# Diff
# --------------------------------------------------------------------------------------


def test_diff_returns_only_unseen():
    state = new_state()
    record_seen(state, job(job_id="1"), "notified", now="t")
    new = diff_new([job(job_id="1"), job(job_id="2")], state)
    assert [j["job_id"] for j in new] == ["2"]


def test_same_job_id_on_different_sources_is_not_collapsed():
    """Keys are namespaced by source, so id collisions across boards stay distinct."""
    state = new_state()
    record_seen(state, job(source="greenhouse:a", job_id="1"), "notified", now="t")
    new = diff_new([job(source="ashby:b", job_id="1")], state)
    assert len(new) == 1


def test_seen_key_is_splittable():
    assert seen_key(job()).split("::", 1) == ["greenhouse:vardaspace", "1"]


# --------------------------------------------------------------------------------------
# Dedupe
# --------------------------------------------------------------------------------------


def test_same_title_across_two_boards_collapses_to_greenhouse():
    """The K2 Space / Applied Intuition case: Greenhouse wins, Ashby is marked duplicate."""
    gh = job(source="greenhouse:k2spacecorporation", job_id="1",
             title="Software Engineering Intern", company="K2 Space")
    ashby = job(source="ashby:K2space", job_id="zz",
                title="Software Engineering Intern", company="K2 Space")

    kept, dropped = dedupe([gh, ashby])
    assert [j["source"] for j in kept] == ["greenhouse:k2spacecorporation"]
    assert len(dropped) == 1
    assert dropped[0][0]["source"] == "ashby:K2space"
    assert dropped[0][1] == "greenhouse:k2spacecorporation::1"


def test_same_title_within_one_board_is_kept_whole():
    """One board legitimately posts the same title for two locations.

    Collapsing those would hide a real job, so dedupe only ever acts ACROSS sources.
    """
    a = job(job_id="1", title="Software Engineer Intern", location="Long Beach")
    b = job(job_id="2", title="Software Engineer Intern", location="Denver")
    kept, dropped = dedupe([a, b])
    assert len(kept) == 2 and dropped == []


def test_different_companies_never_group():
    """Relativity's two boards carry different company strings, so they must not collapse."""
    main = job(source="greenhouse:relativity", job_id="1",
               title="Software Engineer Intern", company="Relativity Space")
    interns = job(source="greenhouse:rsinternboard", job_id="2",
                  title="Software Engineer Intern", company="Relativity (interns)")
    kept, dropped = dedupe([main, interns])
    assert len(kept) == 2 and dropped == []


def test_dedupe_normalizes_punctuation_and_case():
    gh = job(source="greenhouse:x", job_id="1", title="Software Engineer, Intern",
             company="Acme Corp")
    ab = job(source="ashby:y", job_id="2", title="software engineer  intern",
             company="acme corp")
    kept, dropped = dedupe([gh, ab])
    assert len(kept) == 1 and len(dropped) == 1


def test_dedupe_preserves_input_order():
    jobs = [job(job_id=str(i), title=f"Title {i}") for i in range(5)]
    kept, _ = dedupe(jobs)
    assert [j["job_id"] for j in kept] == ["0", "1", "2", "3", "4"]


def test_lever_loses_to_ashby_which_loses_to_greenhouse():
    lever = job(source="lever:x", job_id="1", title="SWE Intern", company="Acme")
    ashby = job(source="ashby:y", job_id="2", title="SWE Intern", company="Acme")
    gh = job(source="greenhouse:z", job_id="3", title="SWE Intern", company="Acme")

    kept, dropped = dedupe([lever, ashby, gh])
    assert [j["source"] for j in kept] == ["greenhouse:z"]
    assert len(dropped) == 2


# --------------------------------------------------------------------------------------
# Health counters (section 9)
# --------------------------------------------------------------------------------------


def _t(hours: int) -> str:
    """A timestamp `hours` after a fixed epoch, for the wall-clock health thresholds."""
    from datetime import datetime, timedelta, timezone
    base = datetime(2026, 9, 1, tzinfo=timezone.utc)
    return (base + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_failure_streak_alerts_at_three():
    state = new_state()
    for h in (0, 4):
        broken, _ = update_health(state, [result("lever:x", ok=False, error="HTTP 500")], _t(h))
        assert broken == []
    broken, _ = update_health(state, [result("lever:x", ok=False, error="HTTP 500")], _t(8))
    assert len(broken) == 1 and "lever:x" in broken[0][2]


def test_success_resets_the_failure_streak():
    state = new_state()
    update_health(state, [result("lever:x", ok=False)], _t(0))
    update_health(state, [result("lever:x", jobs=[job()])], _t(4))
    assert state["sources"]["lever:x"]["consecutive_failures"] == 0


def test_an_unstamped_alert_refires_next_run():
    """Alerts are staged, not stamped. Stamping inside update_health meant one transient
    ntfy failure bought a dead board a full cooldown of silence."""
    state = new_state()
    for h in (0, 4, 8):
        broken, _ = update_health(state, [result("lever:x", ok=False)], _t(h))
    assert len(broken) == 1
    broken, _ = update_health(state, [result("lever:x", ok=False)], _t(12))
    assert len(broken) == 1, "not stamped, so it must fire again"


def test_a_stamped_alert_is_suppressed_until_the_cooldown_expires():
    state = new_state()
    for h in (0, 4, 8):
        broken, _ = update_health(state, [result("lever:x", ok=False)], _t(h))
    poller.stamp_alerts(state, broken, _t(8))

    _, _ = update_health(state, [result("lever:x", ok=False)], _t(12))
    broken2, _ = update_health(state, [result("lever:x", ok=False)], _t(20))
    assert broken2 == [], "inside the 24h cooldown"
    broken3, _ = update_health(state, [result("lever:x", ok=False)],
                               _t(8 + ALERT_COOLDOWN_HOURS + 1))
    assert len(broken3) == 1, "cooldown expired, must re-alert"


def test_cooldown_is_wall_clock_not_run_count():
    """The old cooldown was 48 runs, meant as 'once a day' at */30. GitHub actually
    delivers ~3.8h between runs, which stretched it to 7.6 days."""
    state = new_state()
    for h in (0, 4, 8):
        broken, _ = update_health(state, [result("lever:x", ok=False)], _t(h))
    poller.stamp_alerts(state, broken, _t(8))
    # Only 3 further runs, but 25 hours of wall clock: the cooldown must have expired.
    broken2, _ = update_health(state, [result("lever:x", ok=False)], _t(33))
    assert len(broken2) == 1


def test_a_board_that_never_returns_a_job_eventually_alerts():
    """Previously exempt FOREVER via `last_ok_run is None`, which silenced
    greenhouse:capellaspace and lever:attabotics across their entire lifetime."""
    state = new_state()
    _, drifted = update_health(state, [result("lever:attabotics", jobs=[])], _t(0))
    assert drifted == [], "not immediately -- a briefly empty board is normal"
    _, drifted = update_health(state, [result("lever:attabotics", jobs=[])], _t(12))
    assert drifted == []
    _, drifted = update_health(state, [result("lever:attabotics", jobs=[])], _t(60))
    assert len(drifted) == 1 and "never returned a job" in drifted[0][2]


def test_zero_streak_alerts_after_five_once_it_has_worked():
    state = new_state()
    update_health(state, [result("greenhouse:x", jobs=[job()])], _t(0))
    for i in range(1, 5):
        _, drifted = update_health(state, [result("greenhouse:x", jobs=[])], _t(i * 4))
        assert drifted == []
    _, drifted = update_health(state, [result("greenhouse:x", jobs=[])], _t(20))
    assert len(drifted) == 1 and "greenhouse:x" in drifted[0][2]


def test_a_collapsing_job_count_alerts():
    """Token drift to a smaller board, a renamed payload key, and Workday truncation all
    return HTTP 200 with a plausible payload. The count is the only tell."""
    state = new_state()
    update_health(state, [result("greenhouse:x", jobs=[job(job_id=str(i)) for i in range(300)])], _t(0))
    _, drifted = update_health(state, [result("greenhouse:x", jobs=[job()])], _t(4))
    assert len(drifted) == 1 and "1 jobs, was 300" in drifted[0][2]


def test_a_modest_shrink_does_not_alert():
    state = new_state()
    update_health(state, [result("greenhouse:x", jobs=[job(job_id=str(i)) for i in range(100)])], _t(0))
    _, drifted = update_health(state, [result("greenhouse:x", jobs=[job(job_id=str(i)) for i in range(80)])], _t(4))
    assert drifted == []


def test_a_tiny_board_never_trips_the_count_alert():
    """Wisk legitimately carries 2-3 postings; that must never look like a collapse."""
    state = new_state()
    update_health(state, [result("workday:wisk", jobs=[job(job_id="a"), job(job_id="b")])], _t(0))
    _, drifted = update_health(state, [result("workday:wisk", jobs=[job(job_id="a")])], _t(4))
    assert drifted == []


def test_unpolled_sources_keep_their_counters():
    state = new_state()
    update_health(state, [result("workday:blueorigin:BlueOrigin", ok=False)], _t(0))
    before = dict(state["sources"]["workday:blueorigin:BlueOrigin"])
    update_health(state, [result("greenhouse:x", jobs=[job()])], _t(4))
    assert state["sources"]["workday:blueorigin:BlueOrigin"] == before


def test_state_updates_are_persisted():
    state = new_state()
    update_health(state, [result("greenhouse:physicsx", jobs=[job()],
                                 state_updates={"greenhouse_host": "eu"})], _t(0))
    assert state["sources"]["greenhouse:physicsx"]["greenhouse_host"] == "eu"


# --------------------------------------------------------------------------------------
# Workday cadence (section 8)
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("ats", ["greenhouse", "lever", "ashby", "workday"])
@pytest.mark.parametrize("run", [1, 2, 3, 4])
def test_every_source_polls_every_run(ats, run):
    """Section 8's every-4th-run Workday cadence assumed */30.

    GitHub actually delivers ~1 run per 3.5h, which would leave Workday ~14h stale while it
    holds NVIDIA's 27 matching roles. Probing drew no throttling, so everything polls every
    run -- which also removes any chance of health counters drifting on skipped sources.
    """
    assert should_poll(ats, run)


# --------------------------------------------------------------------------------------
# Source loading
# --------------------------------------------------------------------------------------


def test_load_sources_injects_ats_and_source_id():
    sources = poller.load_sources()
    assert sources, "sources.json must not be empty"
    for cfg in sources:
        assert cfg["ats"] in handlers.HANDLERS
        assert cfg["source_id"].startswith(cfg["ats"] + ":")
        assert cfg["company"]


def test_load_sources_skips_scrape_later_and_unimplemented_ats():
    """An ATS with no handler yet must be skipped, not polled and failed every run.

    Otherwise Workday/Workable/BambooHR would march toward the section 9 three-strike
    alert for a handler that simply has not been written yet.
    """
    ats_types = {c["ats"] for c in poller.load_sources()}
    assert "scrape_later" not in ats_types
    assert ats_types <= set(handlers.HANDLERS)


def test_source_ids_are_unique():
    ids = [c["source_id"] for c in poller.load_sources()]
    assert len(ids) == len(set(ids))


def test_no_source_id_contains_the_key_separator():
    """seen.json keys are "{source}::{job_id}"; a "::" in a source id would break parsing."""
    assert all("::" not in c["source_id"] for c in poller.load_sources())


# --------------------------------------------------------------------------------------
# Per-source backfill
# --------------------------------------------------------------------------------------


class Args:
    """Minimal stand-in for the argparse namespace `run()` consumes."""

    def __init__(self, state, **kw):
        self.state = state
        self.source = self.ats = None
        self.dry_run = self.no_jitter = self.verbose = False
        self.force_workday = True
        self.bootstrap = False
        self.probe = self.test_notify = False
        self.list_open = None
        self.__dict__.update(kw)


def _seeded_state(tmp_path, sources):
    """A non-bootstrap state that already knows about `sources`."""
    p = tmp_path / "seen.json"
    st = new_state()
    st["bootstrapped_at"] = "2026-09-12T00:00:00Z"
    st["run_counter"] = 10
    for sid in sources:
        st["sources"][sid] = {
            "consecutive_failures": 0, "consecutive_zeros": 0, "last_ok_run": 10,
            "last_failure_alert_run": None, "last_zero_alert_run": None,
        }
    save_state(p, st)
    return p


def _run_with(monkeypatch, tmp_path, state_path, jobs_by_source, cfgs, **argkw):
    """Drive poller.run() with a stubbed fetch and a recording notifier."""
    sent = []
    monkeypatch.setattr(poller, "load_sources", lambda *a, **k: cfgs)
    monkeypatch.setattr(poller, "build_session", lambda: object())
    monkeypatch.setattr(poller, "fetch_source",
                        lambda cfg, ctx: result(cfg["source_id"],
                                                jobs=jobs_by_source.get(cfg["source_id"], [])))
    monkeypatch.setattr(poller.notify, "notify_job", lambda j, **k: sent.append(j) or True)
    monkeypatch.setattr(poller.notify, "notify_summary", lambda js, **k: sent.extend(js) or True)
    monkeypatch.setattr(poller.notify, "notify_alert", lambda *a, **k: True)
    rc = poller.run(Args(state_path, **argkw))
    return rc, sent


GH_CFG = {"ats": "greenhouse", "token": "vardaspace", "company": "Varda Space",
          "source_id": "greenhouse:vardaspace"}
WD_CFG = {"ats": "workday", "tenant": "blueorigin", "site": "BlueOrigin", "dc": "wd5",
          "company": "Blue Origin", "source_id": "workday:blueorigin:BlueOrigin"}


def test_new_source_is_backfilled_not_notified(tmp_path, monkeypatch):
    """Registering a handler must not fire a push for every job already on its board."""
    p = _seeded_state(tmp_path, ["greenhouse:vardaspace"])
    wd_jobs = [job(source="workday:blueorigin:BlueOrigin", job_id=str(i),
                   title="Software Engineer Intern", company="Blue Origin")
               for i in range(30)]
    rc, sent = _run_with(monkeypatch, tmp_path, p,
                         {"greenhouse:vardaspace": [], "workday:blueorigin:BlueOrigin": wd_jobs},
                         [GH_CFG, WD_CFG])

    assert rc == 0
    assert sent == [], "a brand-new board must send nothing"
    final, _ = load_state(p)
    outcomes = {v["outcome"] for k, v in final["seen"].items() if k.startswith("workday:")}
    assert outcomes == {"backfill"}
    assert len(final["seen"]) == 30


def test_backfill_does_not_trip_the_anomaly_guard(tmp_path, monkeypatch, caplog):
    """250 jobs from a new board is onboarding, not 'state may have been lost'."""
    p = _seeded_state(tmp_path, ["greenhouse:vardaspace"])
    wd_jobs = [job(source="workday:blueorigin:BlueOrigin", job_id=str(i),
                   title=f"Engineer {i}", company="Blue Origin") for i in range(250)]
    with caplog.at_level("INFO"):
        rc, sent = _run_with(monkeypatch, tmp_path, p,
                             {"greenhouse:vardaspace": [],
                              "workday:blueorigin:BlueOrigin": wd_jobs},
                             [GH_CFG, WD_CFG])
    assert rc == 0 and sent == []
    assert "ANOMALY" not in caplog.text
    assert "BACKFILL" in caplog.text


def test_established_source_still_notifies_during_someone_elses_backfill(tmp_path, monkeypatch):
    """The guard that matters: backfill must not swallow a real notification."""
    p = _seeded_state(tmp_path, ["greenhouse:vardaspace"])
    gh_jobs = [job(source="greenhouse:vardaspace", job_id="new1",
                   title="Software Engineer Intern, Summer 2027", company="Varda Space")]
    wd_jobs = [job(source="workday:blueorigin:BlueOrigin", job_id=str(i),
                   title="Software Engineer Intern", company="Blue Origin")
               for i in range(30)]
    rc, sent = _run_with(monkeypatch, tmp_path, p,
                         {"greenhouse:vardaspace": gh_jobs,
                          "workday:blueorigin:BlueOrigin": wd_jobs},
                         [GH_CFG, WD_CFG])

    assert [j["job_id"] for j in sent] == ["new1"], "the established board must still push"
    final, _ = load_state(p)
    assert final["seen"]["greenhouse:vardaspace::new1"]["outcome"] == "notified"


def test_second_run_of_a_backfilled_source_notifies_normally(tmp_path, monkeypatch):
    """Backfill is once per source, not a permanent mute."""
    p = _seeded_state(tmp_path, ["greenhouse:vardaspace"])
    first = [job(source="workday:blueorigin:BlueOrigin", job_id="old",
                 title="Software Engineer Intern", company="Blue Origin")]
    _run_with(monkeypatch, tmp_path, p,
              {"greenhouse:vardaspace": [], "workday:blueorigin:BlueOrigin": first},
              [GH_CFG, WD_CFG])

    second = first + [job(source="workday:blueorigin:BlueOrigin", job_id="brand-new",
                          title="Software Engineer Intern, Summer 2027", company="Blue Origin")]
    _, sent = _run_with(monkeypatch, tmp_path, p,
                        {"greenhouse:vardaspace": [], "workday:blueorigin:BlueOrigin": second},
                        [GH_CFG, WD_CFG])
    assert [j["job_id"] for j in sent] == ["brand-new"]


def test_bootstrap_still_takes_precedence_over_backfill(tmp_path, monkeypatch):
    """On a true first run everything is 'bootstrap', not 'backfill'."""
    p = tmp_path / "seen.json"
    jobs = [job(source="greenhouse:vardaspace", job_id="1",
                title="Software Engineer Intern", company="Varda Space")]
    rc, sent = _run_with(monkeypatch, tmp_path, p, {"greenhouse:vardaspace": jobs}, [GH_CFG],
                         bootstrap=True)
    assert rc == 0 and sent == []
    final, _ = load_state(p)
    assert {v["outcome"] for v in final["seen"].values()} == {"bootstrap"}
