"""Tests for workday.LINK_RE: extracting tenant/shard/site from a Workday
career-site link found in page HTML."""

from __future__ import annotations

from csg.ats import workday


def test_link_re_bare_site():
    m = workday.LINK_RE.search("https://mastercard.wd1.myworkdayjobs.com/CorporateCareers")
    assert m is not None
    assert (m.group(1), m.group(2), m.group(3)) == ("mastercard", "wd1", "CorporateCareers")


def test_link_re_locale_prefixed_site():
    m = workday.LINK_RE.search("https://acme.wd1.myworkdayjobs.com/en-US/CorporateCareers")
    assert m is not None
    assert (m.group(1), m.group(2), m.group(3)) == ("acme", "wd1", "CorporateCareers")


def test_link_re_individual_job_posting_url():
    # Regression: a full job-posting URL (as opposed to a bare "browse
    # careers" link) has a /job/... path after the site -- the site must
    # still resolve to the real site slug, not to the literal "job" segment.
    url = (
        "https://datarobot.wd1.myworkdayjobs.com/DataRobot_External_Careers/"
        "job/Boston-Massachusetts-US/Staff-Software-Engineer_R-102685/apply"
    )
    m = workday.LINK_RE.search(url)
    assert m is not None
    assert (m.group(1), m.group(2), m.group(3)) == ("datarobot", "wd1", "DataRobot_External_Careers")


def test_encode_decode_round_trip():
    token = workday.encode_token("datarobot", "wd1", "DataRobot_External_Careers")
    assert token == "datarobot/wd1/DataRobot_External_Careers"
    assert workday.decode_token(token) == ("datarobot", "wd1", "DataRobot_External_Careers")
