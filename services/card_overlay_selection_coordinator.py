"""依玩家明確選定的預覽方案管理提醒卡浮層執行階段。"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from services.card_preview_selection_service import CardPreviewSelectionService
from ui.card_preview_settings import CardPreviewProfile


class CardOverlayRuntime(Protocol):
    def start(self) -> None: ...

    def stop(self) -> None: ...


CardOverlayRuntimeFactory = Callable[[CardPreviewProfile], CardOverlayRuntime]


class CardOverlaySelectionCoordinator:
    """Only a currently selected catalog profile may own a running overlay."""

    def __init__(
        self,
        selection: CardPreviewSelectionService,
        runtime_factory: CardOverlayRuntimeFactory,
    ) -> None:
        if not isinstance(selection, CardPreviewSelectionService):
            raise TypeError("selection must be CardPreviewSelectionService.")
        if not callable(runtime_factory):
            raise TypeError("runtime_factory must be callable.")
        self._selection = selection
        self._runtime_factory = runtime_factory
        self._runtime: CardOverlayRuntime | None = None
        self._active_profile_id: str | None = None
        self._started = False

    @property
    def started(self) -> bool:
        return self._started

    @property
    def active_profile_id(self) -> str | None:
        return self._active_profile_id

    def start(self) -> bool:
        if self._started:
            return False
        self._started = True
        self._selection.subscribe(self.sync_selection)
        try:
            return self.sync_selection()
        except Exception:
            self._selection.unsubscribe(self.sync_selection)
            self._started = False
            raise

    def sync_selection(self) -> bool:
        if not self._started:
            return False

        profile = self._selection.selected_profile()
        profile_id = profile.profile_id if profile is not None else None
        if profile_id == self._active_profile_id:
            return False

        replacement = self._runtime_factory(profile) if profile is not None else None
        previous = self._runtime
        if previous is not None:
            previous.stop()
        self._runtime = None
        self._active_profile_id = None

        if replacement is None:
            return True
        try:
            replacement.start()
        except Exception:
            try:
                replacement.stop()
            except Exception:
                pass
            raise
        self._runtime = replacement
        self._active_profile_id = profile_id
        return True

    def stop(self) -> bool:
        if not self._started:
            return False
        self._started = False
        self._selection.unsubscribe(self.sync_selection)
        runtime = self._runtime
        self._runtime = None
        self._active_profile_id = None
        if runtime is not None:
            runtime.stop()
        return True
