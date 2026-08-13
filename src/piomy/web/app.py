"""FastAPI live view, archive browser, settings, health."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote

import uvicorn
from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response, StreamingResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from piomy.auth import hash_password, verify_password
from piomy.config import DEFAULT_ACCENT_COLOR, AppConfig, load_config, save_config
from piomy.storage import (
    PAGE_SIZE,
    archive_ready,
    block_counts_from,
    block_from_rel,
    block_href,
    block_minute,
    cpu_temp_c,
    day_folder,
    display_time_from_rel,
    ensure_thumb,
    free_gb,
    hour_counts_from,
    images_in_block_from,
    latest_images_href,
    list_days,
    list_images_for_day,
    neighbor_blocks,
    neighbor_rels,
    paginate,
    parse_day,
    read_status,
    rel_to_archive,
    resolve_under_archive,
)

log = logging.getLogger(__name__)

PACKAGE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(PACKAGE_DIR / "templates"))
security = HTTPBasic(auto_error=False)


def get_cfg() -> AppConfig:
    return load_config()


def current_accent_color() -> str:
    try:
        return get_cfg().web.accent_color or DEFAULT_ACCENT_COLOR
    except Exception:
        return DEFAULT_ACCENT_COLOR


templates.env.globals["default_accent_color"] = DEFAULT_ACCENT_COLOR


def require_auth(
    credentials: Annotated[HTTPBasicCredentials | None, Depends(security)],
    cfg: Annotated[AppConfig, Depends(get_cfg)],
) -> str:
    expected = cfg.web.password_hash
    if not expected:
        return "setup"
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Basic"},
        )
    if not verify_password(credentials.password, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username or "user"


def _archive_ctx(**extra: Any) -> dict[str, Any]:
    extra.setdefault("latest_images_href", "/archive/latest")
    return {
        "page_size": PAGE_SIZE,
        **extra,
    }


def create_app() -> FastAPI:
    app = FastAPI(title="Pi-O-My", docs_url=None, redoc_url=None)
    static_dir = PACKAGE_DIR / "static"
    static_dir.mkdir(exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/theme.css")
    def theme_css() -> Response:
        accent = current_accent_color()
        return Response(
            content=f":root {{ --accent: {accent}; }}\n",
            media_type="text/css; charset=utf-8",
            headers={"Cache-Control": "no-store"},
        )

    @app.get("/health")
    def health() -> dict[str, Any]:
        cfg = get_cfg()
        status_data = read_status(cfg)
        ok, reason = archive_ready(cfg.archive_path())
        free = None
        if ok:
            try:
                free = round(free_gb(cfg.archive_path()), 3)
            except OSError:
                free = None
        camera_ok = bool(status_data.get("camera_ok"))
        sync = status_data.get("sync") or {}
        healthy = ok and camera_ok
        return {
            "ok": healthy,
            "camera_ok": camera_ok,
            "archive_ok": ok,
            "archive_error": None if ok else reason,
            "free_gb": free if free is not None else status_data.get("free_gb"),
            "last_capture_at": status_data.get("last_capture_at"),
            "capture_fps": status_data.get("capture_fps"),
            "cpu_temp_c": cpu_temp_c(),
            "sync": sync,
        }

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        user: Annotated[str, Depends(require_auth)],
        cfg: Annotated[AppConfig, Depends(get_cfg)],
    ) -> HTMLResponse:
        status_data = read_status(cfg)
        return templates.TemplateResponse(
            request,
            "live.html",
            {
                "title": "Live",
                "free_gb": status_data.get("free_gb"),
                "capture_fps": status_data.get("capture_fps"),
                "cpu_temp_c": cpu_temp_c(),
                "preview_enabled": cfg.preview.enabled,
                "setup_mode": user == "setup",
            },
        )

    @app.get("/stream.mjpg")
    def stream(
        user: Annotated[str, Depends(require_auth)],
        cfg: Annotated[AppConfig, Depends(get_cfg)],
    ) -> StreamingResponse:
        """MJPEG from latest.jpg so capture keeps exclusive camera access."""
        if not cfg.preview.enabled:
            raise HTTPException(404, "Preview disabled")

        def gen():
            boundary = b"frame"
            last_mtime = 0.0
            last_jpeg = b""
            while True:
                try:
                    latest = load_config().latest_path()
                    if latest.is_file():
                        mtime = latest.stat().st_mtime
                        if mtime != last_mtime:
                            last_jpeg = latest.read_bytes()
                            last_mtime = mtime
                    if last_jpeg:
                        yield (
                            b"--" + boundary + b"\r\n"
                            b"Content-Type: image/jpeg\r\n\r\n" + last_jpeg + b"\r\n"
                        )
                except Exception:
                    log.exception("Preview frame failed")
                time.sleep(0.2)

        return StreamingResponse(
            gen(),
            media_type="multipart/x-mixed-replace; boundary=frame",
        )

    @app.get("/archive", response_class=HTMLResponse)
    def archive_index(
        request: Request,
        user: Annotated[str, Depends(require_auth)],
        cfg: Annotated[AppConfig, Depends(get_cfg)],
    ) -> HTMLResponse:
        archive = cfg.archive_path()
        days = list(reversed(list_days(archive)))
        return templates.TemplateResponse(
            request,
            "archive.html",
            _archive_ctx(
                title="Archive",
                level="index",
                days=days,
                day=None,
                hours=[],
                blocks=[],
                images=[],
                hour=None,
                minute_block=None,
                page=1,
                total_pages=1,
                total_images=0,
                prev_block_href=None,
                next_block_href=None,
            ),
        )

    @app.get("/archive/latest")
    def archive_latest(
        user: Annotated[str, Depends(require_auth)],
        cfg: Annotated[AppConfig, Depends(get_cfg)],
    ) -> RedirectResponse:
        href = latest_images_href(cfg.archive_path())
        if not href:
            return RedirectResponse("/archive", status_code=303)
        return RedirectResponse(href, status_code=303)

    @app.get("/archive/{day}", response_class=HTMLResponse)
    def archive_day(
        day: str,
        request: Request,
        user: Annotated[str, Depends(require_auth)],
        cfg: Annotated[AppConfig, Depends(get_cfg)],
    ) -> HTMLResponse:
        if parse_day(day) is None or day_folder(cfg.archive_path(), day) is None:
            raise HTTPException(404, "Day not found")
        archive = cfg.archive_path()
        files = list_images_for_day(archive, day)
        hours = hour_counts_from(files)
        days = list(reversed(list_days(archive)))
        return templates.TemplateResponse(
            request,
            "archive.html",
            _archive_ctx(
                title=f"Archive {day}",
                level="day",
                days=days,
                day=day,
                hours=hours,
                blocks=[],
                images=[],
                hour=None,
                minute_block=None,
                page=1,
                total_pages=1,
                total_images=sum(c for _, c in hours),
                prev_block_href=None,
                next_block_href=None,
            ),
        )

    @app.get("/archive/{day}/{hour}", response_class=HTMLResponse)
    def archive_hour(
        day: str,
        hour: str,
        request: Request,
        user: Annotated[str, Depends(require_auth)],
        cfg: Annotated[AppConfig, Depends(get_cfg)],
    ) -> HTMLResponse:
        if parse_day(day) is None or day_folder(cfg.archive_path(), day) is None:
            raise HTTPException(404, "Day not found")
        try:
            hour_i = int(hour)
        except ValueError as exc:
            raise HTTPException(404, "Bad hour") from exc
        if hour_i < 0 or hour_i > 23:
            raise HTTPException(404, "Bad hour")
        archive = cfg.archive_path()
        files = list_images_for_day(archive, day)
        hours = hour_counts_from(files)
        blocks = block_counts_from(files, hour_i)
        return templates.TemplateResponse(
            request,
            "archive.html",
            _archive_ctx(
                title=f"Archive {day} {hour_i:02d}:00",
                level="hour",
                days=list(reversed(list_days(archive))),
                day=day,
                hours=hours,
                blocks=blocks,
                images=[],
                hour=hour_i,
                minute_block=None,
                page=1,
                total_pages=1,
                total_images=sum(c for _, c in blocks),
                prev_block_href=None,
                next_block_href=None,
            ),
        )

    @app.get("/archive/{day}/{hour}/{minute_block}", response_class=HTMLResponse)
    def archive_block(
        day: str,
        hour: str,
        minute_block: str,
        request: Request,
        user: Annotated[str, Depends(require_auth)],
        cfg: Annotated[AppConfig, Depends(get_cfg)],
        page: int = 1,
    ) -> HTMLResponse:
        if parse_day(day) is None or day_folder(cfg.archive_path(), day) is None:
            raise HTTPException(404, "Day not found")
        try:
            hour_i = int(hour)
            mb = block_minute(int(minute_block))
        except ValueError as exc:
            raise HTTPException(404, "Bad time") from exc
        if hour_i < 0 or hour_i > 23 or mb < 0 or mb > 50:
            raise HTTPException(404, "Bad time")
        archive = cfg.archive_path()
        files = list_images_for_day(archive, day)
        all_imgs = images_in_block_from(files, hour_i, mb)
        page_items, page, total_pages = paginate(all_imgs, page, PAGE_SIZE)
        rels = [rel_to_archive(archive, p) for p in page_items]
        end_m = mb + 9
        days_chrono = list_days(archive)
        prev_b, next_b = neighbor_blocks(
            archive, day, hour_i, mb, files=files, days=days_chrono
        )
        hours = hour_counts_from(files)
        blocks = block_counts_from(files, hour_i)
        return templates.TemplateResponse(
            request,
            "archive.html",
            _archive_ctx(
                title=f"Archive {day} {hour_i:02d}:{mb:02d}",
                level="block",
                days=list(reversed(days_chrono)),
                day=day,
                hours=hours,
                blocks=blocks,
                images=rels,
                hour=hour_i,
                minute_block=mb,
                block_label=f"{hour_i:02d}:{mb:02d}-{hour_i:02d}:{end_m:02d}",
                page=page,
                total_pages=total_pages,
                total_images=len(all_imgs),
                prev_block_href=block_href(*prev_b) if prev_b else None,
                next_block_href=block_href(*next_b) if next_b else None,
                prev_page_href=block_href(day, hour_i, mb, page - 1) if page > 1 else None,
                next_page_href=block_href(day, hour_i, mb, page + 1)
                if page < total_pages
                else None,
            ),
        )

    @app.get("/view/{rel:path}", response_class=HTMLResponse)
    def view_image(
        rel: str,
        request: Request,
        user: Annotated[str, Depends(require_auth)],
        cfg: Annotated[AppConfig, Depends(get_cfg)],
    ) -> HTMLResponse:
        archive = cfg.archive_path()
        path = resolve_under_archive(archive, rel)
        if path is None:
            raise HTTPException(404)
        rel = rel_to_archive(archive, path)
        older, newer = neighbor_rels(archive, rel)
        info = block_from_rel(rel)
        back_href = block_href(*info[:3]) if info else "/archive"
        return templates.TemplateResponse(
            request,
            "view.html",
            {
                "title": display_time_from_rel(rel),
                "rel": rel,
                "stamp": display_time_from_rel(rel),
                "older_rel": older,
                "newer_rel": newer,
                "back_href": back_href,
                "back_label": (
                    f"{info[0]} {info[1]:02d}:{info[2]:02d}" if info else "Archive"
                ),
            },
        )

    @app.get("/media/{rel:path}")
    def media(
        rel: str,
        user: Annotated[str, Depends(require_auth)],
        cfg: Annotated[AppConfig, Depends(get_cfg)],
    ) -> FileResponse:
        path = resolve_under_archive(cfg.archive_path(), rel)
        if path is None:
            raise HTTPException(404)
        return FileResponse(path, media_type="image/jpeg")

    @app.get("/thumb/{rel:path}")
    def thumb(
        rel: str,
        user: Annotated[str, Depends(require_auth)],
        cfg: Annotated[AppConfig, Depends(get_cfg)],
    ) -> FileResponse:
        path = resolve_under_archive(cfg.archive_path(), rel)
        if path is None:
            raise HTTPException(404)
        t = ensure_thumb(path, cfg.thumbs_dir(), cfg.archive_path())
        if t is None:
            return FileResponse(path, media_type="image/jpeg")
        return FileResponse(t, media_type="image/jpeg")

    @app.get("/settings", response_class=HTMLResponse)
    def settings_get(
        request: Request,
        user: Annotated[str, Depends(require_auth)],
        cfg: Annotated[AppConfig, Depends(get_cfg)],
        saved: str | None = None,
        error: str | None = None,
    ) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            "settings.html",
            {
                "title": "Settings",
                "cfg": cfg,
                "saved": saved == "1",
                "error": error,
                "setup_mode": user == "setup",
            },
        )

    @app.post("/settings")
    def settings_post(
        user: Annotated[str, Depends(require_auth)],
        archive_dir: Annotated[str, Form()],
        min_free_gb: Annotated[float, Form()],
        delete_grace_minutes: Annotated[int, Form()],
        interval_seconds: Annotated[float, Form()],
        res_w: Annotated[int, Form()],
        res_h: Annotated[int, Form()],
        jpeg_quality: Annotated[int, Form()],
        rotation: Annotated[int, Form()],
        exposure_mode: Annotated[str, Form()],
        ev: Annotated[float, Form()],
        exposure_time_us: Annotated[str, Form()] = "",
        analogue_gain: Annotated[str, Form()] = "",
        preview_enabled: Annotated[str, Form()] = "off",
        accent_color: Annotated[str, Form()] = DEFAULT_ACCENT_COLOR,
        new_password: Annotated[str, Form()] = "",
        sync_enabled: Annotated[str, Form()] = "off",
        sync_interval_seconds: Annotated[int, Form()] = 60,
        sync_min_age_seconds: Annotated[int, Form()] = 30,
        sync_max_age_days: Annotated[int, Form()] = 14,
        smb_url: Annotated[str, Form()] = "",
        smb_username: Annotated[str, Form()] = "",
        smb_password_file: Annotated[str, Form()] = "/etc/piomy/smb.cred",
    ) -> RedirectResponse:
        cfg = get_cfg()
        try:
            cfg.storage.archive_dir = archive_dir.strip()
            cfg.storage.min_free_gb = float(min_free_gb)
            cfg.storage.delete_grace_minutes = int(delete_grace_minutes)
            cfg.capture.interval_seconds = float(interval_seconds)
            cfg.capture.resolution = [int(res_w), int(res_h)]
            cfg.capture.jpeg_quality = int(jpeg_quality)
            cfg.capture.rotation = int(rotation)
            cfg.capture.exposure_mode = "manual" if exposure_mode == "manual" else "auto"
            cfg.capture.ev = float(ev)
            cfg.capture.exposure_time_us = (
                int(exposure_time_us) if exposure_time_us.strip() else None
            )
            cfg.capture.analogue_gain = (
                float(analogue_gain) if analogue_gain.strip() else None
            )
            cfg.preview.enabled = preview_enabled in ("on", "true", "1", "yes")
            cfg.web.accent_color = (accent_color or DEFAULT_ACCENT_COLOR).strip()
            if new_password.strip():
                cfg.web.password_hash = hash_password(new_password.strip())
            cfg.sync.enabled = sync_enabled in ("on", "true", "1", "yes")
            cfg.sync.interval_seconds = int(sync_interval_seconds)
            cfg.sync.min_age_seconds = int(sync_min_age_seconds)
            cfg.sync.max_age_days = int(sync_max_age_days)
            cfg.sync.smb.url = smb_url.strip()
            cfg.sync.smb.username = smb_username.strip()
            cfg.sync.smb.password_file = smb_password_file.strip()
            save_config(cfg)
        except Exception as exc:
            return RedirectResponse(
                url=f"/settings?error={quote(str(exc))}",
                status_code=303,
            )
        return RedirectResponse(url="/settings?saved=1", status_code=303)

    return app


app = create_app()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [web] %(message)s",
        stream=sys.stdout,
    )
    cfg = load_config()
    if not cfg.web.password_hash:
        log.warning(
            "web.password_hash is empty; UI is open until you set a password in Settings"
        )
    uvicorn.run(
        "piomy.web.app:app",
        host=cfg.web.host,
        port=cfg.web.port,
        workers=cfg.web.workers,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    main()
