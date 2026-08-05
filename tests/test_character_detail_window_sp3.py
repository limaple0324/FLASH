import gc
from pathlib import Path
from tkinter import Button, Frame, Label, TclError, Tk

import pytest

from services.character_detail_choice_service import (
    PlayerCharacterDetailChoice,
)
from services.character_detail_view_service import PlayerCharacterDetail
from services.character_game_data_view_service import CharacterGameDataView
from ui.character_detail_window import _display_value
from ui.home import HomeView, _safe_character_detail_line


def test_character_detail_uses_inline_note_and_confirmed_game_data_sections() -> None:
    source = Path("ui/home.py").read_text(encoding="utf-8")

    assert "def show_character_detail(" in source
    assert "def _build_selected_character_detail(" in source
    assert "備註" in source
    assert "儲存備註" in source
    assert "寵物天賦" in source
    assert "黑曜石" in source
    assert "命魂" in source
    assert "魂器" in source
    assert "_selected_character_detail" in source
    row_loop = source.index("for line, detail, select in rows:")
    selected_guard = source.index("if selected:", row_loop)
    detail_build = source.index(
        "self._build_selected_character_detail(card)",
        selected_guard,
    )
    assert row_loop < selected_guard < detail_build


def _character_card(view: HomeView) -> Frame:
    return next(
        child
        for child in view._pages["characters"].winfo_children()
        if isinstance(child, Frame)
        and any(
            isinstance(grandchild, Label)
            and grandchild.cget("text") == "角色資料"
            for grandchild in child.winfo_children()
        )
    )


def _row_with_text(card: Frame, text: str) -> Frame:
    return next(
        child
        for child in card.winfo_children()
        if isinstance(child, Frame)
        and any(
            isinstance(grandchild, Label)
            and grandchild.cget("text") == text
            for grandchild in child.winfo_children()
        )
    )


def _detail_frames(card: Frame) -> tuple[Frame, ...]:
    return tuple(
        child
        for child in card.winfo_children()
        if isinstance(child, Frame)
        and any(
            isinstance(grandchild, Label)
            and str(grandchild.cget("text")).endswith("｜詳細資料")
            for grandchild in child.winfo_children()
        )
    )


def _row_button(row: Frame) -> Button:
    return next(
        child for child in row.winfo_children() if isinstance(child, Button)
    )


def test_character_detail_is_created_only_after_selected_row_and_moves_cleanly() -> None:
    root = None
    for _attempt in range(2):
        try:
            root = Tk()
            break
        except TclError:
            gc.collect()
    if root is None:
        pytest.skip("目前環境沒有可用顯示")
    root.withdraw()
    first = PlayerCharacterDetail(
        "第一位",
        "第一組",
        100,
        "主要",
        "補師",
        "第一則備註",
    )
    second = PlayerCharacterDetail(
        "第二位",
        "第一組",
        99,
        "次要",
        "輸出",
        "第二則備註",
    )
    holder: dict[str, HomeView] = {}

    def choice(detail: PlayerCharacterDetail) -> PlayerCharacterDetailChoice:
        def select() -> None:
            holder["view"].show_character_detail(
                detail,
                on_save_note=lambda _note: detail,
                on_clear_note=lambda: detail,
            )

        return PlayerCharacterDetailChoice(detail, select)

    try:
        view = HomeView(
            root,
            {"self_check_passed": True},
            character_choices=(choice(first), choice(second)),
        )
        holder["view"] = view
        view.build()
        view.show_page("characters")
        view._set_feature_card_collapsed(
            view._feature_cards["characters.list"],
            False,
            persist=False,
        )
        root.update_idletasks()

        initial_card = _character_card(view)
        first_line = _safe_character_detail_line(first)
        second_line = _safe_character_detail_line(second)
        assert _detail_frames(initial_card) == ()
        assert _row_button(_row_with_text(initial_card, first_line)).cget(
            "text"
        ) == "查看"
        assert _row_button(_row_with_text(initial_card, second_line)).cget(
            "text"
        ) == "查看"

        _row_button(_row_with_text(initial_card, first_line)).invoke()
        root.update_idletasks()
        first_card = _character_card(view)
        first_row = _row_with_text(first_card, first_line)
        second_row = _row_with_text(first_card, second_line)
        first_details = _detail_frames(first_card)
        assert len(first_details) == 1
        first_detail = first_details[0]
        first_children = tuple(first_card.winfo_children())
        assert (
            first_children.index(first_row)
            < first_children.index(first_detail)
            < first_children.index(second_row)
        )
        assert _row_button(first_row).cget("text") == "收起"
        assert _row_button(second_row).cget("text") == "查看"
        assert {
            child.cget("text")
            for child in first_detail.winfo_children()
            if isinstance(child, Label)
        } >= {"第一位｜詳細資料"}
        assert "第二位｜詳細資料" not in {
            child.cget("text")
            for child in first_detail.winfo_children()
            if isinstance(child, Label)
        }

        _row_button(second_row).invoke()
        root.update_idletasks()
        second_card = _character_card(view)
        first_row = _row_with_text(second_card, first_line)
        second_row = _row_with_text(second_card, second_line)
        second_details = _detail_frames(second_card)
        assert len(second_details) == 1
        second_detail = second_details[0]
        second_children = tuple(second_card.winfo_children())
        assert (
            second_children.index(first_row)
            < second_children.index(second_row)
            < second_children.index(second_detail)
        )
        assert not first_detail.winfo_exists()
        assert _row_button(first_row).cget("text") == "查看"
        assert _row_button(second_row).cget("text") == "收起"
        assert {
            child.cget("text")
            for child in second_detail.winfo_children()
            if isinstance(child, Label)
        } >= {"第二位｜詳細資料"}
        assert "第一位｜詳細資料" not in {
            child.cget("text")
            for child in second_detail.winfo_children()
            if isinstance(child, Label)
        }
    finally:
        if "view" in holder:
            holder["view"]._cancel_game_time_tick()
        root.destroy()


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
