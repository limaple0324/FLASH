import pytest

from core.reconnect_policy import (
    ReconnectAction,
    ReconnectPolicy,
    ReconnectScreenState,
)


def test_confirmed_policy_uses_one_minute_unlimited_retry_without_button():
    policy = ReconnectPolicy()

    assert policy.retry_interval_seconds == 60
    assert policy.progress_interval_seconds == 2
    assert policy.disconnect_confirmation_wait_seconds == 5
    assert policy.force_login_wait_seconds == 10
    assert policy.line_transition_wait_seconds == 3
    assert policy.entry_transition_wait_seconds == 10
    assert policy.announcement_transition_wait_seconds == 5
    assert policy.connected_poll_seconds == 5
    assert policy.maximum_attempts is None
    assert policy.announcement_close_allowed is True


@pytest.mark.parametrize(
    ("state", "action"),
    (
        (
            ReconnectScreenState.DISCONNECTED,
            ReconnectAction.CONFIRM_DISCONNECT,
        ),
        (ReconnectScreenState.LOGIN_START, ReconnectAction.START_GAME),
        (
            ReconnectScreenState.FORCE_LOGIN_START,
            ReconnectAction.FORCE_LOGIN,
        ),
        (
            ReconnectScreenState.FORCE_LOGIN_TIMEOUT,
            ReconnectAction.CONFIRM_FORCE_LOGIN_TIMEOUT,
        ),
        (
            ReconnectScreenState.LINE_SELECTION,
            ReconnectAction.SELECT_DEFAULT_LINE,
        ),
        (
            ReconnectScreenState.CHARACTER_SELECTION,
            ReconnectAction.ENTER_GAME,
        ),
    ),
)
def test_known_reconnect_screen_advances_after_short_progress_delay(state, action):
    decision = ReconnectPolicy().decide(state)

    assert decision.action is action
    expected_delay = {
        ReconnectScreenState.DISCONNECTED: 5,
        ReconnectScreenState.LOGIN_START: 10,
        ReconnectScreenState.FORCE_LOGIN_START: 10,
        ReconnectScreenState.FORCE_LOGIN_TIMEOUT: 60,
        ReconnectScreenState.LINE_SELECTION: 3,
        ReconnectScreenState.CHARACTER_SELECTION: 10,
    }[state]
    assert decision.delay_seconds == expected_delay
    assert decision.attempt_limit is None


def test_failed_screen_waits_one_minute_then_rechecks():
    decision = ReconnectPolicy().decide(ReconnectScreenState.FAILED)

    assert decision.action is ReconnectAction.WAIT_AND_RECHECK
    assert decision.delay_seconds == 60
    assert decision.attempt_limit is None


def test_loading_screen_rechecks_without_waiting_a_full_retry_interval():
    decision = ReconnectPolicy().decide(ReconnectScreenState.RECONNECTING)

    assert decision.action is ReconnectAction.WAIT_AND_RECHECK
    assert decision.delay_seconds == 10


def test_connected_screen_resumes_and_keeps_monitoring():
    decision = ReconnectPolicy().decide(ReconnectScreenState.CONNECTED)

    assert decision.action is ReconnectAction.RESUME
    assert decision.delay_seconds == 5


@pytest.mark.parametrize(
    "state",
    (
        ReconnectScreenState.POST_LOGIN_ACTIVITY,
        ReconnectScreenState.POST_LOGIN_RECOMMENDATION,
        ReconnectScreenState.POST_LOGIN_AUTO_DUNGEON,
    ),
)
def test_known_post_login_windows_allow_only_the_confirmed_close_action(state):
    decision = ReconnectPolicy().decide(state)

    assert decision.action is ReconnectAction.CLOSE_ANNOUNCEMENT
    assert decision.delay_seconds == 5


def test_unknown_screen_does_not_guess_or_click():
    decision = ReconnectPolicy().decide(ReconnectScreenState.UNKNOWN)

    assert decision.action is ReconnectAction.OBSERVE_ONLY
    assert decision.delay_seconds == 60


def test_disabled_screen_check_is_observe_only_and_never_clicks():
    decision = ReconnectPolicy().decide(
        ReconnectScreenState.CHECK_DISABLED
    )

    assert decision.action is ReconnectAction.OBSERVE_ONLY
    assert decision.delay_seconds == 60


def test_policy_rejects_invalid_retry_values():
    with pytest.raises(ValueError):
        ReconnectPolicy(retry_interval_seconds=0)
    with pytest.raises(ValueError):
        ReconnectPolicy(progress_interval_seconds=0)
    with pytest.raises(ValueError):
        ReconnectPolicy(disconnect_confirmation_wait_seconds=0)
    with pytest.raises(ValueError):
        ReconnectPolicy(force_login_wait_seconds=0)
    with pytest.raises(ValueError):
        ReconnectPolicy(line_transition_wait_seconds=0)
    with pytest.raises(ValueError):
        ReconnectPolicy(entry_transition_wait_seconds=0)
    with pytest.raises(ValueError):
        ReconnectPolicy(announcement_transition_wait_seconds=0)
    with pytest.raises(ValueError):
        ReconnectPolicy(connected_poll_seconds=0)
    with pytest.raises(ValueError):
        ReconnectPolicy(maximum_attempts=0)
