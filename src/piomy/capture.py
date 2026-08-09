"""Capture daemon: stills to SSD, latest.jpg, status, config reload."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from datetime import datetime, timezone

from piomy.camera import CameraError, create_camera
from piomy.config import AppConfig, config_mtime, load_config
from piomy.storage import (
    archive_ready,
    enforce_min_free,
    free_gb,
    save_jpeg_bytes,
    write_status,
)

log = logging.getLogger(__name__)

_reload_requested = False
_stop_requested = False


def _handle_sighup(signum: int, frame: object) -> None:
    global _reload_requested
    _reload_requested = True
    log.info("SIGHUP received; will reload config")


def _handle_stop(signum: int, frame: object) -> None:
    global _stop_requested
    _stop_requested = True
    log.info("Stop signal received")


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [capture] %(message)s",
        stream=sys.stdout,
    )


def run(cfg: AppConfig | None = None) -> None:
    global _reload_requested, _stop_requested
    _setup_logging()
    signal.signal(signal.SIGHUP, _handle_sighup)
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    prefer_mock = os.environ.get("PIOMY_MOCK_CAMERA", "").lower() in ("1", "true", "yes")
    cfg = cfg or load_config()
    mtime = config_mtime()
    camera = create_camera(prefer_mock=prefer_mock)

    def apply_camera() -> None:
        camera.configure(cfg.capture, cfg.preview)

    try:
        apply_camera()
    except Exception:
        log.exception("Initial camera configure failed")
        write_status(
            cfg,
            {
                "camera_ok": False,
                "last_error": "camera configure failed",
                "last_capture_at": None,
            },
        )

    last_capture_at: str | None = None
    camera_ok = True

    while not _stop_requested:
        if _reload_requested or config_mtime() != mtime:
            _reload_requested = False
            try:
                cfg = load_config()
                mtime = config_mtime()
                apply_camera()
                log.info("Config reloaded")
            except Exception:
                log.exception("Config reload failed")

        ok, reason = archive_ready(cfg.archive_path())
        if not ok:
            camera_ok = camera_ok  # unchanged
            log.error("Archive not ready: %s", reason)
            write_status(
                cfg,
                {
                    "camera_ok": camera_ok,
                    "archive_ok": False,
                    "archive_error": reason,
                    "last_capture_at": last_capture_at,
                    "free_gb": None,
                },
            )
            time.sleep(max(cfg.capture.interval_seconds, 2.0))
            continue

        started = time.monotonic()
        try:
            jpeg = camera.capture_jpeg()
            path = save_jpeg_bytes(
                cfg.archive_path(),
                jpeg,
                cfg.latest_path(),
                thumbs_dir=cfg.thumbs_dir(),
            )
            last_capture_at = datetime.now(timezone.utc).isoformat()
            camera_ok = True
            deleted = enforce_min_free(cfg)
            write_status(
                cfg,
                {
                    "camera_ok": True,
                    "archive_ok": True,
                    "archive_error": None,
                    "last_capture_at": last_capture_at,
                    "last_capture_path": str(path),
                    "free_gb": round(free_gb(cfg.archive_path()), 3),
                    "deleted_for_retention": deleted,
                },
            )
        except CameraError as exc:
            camera_ok = False
            log.error("Capture failed: %s", exc)
            write_status(
                cfg,
                {
                    "camera_ok": False,
                    "archive_ok": True,
                    "last_error": str(exc),
                    "last_capture_at": last_capture_at,
                    "free_gb": round(free_gb(cfg.archive_path()), 3),
                },
            )
        except Exception as exc:
            camera_ok = False
            log.exception("Capture loop error")
            write_status(
                cfg,
                {
                    "camera_ok": False,
                    "archive_ok": True,
                    "last_error": str(exc),
                    "last_capture_at": last_capture_at,
                    "free_gb": round(free_gb(cfg.archive_path()), 3),
                },
            )

        elapsed = time.monotonic() - started
        sleep_for = max(0.0, cfg.capture.interval_seconds - elapsed)
        # Interruptible sleep for signals
        end = time.monotonic() + sleep_for
        while time.monotonic() < end and not _stop_requested and not _reload_requested:
            time.sleep(min(0.25, end - time.monotonic()))

    camera.close()
    log.info("Capture daemon stopped")


def main() -> None:
    run()


if __name__ == "__main__":
    main()
