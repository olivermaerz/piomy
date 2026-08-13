from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path

from PIL import Image

from piomy.config import AppConfig, StorageConfig
from piomy.storage import (
    ThumbWorker,
    archive_ready,
    cpu_temp_c,
    enforce_min_free,
    measured_fps,
    save_jpeg_bytes,
    thumb_path_for,
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


def test_thumb_worker_async(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    latest = archive / "latest.jpg"
    thumbs = archive / ".thumbs"
    dest = save_jpeg_bytes(archive, _jpeg(), latest, thumbs_dir=None)
    assert not thumb_path_for(dest, thumbs, archive).is_file()

    worker = ThumbWorker(maxsize=2)
    try:
        assert worker.enqueue(dest, thumbs, archive) is True
        deadline = time.time() + 10
        out = thumb_path_for(dest, thumbs, archive)
        while time.time() < deadline and not out.is_file():
            time.sleep(0.05)
        assert out.is_file()
    finally:
        worker.close(timeout=10)


def test_min_free_deletes_oldest_day(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    cfg = AppConfig(
        storage=StorageConfig(
            archive_dir=str(archive),
            min_free_gb=1000,
            delete_grace_minutes=60,
        )
    )

    old_day = archive / "2026" / "08" / "01"
    new_day = archive / "2026" / "08" / "02"
    old_thumbs = archive / ".thumbs" / "2026" / "08" / "01"
    old_day.mkdir(parents=True)
    new_day.mkdir(parents=True)
    old_thumbs.mkdir(parents=True)
    old = old_day / "120000_000001.jpg"
    thumb = old_thumbs / "120000_000001.jpg"
    recent = new_day / "120100_000001.jpg"
    old.write_bytes(_jpeg((1, 0, 0)))
    thumb.write_bytes(_jpeg((1, 0, 0)))
    recent.write_bytes(_jpeg((0, 1, 0)))

    now = time.time()
    os.utime(old, (now - 7200, now - 7200))
    os.utime(old_day, (now - 7200, now - 7200))
    os.utime(recent, (now - 10, now - 10))
    os.utime(new_day, (now - 10, now - 10))

    monkeypatch.setattr("piomy.storage.free_bytes", lambda p: 0)
    deleted = enforce_min_free(cfg)
    assert deleted == 1
    assert not old_day.exists()
    assert not old_thumbs.exists()
    assert recent.exists()


def test_min_free_skips_day_within_grace(tmp_path: Path, monkeypatch) -> None:
    archive = tmp_path / "archive"
    archive.mkdir()
    cfg = AppConfig(
        storage=StorageConfig(
            archive_dir=str(archive),
            min_free_gb=1000,
            delete_grace_minutes=30,
        )
    )

    now = datetime.now().astimezone().replace(hour=0, minute=10, second=0, microsecond=0)
    yesterday = (now - timedelta(days=1)).strftime("%Y/%m/%d")
    today = now.strftime("%Y/%m/%d")
    y_dir = archive.joinpath(*yesterday.split("/"))
    t_dir = archive.joinpath(*today.split("/"))
    y_dir.mkdir(parents=True)
    t_dir.mkdir(parents=True)
    (y_dir / "120000_000001.jpg").write_bytes(_jpeg((1, 0, 0)))
    (t_dir / "000500_000001.jpg").write_bytes(_jpeg((0, 1, 0)))

    monkeypatch.setattr("piomy.storage.free_bytes", lambda p: 0)
    deleted = enforce_min_free(cfg, now=now)
    assert deleted == 0
    assert y_dir.exists()
    assert t_dir.exists()


def test_min_free_deletes_oldest_calendar_day_not_recently_touched(
    tmp_path: Path, monkeypatch
) -> None:
    """A fresh mtime on the oldest day must not protect it (that used to skip it
    and delete the next day instead)."""
    archive = tmp_path / "archive"
    archive.mkdir()
    cfg = AppConfig(
        storage=StorageConfig(
            archive_dir=str(archive),
            min_free_gb=1000,
            delete_grace_minutes=30,
        )
    )

    now = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    d9 = (now - timedelta(days=4)).strftime("%Y/%m/%d")
    d10 = (now - timedelta(days=3)).strftime("%Y/%m/%d")
    today = now.strftime("%Y/%m/%d")
    oldest = archive.joinpath(*d9.split("/"))
    middle = archive.joinpath(*d10.split("/"))
    newest = archive.joinpath(*today.split("/"))
    for folder in (oldest, middle, newest):
        folder.mkdir(parents=True)
        (folder / "120000_000001.jpg").write_bytes(_jpeg((1, 0, 0)))

    # Oldest folder looks "recently written" (file manager, leftover .tmp, …)
    t = time.time()
    os.utime(oldest, (t, t))
    os.utime(middle, (t - 86400 * 3, t - 86400 * 3))

    monkeypatch.setattr(
        "piomy.storage.free_bytes",
        lambda p: 0 if oldest.exists() else 10 * (1024**4),
    )
    deleted = enforce_min_free(cfg, now=now)
    assert deleted == 1
    assert not oldest.exists()
    assert middle.exists()
    assert newest.exists()


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
