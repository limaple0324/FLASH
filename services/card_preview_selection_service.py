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

    def _change_selection(
        self,
        selected_profile_id: str | None,
    ) -> CardPreviewSelectionState:
        previous_state = self._state
        previous_unavailable = self.unavailable_stored_profile_id
        previous_stored_profile_id = (
            previous_state.selected_profile_id
            if previous_state.selected_profile_id is not None
            else previous_unavailable
        )
        next_state = CardPreviewSelectionState(selected_profile_id)

        if self._store is not None:
            self._store.save(selected_profile_id)
        self._state = next_state
        self.unavailable_stored_profile_id = None
        try:
            self._notify_changed()
        except Exception as transition_error:
            self._state = previous_state
            self.unavailable_stored_profile_id = previous_unavailable
            rollback_storage_error: Exception | None = None
            if self._store is not None:
                try:
                    self._store.save(previous_stored_profile_id)
                except Exception as error:
                    rollback_storage_error = error

            if (
                previous_state != next_state
                or previous_unavailable is not None
            ):
                try:
                    self._notify_changed()
                except Exception:
                    pass

            if rollback_storage_error is not None:
                raise RuntimeError(
                    "Card preview selection failed and its persisted rollback failed."
                ) from transition_error
            raise
        return self._state

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
        return self._change_selection(profile.profile_id)

    def clear(self) -> CardPreviewSelectionState:
        return self._change_selection(None)
