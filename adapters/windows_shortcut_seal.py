"""Resolve an all-or-nothing Windows shortcut seal without opening the game."""

from __future__ import annotations

import ctypes
import hashlib
import os
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol

from adapters.windows_launch_fingerprint import ShortcutFingerprintResolver
from core.smart_reconnect_authorization import ShortcutFileIdentity, ShortcutSeal


class ShortcutSealResolutionError(RuntimeError):
    """Raised when any part of a shortcut seal cannot be proved."""


class ShortcutFileIdentityProvider(Protocol):
    def identity_for(self, path: Path) -> ShortcutFileIdentity:
        """Return the Windows volume and file-index identity for one path."""


class ShortcutSealResolver(Protocol):
    def resolve(self, paths: Iterable[Path]) -> Mapping[Path, ShortcutSeal]:
        """Resolve every requested path or raise without returning a partial batch."""

    def revalidate(self, expected: ShortcutSeal) -> bool:
        """Compare one saved seal at a future real reopen boundary."""


class _BY_HANDLE_FILE_INFORMATION(ctypes.Structure):
    _fields_ = (
        ("dwFileAttributes", wintypes.DWORD),
        ("ftCreationTime", wintypes.FILETIME),
        ("ftLastAccessTime", wintypes.FILETIME),
        ("ftLastWriteTime", wintypes.FILETIME),
        ("dwVolumeSerialNumber", wintypes.DWORD),
        ("nFileSizeHigh", wintypes.DWORD),
        ("nFileSizeLow", wintypes.DWORD),
        ("nNumberOfLinks", wintypes.DWORD),
        ("nFileIndexHigh", wintypes.DWORD),
        ("nFileIndexLow", wintypes.DWORD),
    )


class Win32ShortcutFileIdentityProvider:
    """Read stable file identity from an open Windows handle."""

    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x00000080

    def __init__(self, *, kernel32=None) -> None:
        self._kernel32 = kernel32

    def identity_for(self, path: Path) -> ShortcutFileIdentity:
        normalized = _normalized_shortcut_path(path, require_exists=True)
        kernel32 = self._kernel32
        if kernel32 is None:
            if os.name != "nt":
                raise ShortcutSealResolutionError(
                    "Windows file identity is unavailable on this platform"
                )
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = (
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        )
        create_file.restype = wintypes.HANDLE
        get_information = kernel32.GetFileInformationByHandle
        get_information.argtypes = (
            wintypes.HANDLE,
            ctypes.POINTER(_BY_HANDLE_FILE_INFORMATION),
        )
        get_information.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL
        handle = create_file(
            str(normalized),
            0,
            self._FILE_SHARE_READ
            | self._FILE_SHARE_WRITE
            | self._FILE_SHARE_DELETE,
            None,
            self._OPEN_EXISTING,
            self._FILE_ATTRIBUTE_NORMAL,
            None,
        )
        invalid_handle = ctypes.c_void_p(-1).value
        handle_value = getattr(handle, "value", handle)
        if handle_value in (None, 0, invalid_handle):
            raise ShortcutSealResolutionError("shortcut file handle is unavailable")
        information = _BY_HANDLE_FILE_INFORMATION()
        try:
            if not get_information(handle, ctypes.byref(information)):
                raise ShortcutSealResolutionError(
                    "shortcut Windows file identity is unavailable"
                )
        finally:
            close_handle(handle)
        file_index = (
            int(information.nFileIndexHigh) << 32
        ) | int(information.nFileIndexLow)
        return ShortcutFileIdentity(
            normalized_path=str(normalized),
            volume_serial_number=int(information.dwVolumeSerialNumber),
            file_index=file_index,
        )


class WindowsShortcutSealResolver:
    """Bind path, Windows file identity, bytes, and launch fingerprint."""

    def __init__(
        self,
        fingerprint_resolver: ShortcutFingerprintResolver,
        *,
        file_identity_provider: ShortcutFileIdentityProvider | None = None,
        content_reader: Callable[[Path], bytes] | None = None,
    ) -> None:
        if not callable(getattr(fingerprint_resolver, "resolve", None)):
            raise TypeError("fingerprint_resolver must provide resolve")
        provider = file_identity_provider or Win32ShortcutFileIdentityProvider()
        if not callable(getattr(provider, "identity_for", None)):
            raise TypeError("file_identity_provider must provide identity_for")
        if content_reader is not None and not callable(content_reader):
            raise TypeError("content_reader must be callable")
        self._fingerprint_resolver = fingerprint_resolver
        self._file_identity_provider = provider
        self._content_reader = content_reader or Path.read_bytes

    def resolve(self, paths: Iterable[Path]) -> dict[Path, ShortcutSeal]:
        try:
            normalized_paths = tuple(
                dict.fromkeys(
                    _normalized_shortcut_path(path, require_exists=True)
                    for path in paths
                )
            )
        except (OSError, TypeError, ValueError) as error:
            raise ShortcutSealResolutionError(
                "shortcut path normalization failed"
            ) from error
        if not normalized_paths:
            raise ShortcutSealResolutionError("shortcut seal batch must not be empty")
        try:
            raw_fingerprints = self._fingerprint_resolver.resolve(normalized_paths)
        except (OSError, RuntimeError, TypeError, ValueError) as error:
            raise ShortcutSealResolutionError(
                "shortcut launch fingerprint resolution failed"
            ) from error
        if not isinstance(raw_fingerprints, Mapping):
            raise ShortcutSealResolutionError(
                "shortcut launch fingerprint result is invalid"
            )
        fingerprints = {
            _normalized_shortcut_path(path, require_exists=False): value
            for path, value in raw_fingerprints.items()
            if isinstance(path, Path)
        }
        resolved: dict[Path, ShortcutSeal] = {}
        try:
            for path in normalized_paths:
                file_identity = self._file_identity_provider.identity_for(path)
                if file_identity.normalized_path != str(path):
                    raise ShortcutSealResolutionError(
                        "shortcut path and Windows file identity disagree"
                    )
                content = self._content_reader(path)
                if not isinstance(content, bytes):
                    raise ShortcutSealResolutionError(
                        "shortcut content reader must return bytes"
                    )
                resolved[path] = ShortcutSeal(
                    file_identity=file_identity,
                    content_sha256=hashlib.sha256(content).hexdigest(),
                    launch_fingerprint=fingerprints.get(path),
                )
        except ShortcutSealResolutionError:
            raise
        except (OSError, TypeError, ValueError) as error:
            raise ShortcutSealResolutionError(
                "shortcut seal evidence is incomplete"
            ) from error
        if len(resolved) != len(normalized_paths):
            raise ShortcutSealResolutionError("shortcut seal batch is incomplete")
        return resolved

    @staticmethod
    def compare(expected: ShortcutSeal, current: ShortcutSeal) -> bool:
        if not isinstance(expected, ShortcutSeal) or not isinstance(
            current, ShortcutSeal
        ):
            return False
        return expected == current

    def revalidate(self, expected: ShortcutSeal) -> bool:
        if not isinstance(expected, ShortcutSeal):
            return False
        try:
            current = self.resolve((Path(expected.file_identity.normalized_path),))
        except ShortcutSealResolutionError:
            return False
        return self.compare(
            expected,
            current.get(Path(expected.file_identity.normalized_path)),
        )


def _normalized_shortcut_path(path: object, *, require_exists: bool) -> Path:
    if not isinstance(path, (str, os.PathLike)):
        raise TypeError("shortcut path must be path-like")
    candidate = Path(path)
    if candidate.suffix.casefold() != ".lnk":
        raise ValueError("shortcut path must end with .lnk")
    resolved = candidate.resolve(strict=require_exists)
    normalized = Path(os.path.normcase(os.path.abspath(os.fspath(resolved))))
    if not normalized.is_absolute():
        raise ValueError("shortcut path must be absolute")
    if require_exists and not normalized.is_file():
        raise FileNotFoundError(normalized)
    return normalized


__all__ = [
    "ShortcutFileIdentityProvider",
    "ShortcutSealResolutionError",
    "ShortcutSealResolver",
    "Win32ShortcutFileIdentityProvider",
    "WindowsShortcutSealResolver",
]
