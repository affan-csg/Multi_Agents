"""Region selection is the only per-run input -- everything else is embedded."""

from __future__ import annotations

import pytest

from csg.config import load_config
from csg.discovery import build_discovery_prompts


@pytest.fixture(scope="module")
def cfg():
    return load_config()


def test_active_region_comes_from_config(cfg):
    """A run with no region argument still has a target."""
    assert cfg.active_region
    assert cfg.resolve_region(None) == cfg.active_region


def test_region_lookup_is_case_insensitive(cfg):
    assert cfg.resolve_region("ga") == "GA"
    assert cfg.resolve_region("EAST COAST") == "East Coast"
    assert cfg.resolve_region("  west coast  ") == "West Coast"


def test_every_configured_region_has_states_and_hubs(cfg):
    assert cfg.regions, "no regions configured"
    for name in cfg.regions:
        assert cfg.region_states(name), f"{name} has no states"
        assert cfg.region_hubs(name), f"{name} has no hubs"


def test_unknown_region_is_passed_through_not_rejected(cfg):
    """Ad-hoc regions still work; they just carry no configured state list."""
    assert cfg.resolve_region("Texas") == "Texas"
    assert cfg.region_states("Texas") == set()


def test_prompts_are_generated_from_the_icp(cfg):
    """Regression: size band and practice areas used to be hardcoded English
    inside web_search.py, so the ICP config was not the source of truth."""
    prompts = build_discovery_prompts(cfg, "GA", 40)
    assert prompts
    joined = " ".join(prompts)

    # size band from cfg.size
    assert str(int(cfg.size["sweet_min"])) in joined
    assert str(int(cfg.size["sweet_max"])) in joined
    # region hubs from cfg.geo
    assert "Atlanta" in joined
    # revenue threshold from cfg.financials
    assert "$10M+" in joined
    # competitors excluded
    assert "staffing" in joined.lower()


def test_prompts_follow_a_config_change(cfg):
    """Editing the ICP must change what gets searched, with no code edit."""
    before = " ".join(build_discovery_prompts(cfg, "GA", 40))
    original = cfg.raw["size"]["sweet_max"]
    try:
        cfg.raw["size"]["sweet_max"] = 9999
        after = " ".join(build_discovery_prompts(cfg, "GA", 40))
    finally:
        cfg.raw["size"]["sweet_max"] = original
    assert "9999" in after and "9999" not in before


def test_prompts_differ_by_region(cfg):
    ga = " ".join(build_discovery_prompts(cfg, "GA", 20))
    mw = " ".join(build_discovery_prompts(cfg, "Midwest", 20))
    assert ga != mw
    assert "Chicago" in mw and "Chicago" not in ga
