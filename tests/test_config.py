from __future__ import annotations

from pathlib import Path

from piomy.config import DEFAULT_ACCENT_COLOR, load_config, save_config, validate, AppConfig


def test_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    cfg = AppConfig()
    cfg.storage.archive_dir = str(tmp_path / "arch")
    cfg.capture.ev = 1.5
    cfg.sync.enabled = True
    cfg.web.accent_color = "#4488ff"
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.storage.archive_dir == cfg.storage.archive_dir
    assert loaded.capture.ev == 1.5
    assert loaded.sync.enabled is True
    assert loaded.web.accent_color == "#4488ff"


def test_validate_rejects_bad_interval() -> None:
    cfg = AppConfig()
    cfg.capture.interval_seconds = 0.01
    try:
        validate(cfg)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_accent_default_and_validation() -> None:
    cfg = AppConfig()
    assert cfg.web.accent_color == DEFAULT_ACCENT_COLOR
    assert cfg.web.workers == 1
    validate(cfg)
    cfg.web.accent_color = "green"
    try:
        validate(cfg)
        assert False, "expected ValueError"
    except ValueError:
        pass
    cfg.web.accent_color = "#AABBCC"
    validate(cfg)
    assert cfg.web.accent_color == "#aabbcc"
    cfg.web.workers = 0
    try:
        validate(cfg)
        assert False, "expected ValueError"
    except ValueError:
        pass
    cfg.web.workers = 2
    validate(cfg)


def test_preview_enabled_roundtrip_ignores_legacy_resolution(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        "storage:\n  archive_dir: /tmp/piomy-arch\n"
        "preview:\n  enabled: false\n  resolution: [640, 480]\n"
    )
    loaded = load_config(path)
    assert loaded.preview.enabled is False
    assert not hasattr(loaded.preview, "resolution")
    save_config(loaded, path)
    again = load_config(path)
    assert again.preview.enabled is False
