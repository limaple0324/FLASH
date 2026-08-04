"""Validate and privately load the fixed offline UI font bundle."""

from __future__ import annotations

import ctypes
import hashlib
import json
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PIL import ImageFont


FR_PRIVATE = 0x10
UI_FONT_MANIFEST_FILENAME = "source_manifest.json"
UI_FONT_FALLBACK_FAMILY = "Microsoft JhengHei UI"
DEFAULT_UI_FONT_ID = "jason_3"
DEFAULT_SIDEBAR_FONT_SIZE = 15
DEFAULT_CONTENT_FONT_SIZE = 14
SIDEBAR_FONT_SIZES = (12, 15, 18)
CONTENT_FONT_SIZES = (11, 14, 17)
CONTENT_HEADING_SIZES = {11: 18, 14: 22, 17: 26}
UI_FONT_OPTIONS = (
    ("cubic_11", "俐方體十一號"),
    ("naikai", "內海字體"),
    ("jason_3", "清松手寫體三"),
    ("jason_4", "清松手寫體四"),
    ("jason_6", "清松手寫體六"),
    ("jason_8", "清松手寫體八"),
    ("jason_9", "清松手寫體九"),
    ("chenyu_luoyan", "辰宇落雁體"),
)

_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True, slots=True)
class UIFontChoice:
    font_id: str
    display_name: str
    internal_family: str
    ui_family: str


@dataclass(frozen=True, slots=True)
class UIFontAsset:
    font_id: str
    display_name: str
    repository: str
    commit: str
    source_path: str
    managed_path: str
    size: int
    sha256: str
    internal_family: str
    ui_family: str
    license_evidence: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UIFontLoadResult:
    success: bool
    code: str
    message: str
    loaded_font_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class UIFontPreferences:
    font_id: str
    sidebar_size: int
    content_size: int

    @property
    def heading_size(self) -> int:
        return CONTENT_HEADING_SIZES[self.content_size]


class PrivateFontBackend(Protocol):
    def add(self, path: Path) -> bool: ...

    def remove(self, path: Path) -> bool: ...


class WindowsPrivateFontBackend:
    """Load fonts for this process only without installing them system-wide."""

    def __init__(self) -> None:
        if sys.platform != "win32":
            raise RuntimeError("Windows private font loading is unavailable.")
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._add = gdi32.AddFontResourceExW
        self._add.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.c_void_p,
        )
        self._add.restype = ctypes.c_int
        self._remove = gdi32.RemoveFontResourceExW
        self._remove.argtypes = (
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.c_void_p,
        )
        self._remove.restype = ctypes.c_bool

    def add(self, path: Path) -> bool:
        return self._add(str(path), FR_PRIVATE, None) > 0

    def remove(self, path: Path) -> bool:
        return bool(self._remove(str(path), FR_PRIVATE, None))


class _UnavailablePrivateFontBackend:
    def add(self, _path: Path) -> bool:
        return False

    def remove(self, _path: Path) -> bool:
        return True


class _FontValidationError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def normalize_ui_font_id(value: object) -> str:
    valid_ids = {font_id for font_id, _label in UI_FONT_OPTIONS}
    return value if isinstance(value, str) and value in valid_ids else DEFAULT_UI_FONT_ID


def normalize_sidebar_font_size(value: object) -> int:
    return (
        value
        if isinstance(value, int)
        and not isinstance(value, bool)
        and value in SIDEBAR_FONT_SIZES
        else DEFAULT_SIDEBAR_FONT_SIZE
    )


def normalize_content_font_size(value: object) -> int:
    return (
        value
        if isinstance(value, int)
        and not isinstance(value, bool)
        and value in CONTENT_FONT_SIZES
        else DEFAULT_CONTENT_FONT_SIZE
    )


def resolve_ui_font_preferences(
    font_id: object,
    sidebar_size: object,
    content_size: object,
) -> UIFontPreferences:
    return UIFontPreferences(
        normalize_ui_font_id(font_id),
        normalize_sidebar_font_size(sidebar_size),
        normalize_content_font_size(content_size),
    )


def read_internal_font_family(path: Path) -> str:
    family, _style = ImageFont.truetype(str(path), size=14).getname()
    if not isinstance(family, str) or not family.strip():
        raise ValueError("font family is unavailable")
    return family.strip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class UIFontService:
    """All-or-nothing validator and lifetime owner for the managed fonts."""

    def __init__(
        self,
        asset_root: Path,
        *,
        backend: PrivateFontBackend | None = None,
        family_reader: Callable[[Path], str] = read_internal_font_family,
    ) -> None:
        self._asset_root = Path(asset_root).resolve(strict=False)
        self._backend = (
            backend
            if backend is not None
            else (
                WindowsPrivateFontBackend()
                if sys.platform == "win32"
                else _UnavailablePrivateFontBackend()
            )
        )
        self._family_reader = family_reader
        self._assets: tuple[UIFontAsset, ...] = ()
        self._loaded_paths: tuple[Path, ...] = ()
        self._result = UIFontLoadResult(
            False,
            "ui_font.not_loaded",
            "離線字體尚未載入，已使用系統預設字體。",
        )

    @property
    def result(self) -> UIFontLoadResult:
        return self._result

    @property
    def loaded(self) -> bool:
        return bool(
            self._result.success
            and len(self._loaded_paths) == len(UI_FONT_OPTIONS)
        )

    @property
    def assets(self) -> tuple[UIFontAsset, ...]:
        return self._assets

    @property
    def choices(self) -> tuple[UIFontChoice, ...]:
        families = {
            asset.font_id: (
                asset.internal_family,
                asset.ui_family,
            )
            for asset in self._assets
        }
        return tuple(
            UIFontChoice(
                font_id,
                display_name,
                (
                    families.get(
                        font_id,
                        (
                            UI_FONT_FALLBACK_FAMILY,
                            UI_FONT_FALLBACK_FAMILY,
                        ),
                    )[0]
                    if self.loaded
                    else UI_FONT_FALLBACK_FAMILY
                ),
                (
                    families.get(
                        font_id,
                        (
                            UI_FONT_FALLBACK_FAMILY,
                            UI_FONT_FALLBACK_FAMILY,
                        ),
                    )[1]
                    if self.loaded
                    else UI_FONT_FALLBACK_FAMILY
                ),
            )
            for font_id, display_name in UI_FONT_OPTIONS
        )

    def family_for(self, font_id: object) -> str:
        normalized = normalize_ui_font_id(font_id)
        if not self.loaded:
            return UI_FONT_FALLBACK_FAMILY
        return next(
            (
                asset.ui_family
                for asset in self._assets
                if asset.font_id == normalized
            ),
            UI_FONT_FALLBACK_FAMILY,
        )

    def load_all(self) -> UIFontLoadResult:
        if self.loaded:
            return self._result
        if self._loaded_paths and not self.close():
            return self._fail(
                "ui_font.cleanup_failed",
                "先前的私有字體資源尚未完整解除，已使用系統預設字體。",
            )
        try:
            assets = self._validate_bundle()
        except _FontValidationError as error:
            messages = {
                "ui_font.manifest_invalid": "離線字體來源紀錄無法驗證，已使用系統預設字體。",
                "ui_font.asset_missing": "離線字體資產不完整，已使用系統預設字體。",
                "ui_font.integrity_failed": "離線字體完整性檢查未通過，已使用系統預設字體。",
                "ui_font.family_mismatch": "離線字體家族資料無法確認，已使用系統預設字體。",
                "ui_font.license_missing": "離線字體授權證據不完整，已使用系統預設字體。",
            }
            return self._fail(error.code, messages.get(error.code, messages["ui_font.manifest_invalid"]))
        loaded: list[Path] = []
        try:
            for asset in assets:
                path = self._managed_path(asset.managed_path)
                if not self._backend.add(path):
                    raise RuntimeError("private font add failed")
                loaded.append(path)
        except Exception:
            remaining: list[Path] = []
            for path in reversed(loaded):
                try:
                    removed = self._backend.remove(path)
                except Exception:
                    removed = False
                if not removed:
                    remaining.append(path)
            self._assets = assets if remaining else ()
            self._loaded_paths = tuple(reversed(remaining))
            code = (
                "ui_font.cleanup_failed"
                if remaining
                else "ui_font.load_failed"
            )
            message = (
                "離線字體載入失敗且尚未完全釋放，已停用自訂字體並改用微軟正黑體介面字體。"
                if remaining
                else "離線字體無法完整載入，已改用微軟正黑體介面字體。"
            )
            self._result = UIFontLoadResult(False, code, message)
            return self._result
        self._assets = assets
        self._loaded_paths = tuple(loaded)
        self._result = UIFontLoadResult(
            True,
            "ui_font.loaded",
            "八種離線字體已完成程序私有載入。",
            tuple(asset.font_id for asset in assets),
        )
        return self._result

    def close(self) -> bool:
        remaining: list[Path] = []
        for path in reversed(self._loaded_paths):
            try:
                removed = self._backend.remove(path)
            except Exception:
                removed = False
            if not removed:
                remaining.append(path)
        self._loaded_paths = tuple(reversed(remaining))
        if remaining:
            self._result = UIFontLoadResult(
                False,
                "ui_font.cleanup_failed",
                "部分私有字體資源尚未完整解除。",
            )
            return False
        if self._assets:
            self._result = UIFontLoadResult(
                False,
                "ui_font.closed",
                "私有字體資源已解除。",
            )
        return True

    def _fail(self, code: str, message: str) -> UIFontLoadResult:
        self._assets = ()
        self._loaded_paths = ()
        self._result = UIFontLoadResult(False, code, message)
        return self._result

    def _validate_bundle(self) -> tuple[UIFontAsset, ...]:
        manifest_path = self._asset_root / UI_FONT_MANIFEST_FILENAME
        if not manifest_path.is_file():
            raise _FontValidationError("ui_font.manifest_invalid")
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise _FontValidationError("ui_font.manifest_invalid") from None
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema_version") != 1
            or payload.get("fallback_family") != UI_FONT_FALLBACK_FAMILY
            or payload.get("default_font_id") != DEFAULT_UI_FONT_ID
        ):
            raise _FontValidationError("ui_font.manifest_invalid")
        raw_fonts = payload.get("fonts")
        raw_licenses = payload.get("licenses")
        if not isinstance(raw_fonts, list) or not isinstance(raw_licenses, list):
            raise _FontValidationError("ui_font.manifest_invalid")
        assets = tuple(self._parse_asset(raw) for raw in raw_fonts)
        if tuple((item.font_id, item.display_name) for item in assets) != UI_FONT_OPTIONS:
            raise _FontValidationError("ui_font.manifest_invalid")
        license_records: dict[str, tuple[int, str]] = {}
        for raw in raw_licenses:
            if not isinstance(raw, Mapping):
                raise _FontValidationError("ui_font.manifest_invalid")
            managed_path = raw.get("managed_path")
            size = raw.get("size")
            sha256 = raw.get("sha256")
            if (
                not isinstance(managed_path, str)
                or not managed_path
                or not isinstance(size, int)
                or isinstance(size, bool)
                or size <= 0
                or not isinstance(sha256, str)
                or _SHA256_PATTERN.fullmatch(sha256) is None
                or managed_path in license_records
            ):
                raise _FontValidationError("ui_font.manifest_invalid")
            repository = raw.get("repository")
            commit = raw.get("commit")
            source_path = raw.get("source_path")
            source_url = raw.get("source_url")
            source_page = raw.get("source_page")
            repository_source = (
                isinstance(repository, str)
                and bool(repository.strip())
                and isinstance(commit, str)
                and _COMMIT_PATTERN.fullmatch(commit) is not None
                and isinstance(source_path, str)
                and bool(source_path.strip())
            )
            official_source = (
                isinstance(source_url, str)
                and source_url.startswith("https://")
                and isinstance(source_page, str)
                and source_page.startswith("https://")
            )
            if repository_source == official_source:
                raise _FontValidationError("ui_font.manifest_invalid")
            self._managed_path(managed_path)
            license_records[managed_path] = (size, sha256)
        for managed_path, (size, sha256) in license_records.items():
            self._validate_file(
                self._managed_path(managed_path),
                size,
                sha256,
                missing_code="ui_font.license_missing",
            )
        for asset in assets:
            for evidence in asset.license_evidence:
                if evidence not in license_records:
                    raise _FontValidationError("ui_font.license_missing")
            path = self._managed_path(asset.managed_path)
            self._validate_file(path, asset.size, asset.sha256)
            try:
                family = self._family_reader(path)
            except Exception:
                raise _FontValidationError("ui_font.family_mismatch") from None
            if family != asset.internal_family:
                raise _FontValidationError("ui_font.family_mismatch")
        return assets

    @staticmethod
    def _parse_asset(raw: object) -> UIFontAsset:
        if not isinstance(raw, Mapping):
            raise _FontValidationError("ui_font.manifest_invalid")
        required_strings = (
            "font_id",
            "display_name",
            "repository",
            "commit",
            "source_path",
            "managed_path",
            "sha256",
            "internal_family",
            "ui_family",
        )
        if any(
            not isinstance(raw.get(name), str) or not str(raw.get(name)).strip()
            for name in required_strings
        ):
            raise _FontValidationError("ui_font.manifest_invalid")
        size = raw.get("size")
        evidence = raw.get("license_evidence")
        if (
            not isinstance(size, int)
            or isinstance(size, bool)
            or size <= 0
            or not isinstance(evidence, list)
            or not evidence
            or any(not isinstance(item, str) or not item for item in evidence)
            or _SHA256_PATTERN.fullmatch(str(raw["sha256"])) is None
            or _COMMIT_PATTERN.fullmatch(str(raw["commit"])) is None
        ):
            raise _FontValidationError("ui_font.manifest_invalid")
        return UIFontAsset(
            font_id=str(raw["font_id"]),
            display_name=str(raw["display_name"]),
            repository=str(raw["repository"]),
            commit=str(raw["commit"]),
            source_path=str(raw["source_path"]),
            managed_path=str(raw["managed_path"]),
            size=size,
            sha256=str(raw["sha256"]),
            internal_family=str(raw["internal_family"]),
            ui_family=str(raw["ui_family"]),
            license_evidence=tuple(evidence),
        )

    def _managed_path(self, managed_path: str) -> Path:
        path = (self._asset_root / managed_path).resolve(strict=False)
        try:
            path.relative_to(self._asset_root)
        except ValueError:
            raise _FontValidationError("ui_font.manifest_invalid") from None
        if path == self._asset_root:
            raise _FontValidationError("ui_font.manifest_invalid")
        return path

    @staticmethod
    def _validate_file(
        path: Path,
        size: int,
        sha256: str,
        *,
        missing_code: str = "ui_font.asset_missing",
    ) -> None:
        if not path.is_file():
            raise _FontValidationError(missing_code)
        try:
            if path.stat().st_size != size or _sha256(path) != sha256:
                raise _FontValidationError("ui_font.integrity_failed")
        except OSError:
            raise _FontValidationError("ui_font.integrity_failed") from None
