"""Decode a player-selected image into a managed display copy."""

from __future__ import annotations

import importlib
import hashlib
import json
import os
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Callable, Mapping

from PIL import Image, ImageOps, UnidentifiedImageError

from config.config_manager import ConfigManager


BACKGROUND_IMAGE_CONFIG_KEY = "background_image_path"
BACKGROUND_GLOBAL_CONFIG_KEY = "background_global_path"
BACKGROUND_PAGE_CONFIG_KEY = "background_page_paths"
BACKGROUND_FILL_CONFIG_KEY = "background_fill_color"
BACKGROUND_OPACITY_CONFIG_KEY = "background_region_opacity"
BACKGROUND_METADATA_CONFIG_KEY = "background_metadata"
BACKGROUND_IMAGE_DIRNAME = "backgrounds"
DEFAULT_BACKGROUND_FILL_COLOR = "#C9A35D"
DEFAULT_BACKGROUND_OPACITY = {
    "sidebar": 48,
    "panel": 50,
    "role_row": 54,
}
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
    original_name: str | None = None
    original_size: tuple[int, int] | None = None
    updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class BackgroundSettings:
    global_path: Path | None
    page_paths: tuple[tuple[str, Path], ...]
    fill_color: str
    sidebar_opacity: int
    panel_opacity: int
    role_row_opacity: int

    def for_page(self, page: str) -> Path | None:
        return dict(self.page_paths).get(page, self.global_path)


@dataclass(frozen=True, slots=True)
class BackgroundMetadata:
    original_name: str
    original_size: tuple[int, int]
    updated_at: str


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
        self._prepared_metadata: dict[Path, dict[str, object]] = {}

    def _managed_path(self, value: object) -> Path | None:
        if not isinstance(value, str) or not value.strip():
            return None
        candidate = Path(value).resolve(strict=False)
        try:
            candidate.relative_to(self._managed_dir)
        except ValueError:
            return None
        return candidate if candidate.is_file() else None

    def current_background(self, page: str | None = None) -> Path | None:
        """Return the configured managed copy, never an arbitrary outside path."""
        raw_pages = self._config.get(BACKGROUND_PAGE_CONFIG_KEY, {})
        if page and isinstance(raw_pages, Mapping):
            page_path = self._managed_path(raw_pages.get(page))
            if page_path is not None:
                return page_path
        return self._managed_path(
            self._config.get(BACKGROUND_GLOBAL_CONFIG_KEY)
            or self._config.get(BACKGROUND_IMAGE_CONFIG_KEY)
        )

    def settings(self) -> BackgroundSettings:
        raw_pages = self._config.get(BACKGROUND_PAGE_CONFIG_KEY, {})
        page_paths: list[tuple[str, Path]] = []
        if isinstance(raw_pages, Mapping):
            for page, raw_path in raw_pages.items():
                path = self._managed_path(raw_path)
                if isinstance(page, str) and page.strip() and path is not None:
                    page_paths.append((page.strip(), path))
        fill_color = self._normalize_color(
            self._config.get(
                BACKGROUND_FILL_CONFIG_KEY,
                DEFAULT_BACKGROUND_FILL_COLOR,
            )
        )
        raw_opacity = self._config.get(BACKGROUND_OPACITY_CONFIG_KEY, {})
        opacity = dict(DEFAULT_BACKGROUND_OPACITY)
        if isinstance(raw_opacity, Mapping):
            for name in opacity:
                value = raw_opacity.get(name)
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and 0 <= value <= 100
                ):
                    opacity[name] = value
        return BackgroundSettings(
            global_path=self.current_background(),
            page_paths=tuple(page_paths),
            fill_color=fill_color,
            sidebar_opacity=opacity["sidebar"],
            panel_opacity=opacity["panel"],
            role_row_opacity=opacity["role_row"],
        )

    def metadata(self, path: Path | None) -> BackgroundMetadata | None:
        if path is None:
            return None
        raw = self._raw_metadata().get(str(Path(path).resolve(strict=False)))
        if not isinstance(raw, Mapping):
            return None
        name = raw.get("original_name")
        size = raw.get("original_size")
        updated_at = raw.get("updated_at")
        if (
            not isinstance(name, str)
            or not isinstance(size, list)
            or len(size) != 2
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in size
            )
            or not isinstance(updated_at, str)
        ):
            return None
        return BackgroundMetadata(
            original_name=name,
            original_size=(size[0], size[1]),
            updated_at=updated_at,
        )

    @staticmethod
    def _normalize_color(value: object) -> str:
        if (
            isinstance(value, str)
            and len(value.strip()) == 7
            and value.strip().startswith("#")
        ):
            try:
                int(value.strip()[1:], 16)
            except ValueError:
                pass
            else:
                return value.strip().upper()
        return DEFAULT_BACKGROUND_FILL_COLOR

    def update_display_settings(
        self,
        *,
        fill_color: str,
        sidebar_opacity: int,
        panel_opacity: int,
        role_row_opacity: int,
    ) -> BackgroundSettings:
        color = self._normalize_color(fill_color)
        values = {
            "sidebar": sidebar_opacity,
            "panel": panel_opacity,
            "role_row": role_row_opacity,
        }
        if any(
            isinstance(value, bool)
            or not isinstance(value, int)
            or not 0 <= value <= 100
            for value in values.values()
        ):
            raise ValueError("背景透明度必須介於 0 到 100。")
        self._config.update_values(
            {
                BACKGROUND_FILL_CONFIG_KEY: color,
                BACKGROUND_OPACITY_CONFIG_KEY: values,
            }
        )
        return self.settings()

    def prepare(
        self,
        source: Path,
        *,
        cancelled: Callable[[], bool] | None = None,
    ) -> BackgroundImageResult:
        """Decode a preview candidate without replacing saved backgrounds."""
        if cancelled is not None and not callable(cancelled):
            raise TypeError("cancelled must be callable.")
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
            if cancelled is not None and cancelled():
                return self._failure(
                    "背景圖片轉換已取消，原本背景已保留。"
                )
            size = tuple(decoded.size)
            published_path = self._publish(decoded)
            if cancelled is not None and cancelled():
                published_path.unlink(missing_ok=True)
                published_path = None
                return self._failure(
                    "背景圖片轉換已取消，原本背景已保留。"
                )
            updated_at = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            metadata = {
                "original_name": source_path.name,
                "original_size": [size[0], size[1]],
                "updated_at": updated_at,
            }
            self._prepared_metadata[published_path] = metadata
            return BackgroundImageResult(
                succeeded=True,
                message="背景圖片已準備預覽；按儲存後才會正式套用。",
                managed_path=published_path,
                original_name=source_path.name,
                original_size=size,
                updated_at=updated_at,
            )
        except (OSError, ValueError):
            if published_path is not None:
                published_path.unlink(missing_ok=True)
            return self._failure("背景圖片無法準備預覽，原本背景已保留。")
        finally:
            decoded.close()

    def commit_prepared(
        self,
        managed_path: Path,
        *,
        apply_all: bool,
        pages: tuple[str, ...] = (),
    ) -> BackgroundImageResult:
        candidate = self._managed_path(str(managed_path))
        if candidate is None:
            return self._failure("預覽背景已失效，原本背景已保留。")
        clean_pages = tuple(
            dict.fromkeys(
                page.strip()
                for page in pages
                if isinstance(page, str) and page.strip()
            )
        )
        if not apply_all and not clean_pages:
            return self._failure("請選擇全部頁面或至少一個功能頁面。")
        page_map = self._raw_page_map()
        values: dict[str, object] = {}
        if apply_all:
            values[BACKGROUND_GLOBAL_CONFIG_KEY] = str(candidate)
            values[BACKGROUND_IMAGE_CONFIG_KEY] = str(candidate)
        else:
            for page in clean_pages:
                page_map[page] = str(candidate)
            values[BACKGROUND_PAGE_CONFIG_KEY] = page_map
        metadata = self._raw_metadata()
        candidate_metadata = self._prepared_metadata.get(candidate)
        if candidate_metadata is not None:
            metadata[str(candidate)] = candidate_metadata
            values[BACKGROUND_METADATA_CONFIG_KEY] = metadata
        previous_data = dict(self._config.data)
        try:
            self._config.update_values(values)
        except Exception:
            self._config.data.clear()
            self._config.data.update(previous_data)
            return self._failure("背景圖片無法保存，原本背景已保留。")
        self._prepared_metadata.pop(candidate, None)
        self._cleanup_unreferenced()
        return BackgroundImageResult(
            succeeded=True,
            message="背景圖片已儲存並套用。",
            managed_path=candidate,
            original_name=(
                str(candidate_metadata["original_name"])
                if candidate_metadata is not None
                else None
            ),
            original_size=(
                tuple(candidate_metadata["original_size"])  # type: ignore[arg-type]
                if candidate_metadata is not None
                else None
            ),
            updated_at=(
                str(candidate_metadata["updated_at"])
                if candidate_metadata is not None
                else None
            ),
        )

    def discard_prepared(self, managed_path: Path | None) -> None:
        if managed_path is None:
            return
        candidate = Path(managed_path).resolve(strict=False)
        self._prepared_metadata.pop(candidate, None)
        if candidate not in self._referenced_paths():
            try:
                candidate.relative_to(self._managed_dir)
                candidate.unlink(missing_ok=True)
            except (ValueError, OSError):
                pass

    def select(self, source: Path) -> BackgroundImageResult:
        """Decode and atomically publish ``source``; retain the old setting on failure."""
        prepared = self.prepare(source)
        if not prepared.succeeded or prepared.managed_path is None:
            return prepared
        result = self.commit_prepared(
            prepared.managed_path,
            apply_all=True,
        )
        if not result.succeeded:
            self.discard_prepared(prepared.managed_path)
        elif result.message == "背景圖片已儲存並套用。":
            result = BackgroundImageResult(
                succeeded=True,
                message="背景圖片已套用。",
                managed_path=result.managed_path,
                original_name=result.original_name,
                original_size=result.original_size,
                updated_at=result.updated_at,
            )
        return result

    def clear(self) -> BackgroundImageResult:
        """Clear the setting and remove only this service's managed copy."""
        managed_path = self.current_background()
        previous_data = dict(self._config.data)
        try:
            self._config.update_values(
                {
                    BACKGROUND_IMAGE_CONFIG_KEY: "",
                    BACKGROUND_GLOBAL_CONFIG_KEY: "",
                }
            )
        except Exception:
            self._config.data.clear()
            self._config.data.update(previous_data)
            return self._failure("背景圖片無法清除，原本背景已保留。")

        self._cleanup_unreferenced()
        return BackgroundImageResult(
            succeeded=True,
            message="背景圖片已清除。",
            managed_path=None,
        )

    def clear_page(self, page: str) -> BackgroundImageResult:
        page_map = self._raw_page_map()
        page_map.pop(page, None)
        try:
            self._config.set(BACKGROUND_PAGE_CONFIG_KEY, page_map)
        except Exception:
            return self._failure("背景圖片無法清除，原本背景已保留。")
        self._cleanup_unreferenced()
        return BackgroundImageResult(
            succeeded=True,
            message="頁面獨立背景已移除。",
            managed_path=self.current_background(page),
        )

    def clear_all(self) -> BackgroundImageResult:
        """Remove global and page assignments while keeping source files untouched."""
        previous_data = dict(self._config.data)
        try:
            self._config.update_values(
                {
                    BACKGROUND_IMAGE_CONFIG_KEY: "",
                    BACKGROUND_GLOBAL_CONFIG_KEY: "",
                    BACKGROUND_PAGE_CONFIG_KEY: {},
                }
            )
        except Exception:
            self._config.data.clear()
            self._config.data.update(previous_data)
            return self._failure("所有背景無法恢復預設，原本設定已保留。")
        self._cleanup_unreferenced()
        return BackgroundImageResult(
            succeeded=True,
            message="所有背景已恢復舊版預設。",
            managed_path=None,
        )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

    def export_settings(self, destination: Path) -> Path:
        """Export assignments, display settings, metadata and managed images."""
        destination = Path(destination).resolve(strict=False)
        if destination.suffix.casefold() != ".zip":
            raise ValueError("背景設定備份必須是 ZIP 檔。")
        destination.parent.mkdir(parents=True, exist_ok=True)
        settings = self.settings()
        referenced = self._referenced_paths()
        path_ids = {
            path: self._sha256(path)
            for path in sorted(referenced, key=str)
        }
        metadata_payload: dict[str, object] = {}
        for path, image_id in path_ids.items():
            metadata = self.metadata(path)
            if metadata is not None:
                metadata_payload[image_id] = {
                    "original_name": metadata.original_name,
                    "original_size": list(metadata.original_size),
                    "updated_at": metadata.updated_at,
                }
        payload = {
            "schema_version": 1,
            "global_image": (
                path_ids.get(settings.global_path)
                if settings.global_path is not None
                else None
            ),
            "page_images": {
                page: path_ids[path]
                for page, path in settings.page_paths
            },
            "fill_color": settings.fill_color,
            "opacity": {
                "sidebar": settings.sidebar_opacity,
                "panel": settings.panel_opacity,
                "role_row": settings.role_row_opacity,
            },
            "metadata": metadata_payload,
        }
        temporary = destination.with_name(
            f".{destination.name}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with zipfile.ZipFile(
                temporary,
                "w",
                compression=zipfile.ZIP_DEFLATED,
            ) as archive:
                archive.writestr(
                    "settings.json",
                    json.dumps(
                        payload,
                        ensure_ascii=False,
                        indent=2,
                    )
                    + "\n",
                )
                written_images: set[str] = set()
                for path, image_id in path_ids.items():
                    if image_id in written_images:
                        continue
                    archive.write(path, f"images/{image_id}.png")
                    written_images.add(image_id)
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return destination

    def import_settings(self, source: Path) -> BackgroundImageResult:
        """Validate a complete backup before replacing any saved assignment."""
        source = Path(source).resolve(strict=False)
        if not source.is_file():
            return self._failure("找不到背景設定備份，原本設定已保留。")
        staged_paths: dict[str, Path] = {}
        previous_data = dict(self._config.data)
        try:
            with zipfile.ZipFile(source, "r") as archive:
                payload = json.loads(
                    archive.read("settings.json").decode("utf-8")
                )
                if (
                    not isinstance(payload, Mapping)
                    or payload.get("schema_version") != 1
                    or not isinstance(payload.get("page_images"), Mapping)
                    or not isinstance(payload.get("opacity"), Mapping)
                    or not isinstance(payload.get("metadata"), Mapping)
                ):
                    raise ValueError("invalid background backup")
                global_image = payload.get("global_image")
                if global_image is not None and not isinstance(
                    global_image,
                    str,
                ):
                    raise ValueError("invalid global image")
                page_images = {
                    str(page): str(image_id)
                    for page, image_id in payload["page_images"].items()
                    if isinstance(page, str)
                    and page.strip()
                    and isinstance(image_id, str)
                    and image_id
                }
                image_ids = set(page_images.values())
                if global_image is not None:
                    image_ids.add(global_image)
                self._managed_dir.mkdir(parents=True, exist_ok=True)
                for image_id in sorted(image_ids):
                    if (
                        len(image_id) != 64
                        or any(
                            character not in "0123456789abcdef"
                            for character in image_id
                        )
                    ):
                        raise ValueError("invalid image identity")
                    data = archive.read(f"images/{image_id}.png")
                    if hashlib.sha256(data).hexdigest() != image_id:
                        raise ValueError("image hash mismatch")
                    destination = (
                        self._managed_dir
                        / f"background-{uuid.uuid4().hex}.png"
                    )
                    with tempfile.NamedTemporaryFile(
                        mode="wb",
                        dir=self._managed_dir,
                        prefix=".background-import-",
                        suffix=".tmp",
                        delete=False,
                    ) as output:
                        temporary_path = Path(output.name)
                        output.write(data)
                        output.flush()
                        os.fsync(output.fileno())
                    try:
                        with Image.open(temporary_path) as verification:
                            verification.verify()
                        os.replace(temporary_path, destination)
                    finally:
                        temporary_path.unlink(missing_ok=True)
                    staged_paths[image_id] = destination
                opacity = payload["opacity"]
                opacity_values = {
                    name: opacity.get(name)
                    for name in DEFAULT_BACKGROUND_OPACITY
                }
                if any(
                    isinstance(value, bool)
                    or not isinstance(value, int)
                    or not 0 <= value <= 100
                    for value in opacity_values.values()
                ):
                    raise ValueError("invalid opacity")
                fill_color = self._normalize_color(
                    payload.get("fill_color")
                )
                metadata_payload: dict[str, object] = {}
                raw_metadata = payload["metadata"]
                for image_id, path in staged_paths.items():
                    value = raw_metadata.get(image_id)
                    if isinstance(value, Mapping):
                        metadata_payload[str(path)] = dict(value)
                global_path = (
                    staged_paths[global_image]
                    if global_image is not None
                    else None
                )
                self._config.update_values(
                    {
                        BACKGROUND_IMAGE_CONFIG_KEY: (
                            str(global_path) if global_path is not None else ""
                        ),
                        BACKGROUND_GLOBAL_CONFIG_KEY: (
                            str(global_path) if global_path is not None else ""
                        ),
                        BACKGROUND_PAGE_CONFIG_KEY: {
                            page: str(staged_paths[image_id])
                            for page, image_id in page_images.items()
                        },
                        BACKGROUND_FILL_CONFIG_KEY: fill_color,
                        BACKGROUND_OPACITY_CONFIG_KEY: opacity_values,
                        BACKGROUND_METADATA_CONFIG_KEY: metadata_payload,
                    }
                )
        except Exception:
            self._config.data.clear()
            self._config.data.update(previous_data)
            for path in staged_paths.values():
                path.unlink(missing_ok=True)
            return self._failure(
                "背景設定備份無法驗證，原本設定已保留。"
            )
        self._cleanup_unreferenced()
        return BackgroundImageResult(
            succeeded=True,
            message="背景設定已匯入並恢復。",
            managed_path=self.current_background(),
        )

    def _raw_page_map(self) -> dict[str, str]:
        raw = self._config.get(BACKGROUND_PAGE_CONFIG_KEY, {})
        return {
            str(page): str(path)
            for page, path in raw.items()
            if isinstance(raw, Mapping)
            and isinstance(page, str)
            and isinstance(path, str)
        } if isinstance(raw, Mapping) else {}

    def _raw_metadata(self) -> dict[str, object]:
        raw = self._config.get(BACKGROUND_METADATA_CONFIG_KEY, {})
        return dict(raw) if isinstance(raw, Mapping) else {}

    def _referenced_paths(self) -> set[Path]:
        paths = {
            path
            for path in (self.current_background(),)
            if path is not None
        }
        raw_pages = self._config.get(BACKGROUND_PAGE_CONFIG_KEY, {})
        if isinstance(raw_pages, Mapping):
            for raw_path in raw_pages.values():
                path = self._managed_path(raw_path)
                if path is not None:
                    paths.add(path)
        return paths

    def _cleanup_unreferenced(self) -> None:
        referenced = self._referenced_paths() | set(self._prepared_metadata)
        if not self._managed_dir.is_dir():
            return
        for candidate in self._managed_dir.glob("background-*.png"):
            resolved = candidate.resolve(strict=False)
            if resolved in referenced:
                continue
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass

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
