import pytest

from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from main import build_services
from services.app_context import AppContext
from services.character_note_service import CharacterNoteService


def _registry() -> WindowRegistry:
    registry = WindowRegistry()
    registry.register_character(
        "char-a",
        "角色甲",
        group="120",
        note="原本備註",
    )
    return registry


def test_note_is_saved_before_live_registry_changes(tmp_path) -> None:
    registry = _registry()
    store = WindowRegistryStore(tmp_path / "window_registry.json")
    store.save(registry)
    service = CharacterNoteService(registry, store)

    updated = service.set_note("char-a", " 守紀優先 ")

    assert updated.note == "守紀優先"
    assert registry.get("char-a").note == "守紀優先"
    assert store.load().get("char-a").note == "守紀優先"


def test_clear_note_persists_none(tmp_path) -> None:
    registry = _registry()
    store = WindowRegistryStore(tmp_path / "window_registry.json")
    store.save(registry)

    CharacterNoteService(registry, store).clear_note("char-a")

    assert registry.get("char-a").note is None
    assert store.load().get("char-a").note is None


def test_save_failure_keeps_live_note_unchanged(tmp_path) -> None:
    registry = _registry()

    class FailingStore(WindowRegistryStore):
        def save(self, _registry):
            raise OSError("disk unavailable")

    service = CharacterNoteService(
        registry,
        FailingStore(tmp_path / "window_registry.json"),
    )

    with pytest.raises(OSError, match="disk unavailable"):
        service.set_note("char-a", "不可套用")

    assert registry.get("char-a").note == "原本備註"


def test_empty_note_requires_explicit_clear(tmp_path) -> None:
    service = CharacterNoteService(
        _registry(),
        WindowRegistryStore(tmp_path / "window_registry.json"),
    )

    with pytest.raises(ValueError, match="clear_note"):
        service.set_note("char-a", "  ")


def test_build_services_registers_confirmed_note_service(tmp_path) -> None:
    build_services(root=tmp_path)

    assert isinstance(AppContext.get(CharacterNoteService), CharacterNoteService)
