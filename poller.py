"""Entry point: load sources, fetch, diff, dedupe, filter, notify, persist.

The pipeline order in `run` is load-bearing; see the comment there before rearranging it.
"""

from __future__ import annotations

import argparse
import hashlib
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
# Not in the spec. More than this many MATCHING jobs in one run means something is wrong
# with state rather than with the job market, so they collapse into one digest instead of
# flooding the phone. Counted on notifiable jobs only: the 2026-09-14 audit found the old
# version counting ALL new jobs across all 61 boards, so a flood of junk on one board
# suppressed -- and permanently buried -- the two real internships beside it.
ANOMALY_NOTIFY_THRESHOLD = 25
# Section 9 alert thresholds, in OBSERVATIONS.
FAILURE_ALERT_STREAK = 3
ZERO_ALERT_STREAK = 5
# The cooldown is WALL-CLOCK, not a run count. It used to be 48 runs, chosen to mean "once
# a day" at the */30 cron -- but GitHub actually delivers ~3.8h between runs, which
# stretched it to 7.6 days. A single dropped alert bought a dead board a week of silence.
ALERT_COOLDOWN_HOURS = 24
# A board that has NEVER returned a job is either a dead token or legitimately empty. It
# now gets one alert once it has been dead this long, instead of being exempt forever --
# which is what silenced greenhouse:capellaspace and lever:attabotics for their entire
# lifetime while both sat at 12 consecutive zeros.
NEVER_WORKED_ALERT_HOURS = 48
# A collapsing job count is the signature of token drift to a smaller board, a renamed
# payload key, or Workday pagination truncating. None of those trip any other alert,
# because they all return HTTP 200 with a plausible-looking payload.
COUNT_DROP_FRACTION = 0.5
COUNT_DROP_MIN_BASELINE = 20
# How often the deadman heartbeat fires. Long enough not to be noise, short enough that a
# silently disabled cron is noticed in days rather than at the end of the hiring season.
HEARTBEAT_HOURS = 168
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
        # Deliberately NOT an automatic bootstrap. In CI a missing state file means the
        # checkout or the commit-back broke, not that it is day one; re-bootstrapping there
        # would mark all ~13,000 open roles seen, send nothing, go green, and then commit
        # the amnesiac state over the good one -- destroying the git-history recovery this
        # docstring promises. The caller decides, via --bootstrap.
        return new_state(), True

    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise StateCorrupt(
            f"{path} exists but is empty. Refusing to re-bootstrap over it: that would "
            f"mark every open role as seen and send nothing. Recover with "
            f"`git checkout HEAD~1 -- {path}`, or pass --bootstrap to start fresh."
        )

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


def maybe_heartbeat(state: dict, results: list, now: str, args) -> bool:
    """Weekly "still alive" push. The deadman switch.

    The healthy output of this tool is silence, so "working but quiet" and "dead" have
    identical observable signatures. Nothing else covers the failures that live OUTSIDE the
    Python: GitHub dropping scheduled runs (it is already dropping ~84% of them), GitHub
    disabling the workflow after 60 days of repo inactivity -- bot commits are widely
    reported not to reset that timer -- Actions being disabled, or a workflow syntax error
    on main. A dead poller sends no alerts by definition, because it never runs.

    Low priority so it does not read as urgent on a lock screen.
    """
    if args.dry_run:
        return False
    elapsed = _hours_since(state.get("last_heartbeat_at"), now)
    if elapsed is not None and elapsed < HEARTBEAT_HOURS:
        return False
    if elapsed is None and state.get("last_heartbeat_at") is None and not state.get("seen"):
        return False        # nothing to report on a brand-new state

    ok = sum(1 for r in results if r.ok)
    notified = sum(1 for v in state["seen"].values() if v.get("outcome") == "notified")
    body = (f"{ok}/{len(results)} boards healthy.\n"
            f"{len(state['seen'])} postings tracked, {notified} sent to you so far.\n"
            f"Run {state.get('run_counter', 0)}. If this stops arriving, something broke.")
    if notify.send(title="Job poller: weekly check-in", body=body,
                   tags="heartbeat", priority="low"):
        state["last_heartbeat_at"] = now
        log.info("heartbeat sent")
        return True
    log.error("::error::heartbeat could not be delivered")
    return False


def filter_version() -> str:
    """Short hash of the section 6 keyword lists.

    Lets the poller notice that the filter itself changed, which is what makes tuning
    retroactive. Before this, widening the lists only affected FUTURE postings: anything
    already recorded as rejected was permanently invisible, because diff_new keys on seen.
    The SpaceX "New Graduate Engineer, Software" roles sat in state exactly that way.
    """
    payload = json.dumps([
        filters.INTERNSHIP_MARKERS, filters.SOFTWARE_MARKERS,
        filters.TITLE_REJECT, filters.CLEARANCE_REJECT, filters.CLEARANCE_ALLOW,
    ], sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def rescan_rejected(state: dict) -> list[str]:
    """Forget rejections the current filter would no longer make.

    Deliberately DELETES the entries rather than trying to re-notify from them: a stored
    entry has only a title, no url or company, so it cannot be pushed. Dropping the key
    lets the next fetch rediscover the job from the live board with full data, after which
    it flows through the ordinary diff -> filter -> notify path. Roles that have since been
    taken down simply never come back, which is correct.
    """
    freed = [k for k, v in state["seen"].items()
             if v.get("outcome") == "rejected"
             and v.get("title")
             and filters.check_title(v["title"]).passed]
    for key in freed:
        del state["seen"][key]
    return freed


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
        "first_ok_run": None,
        "last_nonzero_at": None,
        "backfilled_at": None,
        "job_count_baseline": 0,
        "last_failure_alert_at": None,
        "last_zero_alert_at": None,
        "last_count_alert_at": None,
    })


def update_health(state: dict, results: list[handlers.FetchResult],
                  now: str) -> tuple[list[tuple[str, str, str]], list[tuple[str, str, str]]]:
    """Update per-source counters and decide which alerts are due.

    Only sources actually polled this run are touched. On the three runs in four where
    Workday is skipped, its counters must not drift -- iterating over every configured
    source instead of every result is an easy way to get that wrong.

    Returns (broken, drifted) as lists of human-readable strings, aggregated by the caller
    into a single push rather than one per source.
    """
    broken: list[tuple[str, str, str]] = []
    drifted: list[tuple[str, str, str]] = []

    for result in results:
        health = _source_health(state, result.source)
        health.update(result.state_updates)

        if not result.ok:
            health["consecutive_failures"] += 1
            if (health["consecutive_failures"] >= FAILURE_ALERT_STREAK
                    and _alert_due(health, "last_failure_alert_at", now)):
                broken.append((result.source, "last_failure_alert_at",
                               f"{result.source} ({health['consecutive_failures']}x): "
                               f"{result.error}"))
            continue

        health["consecutive_failures"] = 0
        if health.get("first_ok_run") is None:
            health["first_ok_run"] = now
        health["last_ok_run"] = now
        count = len(result.jobs)

        if count:
            baseline = health.get("job_count_baseline") or 0
            if (baseline >= COUNT_DROP_MIN_BASELINE
                    and count < baseline * COUNT_DROP_FRACTION
                    and _alert_due(health, "last_count_alert_at", now)):
                drifted.append((result.source, "last_count_alert_at",
                                f"{result.source}: {count} jobs, was {baseline}"))
            # High-water mark, decayed gently so a genuine seasonal shrink eventually
            # becomes the new normal rather than alerting forever.
            health["job_count_baseline"] = max(count, int(baseline * 0.9))
            health["consecutive_zeros"] = 0
            health["last_nonzero_at"] = now
            continue

        health["consecutive_zeros"] += 1
        if health.get("last_nonzero_at") is None:
            # Never returned a single job. This was exempt FOREVER, which is exactly what
            # silenced capellaspace and attabotics across their whole lifetime. Now it
            # alerts once, after long enough that a transient empty board is ruled out.
            age = _hours_since(health.get("first_ok_run"), now)
            if (age is not None and age >= NEVER_WORKED_ALERT_HOURS
                    and _alert_due(health, "last_zero_alert_at", now)):
                drifted.append((result.source, "last_zero_alert_at",
                                f"{result.source}: never returned a job in "
                                f"{health['consecutive_zeros']} polls -- token may be dead"))
            continue
        if (health["consecutive_zeros"] >= ZERO_ALERT_STREAK
                and _alert_due(health, "last_zero_alert_at", now)):
            drifted.append((result.source, "last_zero_alert_at",
                            f"{result.source} ({health['consecutive_zeros']} polls at zero)"))

    return broken, drifted


def stamp_alerts(state: dict, alerts: list[tuple[str, str, str]], now: str) -> None:
    """Record that an alert was delivered. Only called after a SUCCESSFUL push.

    Stamping inside update_health meant one transient ntfy failure marked the alert as
    delivered and bought a dead board a full cooldown of silence.
    """
    for sid, field, _ in alerts:
        _source_health(state, sid)[field] = now


def _hours_since(stamp: str | None, now: str) -> float | None:
    if not stamp:
        return None
    try:
        a = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
        b = datetime.strptime(now, "%Y-%m-%dT%H:%M:%SZ")
    except (TypeError, ValueError):
        return None
    return (b - a).total_seconds() / 3600.0


def _alert_due(health: dict, field: str, now: str) -> bool:
    """Wall-clock cooldown, so a throttled cron cannot stretch it. See ALERT_COOLDOWN_HOURS."""
    elapsed = _hours_since(health.get(field), now)
    return elapsed is None or elapsed >= ALERT_COOLDOWN_HOURS


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
    known_sources = {sid for sid, h in state["sources"].items() if _is_established(h)}

    run_counter = state["run_counter"] + 1
    now = utcnow()
    ctx = FetchContext(
        session=build_session(),
        jitter=not args.no_jitter,
        state_hints=state["sources"],
    )

    if is_bootstrap and not args.bootstrap:
        log.error("::error::%s does not exist and --bootstrap was not given. Refusing to "
                  "start fresh: in CI this means the checkout or commit-back broke, and "
                  "bootstrapping would silently mark every open role as seen.", args.state)
        return 3
    current_filter = filter_version()
    # An absent version counts as "changed": the first run after this shipped should do the
    # catch-up too. Safe on a fresh bootstrap, where there is nothing to release.
    if not is_bootstrap and state.get("filter_version") != current_filter:
        freed = rescan_rejected(state)
        if freed:
            log.info("FILTER CHANGED (%s -> %s): released %d previously-rejected job(s) "
                     "for re-evaluation", state.get("filter_version"), current_filter,
                     len(freed))
    state["filter_version"] = current_filter

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

    broken, drifted = update_health(state, results, now)

    new_by_source: dict[str, int] = {}
    for job in new_jobs:
        new_by_source[job["source"]] = new_by_source.get(job["source"], 0) + 1
    for result in results:
        _log_line(result, new_by_source.get(result.source, 0), state["sources"].get(result.source, {}))

    passed: list[dict] = []
    rejected: list[tuple[dict, filters.Verdict]] = []
    undelivered = 0

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
            # Title first, so a doomed job never costs a content fetch.
            if not filters.check_title(job["title"]).passed:
                rejected.append((job, filters.evaluate(job)))
                continue

            # Greenhouse omits descriptions from the bulk listing, so the body for the
            # clearance check is fetched lazily -- only for jobs that are new AND already
            # past the title filter, which is 0-5 requests per run.
            if job["ats"] == "greenhouse" and not job["content"]:
                cfg = by_source.get(job["source"])
                if cfg:
                    job["content"] = greenhouse_fetch_content(cfg, job["job_id"], ctx)

            # filters.evaluate is the single decision point, so the function the tests
            # cover is the one production runs. It used to be reimplemented inline here,
            # leaving evaluate() dead code with five tests pointed at it.
            verdict = filters.evaluate(job)
            if verdict.passed:
                passed.append(job)
            else:
                rejected.append((job, verdict))

        undelivered = _notify_and_record(state, passed, rejected, dropped,
                                         fresh_jobs, now, args)

    # Section 9 alerts, aggregated: a network blip that breaks 20 boards sends one push.
    if (broken or drifted) and not args.dry_run:
        body = "\n".join(["Broken:", *(m for _, _, m in broken)]) if broken else ""
        if drifted:
            body += ("\n" if body else "") + "\n".join(
                ["Suspicious:", *(m for _, _, m in drifted)])
        if notify.notify_alert(
                f"Job poller: {len(broken) + len(drifted)} source(s) unhealthy", body):
            stamp_alerts(state, broken + drifted, now)
        else:
            # Deliberately NOT stamped: an undelivered alert must re-fire next run rather
            # than buy a dead board a full cooldown of silence.
            log.error("::error::health alert could not be delivered; will retry next run")
            undelivered += 1
    for _, _, line in broken + drifted:
        log.warning("::warning::UNHEALTHY %s", line)

    state["run_counter"] = run_counter
    state["last_run_at"] = now
    if is_bootstrap:
        state["bootstrapped_at"] = now

    maybe_heartbeat(state, results, now, args)

    ok_count = sum(1 for r in results if r.ok)
    log.info("run %d: %d/%d sources ok, %d total jobs, %d new, %d notified, %d rejected, "
             "%d duplicate", run_counter, ok_count, len(results), len(all_jobs),
             len(new_jobs), len(passed), len(rejected), len(dropped))

    if args.dry_run:
        log.info("--dry-run: state not written")
        return 0

    save_state(args.state, state)

    if undelivered:
        # Exit non-zero so the Actions run goes RED. Until 2026-09-14 a broken NTFY_TOPIC
        # exited 0 and looked identical to a quiet job market -- and since send() is only
        # reached when there is something to push, it left no evidence on the ~95% of runs
        # with nothing new. The jobs themselves are safe: they stay unseen and retry.
        log.error("::error::%d job(s) could not be delivered; check NTFY_TOPIC and ntfy.sh",
                  undelivered)
        return 1
    return 0


def _is_established(health: dict | None) -> bool:
    """Has this board ever actually been seen working?

    Mere presence in state["sources"] is NOT enough: _source_health setdefaults an entry
    before the result.ok check, so a board whose very FIRST poll failed used to look
    established on its next run. Its whole catalogue then arrived as fresh jobs instead of
    backfill. Anduril's 2.3MB board times out intermittently, so this was a live hazard.
    """
    if not health:
        return False
    return any(health.get(k) is not None
               for k in ("backfilled_at", "first_ok_run", "last_ok_run"))


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

    for sid in {j["source"] for j in jobs}:
        _source_health(state, sid)["backfilled_at"] = now

    matching = [j for j in jobs if filters.check_title(j["title"]).passed]
    log.info("BACKFILL: %d job(s) from %d new source(s), 0 notifications sent",
             len(jobs), len(by_source))
    for sid, count in sorted(by_source.items()):
        log.info("  backfilled %-34s %5d jobs", sid, count)
    if matching:
        log.info("  %d of them match the filter (run --list-open for links):", len(matching))
        for job in matching:
            log.info("    %s - %s", job["company"], job["title"])


def _notify_and_record(state, passed, rejected, dropped, new_jobs, now, args) -> int:
    """Notify, then record. Returns the number of jobs that could NOT be delivered.

    The invariant, which the 2026-09-14 audit found violated: a job is written to seen.json
    only if it was pushed, deliberately filtered out, or deliberately backfilled. A job
    whose push FAILS is left unseen so the next run retries it. Recording first would mark
    it seen and silently swallow the posting -- the one outcome this tool exists to prevent.
    """
    for job, verdict in rejected:
        # Visible in the Actions log, not just as an integer in the summary line. A
        # spurious reject is permanent, so it needs to leave evidence.
        log.info("skip %-28s %-58s %s", job["company"][:28], job["title"][:58], verdict.reason)
        record_seen(state, job, "rejected", reason=verdict.reason, now=now)
    for job, winner in dropped:
        record_seen(state, job, "duplicate", duplicate_of=winner, now=now)

    for job in passed:
        log.info("NEW  %s — %s  %s", job["company"], job["title"], job["url"])

    if args.dry_run:
        log.info("--dry-run: would notify %d job(s)", len(passed))
        return 0
    if not passed:
        return 0

    # Guard against a flood of NOTIFIABLE jobs, which is the only kind that could spam the
    # phone. The old version counted every new job across all 61 boards, so 400 junk
    # postings on one board suppressed the two real internships that arrived alongside
    # them -- and recorded those two as seen without ever pushing them.
    if len(passed) > ANOMALY_NOTIFY_THRESHOLD:
        log.error("::error::ANOMALY: %d matching jobs in one run; sending one digest",
                  len(passed))
        if notify.notify_summary(passed):
            for job in passed:
                record_seen(state, job, "summarized", now=now)
            return 0
        log.error("::error::digest push failed; %d job(s) stay unseen and retry", len(passed))
        return len(passed)

    if len(passed) > SUMMARY_THRESHOLD:
        # Section 7: one summary instead of 9+ pushes. Full list already went to the log.
        if notify.notify_summary(passed):
            for job in passed:
                record_seen(state, job, "summarized", now=now)
            return 0
        log.error("::error::summary push failed; %d job(s) stay unseen and retry next run",
                  len(passed))
        return len(passed)

    undelivered = 0
    for job in passed:
        if notify.notify_job(job):
            record_seen(state, job, "notified", now=now)
        else:
            undelivered += 1
            log.error("::error::push failed for %s; it stays unseen and retries next run",
                      seen_key(job))
    return undelivered


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
    p.add_argument("--bootstrap", action="store_true",
                   help="allow starting from an empty state (marks everything seen, sends "
                        "nothing). Never set in CI -- a missing state file there is a bug.")
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
