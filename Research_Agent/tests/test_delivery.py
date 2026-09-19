"""Delivery-geography classification -- deterministic, no network, no LLM."""

from __future__ import annotations

from csg.delivery import classify


def test_security_clearance_forces_onshore():
    assert classify("Data Engineer", "Active TS/SCI security clearance required.") == "US"
    assert classify("Backend Engineer", "Must be a US citizen.") == "US"


def test_clearance_beats_an_explicit_offshore_marker():
    """A legal constraint outranks a stated preference."""
    assert classify("QA Engineer",
                    "Fully remote, offshore friendly. Security clearance required.") == "US"


def test_leadership_and_client_facing_stay_onshore():
    assert classify("VP of Engineering") == "US"
    assert classify("Director of Data Platform") == "US"
    assert classify("Customer Success Manager") == "US"
    assert classify("Compliance Analyst") == "US"


def test_explicit_regions_are_honoured():
    assert classify("Software Engineer", "Hiring across LATAM.") == "LATAM"
    assert classify("Software Engineer", "Our Karachi office is growing.") == "Pakistan"
    assert classify("Software Engineer", "Nearshore team, US timezone overlap.") == "LATAM"


def test_async_execution_work_offshores():
    assert classify("QA Automation Engineer") == "Pakistan"
    assert classify("Tier 2 Support Engineer") == "Pakistan"
    assert classify("Data Annotation Specialist") == "Pakistan"


def test_core_engineering_defaults_to_nearshore():
    assert classify("Senior Software Engineer") == "LATAM"
    assert classify("Full Stack Developer") == "LATAM"
    assert classify("Product Manager") == "LATAM"


def test_us_substring_does_not_false_match():
    """Regression: the old classifier tested `"us" in location`, so any city
    containing those letters was labelled US onshore."""
    # "Austin" and "Houston" both contain "us".
    assert classify("Software Engineer", "", "Austin, TX") == "LATAM"
    assert classify("QA Engineer", "", "Houston, TX") == "Pakistan"
    # "customer" contains "us" too.
    assert classify("Software Engineer", "Build customer dashboards.") == "LATAM"


def test_unclassifiable_returns_none():
    """None means 'no signal', which the scorer excludes rather than guessing."""
    assert classify("Warehouse Associate") is None
    assert classify("") is None
