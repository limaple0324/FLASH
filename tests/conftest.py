from __future__ import annotations

from collections.abc import Callable

import pytest

from adapters.windows_input_sync import WindowsInputSyncController
from adapters.windows_pointer_sync import WindowsPointerSyncController
from services.app_context import AppContext


_SYNC_CONTROLLER_TYPES = (
    WindowsInputSyncController,
    WindowsPointerSyncController,
)


def _clear_after_closing_registered_sync_controllers(
    original_clear: Callable[[], None],
) -> None:
    seen_instance_ids: set[int] = set()
    failures: list[str] = []
    for service_type in _SYNC_CONTROLLER_TYPES:
        instance = AppContext.get(service_type)
        if type(instance) is not service_type:
            continue
        instance_id = id(instance)
        if instance_id in seen_instance_ids:
            continue
        seen_instance_ids.add(instance_id)
        try:
            closed = instance.close(timeout_seconds=1.0)
        except Exception as error:
            failures.append(
                f"{service_type.__name__}.close raised "
                f"{type(error).__name__}: {error}"
            )
        else:
            if closed is not True:
                failures.append(
                    f"{service_type.__name__}.close returned {closed!r}"
                )
    if failures:
        pytest.fail(
            "registered sync controller shutdown failed before "
            f"AppContext.clear: {'; '.join(failures)}",
            pytrace=False,
        )
    original_clear()


def _install_app_context_clear_guard() -> classmethod:
    original_descriptor = AppContext.__dict__.get("clear")
    if not isinstance(original_descriptor, classmethod):
        raise RuntimeError("AppContext.clear must remain a classmethod")
    original_clear = AppContext.clear

    @classmethod
    def guarded_clear(cls) -> None:
        if cls is not AppContext:
            raise RuntimeError("AppContext.clear guard received another class")
        _clear_after_closing_registered_sync_controllers(original_clear)

    AppContext.clear = guarded_clear
    return original_descriptor


def _restore_app_context_clear(original_descriptor: classmethod) -> None:
    AppContext.clear = original_descriptor


@pytest.fixture(scope="session", autouse=True)
def _guard_app_context_clear_for_the_test_session():
    original_descriptor = _install_app_context_clear_guard()
    try:
        yield
    finally:
        try:
            AppContext.clear()
        finally:
            _restore_app_context_clear(original_descriptor)


@pytest.fixture(autouse=True)
def _clear_app_context_after_each_test(
    _guard_app_context_clear_for_the_test_session,
):
    try:
        yield
    finally:
        AppContext.clear()
