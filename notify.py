"""ntfy push notifications.

The topic always comes from the NTFY_TOPIC environment variable, set as a GitHub Actions
secret. It is never hardcoded and never written to a log line.

BUILD_SPEC.md section 7 specifies the header form of the ntfy API (Title:, Click:, ...).
This uses the JSON publishing API instead, deliberately. `requests` encodes header values
as latin-1, so a title containing an em-dash or a smart quote raises UnicodeEncodeError --
and real job titles contain those constantly ("Software Engineer — Intern, Summer 2027").
The failure mode is the worst kind: the run reports success, state gets written marking the
job as seen, and the push simply never arrives. The JSON API is UTF-8 clean and carries the
same fields.
"""

from __future__ import annotations

import logging
import os

import requests

log = logging.getLogger("poller.notify")

NTFY_URL = "https://ntfy.sh/"
TIMEOUT = (5.0, 15.0)

# ntfy priorities are 1-5 as ints in the JSON API, where section 7 names them as strings.
PRIORITIES = {"min": 1, "low": 2, "default": 3, "high": 4, "max": 5, "urgent": 5}


def topic_from_env() -> str | None:
    return (os.environ.get("NTFY_TOPIC") or "").strip() or None


def send(
    *,
    title: str,
    body: str,
    click: str = "",
    tags: str = "rocket",
    priority: str = "default",
    topic: str | None = None,
    session: requests.Session | None = None,
) -> bool:
    """Publish one notification. Never raises; returns whether it landed.

    Returning a bool rather than raising is what lets the caller keep a failed job OUT of
    seen.json so it is retried next run. A missed push is the one failure this whole tool
    exists to prevent.
    """
    topic = topic or topic_from_env()
    if not topic:
        log.error("NTFY_TOPIC is not set; refusing to send")
        return False

    payload = {
        "topic": topic,
        "title": title,
        "message": body,
        "priority": PRIORITIES.get(priority, 3),
        "tags": [t for t in tags.split(",") if t],
    }
    if click:
        payload["click"] = click

    try:
        poster = session.post if session else requests.post
        resp = poster(NTFY_URL, json=payload, timeout=TIMEOUT)
        resp.raise_for_status()
        return True
    except Exception as exc:
        # Deliberately not logging the payload: the topic is a secret.
        log.error("ntfy send failed (%s): %s", title[:60], exc)
        return False


def notify_job(job: dict, **kw) -> bool:
    """One push for one job. Short enough to read on a lock screen (section 7)."""
    title = f"{job['company']} — {job['title']}"
    lines = [job["title"], job["company"]]
    if job.get("location"):
        lines.append(job["location"])
    lines.append(job["url"])
    return send(title=title, body="\n".join(lines), click=job["url"], **kw)


def notify_summary(jobs: list[dict], **kw) -> bool:
    """One push instead of many.

    Section 7: more than 8 jobs in a single run collapses to a summary with the count and
    the company names, and the full list goes to the Actions log.
    """
    companies = sorted({j["company"] for j in jobs})
    shown = ", ".join(companies[:6])
    if len(companies) > 6:
        shown += f", +{len(companies) - 6} more"
    return send(
        title=f"{len(jobs)} new SWE internship postings",
        body=f"{len(jobs)} new postings across {len(companies)} companies:\n{shown}\n"
             f"Full list in the Actions log.",
        click=jobs[0]["url"] if jobs else "",
        **kw,
    )


def notify_alert(title: str, body: str, **kw) -> bool:
    """High-priority health alert (section 9). A broken fetcher means missed postings."""
    kw.setdefault("priority", "high")
    kw.setdefault("tags", "warning")
    return send(title=title, body=body, **kw)
