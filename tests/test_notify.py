"""Tests for notify.py. No network: every POST goes through a recording double."""

from __future__ import annotations

import pytest
import requests

import notify


class RecordingSession:
    def __init__(self, status=200, raises=None):
        self.status = status
        self.raises = raises
        self.posts: list[dict] = []

    def post(self, url, json=None, timeout=None):
        if self.raises:
            raise self.raises
        self.posts.append({"url": url, "json": json, "timeout": timeout})

        class R:
            status_code = self.status

            def raise_for_status(inner):
                if inner.status_code >= 400:
                    raise requests.HTTPError(str(inner.status_code))

        return R()


@pytest.fixture(autouse=True)
def topic(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "test-topic-xyz")


def make_job(**kw):
    base = {
        "company": "Varda Space",
        "title": "Software Engineer Intern",
        "location": "El Segundo, CA",
        "url": "https://boards.greenhouse.io/vardaspace/jobs/1",
    }
    base.update(kw)
    return base


def test_send_posts_json_payload():
    s = RecordingSession()
    assert notify.send(title="T", body="B", click="https://x", session=s)
    payload = s.posts[0]["json"]
    assert payload["topic"] == "test-topic-xyz"
    assert payload["title"] == "T"
    assert payload["message"] == "B"
    assert payload["click"] == "https://x"
    assert payload["tags"] == ["rocket"]
    assert payload["priority"] == 3, "'default' is 3 in the JSON API"


def test_unicode_title_does_not_raise():
    """The reason for the JSON API deviation.

    As an HTTP header, requests encodes values as latin-1 and this title raises
    UnicodeEncodeError -- the run reports success while the push never arrives.
    """
    s = RecordingSession()
    job = make_job(title="Co‑op — Ground Software “Summer” 2027")
    assert notify.notify_job(job, session=s)
    sent = s.posts[0]["json"]["title"]
    assert "—" in sent, "em-dash must survive intact, not be stripped"
    sent.encode("utf-8")                     # would be latin-1 and fail as a header


def test_priority_names_map_to_ints():
    s = RecordingSession()
    notify.send(title="T", body="B", priority="high", session=s)
    assert s.posts[0]["json"]["priority"] == 4


def test_missing_topic_returns_false_without_posting(monkeypatch):
    monkeypatch.delenv("NTFY_TOPIC", raising=False)
    s = RecordingSession()
    assert notify.send(title="T", body="B", session=s) is False
    assert s.posts == []


def test_blank_topic_is_treated_as_missing(monkeypatch):
    monkeypatch.setenv("NTFY_TOPIC", "   ")
    assert notify.send(title="T", body="B", session=RecordingSession()) is False


def test_network_failure_returns_false_not_raises():
    """The caller uses this to keep a failed job OUT of seen.json so it retries."""
    s = RecordingSession(raises=requests.ConnectTimeout("boom"))
    assert notify.send(title="T", body="B", session=s) is False


def test_http_error_returns_false():
    assert notify.send(title="T", body="B", session=RecordingSession(status=500)) is False


def test_notify_job_body_has_everything_section_7_asks_for():
    s = RecordingSession()
    job = make_job()
    notify.notify_job(job, session=s)
    payload = s.posts[0]["json"]
    for piece in (job["title"], job["company"], job["location"], job["url"]):
        assert piece in payload["message"]
    assert payload["click"] == job["url"]
    assert job["company"] in payload["title"] and job["title"] in payload["title"]


def test_notify_job_without_location():
    s = RecordingSession()
    assert notify.notify_job(make_job(location=""), session=s)


def test_summary_names_companies_and_count():
    s = RecordingSession()
    jobs = [make_job(company=f"Co{i}", url=f"https://x/{i}") for i in range(9)]
    assert notify.notify_summary(jobs, session=s)
    payload = s.posts[0]["json"]
    assert "9" in payload["title"]
    assert "Co0" in payload["message"]
    assert "more" in payload["message"], "only the first few companies are named"


def test_alert_is_high_priority():
    s = RecordingSession()
    notify.notify_alert("broken", "details", session=s)
    payload = s.posts[0]["json"]
    assert payload["priority"] == 4
    assert payload["tags"] == ["warning"]


def test_topic_is_never_in_the_log(caplog):
    """The topic is a secret; a failure must not leak it into the Actions log."""
    s = RecordingSession(raises=requests.ConnectTimeout("boom"))
    with caplog.at_level("ERROR"):
        notify.send(title="T", body="B", session=s)
    assert "test-topic-xyz" not in caplog.text
