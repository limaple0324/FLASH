"""Repeatable, read-only target-desktop verification for FLASH SP1.

The verifier intentionally emits aggregate counts only. Window handles,
process IDs, anonymous fingerprints, launcher arguments, and captured pixels
never appear in its report.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Protocol

from adapters.windows_background_capture import WindowsBackgroundCaptureBackend
from adapters.windows_launch_fingerprint import (
    PowerShellLaunchFingerprintResolver,
    normalize_launch_fingerprint,
)
from adapters.windows_window import (
    Win32WindowBackend,
    WindowBackend,
    WindowInfo,
    WindowsWindowAdapter,
)


class BackgroundCaptureProbe(Protocol):
    last_sample: object | None

    def probe_background_capture(self, window_handle: int) -> bool | None:
        """Return whether a read-only capture produced valid non-blank content."""


@dataclass(frozen=True, slots=True)
class TargetDesktopVerification:
    expected_windows: int
    discovered_windows: int
    windows_with_process_id: int
    unique_process_ids: int
    windows_with_fingerprint: int
    unique_fingerprints: int
    individually_selected: int
    unique_selected_windows: int
    captures_passed: int
    minimized_windows: int
    nonforeground_windows: int | None
    minimum_capture_width: int | None
    maximum_capture_width: int | None
    minimum_capture_height: int | None
    maximum_capture_height: int | None
    failure_codes: tuple[str, ...]

    @property
    def passed(self) -> bool:
        return not self.failure_codes

    def to_dict(self) -> dict[str, object]:
        """Return a secret-safe report suitable for JSON output."""
        return {
            "passed": self.passed,
            "expected_windows": self.expected_windows,
            "discovered_windows": self.discovered_windows,
            "windows_with_process_id": self.windows_with_process_id,
            "unique_process_ids": self.unique_process_ids,
            "windows_with_fingerprint": self.windows_with_fingerprint,
            "unique_fingerprints": self.unique_fingerprints,
            "individually_selected": self.individually_selected,
            "unique_selected_windows": self.unique_selected_windows,
            "wrong_window_selections": (
                self.discovered_windows - self.individually_selected
            ),
            "captures_passed": self.captures_passed,
            "minimized_windows": self.minimized_windows,
            "nonforeground_windows": self.nonforeground_windows,
            "capture_width": {
                "minimum": self.minimum_capture_width,
                "maximum": self.maximum_capture_width,
            },
            "capture_height": {
                "minimum": self.minimum_capture_height,
                "maximum": self.maximum_capture_height,
            },
            "failure_codes": list(self.failure_codes),
            "raw_arguments_emitted": False,
            "captured_pixels_persisted": False,
            "input_sent": False,
        }


class _SelectionBackend:
    """Use one real snapshot while isolating identity filtering from focus."""

    def __init__(self, windows: Iterable[WindowInfo], selected_handle: int):
        self._windows = tuple(windows)
        self._selected_handle = selected_handle

    def list_windows(self) -> list[WindowInfo]:
        return list(self._windows)

    def foreground_handle(self) -> int:
        return self._selected_handle

    def top_window_at(self, _x: int, _y: int) -> int:
        return self._selected_handle


class TargetDesktopVerifier:
    """Verify identity and capture for every matching live window without input."""

    def __init__(
        self,
        *,
        expected_windows: int,
        title_keywords: Iterable[str],
        window_backend: WindowBackend,
        capture_backend: BackgroundCaptureProbe,
    ):
        if expected_windows <= 0:
            raise ValueError("expected_windows must be positive")
        self._expected_windows = expected_windows
        self._keywords = tuple(
            keyword.strip().casefold()
            for keyword in title_keywords
            if isinstance(keyword, str) and keyword.strip()
        )
        if not self._keywords:
            raise ValueError("At least one title keyword is required")
        self._window_backend = window_backend
        self._capture_backend = capture_backend

    @classmethod
    def for_real_windows(
        cls,
        *,
        expected_windows: int = 14,
        title_keywords: Iterable[str] = ("Adobe Flash Player",),
    ) -> "TargetDesktopVerifier":
        return cls(
            expected_windows=expected_windows,
            title_keywords=title_keywords,
            window_backend=Win32WindowBackend(
                PowerShellLaunchFingerprintResolver()
            ),
            capture_backend=WindowsBackgroundCaptureBackend(),
        )

    def _matching_windows(self) -> list[WindowInfo]:
        return [
            window
            for window in self._window_backend.list_windows()
            if all(keyword in window.title.casefold() for keyword in self._keywords)
        ]

    def verify(self) -> TargetDesktopVerification:
        windows = self._matching_windows()
        failures: list[str] = []

        if len(windows) != self._expected_windows:
            failures.append("window_count_mismatch")

        process_ids = [
            window.process_id for window in windows if window.process_id is not None
        ]
        if len(process_ids) != len(windows):
            failures.append("process_id_missing")
        if len(set(process_ids)) != len(process_ids):
            failures.append("process_id_duplicate")

        normalized_fingerprints = [
            normalize_launch_fingerprint(window.launch_fingerprint)
            for window in windows
        ]
        fingerprints = [
            fingerprint
            for fingerprint in normalized_fingerprints
            if fingerprint is not None
        ]
        if len(fingerprints) != len(windows):
            failures.append("fingerprint_missing_or_invalid")
        if len(set(fingerprints)) != len(fingerprints):
            failures.append("fingerprint_duplicate")

        selected_handles: list[int] = []
        for window, fingerprint in zip(windows, normalized_fingerprints):
            if fingerprint is None:
                continue
            adapter = WindowsWindowAdapter(
                self._keywords,
                backend=_SelectionBackend(windows, window.handle),
                launch_fingerprint=fingerprint,
            )
            result = adapter.find_target()
            selected_handle = (
                adapter.last_match.handle
                if result.success and adapter.last_match is not None
                else (
                    result.details.get("handle")
                    if result.code == "window.minimized" and result.details
                    else None
                )
            )
            if selected_handle == window.handle:
                selected_handles.append(window.handle)
        if len(selected_handles) != len(windows):
            failures.append("identity_selection_failed")
        if len(set(selected_handles)) != len(selected_handles):
            failures.append("identity_selection_duplicate")

        capture_widths: list[int] = []
        capture_heights: list[int] = []
        captures_passed = 0
        for window in windows:
            try:
                capture_passed = (
                    self._capture_backend.probe_background_capture(window.handle)
                    is True
                )
            except Exception:
                capture_passed = False
            if not capture_passed:
                continue
            captures_passed += 1
            sample = getattr(self._capture_backend, "last_sample", None)
            width = getattr(sample, "width", None)
            height = getattr(sample, "height", None)
            if isinstance(width, int) and width > 0:
                capture_widths.append(width)
            if isinstance(height, int) and height > 0:
                capture_heights.append(height)
        if captures_passed != len(windows):
            failures.append("background_capture_failed")
        if len(capture_widths) != captures_passed or len(capture_heights) != captures_passed:
            failures.append("capture_dimensions_missing")

        foreground_handle = self._window_backend.foreground_handle()
        nonforeground_windows = (
            sum(window.handle != foreground_handle for window in windows)
            if foreground_handle is not None
            else None
        )

        return TargetDesktopVerification(
            expected_windows=self._expected_windows,
            discovered_windows=len(windows),
            windows_with_process_id=len(process_ids),
            unique_process_ids=len(set(process_ids)),
            windows_with_fingerprint=len(fingerprints),
            unique_fingerprints=len(set(fingerprints)),
            individually_selected=len(selected_handles),
            unique_selected_windows=len(set(selected_handles)),
            captures_passed=captures_passed,
            minimized_windows=sum(window.minimized for window in windows),
            nonforeground_windows=nonforeground_windows,
            minimum_capture_width=min(capture_widths) if capture_widths else None,
            maximum_capture_width=max(capture_widths) if capture_widths else None,
            minimum_capture_height=min(capture_heights) if capture_heights else None,
            maximum_capture_height=max(capture_heights) if capture_heights else None,
            failure_codes=tuple(dict.fromkeys(failures)),
        )
