"""Chinese presentation of a trusted target-window observation."""

from core.target_window_observation import TargetWindowObservation


def target_window_summary(observation: TargetWindowObservation) -> str:
    if not isinstance(observation, TargetWindowObservation):
        raise TypeError("observation must be TargetWindowObservation.")
    if observation.code == "window.not_observed":
        return "尚未完成視窗檢查"
    if observation.safe:
        return "已找到遊戲視窗"
    if not observation.configured:
        return "尚未設定遊戲視窗"
    return "遊戲視窗目前不可操作"


def target_window_player_message(
    observation: TargetWindowObservation,
) -> str:
    if not isinstance(observation, TargetWindowObservation):
        raise TypeError("observation must be TargetWindowObservation.")
    if observation.code == "window.not_observed":
        return "尚未完成本次遊戲視窗檢查。"
    if not observation.configured:
        return "尚未設定遊戲視窗；本次不會送出任何操作。"
    if observation.safe:
        return "已找到遊戲視窗；同步操作仍會依權限及每批安全預檢執行。"

    known_messages = {
        "window.not_found": "目前找不到已設定的遊戲視窗。",
        "window.ambiguous": "找到多個候選視窗，暫時無法安全判斷。",
        "window.invalid_bounds": "遊戲視窗範圍目前無效。",
        "window.minimized": "遊戲視窗目前已最小化。",
        "window.focus_unknown": "目前無法確認前景視窗。",
        "window.not_foreground": "遊戲視窗目前不在前景。",
        "operation_area.overlapped": "遊戲視窗的必要區域目前被遮擋。",
    }
    return (
        known_messages.get(
            observation.code,
            "目前無法安全辨識遊戲視窗。",
        )
        + " 本次不會送出任何操作。"
    )
