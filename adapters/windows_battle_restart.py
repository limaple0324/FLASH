"""Close exactly one verified battle-disconnected window and reopen its shortcut."""

from __future__ import annotations

import ctypes
import os
import time
from dataclasses import dataclass
from ctypes import wintypes
from typing import Callable, Iterable, Protocol

from adapters.windows_launch_fingerprint import (
    PowerShellShortcutFingerprintResolver,
    ShortcutFingerprintResolver,
    normalize_launch_fingerprint,
)
from adapters.windows_window import WindowBackend, WindowInfo
from core.smart_reconnect_authorization import ShortcutSeal
from adapters.windows_shortcut_seal import ShortcutSealResolver
from services.group_launch_service import GroupLaunchTarget


WindowInstanceIdentity = tuple[
    int,
    int,
    int,
    str,
    tuple[int, int, int, int],
    bool,
    int,
    str,
]
WindowInstanceIdentityProvider = Callable[
    [int],
    WindowInstanceIdentity | None,
]
TargetAbsenceCheck = Callable[[], str | None]
MutationAuthorizer = Callable[
    [Callable[[], object]],
    tuple[bool, object | None],
]


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
        expected_seal: ShortcutSeal | None = None,
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
        *,
        shortcut_seal_resolver: ShortcutSealResolver | None = None,
    ) -> None:
        self._shortcut_fingerprint_resolver = (
            shortcut_fingerprint_resolver
            or PowerShellShortcutFingerprintResolver()
        )
        if shortcut_seal_resolver is not None and not callable(
            getattr(shortcut_seal_resolver, "revalidate", None)
        ):
            raise TypeError("shortcut_seal_resolver must provide revalidate")
        self._shortcut_seal_resolver = shortcut_seal_resolver

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
        expected_seal: ShortcutSeal | None = None,
    ) -> tuple[bool, str | None]:
        # Keep the last live-window check inside the same backend boundary as
        # os.startfile. The outer stability check alone cannot cover a
        # self-reopen that occurs immediately before shortcut delivery.
        failure_code = absence_check()
        if failure_code is not None:
            return False, failure_code
        failure_code = self._target_seal_failure(target, expected_seal)
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

    def _target_seal_failure(
        self,
        target: GroupLaunchTarget,
        expected_seal: ShortcutSeal | None,
    ) -> str | None:
        resolver = self._shortcut_seal_resolver
        if resolver is None or expected_seal is None:
            return self._target_fingerprint_failure(target)
        expected_path = os.path.normcase(
            os.path.abspath(os.fspath(target.shortcut_path))
        )
        if (
            expected_seal.launch_fingerprint != target.fingerprint
            or expected_seal.file_identity.normalized_path != expected_path
        ):
            return "battle_shortcut_identity_changed"
        try:
            valid = resolver.revalidate(expected_seal) is True
        except Exception:
            valid = False
        return None if valid else "battle_shortcut_identity_changed"


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

    def restart(
        self,
        window: WindowInfo,
        target: GroupLaunchTarget,
        *,
        close_authorizer: MutationAuthorizer | None = None,
        open_authorizer: MutationAuthorizer | None = None,
        expected_shortcut_seal: ShortcutSeal | None = None,
    ) -> BattleRestartResult:
        fingerprint = normalize_launch_fingerprint(window.launch_fingerprint)
        expected_identity = self._window_instance_identity(window)
        if expected_identity is None or fingerprint != target.fingerprint:
            return BattleRestartResult(
                False,
                "battle_window_identity_invalid",
            )

        try:
            candidates = self._live_candidate_windows()
        except Exception:
            return BattleRestartResult(
                False,
                "battle_window_enumeration_failed",
            )
        failure_code = self._candidate_collection_failure(candidates)
        if failure_code is not None:
            return BattleRestartResult(False, failure_code)
        exact = tuple(
            candidate
            for candidate in candidates
            if self._window_instance_identity(candidate) == expected_identity
        )
        if len(exact) != 1:
            return BattleRestartResult(
                False,
                "battle_window_identity_changed",
            )
        if not self._close_backend.is_window(window.handle):
            return BattleRestartResult(
                False,
                "battle_window_missing",
            )
        # IsWindow proves only that an HWND currently exists. Re-enumerate
        # after that probe and immediately before WM_CLOSE so a handle reused
        # by another process/thread/window instance is never closed.
        try:
            final_candidates = self._live_candidate_windows()
        except Exception:
            return BattleRestartResult(
                False,
                "battle_window_enumeration_failed",
            )
        failure_code = self._candidate_collection_failure(final_candidates)
        if failure_code is not None:
            return BattleRestartResult(False, failure_code)
        final_exact = tuple(
            candidate
            for candidate in final_candidates
            if self._window_instance_identity(candidate) == expected_identity
        )
        if len(final_exact) != 1:
            return BattleRestartResult(
                False,
                "battle_window_identity_changed",
            )
        def close_once():
            return self._close_backend.close_window_if_instance_matches(
                window.handle,
                expected_identity,
                self._current_window_instance_identity,
            )

        try:
            close_permitted, close_result = self._authorize_mutation(
                close_authorizer,
                close_once,
            )
            if not close_permitted or not isinstance(close_result, tuple):
                closed, close_failure = False, "battle_window_authorization_changed"
            else:
                closed, close_failure = close_result
        except Exception:
            closed = False
            close_failure = "battle_window_close_failed"
        if not closed:
            return BattleRestartResult(
                False,
                close_failure or "battle_window_close_failed",
            )

        deadline = self._monotonic_clock() + self._close_timeout_seconds
        while self._close_backend.is_window(window.handle):
            if self._monotonic_clock() >= deadline:
                return BattleRestartResult(
                    False,
                    "battle_window_close_timeout",
                )
            self._sleeper(self._poll_seconds)

        # A self-reopening player may need several scheduler turns after
        # WM_CLOSE. Require the target to remain safely absent for a bounded
        # interval before authorizing a new shortcut.
        failure_code = self._stable_target_absence_failure(
            target,
            ignored_closed_handle=window.handle,
        )
        if failure_code is not None:
            return BattleRestartResult(
                False,
                failure_code,
                window_closed=True,
            )
        def open_once():
            absence_check = lambda: self._live_target_failure(
                target,
                ignored_closed_handle=window.handle,
            )
            if expected_shortcut_seal is None:
                return self._open_backend.open_shortcut_if_target_absent(
                    target,
                    absence_check,
                )
            return self._open_backend.open_shortcut_if_target_absent(
                target,
                absence_check,
                expected_shortcut_seal,
            )

        try:
            open_permitted, open_result = self._authorize_mutation(
                open_authorizer,
                open_once,
            )
            if not open_permitted or not isinstance(open_result, tuple):
                opened, open_failure = False, "battle_shortcut_authorization_changed"
            else:
                opened, open_failure = open_result
        except Exception:
            opened = False
            open_failure = "battle_shortcut_open_failed"
        if not opened:
            return BattleRestartResult(
                False,
                open_failure or "battle_shortcut_open_failed",
                window_closed=True,
            )
        return BattleRestartResult(
            True,
            window_closed=True,
            shortcut_open_requested=True,
        )

    @staticmethod
    def _window_instance_identity(
        window: WindowInfo,
    ) -> WindowInstanceIdentity | None:
        fingerprint = normalize_launch_fingerprint(
            window.launch_fingerprint
        )
        if (
            not isinstance(window.handle, int)
            or isinstance(window.handle, bool)
            or window.handle <= 0
            or not isinstance(window.process_id, int)
            or isinstance(window.process_id, bool)
            or window.process_id <= 0
            or not isinstance(window.thread_id, int)
            or isinstance(window.thread_id, bool)
            or window.thread_id <= 0
            or not isinstance(window.window_class, str)
            or not window.window_class.strip()
            or not isinstance(window.process_lifecycle_token, int)
            or isinstance(window.process_lifecycle_token, bool)
            or window.process_lifecycle_token <= 0
            or not isinstance(window.rect, tuple)
            or len(window.rect) != 4
            or any(
                not isinstance(value, int) or isinstance(value, bool)
                for value in window.rect
            )
            or window.rect[2] <= window.rect[0]
            or window.rect[3] <= window.rect[1]
            or type(window.minimized) is not bool
            or fingerprint is None
        ):
            return None
        return (
            window.handle,
            window.process_id,
            window.thread_id,
            window.window_class,
            window.rect,
            window.minimized,
            window.process_lifecycle_token,
            fingerprint,
        )

    @classmethod
    def _candidate_collection_failure(
        cls,
        candidates: tuple[WindowInfo, ...],
    ) -> str | None:
        identities = tuple(
            cls._window_instance_identity(candidate)
            for candidate in candidates
        )
        if any(identity is None for identity in identities):
            return "battle_window_existing_state_unknown"
        complete = tuple(
            identity for identity in identities if identity is not None
        )
        handles = tuple(identity[0] for identity in complete)
        process_ids = tuple(identity[1] for identity in complete)
        fingerprints = tuple(identity[-1] for identity in complete)
        if (
            len(handles) != len(set(handles))
            or len(process_ids) != len(set(process_ids))
            or len(fingerprints) != len(set(fingerprints))
        ):
            return "battle_window_identity_duplicate"
        return None

    def _current_window_instance_identity(
        self,
        handle: int,
    ) -> WindowInstanceIdentity | None:
        try:
            candidates = self._live_candidate_windows()
        except Exception:
            return None
        if self._candidate_collection_failure(candidates) is not None:
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
    ) -> str | None:
        collection_failure = (
            WindowsBattleWindowRestarter._candidate_collection_failure(
                candidates
            )
        )
        if collection_failure is not None:
            return collection_failure
        fingerprints = tuple(
            WindowsBattleWindowRestarter._window_instance_identity(window)[-1]
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
        *,
        ignored_closed_handle: int | None = None,
    ) -> str | None:
        try:
            candidates = tuple(
                candidate
                for candidate in self._live_candidate_windows()
                if (
                    candidate.handle != ignored_closed_handle
                    or self._close_backend.is_window(candidate.handle)
                )
            )
        except Exception:
            return "battle_window_enumeration_failed"
        return self._missing_target_failure(target, candidates)

    def _stable_target_absence_failure(
        self,
        target: GroupLaunchTarget,
        *,
        ignored_closed_handle: int | None = None,
    ) -> str | None:
        deadline = (
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
                ignored_closed_handle=ignored_closed_handle,
            )
            if failure_code is not None:
                return failure_code
            remaining = deadline - self._monotonic_clock()
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
        open_authorizer: MutationAuthorizer | None = None,
        expected_shortcut_seal: ShortcutSeal | None = None,
    ) -> BattleRestartResult:
        """Retry one shortcut only after a fresh fail-closed enumeration."""
        try:
            candidates = tuple(candidate_windows)
        except Exception:
            return BattleRestartResult(
                False,
                "battle_window_enumeration_failed",
            )
        failure_code = self._missing_target_failure(target, candidates)
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
        )
        if failure_code is not None:
            return BattleRestartResult(False, failure_code)

        def open_once():
            if expected_shortcut_seal is None:
                return self._open_backend.open_shortcut_if_target_absent(
                    target,
                    lambda: self._live_target_failure(target),
                )
            return self._open_backend.open_shortcut_if_target_absent(
                target,
                lambda: self._live_target_failure(target),
                expected_shortcut_seal,
            )

        try:
            open_permitted, open_result = self._authorize_mutation(
                open_authorizer,
                open_once,
            )
            if not open_permitted or not isinstance(open_result, tuple):
                opened, open_failure = False, "battle_shortcut_authorization_changed"
            else:
                opened, open_failure = open_result
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

    @staticmethod
    def _authorize_mutation(
        authorizer: MutationAuthorizer | None,
        callback: Callable[[], object],
    ) -> tuple[bool, object | None]:
        if authorizer is None:
            return True, callback()
        return authorizer(callback)
