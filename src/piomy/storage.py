"""Atomic JPEG writes, archive paths, min-free retention, status."""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from queue import Full

from PIL import Image

from piomy.config import AppConfig

log = logging.getLogger(__name__)

JPEG_SUFFIX = ".jpg"
NAME_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})_(\d{6})\.jpg$")
BLOCK_MINUTES = 10
PAGE_SIZE = 60
RETENTION_CHECK_EVERY = 100


def archive_ready(archive_dir: Path) -> tuple[bool, str]:
    """Return (ok, reason). Refuse to use a missing/unmounted path."""
    try:
        if not archive_dir.exists():
            return False, f"archive_dir does not exist: {archive_dir}"
        if not archive_dir.is_dir():
            return False, f"archive_dir is not a directory: {archive_dir}"
        # Writable check
        probe = archive_dir / ".piomy_write_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
    except OSError as exc:
        return False, f"archive_dir not writable: {exc}"
    return True, "ok"


def free_bytes(path: Path) -> int:
    usage = shutil.disk_usage(path)
    return usage.free


def free_gb(path: Path) -> float:
    return free_bytes(path) / (1024**3)


def day_dir(archive_dir: Path, when: datetime | None = None) -> Path:
    when = when or datetime.now().astimezone()
    return archive_dir / f"{when:%Y}" / f"{when:%m}" / f"{when:%d}"


def archive_filename(when: datetime | None = None) -> str:
    when = when or datetime.now().astimezone()
    # Include microseconds to avoid collisions at high rate
    return f"{when:%H%M%S}_{when.microsecond:06d}{JPEG_SUFFIX}"


def atomic_write_bytes(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    with tmp.open("wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(dest)


def save_jpeg_bytes(
    archive_dir: Path,
    data: bytes,
    latest: Path,
    thumbs_dir: Path | None = None,
    thumb_max: int = 320,
) -> Path:
    """Write archive JPEG, update latest.jpg, optionally write thumbnail synchronously."""
    when = datetime.now().astimezone()
    dest = day_dir(archive_dir, when) / archive_filename(when)
    atomic_write_bytes(dest, data)

    # latest.jpg via rename (copy; hardlink may cross filesystems)
    atomic_write_bytes(latest, data)

    if thumbs_dir is not None:
        try:
            write_thumb(dest, thumbs_dir, archive_dir, thumb_max=thumb_max)
        except Exception:
            log.exception("Thumbnail generation failed for %s", dest)

    return dest


def thumb_worker_main(queue: Any) -> None:
    """Process entrypoint: consume (image, thumbs_dir, archive_dir, thumb_max) jobs."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [thumb] %(message)s",
        stream=sys.stdout,
    )
    while True:
        job = queue.get()
        if job is None:
            break
        try:
            image_s, thumbs_s, archive_s, thumb_max = job
            write_thumb(Path(image_s), Path(thumbs_s), Path(archive_s), thumb_max=int(thumb_max))
        except Exception:
            log.exception("Thumbnail worker failed for job %s", job)


class ThumbWorker:
    """Bounded process queue for Pillow thumbnail generation."""

    def __init__(self, maxsize: int = 8) -> None:
        self._queue: Any = multiprocessing.Queue(maxsize=maxsize)
        self._proc = multiprocessing.Process(
            target=thumb_worker_main,
            args=(self._queue,),
            name="piomy-thumb",
            daemon=True,
        )
        self._proc.start()
        self._dropped = 0

    def enqueue(
        self,
        image: Path,
        thumbs_dir: Path,
        archive_dir: Path,
        thumb_max: int = 320,
    ) -> bool:
        """Queue a thumb job. Returns False if the queue is full (skipped)."""
        job = (str(image), str(thumbs_dir), str(archive_dir), int(thumb_max))
        try:
            self._queue.put_nowait(job)
            return True
        except Full:
            self._dropped += 1
            if self._dropped == 1 or self._dropped % 50 == 0:
                log.warning(
                    "Thumb queue full; skipping (dropped=%s). Web can generate on demand.",
                    self._dropped,
                )
            return False

    def close(self, timeout: float = 30.0) -> None:
        """Signal worker to exit and wait for drain."""
        try:
            self._queue.put(None, timeout=min(5.0, timeout))
        except Exception:
            log.warning("Could not send thumb worker shutdown sentinel")
        self._proc.join(timeout=timeout)
        if self._proc.is_alive():
            log.warning("Thumb worker did not exit in time; terminating")
            self._proc.terminate()
            self._proc.join(timeout=5.0)



def thumb_path_for(image: Path, thumbs_dir: Path, archive_dir: Path) -> Path:
    rel = image.relative_to(archive_dir)
    return thumbs_dir / rel


def write_thumb(
    image: Path,
    thumbs_dir: Path,
    archive_dir: Path,
    thumb_max: int = 320,
) -> Path:
    out = thumb_path_for(image, thumbs_dir, archive_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(image) as im:
        im = im.convert("RGB")
        im.thumbnail((thumb_max, thumb_max))
        tmp = out.with_suffix(out.suffix + ".tmp")
        im.save(tmp, format="JPEG", quality=70, optimize=True)
        tmp.replace(out)
    return out


def ensure_thumb(
    image: Path,
    thumbs_dir: Path,
    archive_dir: Path,
    thumb_max: int = 320,
) -> Path | None:
    if not image.is_file():
        return None
    out = thumb_path_for(image, thumbs_dir, archive_dir)
    if out.is_file():
        return out
    try:
        return write_thumb(image, thumbs_dir, archive_dir, thumb_max=thumb_max)
    except Exception:
        log.exception("Failed to create thumb for %s", image)
        return None


def iter_archive_jpegs(archive_dir: Path) -> list[Path]:
    """All archive JPEGs oldest-first (excludes latest.jpg and thumbs)."""
    results: list[Path] = []
    if not archive_dir.is_dir():
        return results
    for root, dirs, files in os.walk(archive_dir):
        # Skip thumbs and hidden dirs
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        root_path = Path(root)
        for name in files:
            if name.startswith("."):
                continue
            if name == "latest.jpg":
                continue
            if not name.endswith(JPEG_SUFFIX):
                continue
            results.append(root_path / name)
    results.sort(key=lambda p: p.stat().st_mtime)
    return results


def _resolved_under(root: Path, candidate: Path) -> Path | None:
    """Return candidate resolved if it is a strict subdirectory of root."""
    try:
        root_r = root.resolve()
        cand_r = candidate.resolve()
        cand_r.relative_to(root_r)
    except (OSError, ValueError):
        return None
    if cand_r == root_r:
        return None
    return cand_r


def _rmtree_under(root: Path, candidate: Path) -> bool:
    target = _resolved_under(root, candidate)
    if target is None or not target.is_dir():
        return False
    shutil.rmtree(target)
    return True


def _prune_empty_parents(archive_dir: Path, day_folder_path: Path) -> None:
    """Remove empty month/year dirs left after deleting a day folder."""
    try:
        archive_root = archive_dir.resolve()
    except OSError:
        return
    for parent in (day_folder_path.parent, day_folder_path.parent.parent):
        try:
            resolved = parent.resolve()
            resolved.relative_to(archive_root)
            if resolved == archive_root:
                break
            resolved.rmdir()
        except OSError:
            break


def enforce_min_free(cfg: AppConfig) -> int:
    """Delete oldest day folders until min_free_gb is met. Returns days deleted."""
    archive_dir = cfg.archive_path()
    ok, reason = archive_ready(archive_dir)
    if not ok:
        log.error("Cannot enforce retention: %s", reason)
        return 0

    min_free = int(cfg.storage.min_free_gb * (1024**3))
    if free_bytes(archive_dir) >= min_free:
        return 0

    grace = cfg.storage.delete_grace_minutes * 60
    now = time.time()
    deleted = 0
    thumbs_root = cfg.thumbs_dir()

    while free_bytes(archive_dir) < min_free:
        days = list_days(archive_dir)
        if len(days) < 2:
            log.warning(
                "Free space %.2f GiB below min %.2f GiB but no older day to delete",
                free_gb(archive_dir),
                cfg.storage.min_free_gb,
            )
            break

        victim_day = None
        victim_folder = None
        for day in days[:-1]:
            folder = day_folder(archive_dir, day)
            if folder is None:
                continue
            try:
                if now - folder.stat().st_mtime < grace:
                    continue
            except OSError:
                continue
            victim_day = day
            victim_folder = folder
            break

        if victim_folder is None:
            log.warning(
                "Free space %.2f GiB below min %.2f GiB but no deletable day "
                "(grace=%dm)",
                free_gb(archive_dir),
                cfg.storage.min_free_gb,
                cfg.storage.delete_grace_minutes,
            )
            break

        parsed = parse_day(victim_day)
        try:
            removed = _rmtree_under(archive_dir, victim_folder)
            if parsed is not None:
                y, m, d = parsed
                _rmtree_under(archive_dir, thumbs_root / y / m / d)
            if removed:
                _prune_empty_parents(archive_dir, victim_folder)
                deleted += 1
                log.info("Deleted old archive day %s", victim_day)
            else:
                log.error("Refused to delete %s (not under archive)", victim_folder)
                break
        except OSError:
            log.exception("Failed deleting day folder %s", victim_folder)
            break

    return deleted


def list_days(archive_dir: Path) -> list[str]:
    days: list[str] = []
    if not archive_dir.is_dir():
        return days
    for year in sorted(archive_dir.iterdir()):
        if not year.is_dir() or year.name.startswith("."):
            continue
        for month in sorted(year.iterdir()):
            if not month.is_dir() or month.name.startswith("."):
                continue
            for day in sorted(month.iterdir()):
                if day.is_dir() and not day.name.startswith("."):
                    days.append(f"{year.name}-{month.name}-{day.name}")
    return days


def parse_day(day: str) -> tuple[str, str, str] | None:
    parts = day.split("-")
    if len(parts) != 3:
        return None
    y, m, d = parts
    if not (len(y) == 4 and len(m) == 2 and len(d) == 2):
        return None
    if not (y.isdigit() and m.isdigit() and d.isdigit()):
        return None
    return y, m, d


def day_folder(archive_dir: Path, day: str) -> Path | None:
    parsed = parse_day(day)
    if parsed is None:
        return None
    y, m, d = parsed
    folder = archive_dir / y / m / d
    return folder if folder.is_dir() else None


def parse_image_name(name: str) -> tuple[int, int, int, int] | None:
    """Return (hour, minute, second, micro) from HHMMSS_ffffff.jpg."""
    match = NAME_RE.match(name)
    if not match:
        return None
    h, mi, s, us = (int(match.group(i)) for i in range(1, 5))
    if h > 23 or mi > 59 or s > 59:
        return None
    return h, mi, s, us


def list_images_for_day(archive_dir: Path, day: str) -> list[Path]:
    """day format YYYY-MM-DD, sorted by filename (time order)."""
    folder = day_folder(archive_dir, day)
    if folder is None:
        return []
    files = [
        p
        for p in folder.iterdir()
        if p.is_file()
        and p.suffix == JPEG_SUFFIX
        and not p.name.startswith(".")
        and parse_image_name(p.name) is not None
    ]
    files.sort(key=lambda p: p.name)
    return files


def hour_counts(archive_dir: Path, day: str) -> list[tuple[int, int]]:
    """List (hour, count) for hours that have images."""
    counts = [0] * 24
    for path in list_images_for_day(archive_dir, day):
        parsed = parse_image_name(path.name)
        if parsed:
            counts[parsed[0]] += 1
    return [(h, c) for h, c in enumerate(counts) if c > 0]


def block_minute(minute: int) -> int:
    return (minute // BLOCK_MINUTES) * BLOCK_MINUTES


def block_counts(archive_dir: Path, day: str, hour: int) -> list[tuple[int, int]]:
    """List (block_start_minute, count) for a given hour."""
    if hour < 0 or hour > 23:
        return []
    counts = {m: 0 for m in range(0, 60, BLOCK_MINUTES)}
    for path in list_images_for_day(archive_dir, day):
        parsed = parse_image_name(path.name)
        if not parsed or parsed[0] != hour:
            continue
        counts[block_minute(parsed[1])] += 1
    return [(m, counts[m]) for m in range(0, 60, BLOCK_MINUTES) if counts[m] > 0]


def list_images_for_block(
    archive_dir: Path, day: str, hour: int, minute_block: int
) -> list[Path]:
    """Images in [hour:minute_block, hour:minute_block+10), sorted oldest-first."""
    if hour < 0 or hour > 23:
        return []
    minute_block = block_minute(minute_block)
    end = minute_block + BLOCK_MINUTES
    out: list[Path] = []
    for path in list_images_for_day(archive_dir, day):
        parsed = parse_image_name(path.name)
        if not parsed:
            continue
        h, mi, _s, _us = parsed
        if h == hour and minute_block <= mi < end:
            out.append(path)
    return out


def rel_to_archive(archive_dir: Path, path: Path) -> str:
    return str(path.relative_to(archive_dir)).replace("\\", "/")


def path_from_rel(archive_dir: Path, rel: str) -> Path | None:
    return resolve_under_archive(archive_dir, rel)


def day_from_rel(rel: str) -> str | None:
    parts = rel.replace("\\", "/").split("/")
    if len(parts) < 4:
        return None
    return f"{parts[0]}-{parts[1]}-{parts[2]}"


def block_from_rel(rel: str) -> tuple[str, int, int] | None:
    """Return (day, hour, minute_block) for a relative archive path."""
    day = day_from_rel(rel)
    if day is None:
        return None
    name = Path(rel).name
    parsed = parse_image_name(name)
    if parsed is None:
        return None
    h, mi, _s, _us = parsed
    return day, h, block_minute(mi)


def block_href(day: str, hour: int, minute_block: int, page: int = 1) -> str:
    mb = block_minute(minute_block)
    href = f"/archive/{day}/{hour:02d}/{mb:02d}"
    if page > 1:
        href += f"?page={page}"
    return href


def latest_images_href(archive_dir: Path, page_size: int = PAGE_SIZE) -> str | None:
    """Href to the last page of the newest non-empty 10-minute block."""
    latest = latest_image_rel(archive_dir)
    if latest is None:
        return None
    info = block_from_rel(latest)
    if info is None:
        return None
    day, hour, mb = info
    imgs = list_images_for_block(archive_dir, day, hour, mb)
    _, _, total_pages = paginate(imgs, 1, page_size)
    return block_href(day, hour, mb, total_pages)


def neighbor_block(
    archive_dir: Path,
    day: str,
    hour: int,
    minute_block: int,
    *,
    newer: bool,
) -> tuple[str, int, int] | None:
    """Adjacent non-empty block as (day, hour, minute_block), crossing hours/days."""
    mb = block_minute(minute_block)
    key = (day, hour, mb)
    days = list_days(archive_dir)
    if not days:
        return None

    def blocks_for_day(d: str) -> list[tuple[str, int, int]]:
        out: list[tuple[str, int, int]] = []
        for h, _c in hour_counts(archive_dir, d):
            for block_m, _bc in block_counts(archive_dir, d, h):
                out.append((d, h, block_m))
        return out

    if newer:
        if day in days:
            for cand in blocks_for_day(day):
                if cand > key:
                    return cand
            start = days.index(day) + 1
        else:
            start = 0
            while start < len(days) and days[start] < day:
                start += 1
        for d in days[start:]:
            later = blocks_for_day(d)
            if later:
                return later[0]
        return None

    if day in days:
        earlier = [cand for cand in blocks_for_day(day) if cand < key]
        if earlier:
            return earlier[-1]
        end = days.index(day)
    else:
        end = 0
        while end < len(days) and days[end] < day:
            end += 1
    for d in reversed(days[:end]):
        earlier_day = blocks_for_day(d)
        if earlier_day:
            return earlier_day[-1]
    return None


def display_time_from_rel(rel: str) -> str:
    day = day_from_rel(rel)
    parsed = parse_image_name(Path(rel).name)
    if day is None or parsed is None:
        return rel
    h, mi, s, _us = parsed
    return f"{day} {h:02d}:{mi:02d}:{s:02d}"


def neighbor_rel(archive_dir: Path, rel: str, newer: bool) -> str | None:
    """Next/previous frame by time. Crosses day boundaries when needed."""
    path = resolve_under_archive(archive_dir, rel)
    if path is None:
        return None
    day = day_from_rel(rel)
    if day is None:
        return None
    days = list_days(archive_dir)
    if day not in days:
        return None
    day_idx = days.index(day)
    files = list_images_for_day(archive_dir, day)
    names = [p.name for p in files]
    try:
        idx = names.index(path.name)
    except ValueError:
        return None

    if newer:
        if idx + 1 < len(files):
            return rel_to_archive(archive_dir, files[idx + 1])
        # first of next day
        if day_idx + 1 < len(days):
            nxt = list_images_for_day(archive_dir, days[day_idx + 1])
            if nxt:
                return rel_to_archive(archive_dir, nxt[0])
        return None

    if idx > 0:
        return rel_to_archive(archive_dir, files[idx - 1])
    if day_idx > 0:
        prev_files = list_images_for_day(archive_dir, days[day_idx - 1])
        if prev_files:
            return rel_to_archive(archive_dir, prev_files[-1])
    return None


def latest_image_rel(archive_dir: Path) -> str | None:
    days = list_days(archive_dir)
    if not days:
        return None
    for day in reversed(days):
        files = list_images_for_day(archive_dir, day)
        if files:
            return rel_to_archive(archive_dir, files[-1])
    return None


def paginate(items: list, page: int, page_size: int = PAGE_SIZE) -> tuple[list, int, int]:
    """Return (slice, page, total_pages). page is 1-based."""
    total = len(items)
    total_pages = max(1, (total + page_size - 1) // page_size) if total else 1
    page = max(1, min(page, total_pages))
    start = (page - 1) * page_size
    return items[start : start + page_size], page, total_pages


def cpu_temp_c(path: Path | None = None) -> float | None:
    """Read SoC temperature in Celsius from sysfs, or None if unavailable."""
    thermal = path or Path("/sys/class/thermal/thermal_zone0/temp")
    try:
        return round(int(thermal.read_text(encoding="utf-8").strip()) / 1000.0, 1)
    except (OSError, ValueError):
        return None


def measured_fps(timestamps: list[float] | tuple[float, ...]) -> float | None:
    """FPS from monotonic capture timestamps (needs at least two samples)."""
    if len(timestamps) < 2:
        return None
    span = timestamps[-1] - timestamps[0]
    if span <= 0:
        return None
    return round((len(timestamps) - 1) / span, 2)


def write_status(cfg: AppConfig, payload: dict) -> None:
    archive_dir = cfg.archive_path()
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    path = cfg.status_path()
    payload = {
        **payload,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        atomic_write_bytes(path, json.dumps(payload, indent=2).encode("utf-8"))
    except OSError:
        log.exception("Failed writing status")


def read_status(cfg: AppConfig) -> dict:
    path = cfg.status_path()
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def resolve_under_archive(archive_dir: Path, rel: str) -> Path | None:
    """Resolve a relative path safely under archive_dir."""
    rel = rel.lstrip("/")
    try:
        archive_root = archive_dir.resolve()
    except OSError:
        return None
    candidate = (archive_dir / rel).resolve()
    try:
        candidate.relative_to(archive_root)
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate
