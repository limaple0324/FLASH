from pathlib import Path

from services.character_detail_view_service import PlayerCharacterDetail
from services.character_game_data_view_service import CharacterGameDataView
from ui.character_detail_window import _display_value
from ui.home import _safe_character_detail_line


def test_character_detail_uses_note_and_confirmed_game_data_sections() -> None:
    source = Path("ui/character_detail_window.py").read_text(encoding="utf-8")

    assert "備註" in source
    assert "儲存備註" in source
    assert "靈魂石" not in source
    assert "寵物天賦" in source
    assert "黑曜石" in source
    assert "命魂" in source
    assert "魂器" in source
    assert "Scrollbar" in source
    assert 'window.bind("<MouseWheel>"' in source
    assert "canvas.yview_scroll" in source


def test_character_detail_summary_is_player_facing() -> None:
    detail = PlayerCharacterDetail(
        display_name="120古",
        group="120",
        level=120,
        importance="主號",
        role="古",
        note="守紀優先",
        game_data=CharacterGameDataView(
            pet_talent="尚未安全讀取",
            obsidian="已開啟至第 3 頁｜尚餘 8 個未點亮節點",
            life_soul="已讀取 2／5 隻培養寵物",
            artifact="尚未安全讀取",
        ),
    )

    assert _safe_character_detail_line(detail) == (
        "120古｜120｜古｜備註：守紀優先"
    )
    assert _display_value(None) == "尚未設定"
    assert not hasattr(detail, "character_id")
    assert not hasattr(detail, "soul_stone")
