"""Tests for filters.py.

BUILD_SPEC.md section 10 calls filters "the piece most likely to be subtly wrong", so this
suite leans heavily on the substring traps that a naive `in` check would fail, and on the
clearance edge cases where the spec draws a fine distinction between language the user can
satisfy (ITAR, "ability to obtain") and language they cannot ("must possess an active
Secret").

Everything here is a pure function: no network, no filesystem, no clock.
"""

from __future__ import annotations

import pytest

from filters import (
    CLEARANCE_ALLOW,
    CLEARANCE_REJECT,
    INTERNSHIP_MARKERS,
    SOFTWARE_MARKERS,
    TITLE_REJECT,
    check_clearance,
    check_title,
    evaluate,
    normalize_content,
)

# --------------------------------------------------------------------------------------
# Titles that must pass both include gates
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer Intern - Summer 2027",
        "Flight Software Co-Op (Fall 2027)",
        "2027 Summer Intern, Embedded Systems",
        "Autonomy Software Engineering Intern",
        "New Grad Backend Engineer",
        "Student Researcher, Computer Vision",
        "Early Career Firmware Engineer",
        "Winter 2028 DevOps Co-op",
    ],
)
def test_title_passes(title):
    assert check_title(title).passed, title


# --------------------------------------------------------------------------------------
# Titles that fail because one of the two required markers is missing
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,reason",
    [
        ("Software Engineer II", "no_internship_marker"),
        ("Senior Backend Engineer", "no_internship_marker"),
        ("Business Operations Intern", "no_software_marker"),
        ("", "empty_title"),
    ],
)
def test_title_missing_marker(title, reason):
    v = check_title(title)
    assert not v.passed
    assert v.reason == reason


# --------------------------------------------------------------------------------------
# Reject precedence: a reject needle wins over any number of include matches
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title,needle",
    [
        # The headline case: reject beats TWO include matches (intern + software).
        ("GNC Software Engineer Intern", "gnc"),
        ("Guidance, Navigation and Control Intern", "guidance, navigation"),
        ("Flight Dynamics Software Intern", "flight dynamics"),
        ("PhD Research Intern, Machine Learning", "phd"),
        ("Mechanical Engineering Intern", "mechanical"),
        # Spec-mandated false negative, pinned deliberately: a payload software role is a
        # software role, but section 6 lists "payload" as a reject. Documented, not fixed.
        ("Payload Software Intern", "payload"),
    ],
)
def test_title_reject_wins(title, needle):
    v = check_title(title)
    assert not v.passed
    assert v.reason == f"title_reject:{needle}", v.reason


def test_marketing_intern_reports_reject_not_missing_software():
    """Fails on two counts; the reject reason is the more useful one to record."""
    assert check_title("Marketing Intern").reason == "title_reject:marketing"


# --------------------------------------------------------------------------------------
# Substring traps. A naive `in` check fails every one of these.
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineer, Internal Tools",      # "intern" in "internal"
        "International Software Engineer",        # "intern" in "international"
        "Cooperative Systems Engineer",           # "coop" in "cooperative"
        "Software Engineer, Sweden",              # "swe" in "sweden"
    ],
)
def test_substring_traps_must_not_match_internship_marker(title):
    v = check_title(title)
    assert not v.passed
    assert v.reason == "no_internship_marker", v.reason


@pytest.mark.parametrize(
    "title",
    [
        "Software Engineering Interns (multiple openings)",  # plural must still match
        "Infrastructure Software Engineering Intern",        # not "structures"
        "Infrastructure Co-ops, Summer 2027",                # plural of a hyphenated needle
        "SOFTWARE ENGINEER INTERN",                          # casefold
        "Full‑Stack Software Intern",                   # U+2011 non-breaking hyphen
        "Co‑op — Ground Software (8 month)",       # nb-hyphen + em dash
        "Software  Engineering   Intern",                    # collapsed whitespace
        "Software Engineer Intern &amp; Analyst",            # HTML entity in a title
    ],
)
def test_titles_that_must_still_pass(title):
    assert check_title(title).passed, title


def test_infrastructure_singular_does_not_trip_structures_reject():
    assert check_title("Infrastructure Software Intern").passed


# --------------------------------------------------------------------------------------
# Clearance: description-body language
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content,needle",
    [
        # Tag splitting INSIDE the phrase: tags must become spaces, not disappear.
        ("<p>Must possess an <b>active Secret</b> clearance.</p>", "active secret"),
        # Entity-escaped HTML must be unescaped before matching.
        ("&lt;p&gt;Requires an active security clearance&lt;/p&gt;", "active security clearance"),
        # Doubly-escaped, which some boards do.
        ("&amp;lt;p&amp;gt;Requires an active top secret&amp;lt;/p&amp;gt;", "active top secret"),
        ("TS/SCI required.", "ts/sci"),
        ("Candidates with an interim Secret are encouraged.", "interim secret"),
        ("Must currently&nbsp;hold a clearance", "currently hold a clearance"),
    ],
)
def test_clearance_rejects(content, needle):
    v = check_clearance(content)
    assert not v.passed
    assert v.reason == f"clearance:{needle}", v.reason
    assert v.clearance_checked


@pytest.mark.parametrize(
    "content",
    [
        # The spec's own named allow phrase.
        "Applicants must have the ability to obtain a clearance.",
        # The real trap: a reject phrase appears VERBATIM inside an allow context.
        "Must be able to obtain an active security clearance.",
        # Negation.
        "This position does not require an active security clearance.",
        # Preference rather than requirement.
        "An active Secret clearance is preferred but not required.",
        # Boundary: "in-active secret" must not match "active secret".
        "An inactive Secret clearance is acceptable.",
        # The four phrases section 6 explicitly says must not reject.
        "Must be a U.S. Person under ITAR / export control regulations.",
        "This role is subject to export control laws.",
    ],
)
def test_clearance_passes(content):
    v = check_clearance(content)
    assert v.passed, v.reason
    assert v.clearance_checked


def test_distant_allow_phrase_does_not_neutralize_a_later_reject():
    """An allow phrase far away in the document must not license a genuine requirement."""
    content = (
        "Must be able to obtain a clearance for this role, and we will sponsor that "
        "process for the right candidate during onboarding. Separately, applicants who "
        "already possess an active TS/SCI are prioritized."
    )
    assert content.index("active TS/SCI") - content.index("able to obtain") > 90
    v = check_clearance(content)
    assert not v.passed
    assert v.reason == "clearance:ts/sci"


def test_empty_content_passes_unchecked():
    """Lever and Ashby supply no body. Never reject on missing content."""
    v = check_clearance("")
    assert v.passed
    assert not v.clearance_checked


def test_tags_become_spaces_not_nothing():
    assert "active secret" in normalize_content("an <b>active Secret</b> clearance")


# --------------------------------------------------------------------------------------
# evaluate(): the two checks wired together
# --------------------------------------------------------------------------------------


def test_evaluate_passes_title_and_clearance():
    v = evaluate({"title": "Software Engineer Intern", "content": "<p>ITAR applies.</p>"})
    assert v.passed
    assert v.clearance_checked


def test_evaluate_rejects_on_clearance_despite_good_title():
    v = evaluate({
        "title": "Software Engineer Intern",
        "content": "<p>Must possess an active Secret clearance.</p>",
    })
    assert not v.passed
    assert v.reason == "clearance:active secret"


def test_evaluate_skips_clearance_when_no_content():
    v = evaluate({"title": "Software Engineer Intern", "content": ""})
    assert v.passed
    assert not v.clearance_checked


def test_evaluate_short_circuits_on_title_reject():
    """A bad title must not even reach the clearance check."""
    v = evaluate({"title": "Propulsion Intern", "content": "ITAR applies."})
    assert not v.passed
    assert v.reason == "title_reject:propulsion"


# --------------------------------------------------------------------------------------
# List hygiene. Cheap regression net against a typo in a section 6 constant.
# --------------------------------------------------------------------------------------

ALL_LISTS = {
    "INTERNSHIP_MARKERS": INTERNSHIP_MARKERS,
    "SOFTWARE_MARKERS": SOFTWARE_MARKERS,
    "TITLE_REJECT": TITLE_REJECT,
    "CLEARANCE_REJECT": CLEARANCE_REJECT,
    "CLEARANCE_ALLOW": CLEARANCE_ALLOW,
}


@pytest.mark.parametrize("name,needles", ALL_LISTS.items())
def test_lists_are_immutable_tuples(name, needles):
    assert isinstance(needles, tuple), name


@pytest.mark.parametrize("name,needles", ALL_LISTS.items())
def test_needles_are_lowercase_and_stripped(name, needles):
    for n in needles:
        assert n, f"{name} has an empty needle"
        assert n == n.strip().casefold(), f"{name}: {n!r} is not stripped/lowercase"
    assert len(set(needles)) == len(needles), f"{name} has duplicates"


@pytest.mark.parametrize("needle", TITLE_REJECT)
def test_every_reject_needle_rejects_itself(needle):
    v = check_title(needle)
    assert not v.passed
    assert v.reason == f"title_reject:{needle}"


@pytest.mark.parametrize("internship", INTERNSHIP_MARKERS)
@pytest.mark.parametrize("software", SOFTWARE_MARKERS)
def test_every_marker_pair_passes(internship, software):
    """Every include combination must pass, proving no marker is unreachable."""
    assert check_title(f"{internship} {software}").passed


@pytest.mark.parametrize("needle", CLEARANCE_REJECT)
def test_every_clearance_needle_rejects_itself(needle):
    v = check_clearance(f"This role requires {needle} status.")
    assert not v.passed
    assert v.reason == f"clearance:{needle}"


@pytest.mark.parametrize("phrase", CLEARANCE_ALLOW)
def test_every_allow_phrase_alone_passes(phrase):
    assert check_clearance(f"Note: {phrase}.").passed


@pytest.mark.parametrize("title", [
    # NVIDIA phrasing, found live 2026-09-18. `new grad` cannot match these because it
    # requires the two words to be adjacent.
    "AI Compiler Engineer- New College Grad 2027",
    "Software R&D Engineer, VLSI Physical Design - New College Grad 2027",
    "NVIDIA 2027 New College Graduate: Software Engineering",
])
def test_new_college_grad_phrasing_is_caught(title):
    assert check_title(title).passed, title


@pytest.mark.parametrize("title", [
    "Senior Robotics Software Engineer, Sentry Tower",   # "Sentry" contains "entry"
    "Campus Infrastructure Project Manager",             # facilities, not early-career
])
def test_early_career_lookalikes_still_rejected(title):
    """Guards the widened vocabulary against the substring trap it could reintroduce."""
    assert not check_title(title).passed, title
