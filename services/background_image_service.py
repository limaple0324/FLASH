"""Decode a player-selected image into a managed display copy."""

from __future__ import annotations

import importlib
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Callable

from PIL import Image, ImageOps, UnidentifiedImageError

from config.config_manager import ConfigManager


BACKGROUND_IMAGE_CONFIG_KEY = "background_image_path"
BACKGROUND_IMAGE_DIRNAME = "backgrounds"
RAW_IMAGE_SUFFIXES = frozenset(
    {
        ".3fr",
        ".arw",
        ".bay",
        ".cr2",
        ".cr3",
        ".dcr",
        ".dng",
        ".erf",
        ".fff",
        ".iiq",
        ".k25",
        ".kdc",
        ".mef",
        ".mos",
        ".mrw",
        ".nef",
        ".nrw",
        ".orf",
        ".pef",
        ".raf",
        ".raw",
        ".rw2",
        ".rwl",
        ".sr2",
        ".srf",
        ".srw",
        ".x3f",
    }
)

RawpyLoader = Callable[[], ModuleType]


@dataclass(frozen=True, slots=True)
class BackgroundImageResult:
    """Player-safe result returned by a background selection attempt."""

    succeeded: bool
    message: str
    managed_path: Path | None


class BackgroundImageService:
    """Keep originals untouched and publish only fully decoded managed copies."""

    def __init__(
        self,
        config: ConfigManager,
        managed_data_dir: Path,
        *,
        rawpy_loader: RawpyLoader | None = None,
    ) -> None:
        if not isinstance(config, ConfigManager):
            raise TypeError("config must be ConfigManager.")
        self._config = config
        self._managed_dir = (
            Path(managed_data_dir).resolve(strict=False)
            / BACKGROUND_IMAGE_DIRNAME
        )
        self._rawpy_loader = rawpy_loader or (
            lambda: importlib.import_module("rawpy")
        )

    def current_background(self) -> Path | None:
        """Return the configured managed copy, never an arbitrary outside path."""
        raw_path = self._config.get(BACKGROUND_IMAGE_CONFIG_KEY)
        if not isinstance(raw_path, str) or not raw_path.strip():
            return None
        candidate = Path(raw_path).resolve(strict=False)
        try:
            candidate.relative_to(self._managed_dir)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def select(self, source: Path) -> BackgroundImageResult:
        """Decode and atomically publish ``source``; retain the old setting on failure."""
        previous_background = self.current_background()
        try:
            source_path = Path(source)
        except TypeError:
            return self._failure("選取的檔案無效，原本背景已保留。")
        if not source_path.is_file():
            return self._failure("找不到選取的圖片，原本背景已保留。")

        decoded, failure_message = self._decode(source_path)
        if decoded is None:
            return self._failure(failure_message)

        published_path: Path | None = None
        try:
            published_path = self._publish(decoded)
            previous_data = dict(self._config.data)
            try:
                self._config.set(
                    BACKGROUND_IMAGE_CONFIG_KEY,
                    str(published_path),
                )
            except Exception:
                self._config.data.clear()
                self._config.data.update(previous_data)
                published_path.unlink(missing_ok=True)
                return self._failure(
                    "背景圖片無法保存，原本背景已保留。"
                )
        except (OSError, ValueError):
            if published_path is not None:
                published_path.unlink(missing_ok=True)
            return self._failure("背景圖片無法保存，原本背景已保留。")
        finally:
            decoded.close()

        if (
            previous_background is not None
            and previous_background != published_path
        ):
            try:
                previous_background.unlink(missing_ok=True)
            except OSError:
                pass
        return BackgroundImageResult(
            succeeded=True,
            message="背景圖片已套用。",
            managed_path=published_path,
        )

    def clear(self) -> BackgroundImageResult:
        """Clear the setting and remove only this service's managed copy."""
        managed_path = self.current_background()
        previous_data = dict(self._config.data)
        try:
            self._config.set(BACKGROUND_IMAGE_CONFIG_KEY, "")
        except Exception:
            self._config.data.clear()
            self._config.data.update(previous_data)
            return self._failure("背景圖片無法清除，原本背景已保留。")

        if managed_path is not None:
            try:
                managed_path.unlink(missing_ok=True)
            except OSError:
                return BackgroundImageResult(
                    succeeded=True,
                    message=(
                        "背景設定已清除；受管背景副本目前無法刪除，"
                        "原始圖片未受影響。"
                    ),
                    managed_path=None,
                )
        return BackgroundImageResult(
            succeeded=True,
            message="背景圖片已清除。",
            managed_path=None,
        )

    def _failure(self, message: str) -> BackgroundImageResult:
        return BackgroundImageResult(
            succeeded=False,
            message=message,
            managed_path=self.current_background(),
        )

    def _decode(self, source: Path) -> tuple[Image.Image | None, str]:
        try:
            with Image.open(source) as opened:
                opened.load()
                return self._display_image(opened), ""
        except (UnidentifiedImageError, OSError, ValueError):
            pass

        try:
            rawpy = self._rawpy_loader()
        except (ImportError, ModuleNotFoundError, OSError):
            if source.suffix.casefold() in RAW_IMAGE_SUFFIXES:
                return (
                    None,
                    "相機 RAW 圖片解碼元件目前無法使用，原本背景已保留。",
                )
            return (
                None,
                "選取的檔案不是可解碼的圖片，原本背景已保留。",
            )

        try:
            with rawpy.imread(str(source)) as raw:
                pixels = raw.postprocess(
                    use_camera_wb=True,
                    output_bps=8,
                )
            raw_image = (
                pixels
                if isinstance(pixels, Image.Image)
                else Image.fromarray(pixels)
            )
            try:
                return self._display_image(raw_image), ""
            finally:
                raw_image.close()
        except Exception:
            return (
                None,
                "選取的檔案不是可解碼的圖片，原本背景已保留。",
            )

    @staticmethod
    def _display_image(image: Image.Image) -> Image.Image:
        transposed = ImageOps.exif_transpose(image)
        try:
            if transposed.mode in {"RGBA", "LA"} or (
                transposed.mode == "P" and "transparency" in transposed.info
            ):
                return transposed.convert("RGBA")
            return transposed.convert("RGB")
        finally:
            if transposed is not image:
                transposed.close()

    def _publish(self, image: Image.Image) -> Path:
        self._managed_dir.mkdir(parents=True, exist_ok=True)
        identifier = uuid.uuid4().hex
        destination = self._managed_dir / f"background-{identifier}.png"
        temporary = self._managed_dir / f".background-{identifier}.tmp"
        try:
            with temporary.open("xb") as output:
                image.save(output, format="PNG")
                output.flush()
                os.fsync(output.fileno())
            with Image.open(temporary) as verification:
                verification.verify()
            os.replace(temporary, destination)
            return destination
        finally:
            temporary.unlink(missing_ok=True)
