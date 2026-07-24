"""Structured SP1 self-checks for local and packaged verification."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from cards.history_store import CardHistoryStore
from cards.settings import CardDisplaySettings, CardDisplaySettingsResolution
from config.config_manager import ConfigManager
from config.path_manager import PathManager
from core.sp1_boundaries import ExternalAdapter, RecoveryBoundary, SmartReconnectBoundary
from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from domain.progress_store import ActivityProgressStore
from services.app_context import AppContext
from services.activity_progress_service import ActivityProgressService
from services.event_bus import EventBus
from services.card_history_service import CardHistoryService
from services.card_preview_selection_service import CardPreviewSelectionService
from services.card_preview_selection_store import CardPreviewSelectionStore
from services.logger_service import LoggerService


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    passed: bool
    message: str


class SelfCheck:
    """Run deterministic checks without requiring a game window."""

    def __init__(self, context: type[AppContext], paths: PathManager):
        self.context = context
        self.paths = paths

    def _run(self, name: str, check: Callable[[], str]) -> CheckResult:
        try:
            return CheckResult(name=name, passed=True, message=check())
        except Exception as exc:
            return CheckResult(name=name, passed=False, message=str(exc))

    def run_all(self) -> dict[str, object]:
        checks = [
            self._run("path_manager", self._check_paths),
            self._run("config_manager", self._check_config),
            self._run("logger_service", self._check_logger),
            self._run("event_bus", self._check_event_bus),
            self._run("window_registry", self._check_window_registry),
            self._run("activity_progress", self._check_activity_progress),
            self._run("card_history", self._check_card_history),
            self._run("card_display_settings", self._check_card_display_settings),
            self._run("card_preview_selection", self._check_card_preview_selection),
            self._run("recovery_boundary", lambda: self._check_optional(RecoveryBoundary)),
            self._run("smart_reconnect_boundary", lambda: self._check_optional(SmartReconnectBoundary)),
            self._run("external_adapter", lambda: self._check_optional(ExternalAdapter)),
        ]
        return {
            "passed": all(item.passed for item in checks),
            "checks": [asdict(item) for item in checks],
        }

    def _check_paths(self) -> str:
        config_path = self.paths.config_file("settings.json")
        log_path = self.paths.log_file("flash.log")
        for path in (config_path.parent, log_path.parent, self.paths.data_dir()):
            if not Path(path).exists():
                raise RuntimeError(f"Required directory is missing: {path}")
        return "Persistent directories are available."

    def _check_config(self) -> str:
        config = self.context.get(ConfigManager)
        if config is None:
            raise RuntimeError("ConfigManager is not registered.")
        version = config.get("version", "")
        sprint = config.get("sprint", "")
        if not version or sprint != "SP1":
            raise RuntimeError("SP1 configuration is incomplete.")
        if config.recovered_from_corruption:
            backup = config.corrupt_backup_path.name if config.corrupt_backup_path else "unknown"
            return f"Configuration was recovered from corruption; backup saved as {backup}."
        return f"Configuration loaded for SP1 version {version}."

    def _check_logger(self) -> str:
        logger = self.context.get(LoggerService)
        if logger is None:
            raise RuntimeError("LoggerService is not registered.")
        logger.info("FLASH SP1 self-check logger test.")
        if not self.paths.log_file("flash.log").exists():
            raise RuntimeError("Logger did not create flash.log.")
        return "Logger is writable."

    def _check_event_bus(self) -> str:
        bus = self.context.get(EventBus)
        if bus is None:
            raise RuntimeError("EventBus is not registered.")
        received: list[dict[str, object]] = []
        bus.subscribe("self_check", received.append)
        bus.publish("self_check", {"ok": True})
        if received != [{"ok": True}]:
            raise RuntimeError("Event bus did not deliver the test event.")
        return "Event bus delivery succeeded."

    def _check_window_registry(self) -> str:
        registry = self.context.get(WindowRegistry)
        store = self.context.get(WindowRegistryStore)
        if registry is None:
            raise RuntimeError("WindowRegistry is not registered.")
        if store is None:
            raise RuntimeError("WindowRegistryStore is not registered.")
        if store.path.parent != self.paths.data_dir():
            raise RuntimeError("Window registry storage path is outside the managed data directory.")
        if store.recovered_from_corruption:
            backup = store.corrupt_backup.name if store.corrupt_backup else "unknown"
            return f"Character registry recovered from corruption; backup saved as {backup}."
        return f"Character registry loaded with {len(registry.all())} character(s)."

    def _check_activity_progress(self) -> str:
        store = self.context.get(ActivityProgressStore)
        service = self.context.get(ActivityProgressService)
        if store is None:
            raise RuntimeError("ActivityProgressStore is not registered.")
        if service is None:
            raise RuntimeError("ActivityProgressService is not registered.")
        if store.path.parent != self.paths.data_dir():
            raise RuntimeError("Activity progress path is outside the managed data directory.")
        if store.recovered_from_corruption:
            backup = store.corrupt_backup.name if store.corrupt_backup else "unknown"
            return f"Activity progress recovered from corruption; backup saved as {backup}."
        return f"Activity progress loaded with {len(service.all())} record(s)."

    def _check_card_history(self) -> str:
        store = self.context.get(CardHistoryStore)
        service = self.context.get(CardHistoryService)
        if store is None:
            raise RuntimeError("CardHistoryStore is not registered.")
        if service is None:
            raise RuntimeError("CardHistoryService is not registered.")
        if service.store is not store:
            raise RuntimeError("Card history service does not use the registered store.")
        if store.path.parent != self.paths.data_dir():
            raise RuntimeError("Card history path is outside the managed data directory.")
        if store.recovered_from_corruption:
            backup = store.corrupt_backup.name if store.corrupt_backup else "unknown"
            return f"Card history recovered from corruption; backup saved as {backup}."
        return f"Card history loaded with {len(service.all())} record(s)."

    def _check_card_display_settings(self) -> str:
        settings = self.context.get(CardDisplaySettings)
        resolution = self.context.get(CardDisplaySettingsResolution)
        if settings is None:
            raise RuntimeError("CardDisplaySettings is not registered.")
        if resolution is None:
            raise RuntimeError("CardDisplaySettingsResolution is not registered.")
        if resolution.settings is not settings:
            raise RuntimeError(
                "Card display settings resolution does not use the registered settings."
            )

        seconds = settings.lifetime_seconds
        if resolution.recovered_from_invalid:
            return (
                "Card lifetime setting was invalid; "
                f"using safe default of {seconds} seconds."
            )
        if not resolution.configured:
            return f"Card lifetime uses default of {seconds} seconds."
        return f"Card lifetime is configured to {seconds} seconds."

    def _check_card_preview_selection(self) -> str:
        service = self.context.get(CardPreviewSelectionService)
        store = self.context.get(CardPreviewSelectionStore)
        if service is None:
            if store is not None:
                raise RuntimeError(
                    "Card preview selection store is registered without its service."
                )
            return "Card overlay is not configured; no preview catalog is registered."
        if store is None:
            raise RuntimeError("CardPreviewSelectionStore is not registered.")
        if store.path.parent != self.paths.data_dir():
            raise RuntimeError(
                "Card preview selection path is outside the managed data directory."
            )
        if store.recovered_from_corruption:
            backup = store.corrupt_backup.name if store.corrupt_backup else "unknown"
            return (
                "Card overlay is disabled because its selection was corrupt; "
                f"backup saved as {backup}."
            )
        unavailable = service.unavailable_stored_profile_id
        if unavailable is not None:
            return (
                "Card overlay is disabled because the saved preview profile is "
                f"unavailable: {unavailable}."
            )
        state = service.snapshot()
        if not state.overlay_enabled:
            return "Card overlay is configured; the player has not selected a preview profile."
        return (
            "Card overlay is ready with selected preview profile "
            f"{state.selected_profile_id}."
        )

    def _check_optional(self, contract: type[object]) -> str:
        service = self.context.get(contract)
        if service is None:
            return "Boundary is defined; concrete adapter is not registered yet."
        if not isinstance(service, contract):
            raise RuntimeError(f"Registered service does not satisfy {contract.__name__}.")
        return f"Registered {contract.__name__} implementation is valid."
