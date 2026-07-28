"""One versioned lifecycle result for background services."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class LifecycleResult:
    schema_version: int
    action: str
    success: bool
    running: bool
    code: str

    SCHEMA_VERSION = 1


def _running(service: object) -> bool | None:
    for name in ("running", "enabled", "started"):
        value = getattr(service, name, None)
        if type(value) is bool:
            return value
    return None


def start_service(service: object, *args, **kwargs) -> LifecycleResult:
    try:
        raw_result = service.start(*args, **kwargs)
    except Exception:
        return LifecycleResult(
            LifecycleResult.SCHEMA_VERSION,
            "start",
            False,
            bool(_running(service)),
            "lifecycle.start_failed",
        )
    running = _running(service)
    success = running if running is not None else raw_result is not False
    return LifecycleResult(
        LifecycleResult.SCHEMA_VERSION,
        "start",
        bool(success),
        bool(running) if running is not None else bool(success),
        "lifecycle.running" if success else "lifecycle.start_failed",
    )


def stop_service(service: object, *args, **kwargs) -> LifecycleResult:
    try:
        raw_result = service.stop(*args, **kwargs)
    except Exception:
        return LifecycleResult(
            LifecycleResult.SCHEMA_VERSION,
            "stop",
            False,
            bool(_running(service)),
            "lifecycle.stop_failed",
        )
    running = _running(service)
    reported_success = raw_result is not False
    success = (
        reported_success and not running
        if running is not None
        else reported_success
    )
    return LifecycleResult(
        LifecycleResult.SCHEMA_VERSION,
        "stop",
        bool(success),
        bool(running) if running is not None else not bool(success),
        "lifecycle.stopped" if success else "lifecycle.stop_failed",
    )


def cancel_service(service: object, *args, **kwargs) -> LifecycleResult:
    cancel = getattr(service, "cancel", None)
    if not callable(cancel):
        return LifecycleResult(
            LifecycleResult.SCHEMA_VERSION,
            "cancel",
            False,
            bool(_running(service)),
            "lifecycle.cancel_unavailable",
        )
    try:
        raw_result = cancel(*args, **kwargs)
    except Exception:
        return LifecycleResult(
            LifecycleResult.SCHEMA_VERSION,
            "cancel",
            False,
            bool(_running(service)),
            "lifecycle.cancel_failed",
        )
    success = raw_result is not False
    return LifecycleResult(
        LifecycleResult.SCHEMA_VERSION,
        "cancel",
        success,
        bool(_running(service)),
        "lifecycle.cancelled" if success else "lifecycle.cancel_failed",
    )


def join_service(service: object, *args, **kwargs) -> LifecycleResult:
    join = getattr(service, "join", None)
    if not callable(join):
        return LifecycleResult(
            LifecycleResult.SCHEMA_VERSION,
            "join",
            False,
            bool(_running(service)),
            "lifecycle.join_unavailable",
        )
    try:
        raw_result = join(*args, **kwargs)
    except Exception:
        return LifecycleResult(
            LifecycleResult.SCHEMA_VERSION,
            "join",
            False,
            bool(_running(service)),
            "lifecycle.join_failed",
        )
    running = _running(service)
    success = (
        raw_result is not False
        and (running is None or not running)
    )
    return LifecycleResult(
        LifecycleResult.SCHEMA_VERSION,
        "join",
        success,
        bool(running),
        "lifecycle.joined" if success else "lifecycle.join_failed",
    )
