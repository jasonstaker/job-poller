"""Entry point: load sources, fetch, diff, filter, notify, persist.

Build order step 3 landed `--probe`. The full run loop arrives in step 4.
"""

from __future__ import annotations

import argparse
import json
import logging
import pathlib
import sys

import handlers
from handlers import ID_FIELDS, SKIP_ATS, FetchContext, build_session, fetch_source, source_id

ROOT = pathlib.Path(__file__).parent
SOURCES_PATH = ROOT / "sources.json"
STATE_PATH = ROOT / "state" / "seen.json"

log = logging.getLogger("poller")


def load_sources(path: pathlib.Path = SOURCES_PATH) -> list[dict]:
    """Flatten sources.json into a list of source configs.

    sources.json stays byte-identical to BUILD_SPEC.md section 5 -- it is data, not code.
    Section 3 step 2 talks about "the handler for its `ats` type" as though each entry
    carried an `ats` field, but in section 5 the ats type is the *outer key*. That is
    resolved here by injecting `ats` and `source_id` into each record, rather than by
    editing the spec's data.
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    sources: list[dict] = []
    for ats, entries in raw.items():
        if ats in SKIP_ATS:
            continue
        if ats not in ID_FIELDS:
            log.warning("sources.json has unknown ats %r, skipping", ats)
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


def cmd_probe(sources: list[dict], ctx: FetchContext) -> int:
    """Hit every board once and print `source -> status, job count`.

    Section 10 asks for this "one-off script early", before notifications are wired. It is
    a mode of poller.py rather than a separate script on purpose: a standalone probe would
    duplicate the parsing logic and could therefore pass while the real handlers fail,
    which defeats the entire point of running it.

    Sends nothing, writes no state. Never deletes a sources.json entry -- section 10 is
    explicit that results get reported to the user instead.
    """
    print(f"{'source':40} {'status':>6} {'jobs':>6}  note")
    print("-" * 84)

    live: list[handlers.FetchResult] = []
    empty: list[handlers.FetchResult] = []
    dead: list[tuple[handlers.FetchResult, dict]] = []
    verified_broken: list[str] = []

    for cfg in sources:
        result = fetch_source(cfg, ctx)
        status_txt = str(result.status) if result.status is not None else "-"
        note_bits = [cfg.get("status", "")]

        if not result.ok:
            note_bits.append(f"DEAD: {result.error}")
            dead.append((result, cfg))
        elif not result.jobs:
            note_bits.append("EMPTY")
            empty.append(result)
        else:
            live.append(result)
        if result.state_updates.get("greenhouse_host") == "eu":
            note_bits.append("via boards-api.eu")

        if cfg.get("status") == "verified" and (not result.ok or not result.jobs):
            verified_broken.append(result.source)

        note = " ".join(b for b in note_bits if b)
        print(f"{result.source:40} {status_txt:>6} {len(result.jobs):>6}  {note}")

    total = len(sources)
    print("-" * 84)
    print(f"{total} sources: {len(live)} live, {len(empty)} empty, {len(dead)} dead")

    if empty:
        print("\nEmpty boards (200 but zero postings -- may be legitimate):")
        for r in empty:
            print(f"  {r.source}")
    if dead:
        print("\nDead boards (token drift or wrong host) -- REPORT, do not auto-delete:")
        for r, cfg in dead:
            print(f"  {r.source:40} {r.error}")

    # A `verified` token that is broken means the spec's own verification has drifted, so
    # the probe exits non-zero to make that impossible to miss in CI.
    if verified_broken:
        print(f"\nFAIL: {len(verified_broken)} source(s) marked `verified` "
              f"returned nothing: {', '.join(verified_broken)}")
        return 1
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Aerospace SWE internship poller")
    p.add_argument("--probe", action="store_true",
                   help="hit every board, print status and job counts, change nothing")
    p.add_argument("--dry-run", action="store_true",
                   help="run the full pipeline but send no pushes and write no state")
    p.add_argument("--source", help="comma-separated source ids, e.g. greenhouse:vardaspace")
    p.add_argument("--ats", help="comma-separated ats types, e.g. lever,ashby")
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
    # urllib3 logs every retry at WARNING. Section 9 wants one tidy line per source,
    # and the retry noise buries it.
    logging.getLogger("urllib3").setLevel(logging.ERROR)

    sources = select(load_sources(), args.source, args.ats)
    ctx = FetchContext(session=build_session(), jitter=not args.no_jitter)

    if args.probe:
        return cmd_probe(sources, ctx)

    log.error("the run loop lands in build order step 4; use --probe for now")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
