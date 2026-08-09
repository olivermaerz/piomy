from __future__ import annotations

from pathlib import Path

from piomy.config import load_config, save_config, validate, AppConfig


def test_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    cfg = AppConfig()
    cfg.storage.archive_dir = str(tmp_path / "arch")
    cfg.capture.ev = 1.5
    cfg.sync.enabled = True
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.storage.archive_dir == cfg.storage.archive_dir
    assert loaded.capture.ev == 1.5
    assert loaded.sync.enabled is True


def test_validate_rejects_bad_interval() -> None:
    cfg = AppConfig()
    cfg.capture.interval_seconds = 0.01
    try:
        validate(cfg)
        assert False, "expected ValueError"
    except ValueError:
        pass
