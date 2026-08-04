from dataclasses import replace
from pathlib import Path

from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from domain.character import Character, CharacterImportance
from domain.character_store import CharacterStore
from adapters.windows_window import WindowInfo
from core.reconnect_policy import ReconnectScreenState
from main import (
    CHARACTER_FILENAME,
    build_services,
    registered_game_data_target,
)
from services.app_context import AppContext
from services.character_detail_view_service import (
    CharacterDetailViewService,
    PlayerCharacterDetail,
)
from services.character_game_data_view_service import CharacterGameDataView
from services.character_game_data_update_service import CharacterGameDataUpdateService
from services.character_game_data_capture_service import (
    CharacterGameDataCaptureService,
)
from services.character_view_service import CharacterViewService, PlayerCharacterView
from services.group_selection_service import PlayerGroupChoice, PlayerGroupMember


_FINGERPRINT = "a" * 64


def _game_data_selection() -> tuple[PlayerGroupChoice, PlayerGroupMember, WindowInfo]:
    member = PlayerGroupMember("entry-a", "角色甲", "主號", "角色甲乙", "char-a")
    return (
        PlayerGroupChoice("group-a", "目前組別", 1, (member,)),
        member,
        WindowInfo(
            123,
            "",
            True,
            False,
            (1, 2, 500, 600),
            process_id=456,
            window_class="ShockwaveFlash",
            launch_fingerprint=_FINGERPRINT,
            thread_id=789,
            process_lifecycle_token=987654321,
        ),
    )


def test_registered_game_data_target_requires_one_complete_connected_member() -> None:
    selection, member, window = _game_data_selection()

    target = registered_game_data_target(
        "目前組別", selection, member, window, ReconnectScreenState.CONNECTED
    )

    assert target is not None
    assert target.character_id == "char-a"
    assert target.window_handle == 123
    assert target.thread_id == 789
    assert target.process_lifecycle_token == 987654321
    assert registered_game_data_target(
        "目前組別", selection, member, window, ReconnectScreenState.UNKNOWN
    ) is None
    duplicate = PlayerGroupChoice(
        "group-a", "目前組別", 2, (member, member)
    )
    assert registered_game_data_target(
        "目前組別", duplicate, member, window, ReconnectScreenState.CONNECTED
    ) is None
    assert registered_game_data_target(
        "目前組別", selection, member, None, ReconnectScreenState.CONNECTED
    ) is None
    for incomplete in (
        replace(window, visible=False),
        replace(window, minimized=True),
        replace(window, thread_id=None),
        replace(window, window_class=None),
        replace(window, process_lifecycle_token=None),
    ):
        assert registered_game_data_target(
            "目前組別",
            selection,
            member,
            incomplete,
            ReconnectScreenState.CONNECTED,
        ) is None


def test_build_services_loads_character_profiles_into_read_only_view(tmp_path) -> None:
    registry = WindowRegistry()
    registry.register_character(
        "same-character",
        "目前名稱",
        group="14支",
        role="古",
        note="守紀優先",
    )
    WindowRegistryStore(tmp_path / "data" / "window_registry.json").save(registry)
    CharacterStore(tmp_path / "data" / CHARACTER_FILENAME).save(
        (
            Character(
                "same-character",
                "原資料名稱",
                120,
                CharacterImportance.PRIMARY,
            ),
        )
    )

    paths, _logger = build_services(root=tmp_path)

    assert AppContext.get(CharacterStore).path == paths.data_dir() / CHARACTER_FILENAME
    assert AppContext.get(CharacterViewService).all() == (
        PlayerCharacterView(
            display_name="目前名稱",
            group="14支",
            level=120,
            importance="主號",
            role="古",
            note="守紀優先",
        ),
    )


def test_build_services_keeps_missing_character_profiles_empty(tmp_path) -> None:
    build_services(root=tmp_path)

    store = AppContext.get(CharacterStore)
    view_service = AppContext.get(CharacterViewService)
    assert store is not None
    assert view_service is not None
    assert view_service.all() == ()
    assert (tmp_path / "data" / CHARACTER_FILENAME).exists() is False


def test_build_services_isolates_corrupt_character_profiles(tmp_path) -> None:
    path = tmp_path / "data" / CHARACTER_FILENAME
    path.parent.mkdir(parents=True)
    path.write_text("{broken", encoding="utf-8")

    build_services(root=tmp_path)

    store = AppContext.get(CharacterStore)
    assert store.recovered_from_corruption is True
    assert store.recovered_from_backup is False
    assert AppContext.get(CharacterViewService).all() == ()
    assert list(path.parent.glob("characters.json.corrupt*"))


def test_build_services_registers_confirmed_game_data_sections(tmp_path) -> None:
    registry = WindowRegistry()
    registry.register_character("char-a", "角色甲", group="14支")
    WindowRegistryStore(tmp_path / "data" / "window_registry.json").save(registry)

    build_services(root=tmp_path)

    assert isinstance(
        AppContext.get(CharacterGameDataUpdateService),
        CharacterGameDataUpdateService,
    )
    assert isinstance(
        AppContext.get(CharacterGameDataCaptureService),
        CharacterGameDataCaptureService,
    )

    details = AppContext.get(CharacterDetailViewService)
    assert details.all() == (
        PlayerCharacterDetail(
            display_name="角色甲",
            group="14支",
            level=None,
            importance=None,
            role=None,
            note=None,
            game_data=CharacterGameDataView(
                pet_talent="尚未安全讀取",
                obsidian="尚未安全讀取",
                life_soul="尚未安全讀取",
                artifact="尚未安全讀取",
            ),
        ),
    )


def test_main_window_backfills_current_group_character_data_on_start() -> None:
    source = Path("main.py").read_text(encoding="utf-8")
    build_index = source.index("home_view.build()")
    refresh_index = source.index(
        "refresh_character_data(current_character_group)",
        build_index,
    )
    subscribe_index = source.index(
        "auto_click_service.subscribe(",
        refresh_index,
    )

    assert build_index < refresh_index < subscribe_index
