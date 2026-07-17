"""管理玩家明確選定的提醒卡預覽方案，不提供預設選擇。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ui.card_preview_settings import CardPreviewCatalog, CardPreviewProfile


class CardPreviewSelectionStorage(Protocol):
    def load(self) -> str | None: ...

    def save(self, selected_profile_id: str | None) -> None: ...


@dataclass(frozen=True, slots=True)
class CardPreviewSelectionState:
    selected_profile_id: str | None = None

    @property
    def overlay_enabled(self) -> bool:
        return self.selected_profile_id is not None


@dataclass(frozen=True, slots=True)
class CardPreviewChoice:
    """Read-only candidate metadata safe for player-facing selection views."""

    profile_id: str
    display_name: str
    selected: bool


class CardPreviewSelectionService:
    """Only an explicit catalog selection can enable the preview overlay."""

    def __init__(
        self,
        catalog: CardPreviewCatalog,
        store: CardPreviewSelectionStorage | None = None,
    ) -> None:
        if not isinstance(catalog, CardPreviewCatalog):
            raise TypeError("catalog must be CardPreviewCatalog.")
        self._catalog = catalog
        self._store = store
        self._state = CardPreviewSelectionState()
        self._change_listeners: list[Callable[[], None]] = []
        self.unavailable_stored_profile_id: str | None = None
        if store is not None:
            stored_profile_id = store.load()
            if stored_profile_id is not None:
                try:
                    profile = self._catalog.select(stored_profile_id)
                except KeyError:
                    self.unavailable_stored_profile_id = stored_profile_id
                else:
                    self._state = CardPreviewSelectionState(profile.profile_id)

    def subscribe(self, listener: Callable[[], None]) -> None:
        if not callable(listener):
            raise TypeError("listener must be callable.")
        if listener not in self._change_listeners:
            self._change_listeners.append(listener)

    def unsubscribe(self, listener: Callable[[], None]) -> None:
        if listener in self._change_listeners:
            self._change_listeners.remove(listener)

    def _notify_changed(self) -> None:
        for listener in tuple(self._change_listeners):
            listener()

    def snapshot(self) -> CardPreviewSelectionState:
        return self._state

    def available_choices(self) -> tuple[CardPreviewChoice, ...]:
        selected_profile_id = self._state.selected_profile_id
        return tuple(
            CardPreviewChoice(
                profile_id=profile.profile_id,
                display_name=profile.display_name,
                selected=profile.profile_id == selected_profile_id,
            )
            for profile in self._catalog.profiles
        )

    def selected_profile(self) -> CardPreviewProfile | None:
        profile_id = self._state.selected_profile_id
        if profile_id is None:
            return None
        return self._catalog.select(profile_id)

    def select(self, profile_id: str) -> CardPreviewSelectionState:
        profile = self._catalog.select(profile_id)
        if self._store is not None:
            self._store.save(profile.profile_id)
        self._state = CardPreviewSelectionState(
            selected_profile_id=profile.profile_id,
        )
        self.unavailable_stored_profile_id = None
        self._notify_changed()
        return self._state

    def clear(self) -> CardPreviewSelectionState:
        if self._store is not None:
            self._store.save(None)
        self._state = CardPreviewSelectionState()
        self.unavailable_stored_profile_id = None
        self._notify_changed()
        return self._state
