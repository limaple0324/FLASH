"""管理玩家明確選定的提醒卡預覽方案，不提供預設選擇。"""

from __future__ import annotations

from dataclasses import dataclass

from ui.card_preview_settings import CardPreviewCatalog, CardPreviewProfile


@dataclass(frozen=True, slots=True)
class CardPreviewSelectionState:
    selected_profile_id: str | None = None

    @property
    def overlay_enabled(self) -> bool:
        return self.selected_profile_id is not None


class CardPreviewSelectionService:
    """Only an explicit catalog selection can enable the preview overlay."""

    def __init__(self, catalog: CardPreviewCatalog) -> None:
        if not isinstance(catalog, CardPreviewCatalog):
            raise TypeError("catalog must be CardPreviewCatalog.")
        self._catalog = catalog
        self._state = CardPreviewSelectionState()

    def snapshot(self) -> CardPreviewSelectionState:
        return self._state

    def selected_profile(self) -> CardPreviewProfile | None:
        profile_id = self._state.selected_profile_id
        if profile_id is None:
            return None
        return self._catalog.select(profile_id)

    def select(self, profile_id: str) -> CardPreviewSelectionState:
        profile = self._catalog.select(profile_id)
        self._state = CardPreviewSelectionState(
            selected_profile_id=profile.profile_id,
        )
        return self._state

    def clear(self) -> CardPreviewSelectionState:
        self._state = CardPreviewSelectionState()
        return self._state
