"""SP1 Home UI foundation.

Player-facing presentation layer for the FLASH desktop entrypoint.
Keeps engineering diagnostics separated from the player home experience.
"""

from __future__ import annotations

from tkinter import BOTH, X, Button, Frame, Label, LabelFrame, OptionMenu, StringVar


INPUT_POLICY_LABELS = {
    "foreground_only": "僅允許前台",
    "foreground_background": "允許前台與背景",
    "all": "全部允許（含最小化）",
}


def _characters(status: dict[str, object]) -> list[dict[str, object]]:
    registry = status.get("window_registry", {})
    if not isinstance(registry, dict):
        return []
    characters = registry.get("characters", [])
    if not isinstance(characters, list):
        return []
    return [item for item in characters if isinstance(item, dict)]


def _group_text(status: dict[str, object]) -> str:
    characters = _characters(status)
    if not characters:
        return "目前組別\n尚未設定"

    groups = sorted({
        str(item.get("group")).strip()
        for item in characters
        if isinstance(item.get("group"), str) and str(item.get("group")).strip()
    })
    names = [
        str(item.get("display_name")).strip()
        for item in characters
        if isinstance(item.get("display_name"), str) and str(item.get("display_name")).strip()
    ]
    title = "、".join(groups) if groups else "未分組"
    preview = "、".join(names[:3])
    if len(names) > 3:
        preview += f" 等 {len(names)} 個角色"
    return f"目前組別\n{title}\n{preview}"


def _status_text(status: dict[str, object]) -> str:
    if not bool(status.get("self_check_passed", False)):
        return "目前狀態\n● 需要檢查"
    target = status.get("target_window", {})
    if isinstance(target, dict) and bool(target.get("safe", False)):
        return "目前狀態\n● 已找到遊戲視窗"
    return "目前狀態\n● 已準備完成"


def _workspace_text(status: dict[str, object]) -> str:
    characters = _characters(status)
    if characters:
        return f"工作區\n已載入 {len(characters)} 個角色"
    return "工作區\n等待設定組別"


def _card_text(status: dict[str, object]) -> str:
    if not bool(status.get("self_check_passed", False)):
        return "提醒卡\n自我檢查發現問題"
    target = status.get("target_window", {})
    if isinstance(target, dict) and target.get("configured") is False:
        return "提醒卡\n尚未設定遊戲主視窗"
    return "提醒卡\n系統正常"


class HomeView:
    """First version of the player home screen."""

    def __init__(
        self,
        parent,
        status: dict[str, object],
        on_start=None,
        *,
        input_policy: str = "all",
        on_input_policy_change=None,
        on_test_key=None,
    ):
        self.parent = parent
        self.status = status
        self.on_start = on_start
        self.input_policy = (
            input_policy
            if input_policy in INPUT_POLICY_LABELS
            else "all"
        )
        self.on_input_policy_change = on_input_policy_change
        self.on_test_key = on_test_key

    def build(self):
        body = Frame(self.parent, padx=28, pady=24)
        body.pack(fill=BOTH, expand=True)

        Label(
            body,
            text="輔",
            font=("Microsoft JhengHei UI", 24, "bold"),
            anchor="w",
        ).pack(fill=X)

        Label(
            body,
            text=_group_text(self.status),
            font=("Microsoft JhengHei UI", 12),
            anchor="w",
        ).pack(fill=X, pady=12)

        Button(
            body,
            text="查看目前狀態",
            width=18,
            command=self.on_start,
        ).pack(pady=12)

        Label(
            body,
            text=_status_text(self.status),
            font=("Microsoft JhengHei UI", 11),
            anchor="w",
        ).pack(fill=X, pady=12)

        Label(
            body,
            text=_workspace_text(self.status),
            font=("Microsoft JhengHei UI", 11),
            anchor="w",
        ).pack(fill=X, pady=12)

        Label(
            body,
            text=_card_text(self.status),
            font=("Microsoft JhengHei UI", 11),
            anchor="w",
        ).pack(fill=X, pady=12)

        input_frame = LabelFrame(
            body,
            text="同步輸入權限",
            padx=12,
            pady=10,
        )
        input_frame.pack(fill=X, pady=12)

        Label(
            input_frame,
            text="每次操作前仍會重新驗證 14 個視窗的一對一身分。",
            anchor="w",
        ).pack(fill=X)

        label_to_policy = {
            label: policy
            for policy, label in INPUT_POLICY_LABELS.items()
        }
        policy_variable = StringVar(
            master=self.parent,
            value=INPUT_POLICY_LABELS[self.input_policy],
        )
        self._input_policy_variable = policy_variable

        def policy_changed(label: str) -> None:
            policy = label_to_policy.get(label)
            if policy is not None and self.on_input_policy_change is not None:
                self.on_input_policy_change(policy)

        OptionMenu(
            input_frame,
            policy_variable,
            *INPUT_POLICY_LABELS.values(),
            command=policy_changed,
        ).pack(fill=X, pady=(8, 6))

        button_row = Frame(input_frame)
        button_row.pack(fill=X)
        Button(
            button_row,
            text="測試 B（背包）",
            command=(
                (lambda: self.on_test_key("B"))
                if self.on_test_key is not None
                else None
            ),
        ).pack(side="left", padx=(0, 8))
        Button(
            button_row,
            text="測試 C（人物）",
            command=(
                (lambda: self.on_test_key("C"))
                if self.on_test_key is not None
                else None
            ),
        ).pack(side="left")

        return body
