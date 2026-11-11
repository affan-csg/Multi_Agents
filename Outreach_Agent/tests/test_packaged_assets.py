from __future__ import annotations

from outreach import config


def test_asset_path_falls_back_to_package_data(tmp_path, monkeypatch):
    package_root = tmp_path / "outreach"
    package_config = package_root / "config" / "outreach.yaml"
    package_config.parent.mkdir(parents=True)
    package_config.write_text("profile: packaged\n", encoding="utf-8")

    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "source-tree-not-present")
    monkeypatch.setattr(config, "PACKAGE_ROOT", package_root)

    assert config.asset_path("config", "outreach.yaml") == package_config