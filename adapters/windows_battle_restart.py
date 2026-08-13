"""Close exactly one verified battle-disconnected window and reopen its shortcut."""

from __future__ import annotations

import ctypes
import os
import time
from collections import Counter
from dataclasses import dataclass
from ctypes import wintypes
from typing import Callable, Iterable, Protocol

from adapters.windows_launch_fingerprint import (
    PowerShellShortcutFingerprintResolver,
    ShortcutFingerprintResolver,
    normalize_launch_fingerprint,
)
from adapters.windows_window import (
    WindowBackend,
    WindowInfo,
    complete_window_instance_identity,
)
from services.group_launch_service import GroupLaunchTarget


WindowInstanceIdentity = tuple[object, ...]
WindowInstanceIdentityProvider = Callable[
    [int],
    WindowInstanceIdentity | None,
]
TargetAbsenceCheck = Callable[[], str | None]


class WindowCloseBackend(Protocol):
    def is_window(self, handle: int) -> bool:
        """Return whether the exact top-level HWND still exists."""

    def close_window(self, handle: int) -> bool:
        """Legacy exact-HWND close used outside battle reconnect."""

    def close_window_if_instance_matches(
        self,
        handle: int,
        expected_identity: WindowInstanceIdentity,
        current_identity: WindowInstanceIdentityProvider,
    ) -> tuple[bool, str | None]:
        """Close only when the complete identity still matches at delivery."""


class ShortcutOpenBackend(Protocol):
    def open_shortcut(self, target: GroupLaunchTarget) -> bool:
        """Legacy validated shortcut open used outside battle reconnect."""

    def open_shortcut_if_target_absent(
        self,
        target: GroupLaunchTarget,
        absence_check: TargetAbsenceCheck,
    ) -> tuple[bool, str | None]:
        """Open only when target absence is rechecked at delivery."""


@dataclass(frozen=True, slots=True)
class BattleRestartResult:
    success: bool
    failure_code: str | None = None
    window_closed: bool = False
    shortcut_open_requested: bool = False


class Win32WindowCloseBackend:
    WM_CLOSE = 0x0010

    @staticmethod
    def _user32():
        if os.name != "nt":
            return None
        return ctypes.windll.user32

    @staticmethod
    def _configure(user32) -> None:
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsWindow.restype = wintypes.BOOL
        user32.PostMessageW.argtypes = (
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        )
        user32.PostMessageW.restype = wintypes.BOOL

    def is_window(self, handle: int) -> bool:
        user32 = self._user32()
        if user32 is None:
            return False
        self._configure(user32)
        return bool(user32.IsWindow(wintypes.HWND(handle)))

    def close_window(self, handle: int) -> bool:
        user32 = self._user32()
        if user32 is None:
            return False
        self._configure(user32)
        hwnd = wintypes.HWND(handle)
        if not user32.IsWindow(hwnd):
            return False
        return bool(user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0))

    def close_window_if_instance_matches(
        self,
        handle: int,
        expected_identity: WindowInstanceIdentity,
        current_identity: WindowInstanceIdentityProvider,
    ) -> tuple[bool, str | None]:
        user32 = self._user32()
        if user32 is None:
            return False, "battle_window_close_failed"
        self._configure(user32)
        hwnd = wintypes.HWND(handle)
        if not user32.IsWindow(hwnd):
            return False, "battle_window_missing"
        # The full identity check belongs inside the close backend so there is
        # no caller/backend gap in which a reused HWND can silently inherit the
        # already-authorized WM_CLOSE.
        if current_identity(handle) != expected_identity:
            return False, "battle_window_identity_changed"
        if not user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0):
            return False, "battle_window_close_failed"
        return True, None


class WindowsShortcutOpenBackend:
    def __init__(
        self,
        shortcut_fingerprint_resolver: ShortcutFingerprintResolver | None = None,
    ) -> None:
        self._shortcut_fingerprint_resolver = (
            shortcut_fingerprint_resolver
            or PowerShellShortcutFingerprintResolver()
        )

    def _target_fingerprint_failure(
        self,
        target: GroupLaunchTarget,
    ) -> str | None:
        expected = normalize_launch_fingerprint(target.fingerprint)
        if expected is None or not target.shortcut_path.is_file():
            return "battle_shortcut_identity_unresolved"
        try:
            resolved = self._shortcut_fingerprint_resolver.resolve(
                (target.shortcut_path,)
            )
        except Exception:
            return "battle_shortcut_identity_unresolved"
        if set(resolved) != {target.shortcut_path}:
            return "battle_shortcut_identity_unresolved"
        actual = normalize_launch_fingerprint(
            resolved.get(target.shortcut_path)
        )
        if actual != expected:
            return "battle_shortcut_identity_changed"
        return None

    def open_shortcut(self, target: GroupLaunchTarget) -> bool:
        if (
            os.name != "nt"
            or self._target_fingerprint_failure(target) is not None
        ):
            return False
        try:
            os.startfile(str(target.shortcut_path))  # type: ignore[attr-defined]
            return True
        except OSError:
            return False

    def open_shortcut_if_target_absent(
        self,
        target: GroupLaunchTarget,
        absence_check: TargetAbsenceCheck,
    ) -> tuple[bool, str | None]:
        # Keep the last live-window check inside the same backend boundary as
        # os.startfile. The outer stability check alone cannot cover a
        # self-reopen that occurs immediately before shortcut delivery.
        failure_code = absence_check()
        if failure_code is not None:
            return False, failure_code
        failure_code = self._target_fingerprint_failure(target)
        if failure_code is not None:
            return False, failure_code
        # Resolving the shortcut can take long enough for the game target to
        # reopen.  This second check is deliberately after the final identity
        # resolution and immediately before delivery; do not call
        # ``open_shortcut`` here because it would resolve again and create a
        # new check-to-open gap.
        failure_code = absence_check()
        if failure_code is not None:
            return False, failure_code
        if os.name != "nt":
            return False, "battle_shortcut_open_failed"
        try:
            os.startfile(str(target.shortcut_path))  # type: ignore[attr-defined]
        except OSError:
            return False, "battle_shortcut_open_failed"
        return True, None


class WindowsBattleWindowRestarter:
    """Fail closed unless the complete window instance and shortcut agree."""

    def __init__(
        self,
        window_backend: WindowBackend,
        close_backend: WindowCloseBackend,
        open_backend: ShortcutOpenBackend,
        *,
        title_keywords: Iterable[str] = ("Adobe Flash Player",),
        close_timeout_seconds: float = 10.0,
        poll_seconds: float = 0.1,
        absence_stability_seconds: float = 1.0,
        monotonic_clock: Callable[[], float] = time.monotonic,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if (
            close_timeout_seconds <= 0
            or poll_seconds <= 0
            or absence_stability_seconds <= 0
        ):
            raise ValueError("timeouts must be positive.")
        self._title_keywords = tuple(
            keyword.strip().casefold()
            for keyword in title_keywords
            if isinstance(keyword, str) and keyword.strip()
        )
        if not self._title_keywords:
            raise ValueError("title_keywords must not be empty.")
        self._window_backend = window_backend
        self._close_backend = close_backend
        self._open_backend = open_backend
        self._close_timeout_seconds = float(close_timeout_seconds)
        self._poll_seconds = float(poll_seconds)
        self._absence_stability_seconds = float(
            absence_stability_seconds
        )
        self._monotonic_clock = monotonic_clock
        self._sleeper = sleeper

    def close_verified(
        self,
        window: WindowInfo,
        candidate_windows: Iterable[WindowInfo],
        *,
        deadline: float | None = None,
    ) -> BattleRestartResult:
        """Close one exact member of a static, current contract collection.

        The controller owns the semantic before/after contract transition.  At
        this native boundary we only accept the exact complete collection it
        already resolved, then re-enumerate immediately before WM_CLOSE.
        """

        expected_identity = self._window_instance_identity(window)
        if expected_identity is None:
            return BattleRestartResult(False, "battle_window_identity_invalid")
        try:
            expected_candidates = tuple(candidate_windows)
        except Exception:
            return BattleRestartResult(False, "battle_window_enumeration_failed")
        if self._candidate_collection_failure(
            expected_candidates,
            expected_candidates,
        ) is not None:
            return BattleRestartResult(False, "battle_contract_identity_changed")
        if not self._deadline_current(deadline):
            return BattleRestartResult(False, "tcp_reconnect_timeout")
        try:
            candidates = self._live_candidate_windows()
        except Exception:
            return BattleRestartResult(False, "battle_window_enumeration_failed")
        failure_code = self._candidate_collection_failure(
            candidates,
            expected_candidates,
        )
        if failure_code is not None:
            return BattleRestartResult(False, failure_code)
        if not self._identity_occurs_once(expected_identity, candidates):
            return BattleRestartResult(False, "battle_window_identity_changed")
        if not self._close_backend.is_window(window.handle):
            return BattleRestartResult(False, "battle_window_missing")
        try:
            final_candidates = self._live_candidate_windows()
        except Exception:
            return BattleRestartResult(False, "battle_window_enumeration_failed")
        failure_code = self._candidate_collection_failure(
            final_candidates,
            expected_candidates,
        )
        if failure_code is not None:
            return BattleRestartResult(False, failure_code)
        if not self._identity_occurs_once(expected_identity, final_candidates):
            return BattleRestartResult(False, "battle_window_identity_changed")
        if not self._deadline_current(deadline):
            return BattleRestartResult(False, "tcp_reconnect_timeout")
        try:
            closed, close_failure = (
                self._close_backend.close_window_if_instance_matches(
                    window.handle,
                    expected_identity,
                    lambda handle: self._current_window_instance_identity(
                        handle,
                        expected_candidates,
                    ),
                )
            )
        except Exception:
            closed = False
            close_failure = "battle_window_close_failed"
        if not closed:
            return BattleRestartResult(
                False,
                close_failure or "battle_window_close_failed",
            )

        close_deadline = self._monotonic_clock() + self._close_timeout_seconds
        while self._close_backend.is_window(window.handle):
            if not self._deadline_current(deadline):
                return BattleRestartResult(
                    False,
                    "tcp_reconnect_timeout",
                    window_closed=True,
                )
            if self._monotonic_clock() >= close_deadline:
                return BattleRestartResult(
                    False,
                    "battle_window_close_timeout",
                    window_closed=True,
                )
            self._sleeper(self._poll_seconds)
        return BattleRestartResult(True, window_closed=True)

    @staticmethod
    def _window_instance_identity(
        window: WindowInfo,
    ) -> WindowInstanceIdentity | None:
        return complete_window_instance_identity(window)

    def _deadline_current(self, deadline: float | None) -> bool:
        return deadline is None or self._monotonic_clock() < deadline

    @classmethod
    def _identity_occurs_once(
        cls,
        identity: WindowInstanceIdentity,
        candidates: Iterable[WindowInfo],
    ) -> bool:
        return sum(
            cls._window_instance_identity(candidate) == identity
            for candidate in candidates
        ) == 1

    @classmethod
    def _candidate_collection_failure(
        cls,
        candidates: tuple[WindowInfo, ...],
        expected_candidates: tuple[WindowInfo, ...],
    ) -> str | None:
        """Require a full immutable collection, not a fingerprint allowlist."""

        actual_identities = tuple(
            cls._window_instance_identity(candidate) for candidate in candidates
        )
        expected_identities = tuple(
            cls._window_instance_identity(candidate)
            for candidate in expected_candidates
        )
        if (
            any(identity is None for identity in actual_identities)
            or any(identity is None for identity in expected_identities)
        ):
            return "battle_window_existing_state_unknown"
        actual = tuple(
            identity for identity in actual_identities if identity is not None
        )
        expected = tuple(
            identity
            for identity in expected_identities
            if identity is not None
        )
        for identities in (actual, expected):
            handles = tuple(identity[1] for identity in identities)
            process_ids = tuple(identity[2] for identity in identities)
            stable_tokens = tuple(identity[:6] for identity in identities)
            if (
                len(handles) != len(set(handles))
                or len(process_ids) != len(set(process_ids))
                or len(stable_tokens) != len(set(stable_tokens))
            ):
                return "battle_window_identity_duplicate"
        if Counter(actual) != Counter(expected):
            return "battle_contract_identity_changed"
        return None

    def _current_window_instance_identity(
        self,
        handle: int,
        expected_candidates: tuple[WindowInfo, ...],
    ) -> WindowInstanceIdentity | None:
        try:
            candidates = self._live_candidate_windows()
        except Exception:
            return None
        if self._candidate_collection_failure(
            candidates,
            expected_candidates,
        ) is not None:
            return None
        exact = tuple(
            candidate
            for candidate in candidates
            if candidate.handle == handle
        )
        if len(exact) != 1:
            return None
        return self._window_instance_identity(exact[0])

    @staticmethod
    def _missing_target_failure(
        target: GroupLaunchTarget,
        candidates: tuple[WindowInfo, ...],
        expected_candidates: tuple[WindowInfo, ...],
    ) -> str | None:
        collection_failure = (
            WindowsBattleWindowRestarter._candidate_collection_failure(
                candidates,
                expected_candidates,
            )
        )
        if collection_failure is not None:
            return collection_failure
        fingerprints = tuple(
            WindowsBattleWindowRestarter._window_instance_identity(window)[0]
            for window in candidates
        )
        if target.fingerprint in fingerprints:
            return "battle_window_already_exists"
        return None

    def _live_candidate_windows(self) -> tuple[WindowInfo, ...]:
        return tuple(
            window
            for window in self._window_backend.list_windows()
            if all(
                keyword in window.title.casefold()
                for keyword in self._title_keywords
            )
        )

    def _live_target_failure(
        self,
        target: GroupLaunchTarget,
        expected_candidates: tuple[WindowInfo, ...],
        deadline: float | None,
    ) -> str | None:
        if not self._deadline_current(deadline):
            return "tcp_reconnect_timeout"
        try:
            candidates = self._live_candidate_windows()
        except Exception:
            return "battle_window_enumeration_failed"
        return self._missing_target_failure(
            target,
            candidates,
            expected_candidates,
        )

    def _stable_target_absence_failure(
        self,
        target: GroupLaunchTarget,
        expected_candidates: tuple[WindowInfo, ...],
        owner_deadline: float | None,
    ) -> str | None:
        stability_deadline = (
            self._monotonic_clock() + self._absence_stability_seconds
        )
        maximum_checks = max(
            2,
            int(
                self._absence_stability_seconds
                / self._poll_seconds
            )
            + 3,
        )
        for _check in range(maximum_checks):
            failure_code = self._live_target_failure(
                target,
                expected_candidates,
                owner_deadline,
            )
            if failure_code is not None:
                return failure_code
            remaining = stability_deadline - self._monotonic_clock()
            if remaining <= 0:
                return None
            self._sleeper(min(self._poll_seconds, remaining))
        # A stalled or invalid monotonic clock must fail closed rather than
        # turning the stability confirmation into an unbounded loop.
        return "battle_window_absence_unconfirmed"

    def reopen_missing(
        self,
        target: GroupLaunchTarget,
        candidate_windows: Iterable[WindowInfo],
        *,
        deadline: float | None = None,
    ) -> BattleRestartResult:
        """Retry one shortcut only after a fresh fail-closed enumeration."""
        try:
            candidates = tuple(candidate_windows)
        except Exception:
            return BattleRestartResult(
                False,
                "battle_window_enumeration_failed",
            )
        if not self._deadline_current(deadline):
            return BattleRestartResult(False, "tcp_reconnect_timeout")
        failure_code = self._missing_target_failure(target, candidates, candidates)
        if failure_code is not None:
            return BattleRestartResult(
                False,
                failure_code,
            )

        # The caller holds the shared exclusive game-operation lease for this
        # whole method. A bounded stable-absence window catches delayed
        # self-reopens; the opener performs one more check at delivery.
        failure_code = self._stable_target_absence_failure(
            target,
            candidates,
            deadline,
        )
        if failure_code is not None:
            return BattleRestartResult(False, failure_code)

        try:
            opened, open_failure = (
                self._open_backend.open_shortcut_if_target_absent(
                    target,
                    lambda: self._live_target_failure(
                        target,
                        candidates,
                        deadline,
                    ),
                )
            )
        except Exception:
            opened = False
            open_failure = "battle_shortcut_open_failed"
        if not opened:
            return BattleRestartResult(
                False,
                open_failure or "battle_shortcut_open_failed",
            )
        return BattleRestartResult(
            True,
            shortcut_open_requested=True,
        )
