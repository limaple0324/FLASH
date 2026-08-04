from __future__ import annotations

import hashlib
import gc
import json
import sys
from pathlib import Path
from tkinter import Button, Label, TclError, Tk, font as tkfont

import pytest

from config.config_manager import ConfigManager
from services.character_detail_choice_service import (
    PlayerCharacterDetailChoice,
)
from services.character_detail_view_service import PlayerCharacterDetail
from services.game_time_timed_click_service import (
    GameTimeTimedClickSnapshot,
)
from services.ui_font_service import (
    CONTENT_FONT_SIZES,
    CONTENT_HEADING_SIZES,
    DEFAULT_CONTENT_FONT_SIZE,
    DEFAULT_SIDEBAR_FONT_SIZE,
    DEFAULT_UI_FONT_ID,
    SIDEBAR_FONT_SIZES,
    UI_FONT_FALLBACK_FAMILY,
    UI_FONT_OPTIONS,
    UIFontService,
    resolve_ui_font_preferences,
)
from ui.home import HomeView


FONT_ROOT = Path("assets/ui_fonts")
EXPECTED_FONT_RECORDS = (
    (
        "cubic_11",
        "俐方體十一號",
        "ACh-K/Cubic-11",
        "4c566f7d6cc5c05ee360fe9cff56b5da1fcafd4d",
        "fonts/ttf/Cubic_11.ttf",
        "cubic_11/Cubic_11.ttf",
        2_773_732,
        "0193f5f033612496df6b45ee92ac3b335bc6a5a24ff95da55ca87b33e57dcf62",
        "Cubic 11",
    ),
    (
        "naikai",
        "內海字體",
        "max32002/naikaifont",
        "7baa43a6adad4a82eab6fd27c9612b316f69d7c9",
        "tw/NaikaiFont-Regular-Lite.ttf",
        "naikai/NaikaiFont-Regular-Lite.ttf",
        4_664_452,
        "4e42aef5ab08f00bfcb5093205eda1d3e1da57c72ef3029fb1ae513050bfd402",
        "NaikaiFont",
    ),
    (
        "jason_3",
        "清松手寫體三",
        "jasonhandwriting/JasonHandwriting",
        "e488583c07077850aa4a2a6280baa20374cccde2",
        "JasonHandwriting3.ttf",
        "jason_handwriting/JasonHandwriting3.ttf",
        6_089_404,
        "7747c276f3b16306c7ab7d52465647c566d01f91faeefa70c271a3890ed35fb0",
        "JasonHandwriting3",
    ),
    (
        "jason_4",
        "清松手寫體四",
        "jasonhandwriting/JasonHandwriting",
        "e488583c07077850aa4a2a6280baa20374cccde2",
        "JasonHandwriting4.ttf",
        "jason_handwriting/JasonHandwriting4.ttf",
        4_137_276,
        "1db91107a80c78e6d04b6d78f86a4b8946f083cb0b41ff5864009c4e1698ee4b",
        "JasonHandwriting4",
    ),
    (
        "jason_6",
        "清松手寫體六",
        "jasonhandwriting/JasonHandwriting",
        "e488583c07077850aa4a2a6280baa20374cccde2",
        "JasonHandwriting6.ttf",
        "jason_handwriting/JasonHandwriting6.ttf",
        7_462_032,
        "2fa5886c15b4053eb32f3dc9e83fd7a8bf8fc599dcddd37e6c706b3fefe32ffd",
        "JasonHandwriting6",
    ),
    (
        "jason_8",
        "清松手寫體八",
        "jasonhandwriting/JasonHandwriting",
        "e488583c07077850aa4a2a6280baa20374cccde2",
        "JasonHandwriting8.ttf",
        "jason_handwriting/JasonHandwriting8.ttf",
        8_030_664,
        "1437de4a5fee29a5c6b43450df56e734af9cddb5b3a68d236cf5f42be178c93f",
        "JasonHandwriting8",
    ),
    (
        "jason_9",
        "清松手寫體九",
        "jasonhandwriting/JasonHandwriting",
        "e488583c07077850aa4a2a6280baa20374cccde2",
        "JasonHandwriting9.ttf",
        "jason_handwriting/JasonHandwriting9.ttf",
        8_379_644,
        "b50728e2c7442a76a661c4d43c8ec22142e8edc83909a9d40b7c25d04bd04cf5",
        "JasonHandwriting9",
    ),
    (
        "chenyu_luoyan",
        "辰宇落雁體",
        "Chenyu-otf/chenyuluoyan_thin",
        "6e36815b0bec9f4f948298698d00b27a5f0b65c1",
        "ChenYuluoyan-2.0-Thin.ttf",
        "chenyu_luoyan/ChenYuluoyan-2.0-Thin.ttf",
        9_522_140,
        "1289e42a6d1ec995d0cb23aee89efc69fc95749fbd54a610057a3e992dc453db",
        "ChenYuluoyan 2.0",
    ),
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class _RecordingBackend:
    def __init__(
        self,
        *,
        fail_add_at: int | None = None,
        fail_remove_once: str | None = None,
    ) -> None:
        self.fail_add_at = fail_add_at
        self.fail_remove_once = fail_remove_once
        self.add_attempts: list[str] = []
        self.added: list[str] = []
        self.removed: list[str] = []

    def add(self, path: Path) -> bool:
        self.add_attempts.append(path.name)
        if len(self.add_attempts) == self.fail_add_at:
            return False
        self.added.append(path.name)
        return True

    def remove(self, path: Path) -> bool:
        self.removed.append(path.name)
        if path.name == self.fail_remove_once:
            self.fail_remove_once = None
            return False
        return True


def _make_small_bundle(root: Path) -> dict[str, str]:
    license_path = root / "licenses" / "OFL.txt"
    license_path.parent.mkdir(parents=True)
    license_path.write_bytes(b"test license evidence")
    families: dict[str, str] = {}
    fonts: list[dict[str, object]] = []
    for index, (font_id, display_name) in enumerate(UI_FONT_OPTIONS):
        managed_path = f"fonts/{font_id}.ttf"
        path = root / managed_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(f"font-{font_id}".encode("ascii"))
        family = f"TestFamily{index}"
        families[path.name] = family
        fonts.append(
            {
                "font_id": font_id,
                "display_name": display_name,
                "repository": "owner/repository",
                "commit": "a" * 40,
                "source_path": path.name,
                "managed_path": managed_path,
                "size": path.stat().st_size,
                "sha256": _sha256(path),
                "internal_family": family,
                "ui_family": family,
                "license_evidence": ["licenses/OFL.txt"],
            }
        )
    manifest = {
        "schema_version": 1,
        "fallback_family": UI_FONT_FALLBACK_FAMILY,
        "default_font_id": DEFAULT_UI_FONT_ID,
        "fonts": fonts,
        "licenses": [
            {
                "managed_path": "licenses/OFL.txt",
                "repository": "owner/repository",
                "commit": "b" * 40,
                "source_path": "OFL.txt",
                "size": license_path.stat().st_size,
                "sha256": _sha256(license_path),
            }
        ],
    }
    (root / "source_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    return families


def _font_actual(root: Tk, widget) -> dict[str, object]:
    return tkfont.Font(root=root, font=widget.cget("font")).actual()


def _tk_resolved_family(root: Tk, requested_family: str) -> str:
    font = tkfont.Font(root=root, family=requested_family, size=14)
    return str(font.actual("family"))


def _descendants(widget) -> tuple[object, ...]:
    found: list[object] = []
    pending = list(widget.winfo_children())
    while pending:
        child = pending.pop(0)
        found.append(child)
        pending.extend(child.winfo_children())
    return tuple(found)


def _game_time_snapshot(
    offset_ms: int = 0,
    auto_update: bool = True,
) -> GameTimeTimedClickSnapshot:
    return GameTimeTimedClickSnapshot(
        offset_ms,
        auto_update,
        86_399_999,
        "23:59:59.999",
        None,
        False,
        None,
        120,
        2,
        250,
        0,
        "尚未啟用",
    )


def test_managed_font_sources_hashes_families_and_licenses_are_exact() -> None:
    manifest = json.loads(
        (FONT_ROOT / "source_manifest.json").read_text(encoding="utf-8")
    )
    actual_records = tuple(
        (
            item["font_id"],
            item["display_name"],
            item["repository"],
            item["commit"],
            item["source_path"],
            item["managed_path"],
            item["size"],
            item["sha256"],
            item["internal_family"],
        )
        for item in manifest["fonts"]
    )
    assert actual_records == EXPECTED_FONT_RECORDS
    assert tuple((item["font_id"], item["display_name"]) for item in manifest["fonts"]) == UI_FONT_OPTIONS
    assert manifest["default_font_id"] == "jason_3"
    for item in manifest["fonts"]:
        path = FONT_ROOT / item["managed_path"]
        assert path.stat().st_size == item["size"]
        assert _sha256(path) == item["sha256"]

    license_records = {
        item["managed_path"]: item for item in manifest["licenses"]
    }
    assert set(license_records) == {
        "cubic_11/OFL.txt",
        "naikai/SIL_Open_Font_License_1.1.txt",
        "jason_handwriting/README.md",
        "chenyu_luoyan/license.txt",
        "official_sil/OFL-1.1.txt",
    }
    for item in license_records.values():
        path = FONT_ROOT / item["managed_path"]
        assert path.stat().st_size == item["size"]
        assert _sha256(path) == item["sha256"]
    official = license_records["official_sil/OFL-1.1.txt"]
    assert official["source_url"] == "https://openfontlicense.org/documents/OFL.txt"
    assert official["source_page"] == "https://openfontlicense.org/open-font-license-official-text/"

    backend = _RecordingBackend()
    service = UIFontService(FONT_ROOT, backend=backend)
    result = service.load_all()
    assert result.success
    assert tuple((asset.font_id, asset.internal_family) for asset in service.assets) == tuple(
        (record[0], record[-1]) for record in EXPECTED_FONT_RECORDS
    )
    assert service.close()


def test_service_loads_all_then_unloads_in_reverse_order(tmp_path: Path) -> None:
    families = _make_small_bundle(tmp_path)
    backend = _RecordingBackend()
    service = UIFontService(
        tmp_path,
        backend=backend,
        family_reader=lambda path: families[path.name],
    )

    result = service.load_all()

    assert result.success
    assert result.loaded_font_ids == tuple(item[0] for item in UI_FONT_OPTIONS)
    assert len(backend.added) == 8
    assert service.close()
    assert backend.removed == list(reversed(backend.added))


def test_validation_failure_never_calls_private_font_api_and_uses_fallback(
    tmp_path: Path,
) -> None:
    families = _make_small_bundle(tmp_path)
    damaged = tmp_path / "fonts" / "jason_6.ttf"
    damaged.write_bytes(b"damaged")
    backend = _RecordingBackend()
    service = UIFontService(
        tmp_path,
        backend=backend,
        family_reader=lambda path: families[path.name],
    )

    result = service.load_all()

    assert not result.success
    assert result.code == "ui_font.integrity_failed"
    assert backend.add_attempts == []
    assert {choice.ui_family for choice in service.choices} == {
        UI_FONT_FALLBACK_FAMILY
    }


def test_family_mismatch_is_rejected_before_any_private_load(
    tmp_path: Path,
) -> None:
    _make_small_bundle(tmp_path)
    backend = _RecordingBackend()
    service = UIFontService(
        tmp_path,
        backend=backend,
        family_reader=lambda _path: "WrongFamily",
    )

    result = service.load_all()

    assert not result.success
    assert result.code == "ui_font.family_mismatch"
    assert backend.add_attempts == []


def test_partial_private_load_rolls_back_and_never_exposes_mixed_families(
    tmp_path: Path,
) -> None:
    families = _make_small_bundle(tmp_path)
    backend = _RecordingBackend(fail_add_at=4)
    service = UIFontService(
        tmp_path,
        backend=backend,
        family_reader=lambda path: families[path.name],
    )

    result = service.load_all()

    assert not result.success
    assert result.code == "ui_font.load_failed"
    assert backend.removed == list(reversed(backend.added))
    assert not service.loaded
    assert all(
        choice.ui_family == UI_FONT_FALLBACK_FAMILY
        for choice in service.choices
    )


def test_failed_unload_is_retained_and_retried(tmp_path: Path) -> None:
    families = _make_small_bundle(tmp_path)
    backend = _RecordingBackend(fail_remove_once="jason_3.ttf")
    service = UIFontService(
        tmp_path,
        backend=backend,
        family_reader=lambda path: families[path.name],
    )
    assert service.load_all().success

    assert not service.close()
    assert service.result.code == "ui_font.cleanup_failed"
    first_attempts = backend.removed.count("jason_3.ttf")
    assert service.close()
    assert backend.removed.count("jason_3.ttf") == first_attempts + 1


def test_manifest_path_cannot_escape_managed_font_root(tmp_path: Path) -> None:
    families = _make_small_bundle(tmp_path)
    manifest_path = tmp_path / "source_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["fonts"][0]["managed_path"] = "../outside.ttf"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False),
        encoding="utf-8",
    )
    backend = _RecordingBackend()
    service = UIFontService(
        tmp_path,
        backend=backend,
        family_reader=lambda path: families[path.name],
    )

    result = service.load_all()

    assert not result.success
    assert result.code == "ui_font.manifest_invalid"
    assert backend.add_attempts == []


def test_preferences_default_independently_and_survive_reopen(
    tmp_path: Path,
) -> None:
    preferences = resolve_ui_font_preferences("invalid", 12, 99)
    assert preferences.font_id == DEFAULT_UI_FONT_ID
    assert preferences.sidebar_size == 12
    assert preferences.content_size == DEFAULT_CONTENT_FONT_SIZE

    config_path = tmp_path / "settings.json"
    config = ConfigManager(config_path)
    config.update_values(
        {
            "ui_font_id": "jason_8",
            "ui_sidebar_font_size": 18,
            "ui_content_font_size": 11,
        }
    )
    reopened = ConfigManager(config_path)
    restored = resolve_ui_font_preferences(
        reopened.get("ui_font_id"),
        reopened.get("ui_sidebar_font_size"),
        reopened.get("ui_content_font_size"),
    )
    assert restored.font_id == "jason_8"
    assert restored.sidebar_size == 18
    assert restored.content_size == 11


def test_main_lifecycle_settings_and_package_manifest_are_wired() -> None:
    main_source = Path("main.py").read_text(encoding="utf-8")
    spec_source = Path("FLASH.spec").read_text(encoding="utf-8")

    assert 'UI_FONT_ID_KEY = "ui_font_id"' in main_source
    assert 'UI_SIDEBAR_FONT_SIZE_KEY = "ui_sidebar_font_size"' in main_source
    assert 'UI_CONTENT_FONT_SIZE_KEY = "ui_content_font_size"' in main_source
    assert "resolve_ui_font_preferences(" in main_source
    assert "config.update_values(normalized_font_values)" in main_source
    assert "ui_font_service.load_all()" in main_source
    assert "shutdown_ui_font_service(logger)" in main_source
    assert "on_ui_font_change=change_ui_font" in main_source
    assert "on_sidebar_font_size_change=change_sidebar_font_size" in main_source
    assert "on_content_font_size_change=change_content_font_size" in main_source
    assert "('assets/ui_fonts', 'assets/ui_fonts')" in spec_source


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="需要 Windows 程序私有字體介面",
)
def test_real_windows_private_font_api_loads_and_unloads_all_eight() -> None:
    service = UIFontService(FONT_ROOT)
    try:
        result = service.load_all()
        assert result.success, (result.code, result.message)
        assert result.loaded_font_ids == tuple(item[0] for item in UI_FONT_OPTIONS)
    finally:
        assert service.close()


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="需要 Windows 真實視窗",
)
def test_real_tk_900x620_all_eight_fonts_and_three_size_stages() -> None:
    service = UIFontService(FONT_ROOT)
    result = service.load_all()
    assert result.success, (result.code, result.message)
    try:
        root = None
        display_errors: list[str] = []
        for _attempt in range(2):
            try:
                root = Tk()
                break
            except TclError as error:
                display_errors.append(str(error))
                gc.collect()
        if root is None:
            pytest.skip(
                "目前環境沒有可用顯示，不能宣稱真實視窗矩陣通過："
                f"{'；'.join(display_errors)}"
            )
        root.geometry("900x620+20+20")
        root.minsize(900, 620)
        detail = PlayerCharacterDetail(
            "小光",
            "第一組",
            99,
            "主要",
            "補師",
            None,
        )
        game_time_changes: list[tuple[int, bool]] = []

        def change_game_time(
            offset_ms: int,
            auto_update: bool,
        ) -> GameTimeTimedClickSnapshot:
            game_time_changes.append((offset_ms, auto_update))
            return _game_time_snapshot(offset_ms, auto_update)

        try:
            view = HomeView(
                root,
                {"self_check_passed": True},
                character_choices=(
                    PlayerCharacterDetailChoice(detail, lambda: None),
                ),
                ui_font_choices=service.choices,
                game_time_snapshot_provider=_game_time_snapshot,
                on_game_time_settings_change=change_game_time,
            )
            view.build()
            root.deiconify()
            view._cancel_game_time_tick()
            view._poll_game_time()
            root.update()

            game_time_card = view._game_time_sidebar_card
            assert game_time_card is not None
            assert tuple(game_time_card.winfo_children()) == (
                view._game_time_title_label,
                view._game_time_value_label,
            )
            assert tuple(_descendants(game_time_card)) == (
                view._game_time_title_label,
                view._game_time_value_label,
            )
            assert tuple(
                widget.cget("text")
                for widget in _descendants(game_time_card)
            ) == ("遊戲時間", "23:59:59.999")
            settings_time_card = view._feature_cards[
                "settings.game_time"
            ].frame
            settings_text = {
                widget.cget("text")
                for widget in _descendants(settings_time_card)
                if isinstance(widget, (Button, Label))
            }
            assert "來源：系統時間" in settings_text
            assert "自動更新" not in {
                widget.cget("text")
                for widget in _descendants(game_time_card)
            }
            view._game_time_offset_entry.delete(0, "end")
            view._game_time_offset_entry.insert(0, "123")
            view._game_time_auto_variable.set(0)
            view._apply_game_time_settings()
            assert game_time_changes[-1] == (123, False)

            character_card = view._feature_cards["characters.list"]
            view._set_feature_card_collapsed(
                character_card,
                False,
                persist=False,
            )
            theme_card = view._feature_cards["settings.theme"]
            view._set_feature_card_collapsed(
                theme_card,
                False,
                persist=False,
            )
            all_widgets = _descendants(view._root)
            buttons = tuple(
                widget for widget in all_widgets if isinstance(widget, Button)
            )
            button_commands = tuple(
                (str(widget), str(widget.cget("command")))
                for widget in buttons
            )

            previous_font = view.ui_font_id
            rejected_font = next(
                choice
                for choice in service.choices
                if choice.font_id != previous_font
            )
            view.on_ui_font_change = lambda _value: False
            view._ui_font_variable.set(rejected_font.display_name)
            view._change_ui_font_selection(rejected_font.display_name)
            assert view.ui_font_id == previous_font
            assert view._ui_font_variable.get() == view._ui_font_display_name(
                previous_font
            )
            view.on_ui_font_change = None

            view.on_sidebar_font_size_change = lambda _value: False
            view._sidebar_font_size_variable.set("18")
            view._change_sidebar_font_size_selection("18")
            assert view.sidebar_font_size == DEFAULT_SIDEBAR_FONT_SIZE
            assert view._sidebar_font_size_variable.get() == str(
                DEFAULT_SIDEBAR_FONT_SIZE
            )
            view.on_sidebar_font_size_change = None

            view.on_content_font_size_change = lambda _value: False
            view._content_font_size_variable.set("17")
            view._change_content_font_size_selection("17")
            assert view.content_font_size == DEFAULT_CONTENT_FONT_SIZE
            assert view._content_font_size_variable.get() == str(
                DEFAULT_CONTENT_FONT_SIZE
            )
            view.on_content_font_size_change = None

            checked = 0
            size_stages = tuple(
                zip(SIDEBAR_FONT_SIZES, CONTENT_FONT_SIZES)
            )
            for choice in service.choices:
                resolved_family = _tk_resolved_family(
                    root,
                    choice.ui_family,
                )
                assert resolved_family != _tk_resolved_family(
                    root,
                    UI_FONT_FALLBACK_FAMILY,
                )
                for sidebar_size, content_size in size_stages:
                    view._ui_font_variable.set(choice.display_name)
                    view._change_ui_font_selection(choice.display_name)
                    view._sidebar_font_size_variable.set(str(sidebar_size))
                    view._change_sidebar_font_size_selection(
                        str(sidebar_size)
                    )
                    view._content_font_size_variable.set(str(content_size))
                    view._change_content_font_size_selection(
                        str(content_size)
                    )

                    view.show_page("characters")
                    root.update()
                    role_row = next(
                        child
                        for child in character_card.frame.winfo_children()
                        if any(
                            isinstance(grandchild, Button)
                            and grandchild.cget("text") == "查看"
                            for grandchild in child.winfo_children()
                        )
                    )
                    role_label = next(
                        child
                        for child in role_row.winfo_children()
                        if isinstance(child, Label)
                    )
                    role_button = next(
                        child
                        for child in role_row.winfo_children()
                        if isinstance(child, Button)
                    )
                    assert (
                        _font_actual(root, role_label)["family"]
                        == resolved_family
                    )
                    assert _font_actual(root, role_label)["size"] == content_size
                    assert _font_actual(root, role_button)["size"] == content_size
                    assert role_label.winfo_reqwidth() <= role_label.winfo_width()
                    assert (
                        role_label.winfo_rootx() + role_label.winfo_width()
                        <= role_button.winfo_rootx()
                    )
                    assert (
                        role_button.winfo_rootx() + role_button.winfo_width()
                        <= role_row.winfo_rootx() + role_row.winfo_width()
                    )
                    assert max(
                        role_label.winfo_reqheight(),
                        role_button.winfo_reqheight(),
                    ) <= role_row.winfo_height()

                    navigation_button = view._navigation_buttons["characters"]
                    assert (
                        _font_actual(root, navigation_button)["family"]
                        == resolved_family
                    )
                    assert (
                        _font_actual(root, navigation_button)["size"]
                        == sidebar_size
                    )
                    assert (
                        navigation_button.winfo_reqwidth()
                        <= navigation_button.winfo_width()
                    )
                    assert (
                        _font_actual(
                            root,
                            view._game_time_offset_entry,
                        )["size"]
                        == content_size
                    )
                    assert tuple(
                        widget.cget("text")
                        for widget in _descendants(game_time_card)
                        if widget.winfo_manager()
                    ) == ("遊戲時間", "23:59:59.999")
                    assert (
                        _font_actual(
                            root,
                            view._game_time_title_label,
                        )["size"]
                        == sidebar_size
                    )
                    assert (
                        _font_actual(
                            root,
                            view._game_time_value_label,
                        )["size"]
                        == sidebar_size
                    )
                    assert (
                        view._navigation_frame.winfo_rooty()
                        + view._navigation_frame.winfo_height()
                        <= game_time_card.winfo_rooty()
                    )
                    assert (
                        game_time_card.winfo_rooty()
                        + game_time_card.winfo_height()
                        <= view._sidebar.winfo_rooty()
                        + view._sidebar.winfo_height()
                    )
                    assert (
                        view._game_time_title_label.winfo_rooty()
                        + view._game_time_title_label.winfo_height()
                        <= view._game_time_value_label.winfo_rooty()
                    )
                    for clock_label in (
                        view._game_time_title_label,
                        view._game_time_value_label,
                    ):
                        assert (
                            clock_label.winfo_reqwidth()
                            <= clock_label.winfo_width()
                        )
                        assert (
                            clock_label.winfo_reqheight()
                            <= clock_label.winfo_height()
                        )

                    view.show_page("settings")
                    root.update()
                    assert (
                        _font_actual(root, theme_card.title_label)["family"]
                        == resolved_family
                    )
                    assert (
                        _font_actual(root, theme_card.title_label)["size"]
                        == content_size
                    )
                    assert (
                        _font_actual(root, view._ui_font_menu)["size"]
                        == content_size
                    )
                    assert (
                        view._ui_font_menu.winfo_reqwidth()
                        <= view._ui_font_menu.winfo_width()
                    )
                    settings_heading = next(
                        label
                        for label in view._page_heading_labels
                        if label.cget("text") == "設定"
                    )
                    assert (
                        _font_actual(root, settings_heading)["size"]
                        == CONTENT_HEADING_SIZES[content_size]
                    )
                    checked += 1

            assert checked == 24
            assert tuple(
                (str(widget), str(widget.cget("command")))
                for widget in _descendants(view._root)
                if isinstance(widget, Button)
            ) == button_commands
            assert view._navigation_frame.winfo_children()[0] is view._navigation_buttons["home"]
            assert not any(
                isinstance(widget, Label) and widget.cget("text") == "輔"
                for widget in view._sidebar.winfo_children()
            )
        finally:
            root.destroy()
    finally:
        assert service.close()
