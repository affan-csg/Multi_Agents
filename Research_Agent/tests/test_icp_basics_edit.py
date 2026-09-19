"""Tests for config.update_icp_basics -- the safe, targeted in-place edits
the Settings page's Built-in ICP card makes to icp.yaml (via
api.update_settings_icp / PATCH /settings/icp). Only two fields are
editable: Employee Size Band (size.sweet_min/sweet_max) and Revenue Target
(financials.revenue_sweet_min) -- everything else the card displays (region,
tiers, weights, practice areas, disqualifiers) is read-only.

Runs against a real copy of icp.yaml written to tmp_path, never the repo's
own config/icp.yaml -- these tests write to disk and must not leave that
file mutated.
"""

from __future__ import annotations

import pytest

from csg.config import DEFAULT_CONFIG, update_icp_basics


@pytest.fixture
def icp_path(tmp_path):
    dest = tmp_path / "icp.yaml"
    dest.write_text(DEFAULT_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    return dest


# --- happy path ---------------------------------------------------------------

def test_updates_revenue_sweet_min_in_place(icp_path):
    cfg = update_icp_basics({"revenue_sweet_min": 15_000_000}, path=icp_path)
    assert cfg.raw["financials"]["revenue_sweet_min"] == 15_000_000


def test_updates_sweet_size_band(icp_path):
    cfg = update_icp_basics({"sweet_min": 50, "sweet_max": 5000}, path=icp_path)
    assert cfg.size["sweet_min"] == 50
    assert cfg.size["sweet_max"] == 5000


def test_only_touched_lines_change_everything_else_is_byte_identical(icp_path):
    before = icp_path.read_text(encoding="utf-8")
    update_icp_basics({"revenue_sweet_min": 20_000_000}, path=icp_path)
    after = icp_path.read_text(encoding="utf-8")

    before_lines = before.splitlines()
    after_lines = after.splitlines()
    assert len(before_lines) == len(after_lines)
    changed = [i for i, (b, a) in enumerate(zip(before_lines, after_lines)) if b != a]
    assert changed == [i for i, line in enumerate(before_lines) if "revenue_sweet_min:" in line]


def test_comment_on_the_edited_line_is_preserved(icp_path):
    update_icp_basics({"revenue_sweet_min": 20_000_000}, path=icp_path)
    text = icp_path.read_text(encoding="utf-8")
    assert "revenue_sweet_min: 20000000  # prefer $10M+ -- full revenue credit at/above this" in text


def test_multiple_fields_in_one_call(icp_path):
    cfg = update_icp_basics(
        {"revenue_sweet_min": 12_000_000, "sweet_min": 40, "sweet_max": 4000}, path=icp_path,
    )
    assert cfg.raw["financials"]["revenue_sweet_min"] == 12_000_000
    assert cfg.size["sweet_min"] == 40
    assert cfg.size["sweet_max"] == 4000


# --- validation: rejected before any write ------------------------------------

def test_rejects_unknown_field(icp_path):
    before = icp_path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown or non-editable"):
        update_icp_basics({"weights": {}}, path=icp_path)
    assert icp_path.read_text(encoding="utf-8") == before


def test_rejects_region_as_non_editable(icp_path):
    """Region moved out of scope -- only the size band and revenue target
    are editable from the Settings card now."""
    before = icp_path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown or non-editable"):
        update_icp_basics({"active_region": "GA"}, path=icp_path)
    assert icp_path.read_text(encoding="utf-8") == before


def test_rejects_tier_threshold_as_non_editable(icp_path):
    before = icp_path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="Unknown or non-editable"):
        update_icp_basics({"tier_a_threshold": 80}, path=icp_path)
    assert icp_path.read_text(encoding="utf-8") == before


def test_rejects_sweet_min_greater_than_sweet_max(icp_path):
    before = icp_path.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="cannot be greater than"):
        update_icp_basics({"sweet_min": 9000, "sweet_max": 100}, path=icp_path)
    assert icp_path.read_text(encoding="utf-8") == before


def test_rejects_non_numeric_size_value(icp_path):
    with pytest.raises(ValueError, match="must be a whole number"):
        update_icp_basics({"sweet_min": "lots"}, path=icp_path)


def test_rejects_non_positive_revenue_threshold(icp_path):
    with pytest.raises(ValueError, match="must be a positive number"):
        update_icp_basics({"revenue_sweet_min": 0}, path=icp_path)


def test_rejects_non_positive_sweet_min(icp_path):
    with pytest.raises(ValueError, match="must be a positive number"):
        update_icp_basics({"sweet_min": -5}, path=icp_path)


def test_no_temp_file_left_behind_after_a_rejected_edit(icp_path):
    with pytest.raises(ValueError):
        update_icp_basics({"sweet_min": 9999, "sweet_max": 1}, path=icp_path)
    leftovers = list(icp_path.parent.glob("*.tmp"))
    assert leftovers == []


# --- cross-field validation uses the file's CURRENT values for untouched fields --

def test_sweet_range_check_uses_current_file_value_for_untouched_field(icp_path):
    """Only sweet_min is provided -- sweet_max isn't in this call, so the
    range check must compare against the file's existing sweet_max (3000),
    not treat it as absent."""
    with pytest.raises(ValueError, match="cannot be greater than"):
        update_icp_basics({"sweet_min": 5000}, path=icp_path)  # > current sweet_max (3000)
