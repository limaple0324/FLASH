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

    def start(self) -> bool:
        if self._started:
            return False
        self._started = True
        self._selection.subscribe(self.sync_selection)
        return self.sync_selection()

    def sync_selection(self) -> bool:
        if not self._started:
            return False

        profile = self._selection.selected_profile()
        profile_id = profile.profile_id if profile is not None else None
        if profile_id == self._active_profile_id:
            return False

        replacement: CardOverlayRuntime | None = None
        if profile is not None:
            try:
                replacement = self._runtime_factory(profile)
                if replacement.start() is False:
                    raise RuntimeError("overlay start failed")
                runtime_error = getattr(replacement, "last_error", None)
                if isinstance(runtime_error, Exception):
                    raise runtime_error
            except Exception:
                if replacement is not None:
                    try:
                        replacement.stop()
                    except Exception:
                        pass
                raise

        previous = self._runtime
        if previous is not None:
            try:
                if previous.stop() is False:
                    raise RuntimeError("overlay stop failed")
            except Exception:
                if replacement is not None:
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
        runtime = self._runtime
        if runtime is not None and runtime.stop() is False:
            raise RuntimeError("overlay stop failed")
        self._started = False
        self._selection.unsubscribe(self.sync_selection)
        self._runtime = None
        self._active_profile_id = None
        return True
