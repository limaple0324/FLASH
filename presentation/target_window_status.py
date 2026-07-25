"""Chinese presentation of a trusted target-window observation."""

from __future__ import annotations

from core.target_window_observation import TargetWindowObservation


def target_window_player_message(
    observation: TargetWindowObservation,
) -> str:
    if not isinstance(observation, TargetWindowObservation):
        raise TypeError("observation must be TargetWindowObservation.")
    if observation.code == "window.not_observed":
        return "尚未完成本次遊戲視窗檢查；所有遊戲操作保持停用。"
    if not observation.configured:
        return "尚未設定遊戲主視窗；所有遊戲操作保持停用。"
    if observation.safe:
        return "已找到可安全辨識的遊戲視窗；遊戲輸入仍保持停用。"

    known_messages = {
        "window.not_found": "目前找不到已設定的遊戲視窗；所有遊戲操作保持停用。",
        "window.ambiguous": (
            "找到多個符合條件的遊戲視窗，無法安全判斷；所有遊戲操作保持停用。"
        ),
        "window.invalid_bounds": "遊戲視窗範圍無效；所有遊戲操作保持停用。",
        "window.minimized": "遊戲視窗目前已最小化；所有遊戲操作保持停用。",
        "window.focus_unknown": "無法確認目前前景視窗；所有遊戲操作保持停用。",
        "window.not_foreground": "遊戲視窗目前不在前景；所有遊戲操作保持停用。",
        "operation_area.overlapped": (
            "遊戲視窗的必要區域被遮擋；所有遊戲操作保持停用。"
        ),
    }
    return known_messages.get(
        observation.code,
        "目前無法安全辨識遊戲視窗；所有遊戲操作保持停用。",
    )


def target_window_summary(
    observation: TargetWindowObservation,
) -> str:
    if not isinstance(observation, TargetWindowObservation):
        raise TypeError("observation must be TargetWindowObservation.")
    if observation.code == "window.not_observed":
        return "尚未完成視窗檢查"
    if observation.safe:
        return "已找到遊戲視窗"
    if not observation.configured:
        return "尚未設定遊戲視窗"
    return "遊戲視窗不可操作"

