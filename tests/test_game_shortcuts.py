from domain.game_shortcuts import (
    CONFIRMED_GAME_SHORTCUTS,
    GAME_SHORTCUT_BY_KEY,
)


def test_confirmed_shortcut_catalog_matches_supplied_screens() -> None:
    assert tuple(GAME_SHORTCUT_BY_KEY) == (
        "C",
        "W",
        "B",
        "S",
        "Q",
        "K",
        "E",
        "R",
        "F",
        "A",
        "T",
        "X",
        "D",
        "M",
        "TAB",
        "ESC",
        "G",
        "V",
        "P",
        "I",
        "H",
        "N",
        "O",
        "Z",
        "CTRL+↑",
        "CTRL+↓",
    )
    assert len(CONFIRMED_GAME_SHORTCUTS) == 26


def test_combat_state_shortcuts_keep_both_confirmed_meanings() -> None:
    assert GAME_SHORTCUT_BY_KEY["A"].combat_action == "自動攻擊"
    assert GAME_SHORTCUT_BY_KEY["G"].combat_action == "選擇捕捉目標"
    assert GAME_SHORTCUT_BY_KEY["D"].combat_action == "防禦"
    assert GAME_SHORTCUT_BY_KEY["W"].combat_action == "打開／關閉技能面板"
    assert GAME_SHORTCUT_BY_KEY["T"].combat_action == "打開／關閉寵物面板"
    assert GAME_SHORTCUT_BY_KEY["E"].combat_action == "打開／關閉道具面板"
