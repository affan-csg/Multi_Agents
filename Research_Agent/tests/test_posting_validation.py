from datetime import datetime, timedelta, timezone

import pytest

from csg.models import JobPosting
from csg.posting_validation import MAX_CURRENT_POSTING_AGE_DAYS, is_current_validated_posting


NOW = datetime(2026, 11, 6, tzinfo=timezone.utc)


def posting(*, age_days: int | None = 1, raw: dict | None = None) -> JobPosting:
    published = NOW - timedelta(days=age_days) if age_days is not None else None
    return JobPosting(
        domain="example.com", external_id="job-1", title="Data Engineer",
        posted_at=published,
        raw={"_csg_validation": {
            "ownership": "verified", "current": "verified", "active_source": "live_ats_board",
        }, **(raw or {})},
    )


def test_live_posting_is_current_through_the_stale_window():
    assert is_current_validated_posting(posting(age_days=MAX_CURRENT_POSTING_AGE_DAYS), NOW)
    assert not is_current_validated_posting(posting(age_days=MAX_CURRENT_POSTING_AGE_DAYS + 1), NOW)


@pytest.mark.parametrize("status", ["closed", "expired", "filled", "archived"])
def test_closed_vendor_status_excludes_posting(status: str):
    assert not is_current_validated_posting(posting(raw={"status": status}), NOW)


def test_expired_posting_and_non_live_url_are_not_current():
    assert not is_current_validated_posting(posting(raw={"validThrough": "2026-11-05T00:00:00Z"}), NOW)
    assert not is_current_validated_posting(posting(raw={
        "_csg_validation": {"ownership": "verified", "current": "verified"},
    }), NOW)


def test_undated_posting_requires_a_live_listing_verification():
    assert is_current_validated_posting(posting(age_days=None), NOW)
    unverified = posting(age_days=None)
    unverified.raw["_csg_validation"].pop("active_source")
    assert not is_current_validated_posting(unverified, NOW)