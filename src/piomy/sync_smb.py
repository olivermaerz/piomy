"""Sync local archive to Samba via rclone; enforce remote max_age_days."""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from piomy.config import AppConfig, config_mtime, load_config
from piomy.storage import read_status, write_status

log = logging.getLogger(__name__)

_stop_requested = False
_reload_requested = False


def _handle_stop(signum: int, frame: object) -> None:
    global _stop_requested
    _stop_requested = True


def _handle_sighup(signum: int, frame: object) -> None:
    global _reload_requested
    _reload_requested = True


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [sync] %(message)s",
        stream=sys.stdout,
    )


def _read_smb_password(password_file: str) -> str:
    path = Path(password_file)
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8").strip()


def _smb_to_rclone_remote(url: str) -> tuple[str, str]:
    """
    Convert //host/share/path to (host, share/path) for rclone :smb: host/share/path
    """
    cleaned = url.strip().replace("\\", "/")
    while cleaned.startswith("//"):
        cleaned = cleaned[2:]
    cleaned = cleaned.lstrip("/")
    parts = cleaned.split("/", 1)
    host = parts[0]
    rest = parts[1] if len(parts) > 1 else ""
    return host, rest


def _rclone_available() -> bool:
    return shutil.which("rclone") is not None


def _obscure_password(password: str) -> str:
    """rclone expects obscured passwords for SMB pass options."""
    try:
        proc = subprocess.run(
            ["rclone", "obscure", password],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        return proc.stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        log.exception("rclone obscure failed")
        return password


def build_rclone_env(cfg: AppConfig) -> dict[str, str]:
    env = os.environ.copy()
    host, _ = _smb_to_rclone_remote(cfg.sync.smb.url)
    password = _read_smb_password(cfg.sync.smb.password_file)
    env["RCLONE_SMB_HOST"] = host
    if cfg.sync.smb.username:
        env["RCLONE_SMB_USER"] = cfg.sync.smb.username
    if password:
        env["RCLONE_SMB_PASS"] = _obscure_password(password)
    return env


def remote_path(cfg: AppConfig) -> str:
    _host, rest = _smb_to_rclone_remote(cfg.sync.smb.url)
    # rclone on-the-fly :smb:share/path
    return f":smb:{rest}" if rest else ":smb:"


def run_rclone(args: list[str], cfg: AppConfig, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    env = build_rclone_env(cfg)
    cmd = ["rclone", *args]
    log.info("Running: %s", " ".join(cmd))
    return subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def sync_once(cfg: AppConfig) -> dict:
    """Copy aged local JPEGs to SMB; delete remote files older than max_age_days."""
    result = {
        "ok": False,
        "error": None,
        "copied": False,
        "cleaned": False,
        "at": datetime.now(timezone.utc).isoformat(),
    }
    if not cfg.sync.enabled:
        result["error"] = "sync disabled"
        return result
    if not _rclone_available():
        result["error"] = "rclone not found in PATH"
        return result

    archive = cfg.archive_path()
    if not archive.is_dir():
        result["error"] = f"archive missing: {archive}"
        return result

    remote = remote_path(cfg)
    # Copy archive tree; skip thumbs/status/latest
    min_age = f"{cfg.sync.min_age_seconds}s"
    copy = run_rclone(
        [
            "copy",
            str(archive),
            f"{remote}/archive",
            "--min-age",
            min_age,
            "--exclude",
            ".thumbs/**",
            "--exclude",
            ".piomy_status.json",
            "--exclude",
            "latest.jpg",
            "--exclude",
            "*.tmp",
            "--retries",
            "3",
            "--low-level-retries",
            "5",
        ],
        cfg,
    )
    if copy.returncode != 0:
        result["error"] = (copy.stderr or copy.stdout or "rclone copy failed")[:500]
        log.error("rclone copy failed: %s", result["error"])
        return result
    result["copied"] = True

    # Delete remote files older than max_age_days
    max_age = f"{cfg.sync.max_age_days}d"
    cleanup = run_rclone(
        [
            "delete",
            f"{remote}/archive",
            "--min-age",
            max_age,
            "--rmdirs",
        ],
        cfg,
    )
    if cleanup.returncode != 0:
        result["error"] = (cleanup.stderr or cleanup.stdout or "rclone delete failed")[:500]
        log.error("rclone delete failed: %s", result["error"])
        return result
    result["cleaned"] = True
    result["ok"] = True
    return result


def update_sync_status(cfg: AppConfig, sync_result: dict) -> None:
    status = read_status(cfg)
    status["sync"] = sync_result
    write_status(cfg, status)


def run(cfg: AppConfig | None = None) -> None:
    global _reload_requested, _stop_requested
    _setup_logging()
    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGHUP, _handle_sighup)

    cfg = cfg or load_config()
    mtime = config_mtime()

    while not _stop_requested:
        if _reload_requested or config_mtime() != mtime:
            _reload_requested = False
            try:
                cfg = load_config()
                mtime = config_mtime()
                log.info("Config reloaded")
            except Exception:
                log.exception("Config reload failed")

        if cfg.sync.enabled:
            try:
                result = sync_once(cfg)
                update_sync_status(cfg, result)
                if result["ok"]:
                    log.info("Sync ok")
                else:
                    log.warning("Sync not ok: %s", result.get("error"))
            except Exception as exc:
                log.exception("Sync iteration failed")
                update_sync_status(
                    cfg,
                    {
                        "ok": False,
                        "error": str(exc),
                        "at": datetime.now(timezone.utc).isoformat(),
                    },
                )
        else:
            update_sync_status(
                cfg,
                {
                    "ok": True,
                    "error": "disabled",
                    "at": datetime.now(timezone.utc).isoformat(),
                },
            )

        # Sleep in slices
        end = time.monotonic() + max(cfg.sync.interval_seconds, 10)
        while time.monotonic() < end and not _stop_requested and not _reload_requested:
            time.sleep(min(1.0, end - time.monotonic()))

    log.info("Sync daemon stopped")


def main() -> None:
    run()


if __name__ == "__main__":
    main()
