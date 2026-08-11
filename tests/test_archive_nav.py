from __future__ import annotations

from pathlib import Path

from piomy.storage import (
    PAGE_SIZE,
    block_counts,
    block_from_rel,
    block_href,
    hour_counts,
    latest_image_rel,
    latest_images_href,
    list_images_for_block,
    neighbor_block,
    neighbor_rel,
    paginate,
    parse_image_name,
)


def _touch_jpeg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # minimal bytes; listing only cares about names
    path.write_bytes(b"\xff\xd8\xff\xd9")


def test_parse_image_name() -> None:
    assert parse_image_name("153042_000123.jpg") == (15, 30, 42, 123)
    assert parse_image_name("latest.jpg") is None


def test_hour_and_block_counts(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    day = "2026-08-09"
    base = archive / "2026" / "08" / "09"
    _touch_jpeg(base / "150001_000001.jpg")
    _touch_jpeg(base / "150959_000001.jpg")
    _touch_jpeg(base / "151000_000001.jpg")
    _touch_jpeg(base / "162000_000001.jpg")

    assert hour_counts(archive, day) == [(15, 3), (16, 1)]
    assert block_counts(archive, day, 15) == [(0, 2), (10, 1)]
    block = list_images_for_block(archive, day, 15, 0)
    assert [p.name for p in block] == ["150001_000001.jpg", "150959_000001.jpg"]


def test_neighbor_crosses_days(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _touch_jpeg(archive / "2026" / "08" / "09" / "235959_000001.jpg")
    _touch_jpeg(archive / "2026" / "08" / "10" / "000001_000001.jpg")
    _touch_jpeg(archive / "2026" / "08" / "10" / "000002_000001.jpg")

    first = "2026/08/09/235959_000001.jpg"
    second = "2026/08/10/000001_000001.jpg"
    third = "2026/08/10/000002_000001.jpg"

    assert neighbor_rel(archive, first, newer=True) == second
    assert neighbor_rel(archive, second, newer=False) == first
    assert neighbor_rel(archive, second, newer=True) == third
    assert neighbor_rel(archive, first, newer=False) is None
    assert neighbor_rel(archive, third, newer=True) is None

    assert block_from_rel(first) == ("2026-08-09", 23, 50)
    assert block_href("2026-08-09", 23, 50) == "/archive/2026-08-09/23/50"
    assert latest_image_rel(archive) == third


def test_paginate() -> None:
    items = list(range(65))
    page1, p, total = paginate(items, 1, 60)
    assert len(page1) == 60 and p == 1 and total == 2
    page2, p, total = paginate(items, 2, 60)
    assert page2 == list(range(60, 65)) and p == 2


def test_latest_images_href_last_page(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    base = archive / "2026" / "08" / "10"
    for i in range(PAGE_SIZE + 3):
        # 16:20 block — names must stay in that minute range
        minute = 20 + (i // 60)
        second = i % 60
        _touch_jpeg(base / f"16{minute:02d}{second:02d}_{i:06d}.jpg")

    href = latest_images_href(archive, page_size=PAGE_SIZE)
    assert href == "/archive/2026-08-10/16/20?page=2"


def test_neighbor_block_crosses_hours_and_days(tmp_path: Path) -> None:
    archive = tmp_path / "archive"
    _touch_jpeg(archive / "2026" / "08" / "09" / "235500_000001.jpg")
    _touch_jpeg(archive / "2026" / "08" / "10" / "000100_000001.jpg")
    _touch_jpeg(archive / "2026" / "08" / "10" / "001500_000001.jpg")

    assert neighbor_block(archive, "2026-08-09", 23, 50, newer=True) == (
        "2026-08-10",
        0,
        0,
    )
    assert neighbor_block(archive, "2026-08-10", 0, 0, newer=False) == (
        "2026-08-09",
        23,
        50,
    )
    assert neighbor_block(archive, "2026-08-10", 0, 0, newer=True) == (
        "2026-08-10",
        0,
        10,
    )
    assert neighbor_block(archive, "2026-08-10", 0, 10, newer=True) is None
    assert neighbor_block(archive, "2026-08-09", 23, 50, newer=False) is None
