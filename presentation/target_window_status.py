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
