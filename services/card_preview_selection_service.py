"""管理玩家明確選定的提醒卡預覽方案，不提供預設選擇。"""

from __future__ import annotations

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

    def snapshot(self) -> CardPreviewSelectionState:
        return self._state

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
        return self._state

    def clear(self) -> CardPreviewSelectionState:
        if self._store is not None:
            self._store.save(None)
        self._state = CardPreviewSelectionState()
        self.unavailable_stored_profile_id = None
        return self._state
