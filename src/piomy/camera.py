"""picamera2 wrapper with exposure/EV/rotation. Mock fallback for non-Pi hosts."""

from __future__ import annotations

import io
import logging
import time
from typing import Any

from PIL import Image, ImageDraw

from piomy.config import CaptureConfig

log = logging.getLogger(__name__)


class CameraError(RuntimeError):
    pass


class BaseCamera:
    def configure(self, capture: CaptureConfig) -> None:
        raise NotImplementedError

    def capture_jpeg(self) -> bytes:
        raise NotImplementedError

    def close(self) -> None:
        pass


class MockCamera(BaseCamera):
    """Generates placeholder frames when picamera2 is unavailable."""

    def __init__(self) -> None:
        self._capture = CaptureConfig()
        self._n = 0

    def configure(self, capture: CaptureConfig) -> None:
        self._capture = capture
        log.warning("Using MockCamera (picamera2 not available)")

    def _frame(self, size: tuple[int, int], label: str) -> bytes:
        self._n += 1
        img = Image.new("RGB", size, color=(32, 48, 40))
        draw = ImageDraw.Draw(img)
        draw.text((20, 20), f"Pi-O-My mock {label}", fill=(220, 220, 200))
        draw.text((20, 50), time.strftime("%Y-%m-%d %H:%M:%S"), fill=(180, 200, 180))
        draw.text((20, 80), f"frame {self._n} rot={self._capture.rotation}", fill=(160, 180, 160))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self._capture.jpeg_quality)
        return buf.getvalue()

    def capture_jpeg(self) -> bytes:
        w, h = self._capture.resolution
        return self._frame((w, h), "archive")


class PiCamera(BaseCamera):
    def __init__(self) -> None:
        try:
            from picamera2 import Picamera2  # type: ignore
            from libcamera import Transform  # type: ignore
        except ImportError as exc:
            raise CameraError("picamera2 is not installed") from exc
        self._Picamera2 = Picamera2
        self._Transform = Transform
        self._picam: Any = None
        self._capture = CaptureConfig()

    def configure(self, capture: CaptureConfig) -> None:
        self._capture = capture
        if self._picam is not None:
            try:
                self._picam.stop()
            except Exception:
                pass
            try:
                self._picam.close()
            except Exception:
                pass
            self._picam = None

        picam = self._Picamera2()
        transform = self._transform_for(capture.rotation)
        still_w, still_h = capture.resolution

        # On Pi, RGB888 buffers are BGR in memory. Prefer capture_file for JPEG;
        # the array path swaps channels for Pillow.
        config = picam.create_still_configuration(
            main={"size": (still_w, still_h), "format": "RGB888"},
            transform=transform,
            buffer_count=2,
        )
        picam.configure(config)
        try:
            picam.options["quality"] = int(capture.jpeg_quality)
        except Exception:
            log.exception("Could not set JPEG quality option")
        self._apply_controls(picam, capture)
        picam.start()
        time.sleep(0.3)  # let auto-exposure settle
        self._picam = picam
        log.info(
            "Camera started still=%sx%s rotation=%s mode=%s",
            still_w,
            still_h,
            capture.rotation,
            capture.exposure_mode,
        )

    def _transform_for(self, rotation: int) -> Any:
        try:
            return self._Transform(rotation=rotation)
        except TypeError:
            return self._Transform()

    def _apply_controls(self, picam: Any, capture: CaptureConfig) -> None:
        controls: dict[str, Any] = {}
        if capture.exposure_mode == "auto":
            controls["AeEnable"] = True
            controls["ExposureValue"] = float(capture.ev)
        else:
            controls["AeEnable"] = False
            if capture.exposure_time_us is not None:
                controls["ExposureTime"] = int(capture.exposure_time_us)
            if capture.analogue_gain is not None:
                controls["AnalogueGain"] = float(capture.analogue_gain)
        if controls:
            try:
                picam.set_controls(controls)
            except Exception:
                log.exception("Failed applying camera controls: %s", controls)

    def _array_to_rgb_image(self, arr: Any) -> Image.Image:
        """Turn a picamera2 RGB888 array into a Pillow RGB image."""
        if getattr(arr, "ndim", 0) == 3 and arr.shape[2] >= 3:
            # Buffer is BGR-ordered; reverse for Pillow.
            rgb = arr[:, :, ::-1]
            return Image.fromarray(rgb[..., :3].copy())
        return Image.fromarray(arr)

    def capture_jpeg(self) -> bytes:
        if self._picam is None:
            raise CameraError("Camera not configured")
        try:
            self._picam.options["quality"] = int(self._capture.jpeg_quality)
        except Exception:
            pass
        try:
            buf = io.BytesIO()
            self._picam.capture_file(buf, format="jpeg")
            data = buf.getvalue()
            if data.startswith(b"\xff\xd8"):
                return data
        except Exception:
            log.exception("capture_file jpeg failed; falling back to array path")

        arr = self._picam.capture_array("main")
        img = self._array_to_rgb_image(arr)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=self._capture.jpeg_quality)
        return buf.getvalue()

    def close(self) -> None:
        if self._picam is None:
            return
        try:
            self._picam.stop()
        except Exception:
            pass
        try:
            self._picam.close()
        except Exception:
            pass
        self._picam = None


def create_camera(prefer_mock: bool = False) -> BaseCamera:
    if prefer_mock:
        return MockCamera()
    try:
        return PiCamera()
    except CameraError:
        log.warning("Falling back to MockCamera")
        return MockCamera()
