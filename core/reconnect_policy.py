"""Confirmed SP1 reconnect policy for the observed game login flow."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ReconnectScreenState(str, Enum):
    CONNECTED = "connected"
    DISCONNECTED = "disconnected"
    LOGIN_START = "login_start"
    FORCE_LOGIN_START = "force_login_start"
    FORCE_LOGIN_TIMEOUT = "force_login_timeout"
    LINE_SELECTION = "line_selection"
    CHARACTER_SELECTION = "character_selection"
    POST_LOGIN_ACTIVITY = "post_login_activity"
    POST_LOGIN_RECOMMENDATION = "post_login_recommendation"
    POST_LOGIN_AUTO_DUNGEON = "post_login_auto_dungeon"
    RECONNECTING = "reconnecting"
    FAILED = "failed"
    CHECK_DISABLED = "check_disabled"
    UNKNOWN = "unknown"


class ReconnectAction(str, Enum):
    RESUME = "resume"
    CONFIRM_DISCONNECT = "confirm_disconnect"
    START_GAME = "start_game"
    FORCE_LOGIN = "force_login"
    CONFIRM_FORCE_LOGIN_TIMEOUT = "confirm_force_login_timeout"
    SELECT_DEFAULT_LINE = "select_default_line"
    ENTER_GAME = "enter_game"
    WAIT_AND_RECHECK = "wait_and_recheck"
    CLOSE_ANNOUNCEMENT = "close_announcement"
    OBSERVE_ONLY = "observe_only"


@dataclass(frozen=True, slots=True)
class ReconnectDecision:
    action: ReconnectAction
    delay_seconds: int
    attempt_limit: int | None


@dataclass(frozen=True, slots=True)
class ReconnectPolicy:
    """User-confirmed flow, timing, and unlimited retry behavior."""

    retry_interval_seconds: int = 60
    progress_interval_seconds: int = 2
    disconnect_confirmation_wait_seconds: int = 5
    force_login_wait_seconds: int = 10
    line_transition_wait_seconds: int = 3
    entry_transition_wait_seconds: int = 10
    announcement_transition_wait_seconds: int = 5
    connected_poll_seconds: int = 5
    maximum_attempts: int | None = None
    requires_reconnect_button: bool = False
    announcement_close_allowed: bool = True

    def __post_init__(self) -> None:
        if self.retry_interval_seconds <= 0:
            raise ValueError("retry_interval_seconds must be positive")
        if self.progress_interval_seconds <= 0:
            raise ValueError("progress_interval_seconds must be positive")
        if self.disconnect_confirmation_wait_seconds <= 0:
            raise ValueError(
                "disconnect_confirmation_wait_seconds must be positive"
            )
        if self.force_login_wait_seconds <= 0:
            raise ValueError("force_login_wait_seconds must be positive")
        if self.line_transition_wait_seconds <= 0:
            raise ValueError("line_transition_wait_seconds must be positive")
        if self.entry_transition_wait_seconds <= 0:
            raise ValueError("entry_transition_wait_seconds must be positive")
        if self.announcement_transition_wait_seconds <= 0:
            raise ValueError(
                "announcement_transition_wait_seconds must be positive"
            )
        if self.connected_poll_seconds <= 0:
            raise ValueError("connected_poll_seconds must be positive")
        if self.maximum_attempts is not None and self.maximum_attempts <= 0:
            raise ValueError("maximum_attempts must be positive or None")

    def decide(self, state: ReconnectScreenState) -> ReconnectDecision:
        if state is ReconnectScreenState.CONNECTED:
            return ReconnectDecision(
                action=ReconnectAction.RESUME,
                delay_seconds=self.connected_poll_seconds,
                attempt_limit=self.maximum_attempts,
            )
        if state is ReconnectScreenState.DISCONNECTED:
            return ReconnectDecision(
                action=ReconnectAction.CONFIRM_DISCONNECT,
                delay_seconds=self.disconnect_confirmation_wait_seconds,
                attempt_limit=self.maximum_attempts,
            )
        if state is ReconnectScreenState.LOGIN_START:
            return ReconnectDecision(
                action=ReconnectAction.START_GAME,
                delay_seconds=self.entry_transition_wait_seconds,
                attempt_limit=self.maximum_attempts,
            )
        if state is ReconnectScreenState.FORCE_LOGIN_START:
            return ReconnectDecision(
                action=ReconnectAction.FORCE_LOGIN,
                delay_seconds=self.force_login_wait_seconds,
                attempt_limit=self.maximum_attempts,
            )
        if state is ReconnectScreenState.FORCE_LOGIN_TIMEOUT:
            return ReconnectDecision(
                action=ReconnectAction.CONFIRM_FORCE_LOGIN_TIMEOUT,
                delay_seconds=self.retry_interval_seconds,
                attempt_limit=self.maximum_attempts,
            )
        if state is ReconnectScreenState.LINE_SELECTION:
            return ReconnectDecision(
                action=ReconnectAction.SELECT_DEFAULT_LINE,
                delay_seconds=self.line_transition_wait_seconds,
                attempt_limit=self.maximum_attempts,
            )
        if state is ReconnectScreenState.CHARACTER_SELECTION:
            return ReconnectDecision(
                action=ReconnectAction.ENTER_GAME,
                delay_seconds=self.entry_transition_wait_seconds,
                attempt_limit=self.maximum_attempts,
            )
        if state in {
            ReconnectScreenState.POST_LOGIN_ACTIVITY,
            ReconnectScreenState.POST_LOGIN_RECOMMENDATION,
            ReconnectScreenState.POST_LOGIN_AUTO_DUNGEON,
        } and self.announcement_close_allowed:
            return ReconnectDecision(
                action=ReconnectAction.CLOSE_ANNOUNCEMENT,
                delay_seconds=self.announcement_transition_wait_seconds,
                attempt_limit=self.maximum_attempts,
            )
        if state is ReconnectScreenState.RECONNECTING:
            return ReconnectDecision(
                action=ReconnectAction.WAIT_AND_RECHECK,
                delay_seconds=self.force_login_wait_seconds,
                attempt_limit=self.maximum_attempts,
            )
        if state is ReconnectScreenState.FAILED:
            return ReconnectDecision(
                action=ReconnectAction.WAIT_AND_RECHECK,
                delay_seconds=self.retry_interval_seconds,
                attempt_limit=self.maximum_attempts,
            )
        return ReconnectDecision(
            action=ReconnectAction.OBSERVE_ONLY,
            delay_seconds=self.retry_interval_seconds,
            attempt_limit=self.maximum_attempts,
        )
