from pathlib import Path

from core.target_window_observation import TargetWindowObservation
from services.character_view_service import PlayerCharacterView
from ui.home import _safe_character_lines, _workspace_state_text
from workspace.models import WorkspaceState


def test_home_has_real_product_pages_and_group_selection() -> None:
    source = Path("ui/home.py").read_text(encoding="utf-8")

    for label in ("首頁", "組別與視窗", "同步與重連", "角色資料", "設定"):
        assert label in source
    assert "目前組別" in source
    assert "on_group_change" in source
    assert "靈魂石" not in source


def test_character_page_uses_confirmed_note_field() -> None:
    lines = _safe_character_lines(
        (
            PlayerCharacterView(
                display_name="120古",
                group="120",
                level=120,
                importance="主號",
                role="古",
                note="守紀優先",
            ),
        )
    )

    assert lines == ("120古｜120｜古｜備註：守紀優先",)


def test_workspace_player_text_does_not_expose_ids() -> None:
    text = _workspace_state_text(WorkspaceState(next_step="選擇組別"))

    assert text == (
        "目前組別：尚未選擇\n"
        "目前活動：等待可信遊戲進度\n"
        "下一步：選擇組別"
    )
    assert "group_id" not in text


def test_target_window_observation_remains_safe_read_only_value() -> None:
    observation = TargetWindowObservation(
        configured=True,
        safe=True,
        code="window.ready",
    )

    assert not hasattr(observation, "handle")
