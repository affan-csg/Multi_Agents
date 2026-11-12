from __future__ import annotations

from pathlib import Path

import pytest

from csg import config, http


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


def test_runtime_directory_is_created_and_is_writable(tmp_path):
    runtime_root = tmp_path / "runtime" / "nested"

    ensured = config.ensure_runtime_dir(runtime_root)

    assert ensured == runtime_root
    assert runtime_root.is_dir()
    assert runtime_root.exists()
    assert runtime_root.stat().st_mode & 0o777


def test_runtime_directory_permission_errors_are_not_silenced(tmp_path, monkeypatch):
    runtime_root = tmp_path / "runtime"

    def boom(self, *args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "mkdir", boom)

    with pytest.raises(PermissionError, match="not writable"):
        config.ensure_runtime_dir(runtime_root)


def test_fetcher_cache_writes_surface_permission_failures(tmp_path, monkeypatch):
    cache_dir = tmp_path / "cache"
    monkeypatch.setattr(http, "CACHE_DIR", cache_dir)

    def boom(self, *args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(Path, "write_text", boom)

    fetcher = http.Fetcher("unit-test", use_cache=True)
    with pytest.raises(PermissionError, match="permission denied"):
        fetcher._write_cache("abc123", "https://example.com", 200, "ok")