from __future__ import annotations

from csg import config


def test_asset_path_falls_back_to_package_data(tmp_path, monkeypatch):
    package_root = tmp_path / "csg"
    package_config = package_root / "config" / "icp.yaml"
    package_config.parent.mkdir(parents=True)
    package_config.write_text("profile: packaged\n", encoding="utf-8")

    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "source-tree-not-present")
    monkeypatch.setattr(config, "PACKAGE_ROOT", package_root)

    assert config.asset_path("config", "icp.yaml") == package_config


def test_installed_config_uses_writable_runtime_copy(tmp_path, monkeypatch):
    package_root = tmp_path / "csg"
    package_config = package_root / "config" / "icp.yaml"
    package_config.parent.mkdir(parents=True)
    package_config.write_text("profile: packaged\n", encoding="utf-8")
    runtime_root = tmp_path / "runtime"

    monkeypatch.setattr(config, "PROJECT_ROOT", tmp_path / "source-tree-not-present")
    monkeypatch.setattr(config, "PACKAGE_ROOT", package_root)
    monkeypatch.setattr(config, "RUNTIME_ROOT", runtime_root)
    monkeypatch.setattr(config, "DEFAULT_CONFIG", package_config)

    editable = config._editable_config_path()
    assert editable == runtime_root / "icp.yaml"
    assert editable.read_text(encoding="utf-8") == package_config.read_text(encoding="utf-8")