from dataclasses import dataclass, field
from types import SimpleNamespace

from config.path_manager import PathManager
from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from domain.character import Character, CharacterImportance
from domain.character_store import CharacterStore
from domain.soul_stone import SoulStoneRecord
from domain.soul_stone_store import SoulStoneStore
from main import build_services, create_main_window
from services.app_context import AppContext
from services.character_detail_view_service import CharacterDetailViewService
from services.soul_stone_service import SoulStoneService
from ui.soul_stone_editor_window import SoulStoneEditorWindow


class FakeRootWindow:
    def __init__(self) -> None:
        self.protocols = {}

    def title(self, _value) -> None:
        pass

    def geometry(self, _value) -> None:
        pass

    def minsize(self, _width, _height) -> None:
        pass

    def after_idle(self, callback) -> None:
        self.after_idle_callback = callback

    def after(self, _delay, callback):
        self.after_callback = callback
        return "after-id"

    def protocol(self, name, callback) -> None:
        self.protocols[name] = callback

    def destroy(self) -> None:
        pass


class FakeDialogWindow:
    def __init__(self) -> None:
        self.destroyed = False

    def title(self, _value) -> None:
        pass

    def transient(self, _master) -> None:
        pass

    def protocol(self, _name, _callback) -> None:
        pass

    def destroy(self) -> None:
        self.destroyed = True


class FakeHomeView:
    def __init__(self, *_args, **kwargs) -> None:
        self.kwargs = kwargs

    def build(self) -> None:
        pass

    def refresh_cards(self) -> None:
        pass


@dataclass
class FlowHarness:
    root: FakeRootWindow
    list_windows: list = field(default_factory=list)
    detail_windows: list = field(default_factory=list)
    editors: list = field(default_factory=list)
    editor_renders: list = field(default_factory=list)
    editor_errors: list[tuple[str, str]] = field(default_factory=list)
    messagebox_errors: list[tuple[str, str]] = field(default_factory=list)
    messagebox_error_parents: list = field(default_factory=list)
    messagebox_infos: list[tuple[str, str]] = field(default_factory=list)
    messagebox_info_parents: list = field(default_factory=list)


def _seed_characters(
    tmp_path,
    character_ids: tuple[str, ...],
    *,
    soul_stone_note: str | None,
) -> None:
    registry = WindowRegistry()
    for character_id in character_ids:
        registry.register_character(
            character_id,
            "完全相同角色",
            group="十四支",
            role="古",
            note="相同備註",
        )
    WindowRegistryStore(tmp_path / "data" / "window_registry.json").save(
        registry
    )
    CharacterStore(tmp_path / "data" / "characters.json").save(
        tuple(
            Character(
                character_id,
                "相同舊名稱",
                120,
                CharacterImportance.PRIMARY,
            )
            for character_id in character_ids
        )
    )
    if soul_stone_note is not None:
        SoulStoneStore(tmp_path / "data" / "soul_stones.json").save(
            tuple(
                SoulStoneRecord(character_id, soul_stone_note)
                for character_id in character_ids
            )
        )


def _install_flow_fakes(monkeypatch) -> FlowHarness:
    import main

    harness = FlowHarness(root=FakeRootWindow())

    class FakeCharacterListWindow:
        def __init__(self, master, on_select):
            self.master = master
            self.on_select = on_select
            self.is_open = False
            self.open_calls = []
            self.close_calls = 0
            harness.list_windows.append(self)

        def open(self, details) -> None:
            self.is_open = True
            self.open_calls.append(tuple(details))

        def close(self) -> None:
            self.is_open = False
            self.close_calls += 1

    class FakeCharacterDetailWindow:
        def __init__(
            self,
            master,
            *,
            on_edit_soul_stone=None,
        ):
            self.master = master
            self.on_edit_soul_stone = on_edit_soul_stone
            self.open_calls = []
            self.close_calls = 0
            self.is_open = False
            harness.detail_windows.append(self)

        def open(self, detail) -> None:
            self.is_open = True
            self.open_calls.append(detail)

        def close(self) -> None:
            self.is_open = False
            self.close_calls += 1

        def edit_soul_stone(self) -> None:
            assert callable(self.on_edit_soul_stone)
            self.on_edit_soul_stone()

    def record_editor_render(
        _window,
        display_name,
        initial_note,
        on_save,
        on_clear,
        on_close,
    ) -> None:
        harness.editor_renders.append(
            SimpleNamespace(
                display_name=display_name,
                initial_note=initial_note,
                on_save=on_save,
                on_clear=on_clear,
                on_close=on_close,
            )
        )

    def build_editor(master, on_save, on_clear, **options):
        editor = SoulStoneEditorWindow(
            master,
            on_save,
            on_clear,
            window_factory=lambda _master: FakeDialogWindow(),
            renderer=record_editor_render,
            error_reporter=options.get(
                "error_reporter",
                lambda title, message: harness.editor_errors.append(
                    (title, message)
                ),
            ),
        )
        harness.editors.append(editor)
        return editor

    def record_messagebox_error(title, message, **options) -> None:
        harness.messagebox_errors.append((title, message))
        harness.messagebox_error_parents.append(options.get("parent"))

    def record_messagebox_info(title, message, **options) -> None:
        harness.messagebox_infos.append((title, message))
        harness.messagebox_info_parents.append(options.get("parent"))

    monkeypatch.setattr(main, "Tk", lambda: harness.root)
    monkeypatch.setattr(main, "HomeView", FakeHomeView)
    monkeypatch.setattr(main, "CharacterListWindow", FakeCharacterListWindow)
    monkeypatch.setattr(main, "CharacterDetailWindow", FakeCharacterDetailWindow)
    monkeypatch.setattr(main, "SoulStoneEditorWindow", build_editor, raising=False)
    monkeypatch.setattr(main, "apply_window_icon", lambda _window: None)
    monkeypatch.setattr(
        main,
        "_build_registered_card_overlay_runtime",
        lambda _window: None,
    )
    monkeypatch.setattr(
        main.messagebox,
        "showerror",
        record_messagebox_error,
    )
    monkeypatch.setattr(
        main.messagebox,
        "showwarning",
        record_messagebox_error,
    )
    monkeypatch.setattr(
        main.messagebox,
        "showinfo",
        record_messagebox_info,
    )
    return harness


def _create_flow(monkeypatch, tmp_path):
    harness = _install_flow_fakes(monkeypatch)
    created = create_main_window({}, AppContext.get(PathManager))
    created._home_view.kwargs["on_show_group_characters"]()
    return created, harness


def _select_listed_character(harness: FlowHarness, index: int):
    listed = harness.list_windows[0].open_calls[-1]
    harness.list_windows[0].on_select(listed[index])
    return listed


def _open_editor(harness: FlowHarness) -> None:
    harness.detail_windows[0].edit_soul_stone()


def test_duplicate_visible_details_update_only_selected_stable_identity_and_refresh(
    monkeypatch,
    tmp_path,
) -> None:
    _seed_characters(
        tmp_path,
        ("character-a", "character-b"),
        soul_stone_note="共同舊紀錄",
    )
    build_services(root=tmp_path)
    _created, harness = _create_flow(monkeypatch, tmp_path)

    listed = _select_listed_character(harness, 1)
    assert listed[0] == listed[1]
    assert listed[0] is not listed[1]
    _open_editor(harness)
    assert harness.editor_renders[-1].initial_note == "共同舊紀錄"

    harness.editor_renders[-1].on_save("只修改第二個角色")

    soul_stones = AppContext.get(SoulStoneService)
    assert soul_stones.for_character("character-a").note == "共同舊紀錄"
    assert soul_stones.for_character("character-b").note == "只修改第二個角色"
    assert harness.detail_windows[0].open_calls[-1].soul_stone == "只修改第二個角色"
    assert not hasattr(harness.detail_windows[0].open_calls[-1], "character_id")


def test_clear_refreshes_selected_detail_to_none(monkeypatch, tmp_path) -> None:
    _seed_characters(
        tmp_path,
        ("character-a",),
        soul_stone_note="準備清除",
    )
    build_services(root=tmp_path)
    _created, harness = _create_flow(monkeypatch, tmp_path)
    _select_listed_character(harness, 0)
    _open_editor(harness)

    harness.editor_renders[-1].on_clear()

    assert AppContext.get(SoulStoneService).for_character("character-a") is None
    assert len(harness.detail_windows[0].open_calls) == 2
    assert harness.detail_windows[0].open_calls[-1].soul_stone is None


def test_open_editor_blocks_switching_detail_and_keeps_unsaved_input(
    monkeypatch,
    tmp_path,
) -> None:
    _seed_characters(
        tmp_path,
        ("character-a", "character-b"),
        soul_stone_note="共同舊紀錄",
    )
    build_services(root=tmp_path)
    _created, harness = _create_flow(monkeypatch, tmp_path)
    listed = _select_listed_character(harness, 0)
    _open_editor(harness)
    active_editor = harness.editors[-1]
    active_render = harness.editor_renders[-1]
    active_render.pending_note = "尚未保存的內容"
    shown_for_first_character = harness.detail_windows[0].open_calls[-1]

    harness.list_windows[0].on_select(listed[1])

    assert harness.editors == [active_editor]
    assert active_editor.is_open is True
    assert harness.editor_renders == [active_render]
    assert active_render.pending_note == "尚未保存的內容"
    assert harness.detail_windows[0].open_calls == [shown_for_first_character]
    assert harness.messagebox_infos
    title, message = harness.messagebox_infos[-1]
    assert "靈魂石" in title + message
    assert "保存" in message
    assert harness.messagebox_info_parents[-1] is harness.root
    assert "character-a" not in title + message
    assert "character-b" not in title + message


def test_save_failure_keeps_old_record_and_detail_with_chinese_error(
    monkeypatch,
    tmp_path,
) -> None:
    _seed_characters(
        tmp_path,
        ("character-a",),
        soul_stone_note="原本紀錄",
    )
    build_services(root=tmp_path)
    _created, harness = _create_flow(monkeypatch, tmp_path)
    _select_listed_character(harness, 0)
    _open_editor(harness)
    soul_stones = AppContext.get(SoulStoneService)

    def fail_save(_records) -> None:
        raise OSError("private disk failure")

    monkeypatch.setattr(soul_stones.store, "save", fail_save)
    harness.editor_renders[-1].on_save("不應套用")

    assert soul_stones.for_character("character-a").note == "原本紀錄"
    assert harness.detail_windows[0].open_calls[-1].soul_stone == "原本紀錄"
    assert len(harness.detail_windows[0].open_calls) == 1
    assert harness.editors[-1].is_open is True
    player_errors = [
        *harness.editor_errors,
        *harness.messagebox_errors,
    ]
    assert player_errors
    title, message = player_errors[-1]
    assert title == "輔｜靈魂石紀錄"
    assert "無法保存" in message
    assert "原本紀錄已保留" in message
    assert "private disk failure" not in title + message
    assert harness.messagebox_error_parents[-1] is harness.root


def test_saved_record_refresh_failure_does_not_claim_old_record_was_kept(
    monkeypatch,
    tmp_path,
) -> None:
    _seed_characters(
        tmp_path,
        ("character-a",),
        soul_stone_note="原本紀錄",
    )
    build_services(root=tmp_path)
    _created, harness = _create_flow(monkeypatch, tmp_path)
    _select_listed_character(harness, 0)
    _open_editor(harness)
    details = AppContext.get(CharacterDetailViewService)

    def fail_refresh():
        raise RuntimeError("private refresh failure")

    monkeypatch.setattr(details, "all_with_identities", fail_refresh)
    harness.editor_renders[-1].on_save("已經保存的新紀錄")

    soul_stones = AppContext.get(SoulStoneService)
    assert soul_stones.for_character("character-a").note == "已經保存的新紀錄"
    assert SoulStoneStore(
        tmp_path / "data" / "soul_stones.json"
    ).load() == (
        SoulStoneRecord("character-a", "已經保存的新紀錄"),
    )
    player_errors = [
        *harness.editor_errors,
        *harness.messagebox_errors,
    ]
    assert player_errors
    assert any(
        "紀錄" in title + message and "已保存" in title + message
        for title, message in player_errors
    )
    assert all(
        "原本紀錄已保留" not in title + message
        for title, message in player_errors
    )
    assert all(
        "private refresh failure" not in title + message
        for title, message in player_errors
    )


def test_clicking_old_list_snapshot_fetches_latest_detail_by_identity(
    monkeypatch,
    tmp_path,
) -> None:
    _seed_characters(
        tmp_path,
        ("character-a",),
        soul_stone_note="清單建立時的舊紀錄",
    )
    build_services(root=tmp_path)
    _created, harness = _create_flow(monkeypatch, tmp_path)
    listed = harness.list_windows[0].open_calls[-1]
    assert listed[0].soul_stone == "清單建立時的舊紀錄"

    AppContext.get(SoulStoneService).set_for_character(
        "character-a",
        "點擊前已更新的紀錄",
    )
    harness.list_windows[0].on_select(listed[0])

    opened = harness.detail_windows[0].open_calls[-1]
    assert opened.soul_stone == "點擊前已更新的紀錄"
    assert opened is not listed[0]
