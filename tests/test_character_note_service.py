import pytest

from core.window_registry import WindowHealth, WindowRegistry
from core.window_registry_store import WindowRegistryStore
from main import build_services
from services.app_context import AppContext
from services.character_note_service import CharacterNoteService
from services.identity_data_transaction_coordinator import (
    IdentityDataTransactionCoordinator,
)


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
    registry.confirm_window(
        "char-a",
        handle=321,
        process_id=9520,
        window_class="GameWindow",
        rect=(0, 0, 800, 600),
        health=WindowHealth.READY,
    )
    coordinator = IdentityDataTransactionCoordinator()
    store = WindowRegistryStore(tmp_path / "window_registry.json", coordinator)
    store.save(registry)
    service = CharacterNoteService(registry, store, coordinator)

    updated = service.set_note("char-a", " 守紀優先 ")

    assert updated.note == "守紀優先"
    assert registry.get("char-a").note == "守紀優先"
    assert registry.get("char-a").handle == 321
    assert registry.get("char-a").health is WindowHealth.READY
    assert registry.get("char-a").confirmed is True
    assert store.load().get("char-a").note == "守紀優先"


def test_clear_note_persists_none(tmp_path) -> None:
    registry = _registry()
    coordinator = IdentityDataTransactionCoordinator()
    store = WindowRegistryStore(tmp_path / "window_registry.json", coordinator)
    store.save(registry)

    CharacterNoteService(registry, store, coordinator).clear_note("char-a")

    assert registry.get("char-a").note is None
    assert store.load().get("char-a").note is None


def test_save_failure_keeps_live_note_unchanged(tmp_path) -> None:
    registry = _registry()
    coordinator = IdentityDataTransactionCoordinator()

    class FailingStore(WindowRegistryStore):
        def stage_save(self, _transaction, _registry):
            raise OSError("disk unavailable")

    service = CharacterNoteService(
        registry,
        FailingStore(tmp_path / "window_registry.json", coordinator),
        coordinator,
    )

    with pytest.raises(OSError, match="disk unavailable"):
        service.set_note("char-a", "不可套用")

    assert registry.get("char-a").note == "原本備註"


def test_empty_note_requires_explicit_clear(tmp_path) -> None:
    coordinator = IdentityDataTransactionCoordinator()
    service = CharacterNoteService(
        _registry(),
        WindowRegistryStore(tmp_path / "window_registry.json", coordinator),
        coordinator,
    )

    with pytest.raises(ValueError, match="clear_note"):
        service.set_note("char-a", "  ")


def test_note_service_rejects_store_using_another_coordinator(tmp_path) -> None:
    owner = IdentityDataTransactionCoordinator()
    other = IdentityDataTransactionCoordinator()
    store = WindowRegistryStore(tmp_path / "window_registry.json", owner)

    with pytest.raises(ValueError, match="injected coordinator"):
        CharacterNoteService(_registry(), store, other)


def test_memory_publish_failure_restores_file_and_exact_live_runtime(
    tmp_path,
    monkeypatch,
) -> None:
    registry = _registry()
    registry.confirm_window(
        "char-a",
        handle=987,
        process_id=456,
        window_class="GameWindow",
        rect=(10, 20, 810, 620),
        health=WindowHealth.WARNING,
    )
    coordinator = IdentityDataTransactionCoordinator()
    store = WindowRegistryStore(tmp_path / "window_registry.json", coordinator)
    store.save(registry)
    before_file = store.path.read_bytes()
    before_runtime = registry.get("char-a")
    service = CharacterNoteService(registry, store, coordinator)
    original_replace = registry.replace_runtime
    calls = 0

    def fail_first_publish(candidate: WindowRegistry) -> None:
        nonlocal calls
        calls += 1
        original_replace(candidate)
        if calls == 1:
            raise OSError("live publish interrupted")

    monkeypatch.setattr(registry, "replace_runtime", fail_first_publish)

    with pytest.raises(OSError, match="live publish interrupted"):
        service.set_note("char-a", "不可保留")

    assert store.path.read_bytes() == before_file
    assert registry.get("char-a") == before_runtime
    assert registry.get("char-a").handle == 987
    assert registry.get("char-a").health is WindowHealth.WARNING


def test_build_services_registers_confirmed_note_service(tmp_path) -> None:
    build_services(root=tmp_path)

    assert isinstance(AppContext.get(CharacterNoteService), CharacterNoteService)
