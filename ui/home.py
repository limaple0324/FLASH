"""Home UI compatibility layer with smart-reconnect scope-accurate wording."""

from __future__ import annotations

from . import home_legacy as _legacy

for _name in dir(_legacy):
    if not _name.startswith("__"):
        globals()[_name] = getattr(_legacy, _name)


def _normalize_smart_reconnect_message(message: object) -> str:
    value = message if isinstance(message, str) else str(message or "")
    changed = False
    for phrase in (
        "目前組別的安全視窗身分尚未完成",
        "目前組別的安全視窗身分尚未完整",
        "安全視窗身分尚未完成",
        "安全視窗身分尚未完整",
    ):
        replaced = value.replace(phrase, "智慧重連安全操作尚未完成")
        changed = changed or replaced != value
        value = replaced
    if changed and "唯一可靠捷徑來源" not in value:
        value = (
            value.rstrip("。")
            + "；只有需要自動關閉／重開的視窗，才需要唯一可靠捷徑來源。"
        )
    return value


class SmartReconnectToggleViewResult(_legacy.SmartReconnectToggleViewResult):
    """Normalize reconnect scope at the result boundary, without monkeypatching UI."""

    def __init__(self, success: bool, enabled: bool, message: str) -> None:
        object.__setattr__(self, "success", success)
        object.__setattr__(self, "enabled", enabled)
        object.__setattr__(
            self,
            "message",
            _normalize_smart_reconnect_message(message),
        )


globals()["SmartReconnectToggleViewResult"] = SmartReconnectToggleViewResult
