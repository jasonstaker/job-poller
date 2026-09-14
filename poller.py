"""Entry point: load sources, fetch, diff, dedupe, filter, notify, persist.

The pipeline order in `run` is load-bearing; see the comment there before rearranging it.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import pathlib
import re
import sys
import unicodedata
from datetime import datetime, timezone

import filters
import handlers
import notify
from handlers import (
    ID_FIELDS,
    SKIP_ATS,
    FetchContext,
    build_session,
    fetch_source,
    greenhouse_fetch_content,
    source_id,
)

ROOT = pathlib.Path(__file__).parent
SOURCES_PATH = ROOT / "sources.json"
STATE_PATH = ROOT / "state" / "seen.json"

SCHEMA_VERSION = 1

# Section 7: more than this many notifiable jobs in one run collapses to a summary.
SUMMARY_THRESHOLD = 8
# Not in the spec. If a run ever produces this many new jobs, something is wrong with the
# state file rather than with the job market -- suppress the flood and say so instead.
ANOMALY_THRESHOLD = 200
# Section 9 alert thresholds.
FAILURE_ALERT_STREAK = 3
ZERO_ALERT_STREAK = 5
# Section 9 taken literally would re-alert every 30 minutes forever on a permanently dead
# board. Fire at the threshold, then at most once a day (48 runs at the */30 cadence).
ALERT_COOLDOWN_RUNS = 48
# Section 8 says poll Workday every 4th run because it throttles harder. That assumed the
# */30 cron; GitHub actually delivers roughly one run every 3.5 hours, which would leave
# Workday ~14 hours stale -- and Workday holds NVIDIA's 27 matching roles. ~180 rapid probe
# requests on 2026-09-14 drew no throttling at all, so every source is polled every run.

# Which board wins when the same role appears on two of them. Greenhouse first because it
# is the only ATS that exposes a description body for the clearance check.
SOURCE_PRIORITY = {"greenhouse": 0, "ashby": 1, "lever": 2,
                   "workday": 3, "workable": 4, "bamboohr": 5, "pinpoint": 6}

log = logging.getLogger("poller")


class StateCorrupt(RuntimeError):
    """seen.json exists but cannot be trusted. Never overwrite it; abort instead."""


# --------------------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------------------


def load_sources(path: pathlib.Path = SOURCES_PATH) -> list[dict]:
    """Flatten sources.json into a list of source configs.

    sources.json mirrors BUILD_SPEC.md section 5 -- it is data, not code. Section 3 step 2
    talks about "the handler for its `ats` type" as though each entry carried an `ats`
    field, but in section 5 the ats type is the *outer key*. That is reconciled here by
    injecting `ats` and `source_id`, rather than by reshaping the spec's data.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    sources: list[dict] = []
    for ats, entries in raw.items():
        if ats in SKIP_ATS:
            continue
        if ats not in ID_FIELDS:
            log.warning("sources.json has unknown ats %r, skipping", ats)
            continue
        if ats not in handlers.HANDLERS:
            # An ats with no handler is skipped rather than polled-and-failed, so it cannot
            # march toward the section 9 three-strike alert for a handler that does not
            # exist. A test asserts every sources.json key is handled or explicitly skipped.
            log.debug("no handler for ats %r, skipping %d source(s)", ats, len(entries))
            continue
        for entry in entries:
            cfg = dict(entry)
            cfg["ats"] = ats
            cfg["source_id"] = source_id(cfg)
            sources.append(cfg)
    return sources


def select(sources: list[dict], only: str | None, ats: str | None) -> list[dict]:
    """Narrow the source list for --source / --ats."""
    if ats:
        wanted_ats = {a.strip() for a in ats.split(",")}
        sources = [s for s in sources if s["ats"] in wanted_ats]
    if only:
        wanted = {s.strip() for s in only.split(",")}
        sources = [s for s in sources if s["source_id"] in wanted]
    return sources


def should_poll(ats: str, run_counter: int, force_workday: bool = False) -> bool:
    """Every source, every run -- see the note on the Workday cadence above.

    Kept as a function rather than inlined so a future per-ATS cadence has somewhere to go,
    and so the existing call site and tests stay meaningful.
    """
    return True


# --------------------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------------------


def utcnow() -> str:
    """Always UTC. Development is on Windows local time, CI is UTC."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_state() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "bootstrapped_at": None,
        "last_run_at": None,
        "run_counter": 0,
        "sources": {},
        "seen": {},
    }


def _tmp_path(path: pathlib.Path) -> pathlib.Path:
    # Same directory, so os.replace stays atomic instead of degrading to a cross-device
    # copy the way a system-temp file would.
    return path.parent / (path.name + ".tmp")


def load_state(path: pathlib.Path = STATE_PATH) -> tuple[dict, bool]:
    """Return (state, is_bootstrap).

    Bootstrap is detected by the explicit `bootstrapped_at` sentinel, never by
    `len(seen) == 0`. Those are different situations: a first run has no sentinel, whereas
    a poller whose boards all happen to be empty has one. Conflating them would re-flood
    the user with hundreds of pushes the first time every board went quiet.

    A file that exists but cannot be parsed is CORRUPT and raises. It is never overwritten
    and never re-bootstrapped: silently starting over would look like success while
    destroying history, and the recovery (`git checkout HEAD~1 -- state/seen.json`) is only
    possible if the bad state was not committed on top of the good one.
    """
    stale = _tmp_path(path)
    if stale.exists():
        # Never promote a temp file -- there is no way to know it was complete.
        log.warning("discarding stale %s from an interrupted write", stale.name)
        stale.unlink()

    if not path.exists():
        return new_state(), True

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        log.warning("%s is empty; treating as first run", path)
        return new_state(), True

    try:
        state = json.loads(text)
    except json.JSONDecodeError as exc:
        raise StateCorrupt(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(state, dict):
        raise StateCorrupt(f"{path} is a {type(state).__name__}, expected an object")
    if not isinstance(state.get("seen"), dict):
        raise StateCorrupt(f"{path} has no usable 'seen' object")
    if not isinstance(state.get("sources"), dict):
        raise StateCorrupt(f"{path} has no usable 'sources' object")
    if state.get("schema_version", 1) > SCHEMA_VERSION:
        raise StateCorrupt(
            f"{path} is schema v{state['schema_version']}, this poller understands "
            f"v{SCHEMA_VERSION}. Upgrade the poller rather than downgrading the state."
        )

    state.setdefault("run_counter", 0)
    state.setdefault("last_run_at", None)
    return state, not state.get("bootstrapped_at")


def save_state(path: pathlib.Path, state: dict) -> None:
    """Atomic write.

    sort_keys is load-bearing rather than cosmetic: it keeps the git diff minimal and
    deterministic, and because every seen key starts with its source id, a flat sorted dict
    groups each board's entries contiguously anyway.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(path)
    payload = json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False) + "\n"

    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    # os.replace, NOT os.rename: rename raises FileExistsError on Windows when the
    # destination exists, which is every run after the first.
    os.replace(tmp, path)


def seen_key(job: dict) -> str:
    """"{source}::{job_id}". Split with split("::", 1) -- source ids never contain "::"."""
    return f"{job['source']}::{job['job_id']}"


def record_seen(state: dict, job: dict, outcome: str, *, reason: str = "",
                duplicate_of: str = "", now: str = "") -> None:
    """Write one job into seen.json with what happened to it.

    Section 3 step 7 requires that rejected jobs are recorded too, so a rejection is never
    re-evaluated and can never re-notify. `reason` is kept so every rejection stays
    auditable -- after a month the keyword lists can be tuned against real evidence.
    """
    entry = {"first_seen": now or utcnow(), "outcome": outcome}
    # Bootstrap writes thousands of rows at once and those titles have no debugging value.
    if outcome != "bootstrap":
        entry["title"] = job["title"]
    if reason:
        entry["reason"] = reason
    if duplicate_of:
        entry["duplicate_of"] = duplicate_of
    state["seen"][seen_key(job)] = entry


# --------------------------------------------------------------------------------------
# Diff and dedupe
# --------------------------------------------------------------------------------------


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm(text: str) -> str:
    s = unicodedata.normalize("NFKD", text or "").casefold()
    return _NON_ALNUM.sub(" ", s).strip()


def dedupe_key(job: dict) -> tuple[str, str]:
    return (_norm(job["company"]), _norm(job["title"]))


def diff_new(jobs: list[dict], state: dict) -> list[dict]:
    """Jobs whose (source, job_id) is not already in seen.json."""
    seen = state["seen"]
    return [j for j in jobs if seen_key(j) not in seen]


def dedupe(new_jobs: list[dict]) -> tuple[list[dict], list[tuple[dict, str]]]:
    """Collapse the same role appearing on two different boards.

    Grouped on (company, title) and applied ONLY across different sources. A group that
    lives entirely within one source is kept whole: a single board legitimately posts
    "Software Engineer Intern" separately for two locations, and collapsing those would
    hide a real job.

    Returns (kept, [(dropped_job, winning_key), ...]).
    """
    groups: dict[tuple[str, str], list[tuple[int, dict]]] = {}
    for index, job in enumerate(new_jobs):
        groups.setdefault(dedupe_key(job), []).append((index, job))

    kept: list[tuple[int, dict]] = []
    dropped: list[tuple[dict, str]] = []

    for group in groups.values():
        sources = {job["source"] for _, job in group}
        if len(sources) == 1:
            kept.extend(group)
            continue
        best = min(sources, key=lambda s: (SOURCE_PRIORITY.get(s.split(":", 1)[0], 99), s))
        winners = [(i, j) for i, j in group if j["source"] == best]
        kept.extend(winners)
        winning_key = seen_key(winners[0][1])
        for _, job in group:
            if job["source"] != best:
                dropped.append((job, winning_key))

    kept.sort(key=lambda pair: pair[0])          # preserve the original fetch order
    return [job for _, job in kept], dropped


# --------------------------------------------------------------------------------------
# Health (section 9)
# --------------------------------------------------------------------------------------


def _source_health(state: dict, sid: str) -> dict:
    return state["sources"].setdefault(sid, {
        "consecutive_failures": 0,
        "consecutive_zeros": 0,
        "last_ok_run": None,
        "last_failure_alert_run": None,
        "last_zero_alert_run": None,
    })


def update_health(state: dict, results: list[handlers.FetchResult],
                  run_counter: int) -> tuple[list[str], list[str]]:
    """Update per-source counters and decide which alerts are due.

    Only sources actually polled this run are touched. On the three runs in four where
    Workday is skipped, its counters must not drift -- iterating over every configured
    source instead of every result is an easy way to get that wrong.

    Returns (broken, drifted) as lists of human-readable strings, aggregated by the caller
    into a single push rather than one per source.
    """
    broken: list[str] = []
    drifted: list[str] = []

    for result in results:
        health = _source_health(state, result.source)
        health.update(result.state_updates)

        if not result.ok:
            health["consecutive_failures"] += 1
            if (health["consecutive_failures"] >= FAILURE_ALERT_STREAK
                    and _alert_due(health, "last_failure_alert_run", run_counter)):
                health["last_failure_alert_run"] = run_counter
                broken.append(f"{result.source} ({health['consecutive_failures']}x): {result.error}")
            continue

        health["consecutive_failures"] = 0
        if result.jobs:
            health["consecutive_zeros"] = 0
            health["last_ok_run"] = run_counter
            continue

        health["consecutive_zeros"] += 1
        # Section 9 says "a source that PREVIOUSLY RETURNED JOBS returns zero". A board
        # that has never returned anything is a bad token or a legitimately empty board
        # (section 5 says Attabotics may be one) -- the probe reports those, not a push
        # every 30 minutes.
        if health["last_ok_run"] is None:
            continue
        if (health["consecutive_zeros"] >= ZERO_ALERT_STREAK
                and _alert_due(health, "last_zero_alert_run", run_counter)):
            health["last_zero_alert_run"] = run_counter
            drifted.append(f"{result.source} ({health['consecutive_zeros']} runs at zero)")

    return broken, drifted


def _alert_due(health: dict, field: str, run_counter: int) -> bool:
    last = health.get(field)
    return last is None or (run_counter - last) >= ALERT_COOLDOWN_RUNS


# --------------------------------------------------------------------------------------
# Probe
# --------------------------------------------------------------------------------------


def cmd_probe(sources: list[dict], ctx: FetchContext) -> int:
    """Hit every board once and print `source -> status, job count`.

    Section 10 asks for this "one-off script early", before notifications are wired. It is
    a mode of poller.py rather than a separate script on purpose: a standalone probe would
    duplicate the parsing logic and could therefore pass while the real handlers fail,
    which defeats the entire point of running it.

    Sends nothing, writes no state, and never deletes a sources.json entry.
    """
    print(f"{'source':40} {'status':>6} {'jobs':>6}  note")
    print("-" * 84)

    live, empty, dead, verified_broken = [], [], [], []

    for cfg in sources:
        result = fetch_source(cfg, ctx)
        status_txt = str(result.status) if result.status is not None else "-"
        note_bits = [cfg.get("status", "")]

        if not result.ok:
            note_bits.append(f"DEAD: {result.error}")
            dead.append(result)
        elif not result.jobs:
            note_bits.append("EMPTY")
            empty.append(result)
        else:
            live.append(result)
        if result.state_updates.get("greenhouse_host") == "eu":
            note_bits.append("via boards-api.eu")

        if cfg.get("status") == "verified" and (not result.ok or not result.jobs):
            verified_broken.append(result.source)

        print(f"{result.source:40} {status_txt:>6} {len(result.jobs):>6}  "
              f"{' '.join(b for b in note_bits if b)}")

    print("-" * 84)
    print(f"{len(sources)} sources: {len(live)} live, {len(empty)} empty, {len(dead)} dead")

    if empty:
        print("\nEmpty boards (200 but zero postings -- may be legitimate):")
        for r in empty:
            print(f"  {r.source}")
    if dead:
        print("\nDead boards (token drift or wrong host) -- REPORT, do not auto-delete:")
        for r in dead:
            print(f"  {r.source:40} {r.error}")

    if verified_broken:
        print(f"\nFAIL: {len(verified_broken)} source(s) marked `verified` returned "
              f"nothing: {', '.join(verified_broken)}")
        return 1
    return 0


# --------------------------------------------------------------------------------------
# The run loop
# --------------------------------------------------------------------------------------


def _log_line(result: handlers.FetchResult, new_count: int, health: dict) -> None:
    """Section 9: one scannable line per source, per run."""
    if not result.ok:
        tag, status = "FAIL", "-"
    elif not result.jobs:
        tag, status = "warn", str(result.status)
    else:
        tag, status = "ok  ", str(result.status)

    extra = ""
    if not result.ok:
        extra = f"  {result.error}"
    elif not result.jobs and health.get("consecutive_zeros"):
        extra = f"  (zero streak {health['consecutive_zeros']})"
    if result.pages > 1:
        # Cheapest possible tripwire for the Workday `total`-only-on-page-1 quirk: a
        # paginated board that suddenly reports 2p instead of 24p has been truncated.
        extra += f"  {result.pages}p"

    log.info("[%s] %-34s %4s %5d jobs %4d new %5.1fs%s", tag, result.source, status,
             len(result.jobs), new_count, result.elapsed_ms / 1000, extra)


def run(args) -> int:
    sources = select(load_sources(), args.source, args.ats)
    by_source = {cfg["source_id"]: cfg for cfg in sources}

    try:
        state, is_bootstrap = load_state(args.state)
    except StateCorrupt as exc:
        log.error("STATE CORRUPT: %s", exc)
        log.error("Not overwriting. Recover with: git checkout HEAD~1 -- %s", args.state)
        if not args.dry_run:
            notify.notify_alert("Job poller: state unreadable", str(exc))
        return 2

    # Captured BEFORE update_health, which setdefaults an entry for every polled source.
    # A source absent here has never been polled, so everything on its board is history
    # rather than news -- see the backfill split below.
    known_sources = set(state["sources"])

    run_counter = state["run_counter"] + 1
    now = utcnow()
    ctx = FetchContext(
        session=build_session(),
        jitter=not args.no_jitter,
        state_hints=state["sources"],
    )

    if is_bootstrap:
        log.info("BOOTSTRAP: no prior state, recording everything and sending nothing")

    # 1-2. Fetch and normalize. fetch_source never raises, so one dead board cannot abort.
    results = []
    for cfg in sources:
        if not should_poll(cfg["ats"], run_counter):
            log.info("[skip] %-34s (cadence: run %d)", cfg["source_id"], run_counter)
            continue
        results.append(fetch_source(cfg, ctx))

    all_jobs = [job for r in results if r.ok for job in r.jobs]

    # 3. Diff against seen BEFORE dedupe. Dropping a duplicate first would mean its job_id
    #    never enters seen.json, so it gets re-evaluated forever -- and on any run where the
    #    primary board fails, the secondary copy survives dedupe and fires a duplicate push
    #    for a job notified weeks ago. This ordering is what closes that hole.
    new_jobs = diff_new(all_jobs, state)

    # 3b. Split off boards being polled for the very first time. Registering a new handler
    #     makes every job on its boards "new" at once -- adding Workday alone produces
    #     ~1,800. Without this they would trip ANOMALY_THRESHOLD and fire a "state may have
    #     been lost" alert, which is safe but false, and which hides every matching role.
    #     This is section 3's first-run bootstrap logic applied per source instead of per
    #     repo, and it makes every future handler addition a non-event.
    backfill_jobs: list[dict] = []
    fresh_jobs = new_jobs
    if not is_bootstrap:
        backfill_jobs = [j for j in new_jobs if j["source"] not in known_sources]
        fresh_jobs = [j for j in new_jobs if j["source"] in known_sources]

    # 4. Dedupe across boards, before filtering: it avoids a content fetch on a copy that
    #    is about to be discarded, and leaves the Greenhouse copy (the one with a
    #    description body for the clearance check) as the survivor.
    #    Backfilled jobs are excluded -- they never notify, so a dedupe decision about them
    #    could only suppress a real notification from an established board.
    kept, dropped = dedupe(fresh_jobs)

    broken, drifted = update_health(state, results, run_counter)

    new_by_source: dict[str, int] = {}
    for job in new_jobs:
        new_by_source[job["source"]] = new_by_source.get(job["source"], 0) + 1
    for result in results:
        _log_line(result, new_by_source.get(result.source, 0), state["sources"].get(result.source, {}))

    passed: list[dict] = []
    rejected: list[tuple[dict, filters.Verdict]] = []

    if is_bootstrap:
        for job in new_jobs:
            record_seen(state, job, "bootstrap", now=now)
        log.info("BOOTSTRAP: recorded %d jobs across %d sources, 0 notifications sent",
                 len(new_jobs), len(results))
    else:
        if backfill_jobs:
            _record_backfill(state, backfill_jobs, now)

        # 5. Filter. Title first; then, for Greenhouse survivors only, fetch the one
        #    description body needed for the clearance check.
        for job in kept:
            verdict = filters.check_title(job["title"])
            if not verdict.passed:
                rejected.append((job, verdict))
                continue

            if job["ats"] == "greenhouse" and not job["content"]:
                cfg = by_source.get(job["source"])
                if cfg:
                    job["content"] = greenhouse_fetch_content(cfg, job["job_id"], ctx)

            clearance = filters.check_clearance(job["content"]) if job["content"] else None
            if clearance is not None and not clearance.passed:
                rejected.append((job, clearance))
                continue
            passed.append(job)

        _notify_and_record(state, passed, rejected, dropped, fresh_jobs, now, args)

    # Section 9 alerts, aggregated: a network blip that breaks 20 boards sends one push.
    if (broken or drifted) and not args.dry_run:
        body = "\n".join(["Broken:", *broken] if broken else [])
        if drifted:
            body += ("\n" if body else "") + "\n".join(["Zero for several runs:", *drifted])
        notify.notify_alert(f"Job poller: {len(broken) + len(drifted)} source(s) unhealthy", body)
    for line in broken + drifted:
        log.warning("UNHEALTHY %s", line)

    state["run_counter"] = run_counter
    state["last_run_at"] = now
    if is_bootstrap:
        state["bootstrapped_at"] = now

    ok_count = sum(1 for r in results if r.ok)
    log.info("run %d: %d/%d sources ok, %d total jobs, %d new, %d notified, %d rejected, "
             "%d duplicate", run_counter, ok_count, len(results), len(all_jobs),
             len(new_jobs), len(passed), len(rejected), len(dropped))

    if args.dry_run:
        log.info("--dry-run: state not written")
        return 0

    save_state(args.state, state)
    return 0


def _record_backfill(state: dict, jobs: list[dict], now: str) -> None:
    """Absorb a never-before-polled board into state without notifying.

    Logs the roles that WOULD have matched, because the whole point of adding a source is
    those roles -- silently swallowing them is what the first-run bootstrap got wrong until
    `--list-open` was added. Re-run `poller.py --list-open` afterwards for the full list.
    """
    by_source: dict[str, int] = {}
    for job in jobs:
        record_seen(state, job, "backfill", now=now)
        by_source[job["source"]] = by_source.get(job["source"], 0) + 1

    matching = [j for j in jobs if filters.check_title(j["title"]).passed]
    log.info("BACKFILL: %d job(s) from %d new source(s), 0 notifications sent",
             len(jobs), len(by_source))
    for sid, count in sorted(by_source.items()):
        log.info("  backfilled %-34s %5d jobs", sid, count)
    if matching:
        log.info("  %d of them match the filter (run --list-open for links):", len(matching))
        for job in matching:
            log.info("    %s - %s", job["company"], job["title"])


def _notify_and_record(state, passed, rejected, dropped, new_jobs, now, args) -> None:
    """Notify, then record. Ordering matters -- see below.

    A job whose push FAILS is deliberately left out of seen.json so the next run retries
    it. Recording first would mark it seen and silently swallow the posting, which is the
    one outcome this tool exists to prevent.
    """
    for job, verdict in rejected:
        record_seen(state, job, "rejected", reason=verdict.reason, now=now)
    for job, winner in dropped:
        record_seen(state, job, "duplicate", duplicate_of=winner, now=now)

    for job in passed:
        log.info("NEW  %s — %s  %s", job["company"], job["title"], job["url"])

    if args.dry_run:
        log.info("--dry-run: would notify %d job(s)", len(passed))
        return
    if not passed:
        return

    # Not in the spec. A run this large means the state file was lost or truncated, not
    # that 200 internships opened at once -- so say that instead of sending 200 pushes.
    if len(new_jobs) > ANOMALY_THRESHOLD:
        log.error("ANOMALY: %d new jobs in one run; suppressing individual pushes",
                  len(new_jobs))
        notify.notify_alert(
            "Job poller anomaly",
            f"{len(new_jobs)} new jobs in one run ({len(passed)} would have notified). "
            f"State may have been lost; check state/seen.json.",
        )
        for job in passed:
            record_seen(state, job, "summarized", now=now)
        return

    if len(passed) > SUMMARY_THRESHOLD:
        # Section 7: one summary instead of 9+ pushes. Full list already went to the log.
        if notify.notify_summary(passed):
            for job in passed:
                record_seen(state, job, "summarized", now=now)
        else:
            log.error("summary push failed; %d job(s) stay unseen and retry next run",
                      len(passed))
        return

    for job in passed:
        if notify.notify_job(job):
            record_seen(state, job, "notified", now=now)
        else:
            log.error("push failed for %s; it stays unseen and retries next run",
                      seen_key(job))


def cmd_list_open(sources: list[dict], ctx: FetchContext, path: pathlib.Path) -> int:
    """Write every currently-open posting that passes the title filter to a markdown file.

    Exists because the section 3 bootstrap is silent by design: it marks everything on
    every board as seen and sends nothing, which is right (otherwise the first run fires
    several hundred pushes) but leaves the whole existing backlog invisible. Notifications
    only ever cover what appears AFTER the bootstrap, so this is how you see what is
    already out there -- and re-running it is how the list stays current.

    Read-only: no notifications, no state written.
    """
    rows: list[dict] = []
    for cfg in sources:
        result = fetch_source(cfg, ctx)
        if not result.ok:
            log.warning("[FAIL] %s: %s", result.source, result.error)
            continue
        rows.extend(j for j in result.jobs if filters.check_title(j["title"]).passed)

    rows.sort(key=lambda j: (j["company"].casefold(), j["title"].casefold()))
    companies = sorted({j["company"] for j in rows}, key=str.casefold)

    lines = [
        f"# Open SWE internships matching the filter ({len(rows)})",
        "",
        f"Generated {utcnow()} by `python poller.py --list-open`. Re-run it to refresh;",
        "this is a point-in-time snapshot, not something the poller keeps up to date.",
        "",
        f"{len(rows)} roles across {len(companies)} companies.",
    ]
    current = None
    for job in rows:
        if job["company"] != current:
            current = job["company"]
            lines += ["", f"## {current}", ""]
        where = f" — {job['location']}" if job["location"] else ""
        lines.append(f"- [{job['title']}]({job['url']}){where}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    log.info("wrote %d roles across %d companies to %s", len(rows), len(companies), path)
    return 0


def cmd_test_notify() -> int:
    """Send one push and exit. Verifies the whole notification path end to end.

    Worth having as a permanent mode rather than a one-off: a working poller that finds
    nothing for a week looks exactly like a poller whose NTFY_TOPIC is wrong, and the
    difference only shows up when a posting is missed. This tells them apart in 5 seconds.

    The em-dash in the title is deliberate. As an HTTP header it would raise
    UnicodeEncodeError under latin-1, so a push that arrives intact is proof the JSON API
    deviation in notify.py actually works against the real service.
    """
    ok = notify.send(
        title="Job poller — test push",
        body="NTFY_TOPIC is wired correctly.\n"
             "The em-dash in this title proves UTF-8 encoding survives end to end.",
        tags="white_check_mark",
    )
    log.info("test push: %s", "sent" if ok else "FAILED")
    return 0 if ok else 1


# --------------------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Aerospace SWE internship poller")
    p.add_argument("--probe", action="store_true",
                   help="hit every board, print status and job counts, change nothing")
    p.add_argument("--dry-run", action="store_true",
                   help="run the full pipeline but send no pushes and write no state")
    p.add_argument("--source", help="comma-separated source ids, e.g. greenhouse:vardaspace")
    p.add_argument("--ats", help="comma-separated ats types, e.g. lever,ashby")
    p.add_argument("--list-open", nargs="?", const="open-internships.md", metavar="PATH",
                   help="write all currently-open matching roles to a markdown file and exit")
    p.add_argument("--test-notify", action="store_true",
                   help="send one test push and exit; verifies NTFY_TOPIC end to end")
    p.add_argument("--no-jitter", action="store_true", help="skip inter-request sleeps")
    p.add_argument("--state", type=pathlib.Path, default=STATE_PATH)
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(message)s",
        stream=sys.stdout,
    )
    # urllib3 logs every retry at WARNING; section 9 wants one tidy line per source and
    # the retry noise buries it.
    logging.getLogger("urllib3").setLevel(logging.ERROR)

    if args.list_open:
        sources = select(load_sources(), args.source, args.ats)
        ctx = FetchContext(session=build_session(), jitter=not args.no_jitter)
        return cmd_list_open(sources, ctx, pathlib.Path(args.list_open))

    if args.test_notify:
        return cmd_test_notify()

    if args.probe:
        sources = select(load_sources(), args.source, args.ats)
        ctx = FetchContext(session=build_session(), jitter=not args.no_jitter)
        return cmd_probe(sources, ctx)

    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
