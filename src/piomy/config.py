"""YAML config load, validate, and save."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any, Literal, TypeVar

import yaml

log = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("/etc/piomy/config.yaml")
ENV_CONFIG_PATH = "PIOMY_CONFIG"
DEFAULT_ACCENT_COLOR = "#6fbf7a"
_ACCENT_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

T = TypeVar("T")


@dataclass
class StorageConfig:
    archive_dir: str = "/var/lib/piomy/archive"
    min_free_gb: float = 20.0
    delete_grace_minutes: int = 30


@dataclass
class CaptureConfig:
    interval_seconds: float = 1.0
    resolution: list[int] = field(default_factory=lambda: [2592, 1944])
    jpeg_quality: int = 85
    rotation: int = 0
    exposure_mode: Literal["auto", "manual"] = "auto"
    ev: float = 0.0
    exposure_time_us: int | None = None
    analogue_gain: float | None = None


@dataclass
class PreviewConfig:
    resolution: list[int] = field(default_factory=lambda: [640, 480])
    enabled: bool = True


@dataclass
class WebConfig:
    host: str = "0.0.0.0"
    port: int = 8080
    password_hash: str = ""
    accent_color: str = DEFAULT_ACCENT_COLOR
    workers: int = 2


@dataclass
class SmbConfig:
    url: str = "//192.168.0.10/share/piomy"
    username: str = ""
    password_file: str = "/etc/piomy/smb.cred"


@dataclass
class SyncConfig:
    enabled: bool = False
    interval_seconds: int = 60
    min_age_seconds: int = 30
    max_age_days: int = 14
    smb: SmbConfig = field(default_factory=SmbConfig)


@dataclass
class AppConfig:
    storage: StorageConfig = field(default_factory=StorageConfig)
    capture: CaptureConfig = field(default_factory=CaptureConfig)
    preview: PreviewConfig = field(default_factory=PreviewConfig)
    web: WebConfig = field(default_factory=WebConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)

    def archive_path(self) -> Path:
        return Path(self.storage.archive_dir)

    def latest_path(self) -> Path:
        return self.archive_path() / "latest.jpg"

    def status_path(self) -> Path:
        return self.archive_path() / ".piomy_status.json"

    def thumbs_dir(self) -> Path:
        return self.archive_path() / ".thumbs"


def config_path() -> Path:
    override = os.environ.get(ENV_CONFIG_PATH)
    if override:
        return Path(override)
    return DEFAULT_CONFIG_PATH


def _nested_type(f) -> type | None:
    default = getattr(f, "default", None)
    if default is not None and default is not field and is_dataclass(default):
        return type(default)
    factory = getattr(f, "default_factory", None)
    if callable(factory):
        try:
            sample = factory()
            if is_dataclass(sample):
                return type(sample)
        except TypeError:
            pass
    return None


def _build(cls: type[T], data: dict[str, Any] | None) -> T:
    """Build nested dataclasses from a dict, keeping defaults for missing keys."""
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected mapping for {cls.__name__}, got {type(data)}")

    base = asdict(cls())  # type: ignore[arg-type]
    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        nested = _nested_type(f)
        if nested is not None:
            base[f.name] = asdict(_build(nested, value if isinstance(value, dict) else {}))
        else:
            base[f.name] = value
    return _from_dict(cls, base)


def _from_dict(cls: type[T], data: dict[str, Any]) -> T:
    kwargs: dict[str, Any] = {}
    for f in fields(cls):
        value = data[f.name]
        nested = _nested_type(f)
        if nested is not None:
            kwargs[f.name] = _from_dict(nested, value)
        else:
            kwargs[f.name] = value
    return cls(**kwargs)


def validate(cfg: AppConfig) -> None:
    if cfg.storage.min_free_gb < 1:
        raise ValueError("storage.min_free_gb must be >= 1")
    if cfg.storage.delete_grace_minutes < 0:
        raise ValueError("storage.delete_grace_minutes must be >= 0")
    if cfg.capture.interval_seconds < 0.2:
        raise ValueError("capture.interval_seconds must be >= 0.2")
    if cfg.capture.jpeg_quality < 1 or cfg.capture.jpeg_quality > 100:
        raise ValueError("capture.jpeg_quality must be 1..100")
    if cfg.capture.rotation not in (0, 90, 180, 270):
        raise ValueError("capture.rotation must be 0, 90, 180, or 270")
    if cfg.capture.exposure_mode not in ("auto", "manual"):
        raise ValueError("capture.exposure_mode must be auto or manual")
    if len(cfg.capture.resolution) != 2:
        raise ValueError("capture.resolution must be [width, height]")
    if len(cfg.preview.resolution) != 2:
        raise ValueError("preview.resolution must be [width, height]")
    if cfg.web.port < 1 or cfg.web.port > 65535:
        raise ValueError("web.port out of range")
    if cfg.web.workers < 1 or cfg.web.workers > 8:
        raise ValueError("web.workers must be 1..8")
    accent = (cfg.web.accent_color or "").strip()
    if not _ACCENT_RE.match(accent):
        raise ValueError("web.accent_color must be a #RRGGBB hex color")
    cfg.web.accent_color = accent.lower()
    if cfg.sync.interval_seconds < 10:
        raise ValueError("sync.interval_seconds must be >= 10")
    if cfg.sync.max_age_days < 1:
        raise ValueError("sync.max_age_days must be >= 1")
    if not cfg.storage.archive_dir:
        raise ValueError("storage.archive_dir is required")


def load_config(path: Path | None = None) -> AppConfig:
    path = path or config_path()
    if not path.is_file():
        log.warning("Config not found at %s; using defaults", path)
        cfg = AppConfig()
        validate(cfg)
        return cfg
    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    if not isinstance(raw, dict):
        raise ValueError("Config root must be a mapping")
    cfg = AppConfig(
        storage=_build(StorageConfig, raw.get("storage")),
        capture=_build(CaptureConfig, raw.get("capture")),
        preview=_build(PreviewConfig, raw.get("preview")),
        web=_build(WebConfig, raw.get("web")),
        sync=_build(SyncConfig, raw.get("sync")),
    )
    validate(cfg)
    return cfg


def save_config(cfg: AppConfig, path: Path | None = None) -> None:
    validate(cfg)
    path = path or config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(cfg)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(data, fh, default_flow_style=False, sort_keys=False)
    tmp.replace(path)


def config_mtime(path: Path | None = None) -> float:
    path = path or config_path()
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0
