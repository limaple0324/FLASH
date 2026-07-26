"""Player-confirmed game shortcut catalog from the supplied game screens."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GameShortcut:
    key: str
    action: str
    combat_action: str | None = None

    @property
    def player_label(self) -> str:
        label = f"{self.key}｜{self.action}"
        if self.combat_action:
            label += f"；戰鬥狀態：{self.combat_action}"
        return label


CONFIRMED_GAME_SHORTCUTS = (
    GameShortcut("C", "打開／關閉人物面板"),
    GameShortcut(
        "W",
        "打開／關閉寵物面板",
        "打開／關閉技能面板",
    ),
    GameShortcut(
        "B",
        "打開／關閉包裹面板",
        "打開／關閉道具面板",
    ),
    GameShortcut("S", "打開／關閉技能面板"),
    GameShortcut("Q", "打開／關閉任務面板"),
    GameShortcut("K", "打開／關閉社交面板"),
    GameShortcut("E", "打開／關閉公會面板"),
    GameShortcut("R", "打開／關閉成就面板"),
    GameShortcut("F", "飛行／降落"),
    GameShortcut("A", "選擇切磋目標", "自動攻擊"),
    GameShortcut("T", "選擇組隊目標", "打開／關閉寵物面板"),
    GameShortcut("X", "選擇交易目標"),
    GameShortcut("D", "選擇商家目標", "防禦"),
    GameShortcut("M", "打開／關閉地圖面板"),
    GameShortcut("TAB", "打開／關閉地圖面板"),
    GameShortcut("ESC", "關閉目前面板"),
    GameShortcut("G", "打開／關閉打造面板", "選擇捕捉目標"),
    GameShortcut("V", "打開／關閉煉化面板"),
    GameShortcut("P", "打開／關閉系統面板"),
    GameShortcut("I", "打開／關閉排行榜面板"),
    GameShortcut("H", "打開／關閉幫助面板"),
    GameShortcut("N", "打開／關閉商城面板"),
    GameShortcut("O", "打開／關閉便攜擴充套件包"),
    GameShortcut("Z", "打開／關閉玩法面板"),
    GameShortcut("CTRL+↑", "聊天輸入處上翻聊天紀錄"),
    GameShortcut("CTRL+↓", "聊天輸入處下翻聊天紀錄"),
)

GAME_SHORTCUT_BY_LABEL = {
    shortcut.player_label: shortcut for shortcut in CONFIRMED_GAME_SHORTCUTS
}
GAME_SHORTCUT_BY_KEY = {
    shortcut.key: shortcut for shortcut in CONFIRMED_GAME_SHORTCUTS
}
