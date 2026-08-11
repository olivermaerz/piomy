from __future__ import annotations

import os
import time
from io import BytesIO
from pathlib import Path

from PIL import Image

from piomy.config import AppConfig, StorageConfig
from piomy.storage import (
    archive_ready,
    cpu_temp_c,
    enforce_min_free,
    measured_fps,
    save_jpeg_bytes,
)


def _jpeg(color: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (64, 48), color=color).save(buf, format="JPEG")
    return buf.getvalue()


def test_atomic_save_and_latest(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    latest = archive / "latest.jpg"
    thumbs = archive / ".thumbs"
    data = _jpeg()

    dest = save_jpeg_bytes(archive, data, latest, thumbs_dir=thumbs)
    assert dest.is_file()
    assert latest.is_file()
    assert latest.read_bytes() == data
    ok, reason = archive_ready(archive)
    assert ok, reason
    assert any(thumbs.rglob("*.jpg"))


def test_min_free_respects_grace(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    cfg = AppConfig(
        storage=StorageConfig(
            archive_dir=str(archive),
            min_free_gb=1000,
            delete_grace_minutes=60,
        )
    )

    day = archive / "2026" / "08" / "01"
    day.mkdir(parents=True)
    old = day / "120000_000001.jpg"
    recent = day / "120100_000001.jpg"
    old.write_bytes(_jpeg((1, 0, 0)))
    recent.write_bytes(_jpeg((0, 1, 0)))

    now = time.time()
    os.utime(old, (now - 7200, now - 7200))
    os.utime(recent, (now - 10, now - 10))

    monkeypatch.setattr("piomy.storage.free_bytes", lambda p: 0)
    deleted = enforce_min_free(cfg)
    assert deleted >= 1
    assert not old.exists()
    assert recent.exists()


def test_cpu_temp_c(tmp_path: Path) -> None:
    path = tmp_path / "temp"
    path.write_text("48234\n", encoding="utf-8")
    assert cpu_temp_c(path) == 48.2
    assert cpu_temp_c(tmp_path / "missing") is None


def test_measured_fps() -> None:
    assert measured_fps([]) is None
    assert measured_fps([1.0]) is None
    assert measured_fps([1.0, 1.0]) is None
    assert measured_fps([0.0, 1.0, 2.0]) == 1.0
    assert measured_fps([0.0, 0.5, 1.0]) == 2.0
