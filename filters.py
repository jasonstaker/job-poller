"""Include/exclude logic for job titles and description bodies.

The four keyword lists come verbatim from BUILD_SPEC.md section 6 and are meant to be
trivially editable -- they are the top of this file and nothing else needs to change when
they do.

Two deviations from the spec, both deliberate:

1. Matching is word-boundary, not substring. Section 6 says "case-insensitive" but not how
   to match, and plain `in` is wrong for needles that are actually in these lists:
   `intern` matches "Internal Tools" and "International", `coop` matches "Cooperative",
   `swe` matches "Sweden", `structures` matches "Infrastructures", and `active secret`
   matches "inactive secret".

2. The clearance allow-list is window-scoped rather than global. Section 6 says not to
   reject on "ability to obtain a clearance", but "Must be able to obtain an active
   security clearance" contains a reject phrase verbatim. Allowing globally is also wrong:
   a long JD can carry both "ITAR" and a genuine "must possess an active TS/SCI".
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable

# --------------------------------------------------------------------------------------
# Section 6 keyword lists. Tuples, not lists, so a caller cannot mutate shared state.
# --------------------------------------------------------------------------------------

INTERNSHIP_MARKERS: tuple[str, ...] = (
    "intern", "internship", "co-op", "coop", "early career", "student", "new grad",
    "summer 2027", "fall 2027", "winter 2028",
    # Added 2026-09-16. Not in section 6, but these are the words companies on the live
    # board list actually use, each verified against a real posting that was being dropped:
    "new graduate",     # SpaceX  "New Graduate Engineer, Software"
    "entry level",      # Muon    "Software Engineer, Entry-Level"
    "associate",        # Astranis uses "... Associate (Spring 2027)" instead of "Intern"
    "emerging talent",  # Vast    "Emerging Talent - ... Internship"
    "university",       # common on Workday student boards
)

SOFTWARE_MARKERS: tuple[str, ...] = (
    "software", "swe", "flight software", "ground software", "embedded", "firmware",
    "autonomy", "simulation", "backend", "full stack", "full-stack", "platform",
    "infrastructure", "devops", "robotics software", "perception", "data engineer",
    "machine learning", "computer vision",
    # Added 2026-09-16, each from a real posting rejected as no_software_marker:
    "developer",         # "Server Firmware Developer", "Web Developer Intern"
    "sde",               # Amazon-style titles
    "web",               # Rocket Lab "Web Services Intern"
    "site reliability",  # Varda "Site Reliability Internship - Spring 2027"
    "sre",
    "computer science",
    "test automation",   # Zipline "System Test Automation Intern (Summer 2027)"
    "distributed systems",
    "compiler",
    "data science",
)

TITLE_REJECT: tuple[str, ...] = (
    "gnc", "guidance navigation", "guidance, navigation", "flight dynamics",
    "controls engineer", "astrodynamics", "mechanical", "propulsion", "structures",
    "thermal", "avionics hardware", "manufacturing", "rf engineer", "payload",
    "technician", "machinist", "welder", "composites", "quality engineer",
    "supply chain", "recruiter", "sales", "marketing", "finance", "legal",
    "phd", "doctoral",
)

CLEARANCE_REJECT: tuple[str, ...] = (
    "active security clearance", "active secret", "active top secret", "ts/sci",
    "must possess a clearance", "currently hold a clearance", "interim secret",
)

# Not named by the spec, but implied by it. Section 6 says ITAR / U.S. Person /
# export control / "ability to obtain a clearance" must NOT reject -- the user clears
# those. The first four never collide with a reject needle on their own; they are kept
# here to document intent and to stay correct if a bare "clearance" needle is ever added
# to CLEARANCE_REJECT. The rest are the phrases that actually do the work: negation,
# preference, and "able to obtain", each of which can wrap a verbatim reject phrase.
CLEARANCE_ALLOW: tuple[str, ...] = (
    "itar", "u.s. person", "us person", "export control",
    "ability to obtain", "able to obtain", "eligible to obtain", "willing to obtain",
    "or obtain", "does not require", "not required", "no clearance",
    "preferred", "is a plus", "nice to have",
)

# --------------------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------------------

# How far around a reject match to look for an allow phrase. 90 characters back covers
# "must be able to obtain an ...", "this position does not require an ...", and
# "candidates with an ... are preferred" while staying inside one sentence. 24 forward
# catches a trailing "... is preferred" / "... not required".
CLEARANCE_WINDOW_BEFORE = 90
CLEARANCE_WINDOW_AFTER = 24

_DASHES = re.compile(r"[‐-―−]")
_SMART_QUOTES = re.compile(r"[‘’‛]")
_SMART_DQUOTES = re.compile(r"[“”„‟]")
_WS = re.compile(r"\s+")
_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_BLOCK_TAGS = re.compile(r"</?(br|p|li|div|h[1-6]|tr|ul|ol|table)[^>]*>", re.I)
_ANY_TAG = re.compile(r"<[^>]+>")
_SEPARATORS = re.compile(r"[ \-]+")

# Allowed continuation after a needle, so a keyword also matches its longer inflections.
# Added 2026-09-16 after an audit found real postings being silently dropped:
#   "new grad"      did not match SpaceX  "New Graduate Engineer, Software"
#   "data engineer" did not match Kodiak  "Winter 2027 Intern, Data Engineering"
# The old pattern allowed only an optional trailing "s", so anything longer failed.
#
# This is deliberately a SHORT closed list of English suffixes rather than `[a-z]*`. The
# word-boundary lookarounds are what stop `intern` matching "Internal" and `swe` matching
# "Sweden", and a greedy tail would blow straight through them -- "intern" + "al" would
# match "internal" again, undoing the single most important property of this module. Every
# substring-trap test in tests/test_filters.py guards exactly that.
_TAIL = r"(?:s|es|ing|ed|uate|uates|uation|ship|ships|er|ers)?"


def _needle_pattern(needle: str) -> re.Pattern[str]:
    """Compile one keyword into a word-boundary pattern.

    ``(?<![a-z0-9]) ... (?![a-z0-9])`` is the boundary: it stops `intern` matching
    "internal", `coop` matching "cooperative", `swe` matching "sweden", and `active`
    matching "inactive". A plain ``\\b`` would not work here -- needles like `ts/sci` and
    `u.s. person` begin and end on punctuation, where ``\\b`` flips meaning.

    The optional trailing ``s?`` keeps plurals working ("Interns", "co-ops").

    Spaces and hyphens both become ``[\\s\\-]+``, which makes `full stack` equivalent to
    `full-stack`, tolerates a double space in "guidance,  navigation" -- and, the real
    payoff, lets a multi-word clearance needle survive HTML tag stripping, where
    ``an <b>active Secret</b> clearance`` normalizes to "an active secret clearance" with
    the tags replaced by spaces.
    """
    body = re.escape(needle.casefold())
    # re.escape backslashes both space and hyphen; undo that so the separator class can be
    # substituted in uniformly.
    body = body.replace("\\ ", " ").replace("\\-", "-")
    body = _SEPARATORS.sub(r"[\\s\\-]+", body)
    return re.compile(rf"(?<![a-z0-9]){body}{_TAIL}(?![a-z0-9])")


def _compile_all(needles: Iterable[str]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple((n, _needle_pattern(n)) for n in needles)


_INTERNSHIP_RE = _compile_all(INTERNSHIP_MARKERS)
_SOFTWARE_RE = _compile_all(SOFTWARE_MARKERS)
_TITLE_REJECT_RE = _compile_all(TITLE_REJECT)
_CLEARANCE_REJECT_RE = _compile_all(CLEARANCE_REJECT)
_CLEARANCE_ALLOW_RE = _compile_all(CLEARANCE_ALLOW)


@dataclass(frozen=True)
class Verdict:
    """Outcome of a filter check.

    `reason` is written into state/seen.json for rejected jobs so every rejection stays
    auditable -- after a month of running, the user can grep the rejected titles and tune
    the keyword lists with evidence instead of guesses.
    """

    passed: bool
    reason: str = ""
    matched: tuple[str, ...] = ()
    clearance_checked: bool = False


def normalize_title(title: str) -> str:
    """Fold a raw title into the form the compiled patterns expect."""
    if not title:
        return ""
    s = html.unescape(title)
    s = s.replace(" ", " ")
    s = unicodedata.normalize("NFKD", s)
    s = _DASHES.sub("-", s)
    s = _SMART_QUOTES.sub("'", s)
    s = _SMART_DQUOTES.sub('"', s)
    s = s.casefold()
    return _WS.sub(" ", s).strip()


def normalize_content(raw: str) -> str:
    """Fold a Greenhouse HTML description body into matchable plain text."""
    if not raw:
        return ""
    s = html.unescape(raw)
    # Some boards double-escape, so "&lt;p&gt;" only becomes "<p>" after a second pass.
    # Conditional, so a body that legitimately contains a lone "&" is not chewed further
    # than it needs to be.
    if "&lt;" in s or "&amp;" in s or "&#" in s:
        s = html.unescape(s)
    s = _SCRIPT_STYLE.sub(" ", s)
    s = _BLOCK_TAGS.sub("\n", s)
    # A SPACE, not "" -- otherwise "an <b>active Secret</b> clearance" collapses to
    # "activesecret" and the reject needle silently stops matching.
    s = _ANY_TAG.sub(" ", s)
    s = s.replace(" ", " ")
    s = unicodedata.normalize("NFKD", s)
    s = _DASHES.sub("-", s)
    s = _SMART_QUOTES.sub("'", s)
    s = _SMART_DQUOTES.sub('"', s)
    s = s.casefold()
    # Collapse last, so the doubled spaces left behind by stripped tags disappear.
    return _WS.sub(" ", s).strip()


def find_needle(haystack: str, compiled: Iterable[tuple[str, re.Pattern[str]]]) -> str | None:
    """Return the first needle that matches, or None. Reports *which* one, for logging."""
    for needle, pattern in compiled:
        if pattern.search(haystack):
            return needle
    return None


def check_title(title: str) -> Verdict:
    """Apply the three section 6 title lists.

    Rejects are checked first. That yields the same boolean as checking them last, but it
    makes the spec's "reject wins over any include match" precedence unmistakable in the
    code, and it gives the more useful reason for a title like "Marketing Intern" that
    fails on two counts.
    """
    t = normalize_title(title)
    if not t:
        return Verdict(False, "empty_title")

    hit = find_needle(t, _TITLE_REJECT_RE)
    if hit:
        return Verdict(False, f"title_reject:{hit}")

    internship = find_needle(t, _INTERNSHIP_RE)
    if not internship:
        return Verdict(False, "no_internship_marker")

    software = find_needle(t, _SOFTWARE_RE)
    if not software:
        return Verdict(False, "no_software_marker")

    return Verdict(True, matched=(internship, software))


def check_clearance(
    content: str,
    *,
    window_before: int = CLEARANCE_WINDOW_BEFORE,
    window_after: int = CLEARANCE_WINDOW_AFTER,
) -> Verdict:
    """Reject on clearance language the user genuinely cannot satisfy.

    Allow beats reject at the match site: each occurrence of a reject phrase is
    neutralized if an allow phrase sits in the surrounding window. Only an occurrence with
    no allow phrase near it rejects the job.

    An empty body is a pass with clearance_checked=False, because rejecting on missing
    content would drop real jobs -- a spurious push costs two seconds, a missed posting
    costs the internship.

    Which sources actually reach this check, measured 2026-09-14: Lever (64/64 matching
    roles), Ashby (8/8) and Pinpoint (3/3) all ship descriptions inline; Greenhouse supplies
    one lazily via greenhouse_fetch_content; Workday and BambooHR supply none. So roughly
    half the corpus is clearance-checked. An earlier version of this docstring claimed
    Lever and Ashby "supply no description at all", which was simply wrong.
    """
    s = normalize_content(content)
    if not s:
        return Verdict(True, clearance_checked=False)

    for needle, pattern in _CLEARANCE_REJECT_RE:
        for m in pattern.finditer(s):
            start = max(0, m.start() - window_before)
            window = s[start : m.end() + window_after]
            if find_needle(window, _CLEARANCE_ALLOW_RE) is None:
                return Verdict(False, f"clearance:{needle}", clearance_checked=True)

    return Verdict(True, clearance_checked=True)


def evaluate(job: dict) -> Verdict:
    """Title check, then the clearance check if the job carries a description body."""
    verdict = check_title(job.get("title", ""))
    if not verdict.passed:
        return verdict

    content = job.get("content", "")
    if not content:
        return verdict

    clearance = check_clearance(content)
    if not clearance.passed:
        return clearance
    return Verdict(True, matched=verdict.matched, clearance_checked=True)
