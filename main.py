"""Cumulative FLASH desktop entrypoint."""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from tkinter import PhotoImage, TclError, Tk, filedialog, messagebox

from adapters.background_capability import BackgroundCapabilityProbe
from adapters.windows_background_capture import WindowsBackgroundCaptureBackend
from adapters.windows_app_identity import (
    configure_process_app_identity,
    configure_tk_window_app_identity,
)
from adapters.windows_input_sync import (
    WindowInputPolicy,
    WindowsInputSyncController,
    normalize_input_policy,
)
from adapters.windows_launch_fingerprint import (
    PowerShellLaunchFingerprintResolver,
    PowerShellShortcutFingerprintResolver,
    normalize_launch_fingerprint,
)
from adapters.windows_smart_reconnect import WindowsSmartReconnectController
from adapters.windows_pointer_sync import WindowsPointerSyncController
from adapters.windows_target_desktop_verifier import TargetDesktopVerifier
from adapters.windows_window import Win32WindowBackend, WindowsWindowAdapter
from adapters.windows_work_area import WindowsWorkAreaReader
from cards.history_store import CardHistoryStore
from cards.service import CardService
from cards.settings import (
    CardDisplaySettings,
    CardDisplaySettingsResolution,
    resolve_card_display_settings,
)
from config.config_manager import ConfigManager
from config.path_manager import PathManager
from core.bootstrap import Bootstrap
from core.sp1_boundaries import ExternalAdapter, SmartReconnectBoundary
from core.reconnect_policy import ReconnectScreenState
from core.target_window_observation import TargetWindowObservation
from core.window_registry import WindowRegistry
from core.version import MILESTONE
from core.window_registry_store import WindowRegistryStore
from decision.service import DecisionService
from domain.activity_schedule import (
    ActivityScheduleCatalog,
    build_confirmed_activity_catalog,
)
from domain.character_store import CharacterStore
from domain.progress_store import ActivityProgressStore
from habit.service import ActivityOrderHabitService
from habit.store import ActivityOrderHabitStore
from services.activity_progress_service import ActivityProgressService
from services.activity_reminder_monitor import ActivityReminderMonitor
from services.activity_reminder_service import ActivityReminderService
from services.activity_schedule_view_service import ActivityScheduleViewService
from services.app_context import AppContext
from services.auto_click_service import (
    AutoClickHotkeyMonitor,
    AutoClickService,
    AutoClickSettings,
    Win32CursorClickBackend,
)
from services.card_coordinator import CardCoordinator
from services.card_display_settings_service import CardDisplaySettingsService
from services.card_expiry_monitor import CardExpiryMonitor
from services.card_history_service import CardHistoryService
from services.card_overlay_layout_service import CardOverlayLayoutService
from services.card_overlay_runtime import build_windows_card_overlay_runtime
from services.card_view_state_service import CardViewStateService
from services.character_detail_view_service import CharacterDetailViewService
from services.character_detail_choice_service import CharacterDetailChoiceService
from services.character_note_service import CharacterNoteService
from services.character_view_service import CharacterViewService
from services.event_bus import EventBus
from services.group_selection_service import (
    GroupSelectionService,
    default_legacy_group_config_path,
)
from services.group_configuration_service import (
    GroupConfigurationService,
    SyncCycleError,
)
from services.group_launch_service import GroupLaunchPlan, GroupLaunchService
from services.group_role_status_monitor import GroupRoleStatusMonitor
from services.group_role_status_service import GroupRoleStatusService
from services.keyboard_sync_monitor import KeyboardSyncMonitor
from services.logger_service import LoggerService
from services.mouse_sync_monitor import MouseSyncMonitor
from services.reconnect_failure_status_service import (
    ReconnectFailureStatusService,
)
from services.smart_reconnect_monitor import SmartReconnectMonitor
from services.sync_scope_service import SyncScopeService
from services.sync_conflict_arbiter import SyncConflictArbiter
from services.deferred_sync_operation_service import (
    DeferredSyncOperationMonitor,
    DeferredSyncOperationService,
)
from services.sync_operation_record_store import (
    SyncOperationRecordStore,
)
from services.target_window_state_service import (
    TARGET_WINDOW_OBSERVED_EVENT,
    TargetWindowStateService,
)
from ui.home import HomeView
from ui.character_detail_window import CharacterDetailWindow
from ui.card_overlay import CardSize
from ui.tk_card_presenter import TkCardTextSettings
from workspace.models import WorkspaceState
from workspace.service import WorkspaceService

APP_TITLE = "輔"
SELF_CHECK_ARGUMENT = "--self-check"
TARGET_DESKTOP_VERIFY_ARGUMENT = "--verify-target-desktop"
TARGET_WINDOW_KEY = "target_window_keywords"
TARGET_WINDOW_FINGERPRINT_KEY = "target_window_fingerprint"
INPUT_POLICY_KEY = "input_policy"
SMART_RECONNECT_ENABLED_KEY = "smart_reconnect_enabled"
SMART_RECONNECT_CONSENT_KEY = "smart_reconnect_consent_v1"
CURRENT_GROUP_NAME_KEY = "current_group_name"
REGISTRY_FILENAME = "window_registry.json"
RECONNECT_STATE_FILENAME = "smart_reconnect_state.json"
GROUP_CONFIGURATION_FILENAME = "group_configuration.json"
OPERATION_RECORD_FILENAME = "operation_records.json"
OPERATION_RECORD_ARCHIVE_DIRNAME = "角色每日紀錄"
DEFERRED_SYNC_STATE_FILENAME = "deferred_sync_operations.json"
TARGET_DESKTOP_REPORT_FILENAME = "target_desktop_verification.json"
CHARACTER_FILENAME = "characters.json"
ACTIVITY_PROGRESS_FILENAME = "activity_progress.json"
CARD_HISTORY_FILENAME = "card_history.json"
ACTIVITY_ORDER_HABIT_FILENAME = "activity_order_habit.json"
APP_ICON_PNG = Path("assets") / "flash_icon.png"
APP_ICON_ICO = Path("assets") / "flash_icon.ico"
RECONNECT_REFERENCE_DIR = Path("assets") / "reconnect_reference"


def resource_path(relative_path: Path) -> Path:
    """Resolve files both from source and from a PyInstaller bundle."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / relative_path
    return Path(__file__).resolve().parent / relative_path


def apply_window_icon(window: Tk) -> None:
    ico_path = resource_path(APP_ICON_ICO)
    if sys.platform == "win32" and ico_path.exists():
        try:
            window.iconbitmap(str(ico_path))
            # The known-good 輔V0.2 implementation uses only iconbitmap on
            # Windows. Calling iconphoto or overriding WM_SETICON afterwards
            # makes Windows 11 fall back to the Python/Tk taskbar icon.
            return
        except TclError:
            pass
    png_path = resource_path(APP_ICON_PNG)
    if png_path.exists():
        try:
            icon = PhotoImage(file=str(png_path))
            window.iconphoto(True, icon)
            window._flash_icon = icon
        except TclError:
            pass


def taskbar_icon_resource() -> str:
    """Return the exact confirmed plus icon resource used by Windows."""
    if bool(getattr(sys, "frozen", False)):
        return f"{Path(sys.executable).resolve()},0"
    return f"{resource_path(APP_ICON_ICO).resolve()},0"


def _normalize_window_keywords(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _normalize_window_fingerprint(value: object) -> str | None:
    return normalize_launch_fingerprint(value)


def build_services(root: Path | None = None):
    """Create, load, and register the cumulative SP1+SP2 services."""
    AppContext.clear()
    paths = PathManager(root=root)
    logger = LoggerService(paths.log_file("flash.log"))
    config = ConfigManager(paths.config_file("settings.json"))
    config.ensure_defaults(
        {
            INPUT_POLICY_KEY: WindowInputPolicy.ALL.value,
            SMART_RECONNECT_ENABLED_KEY: False,
            SMART_RECONNECT_CONSENT_KEY: False,
        }
    )
    event_bus = EventBus(logger=logger)
    target_window_state_service = TargetWindowStateService(event_bus, logger)
    card_display_settings_resolution = resolve_card_display_settings(config.data)

    registry_store = WindowRegistryStore(paths.data_dir() / REGISTRY_FILENAME)
    registry = registry_store.load()
    character_store = CharacterStore(paths.data_dir() / CHARACTER_FILENAME)
    characters = character_store.load()
    character_view_service = CharacterViewService(registry, characters)
    character_detail_view_service = CharacterDetailViewService(character_view_service)
    character_note_service = CharacterNoteService(registry, registry_store)
    legacy_group_config_path = (
        default_legacy_group_config_path()
        if root is None
        else paths.root / ".legacy-group-import-disabled"
    )
    shortcut_fingerprint_resolver = PowerShellShortcutFingerprintResolver()
    group_configuration_service = GroupConfigurationService(
        paths.data_dir() / GROUP_CONFIGURATION_FILENAME,
        legacy_config_path=legacy_group_config_path,
    )
    group_selection_service = GroupSelectionService(
        registry,
        legacy_config_path=group_configuration_service.path,
    )
    group_launch_service = GroupLaunchService(
        group_configuration_service.path,
        shortcut_fingerprint_resolver,
    )
    sync_scope_service = SyncScopeService(
        group_configuration_service,
        shortcut_fingerprint_resolver,
    )
    progress_store = ActivityProgressStore(
        paths.data_dir() / ACTIVITY_PROGRESS_FILENAME
    )
    progress_service = ActivityProgressService(progress_store)
    activity_schedule_catalog = build_confirmed_activity_catalog()
    activity_schedule_view_service = ActivityScheduleViewService(
        activity_schedule_catalog
    )
    for rule in activity_schedule_catalog.all():
        progress_service.register_definition(rule.definition)
    decision_service = DecisionService()
    activity_order_habit_store = ActivityOrderHabitStore(
        paths.data_dir() / ACTIVITY_ORDER_HABIT_FILENAME
    )
    activity_order_habit_service = ActivityOrderHabitService(
        activity_order_habit_store
    )
    initial_group_choice = group_selection_service.initial_choice(
        config.get(CURRENT_GROUP_NAME_KEY)
    )
    workspace_service = WorkspaceService(
        WorkspaceState(
            current_group=(
                group_selection_service.workspace_group(initial_group_choice)
                if initial_group_choice is not None
                else None
            ),
            next_step=(
                "查看目前需要注意的內容"
                if initial_group_choice is not None
                else "選擇組別"
            ),
        )
    )
    card_history_store = CardHistoryStore(paths.data_dir() / CARD_HISTORY_FILENAME)
    card_history_service = CardHistoryService(card_history_store)
    card_service = CardService(card_display_settings_resolution.settings)

    def register_card_display_settings(
        resolution: CardDisplaySettingsResolution,
    ) -> None:
        AppContext.register(CardDisplaySettings, resolution.settings)
        AppContext.register(CardDisplaySettingsResolution, resolution)

    card_display_settings_service = CardDisplaySettingsService(
        config,
        card_service,
        card_display_settings_resolution,
        on_changed=register_card_display_settings,
    )
    card_coordinator = CardCoordinator(card_service, card_history_service)
    activity_reminder_service = ActivityReminderService(
        activity_schedule_catalog,
        card_coordinator,
        workspace_service.snapshot,
    )
    card_view_state_service = CardViewStateService(card_service)
    reconnect_failure_status_service = ReconnectFailureStatusService()

    def configured_role_names() -> tuple[str, ...]:
        names: list[str] = []
        for group in group_configuration_service.groups():
            plan = group_launch_service.plan(group.name)
            candidates = (
                tuple(target.display_name for target in plan.targets)
                if plan.ready
                else tuple(entry.display_name for entry in group.entries)
            )
            for name in candidates:
                if name not in names:
                    names.append(name)
        return tuple(names)

    operation_record_store = SyncOperationRecordStore(
        paths.data_dir() / OPERATION_RECORD_FILENAME,
        paths.data_dir() / OPERATION_RECORD_ARCHIVE_DIRNAME,
        role_names_provider=configured_role_names,
    )

    AppContext.register(PathManager, paths)
    AppContext.register(LoggerService, logger)
    AppContext.register(ConfigManager, config)
    AppContext.register(
        CardDisplaySettings,
        card_display_settings_resolution.settings,
    )
    AppContext.register(
        CardDisplaySettingsResolution,
        card_display_settings_resolution,
    )
    AppContext.register(EventBus, event_bus)
    AppContext.register(TargetWindowStateService, target_window_state_service)
    AppContext.register(WindowRegistryStore, registry_store)
    AppContext.register(WindowRegistry, registry)
    AppContext.register(CharacterStore, character_store)
    AppContext.register(CharacterViewService, character_view_service)
    AppContext.register(CharacterDetailViewService, character_detail_view_service)
    AppContext.register(CharacterNoteService, character_note_service)
    AppContext.register(GroupSelectionService, group_selection_service)
    AppContext.register(
        GroupConfigurationService,
        group_configuration_service,
    )
    AppContext.register(GroupLaunchService, group_launch_service)
    AppContext.register(SyncScopeService, sync_scope_service)
    AppContext.register(ActivityProgressStore, progress_store)
    AppContext.register(ActivityProgressService, progress_service)
    AppContext.register(ActivityScheduleCatalog, activity_schedule_catalog)
    AppContext.register(ActivityScheduleViewService, activity_schedule_view_service)
    AppContext.register(DecisionService, decision_service)
    AppContext.register(ActivityOrderHabitStore, activity_order_habit_store)
    AppContext.register(ActivityOrderHabitService, activity_order_habit_service)
    AppContext.register(WorkspaceService, workspace_service)
    AppContext.register(CardHistoryStore, card_history_store)
    AppContext.register(CardHistoryService, card_history_service)
    AppContext.register(CardService, card_service)
    AppContext.register(CardDisplaySettingsService, card_display_settings_service)
    AppContext.register(CardCoordinator, card_coordinator)
    AppContext.register(ActivityReminderService, activity_reminder_service)
    AppContext.register(CardViewStateService, card_view_state_service)
    AppContext.register(
        ReconnectFailureStatusService,
        reconnect_failure_status_service,
    )

    def role_name_for_fingerprint(fingerprint: str) -> str:
        matches: list[str] = []
        for group in group_configuration_service.groups():
            plan = group_launch_service.plan(group.name)
            target = (
                plan.target_for_fingerprint(fingerprint)
                if plan.ready
                else None
            )
            if target is not None and target.display_name not in matches:
                matches.append(target.display_name)
        return matches[0] if len(matches) == 1 else "未知角色"

    AppContext.register(SyncOperationRecordStore, operation_record_store)
    reconnect_controller = WindowsSmartReconnectController.for_real_windows(
        reference_dir=resource_path(RECONNECT_REFERENCE_DIR),
        state_path=paths.data_dir() / RECONNECT_STATE_FILENAME,
        failure_status_service=reconnect_failure_status_service,
        failure_record_callback=lambda role_name, detail: (
            operation_record_store.append(
                "智慧重連",
                role_name,
                detail,
            )
        ),
    )
    deferred_sync_service = DeferredSyncOperationService(
        state_path=paths.data_dir() / DEFERRED_SYNC_STATE_FILENAME,
        on_failure=lambda record: operation_record_store.append(
            "同步補做",
            role_name_for_fingerprint(record.target_id),
            f"{record.operation}－{record.failure_code}",
        )
    )
    AppContext.register(
        DeferredSyncOperationService,
        deferred_sync_service,
    )
    sync_conflict_arbiter = SyncConflictArbiter(
        on_conflict=lambda record: (
            logger.warning(
                "Overlapping synchronized operation skipped; "
                f"active={record.active_operation}; "
                f"skipped={record.skipped_operation}"
            ),
            operation_record_store.append(
                "同步衝突",
                role_name_for_fingerprint(record.target_id),
                (
                    f"已執行 {record.active_operation}；"
                    f"略過 {record.skipped_operation}"
                ),
            ),
        )
    )
    AppContext.register(SyncConflictArbiter, sync_conflict_arbiter)
    AppContext.register(
        WindowsInputSyncController,
        WindowsInputSyncController.for_real_windows(
            conflict_arbiter=sync_conflict_arbiter,
            deferred_service=deferred_sync_service,
            reconnecting_provider=(
                reconnect_controller.reconnecting_fingerprints
            ),
            role_operation_callback=lambda fingerprint, operation, outcome: (
                operation_record_store.append(
                    "同步操作",
                    role_name_for_fingerprint(fingerprint),
                    f"{operation}－{outcome}",
                )
            ),
            screen_state_provider=lambda fingerprint: (
                reconnect_controller.observe_screen_states(
                    (fingerprint,)
                ).get(fingerprint)
            ),
        ),
    )
    AppContext.register(
        WindowsPointerSyncController,
        WindowsPointerSyncController.for_real_windows(
            conflict_arbiter=sync_conflict_arbiter,
            deferred_service=deferred_sync_service,
            reconnecting_provider=(
                reconnect_controller.reconnecting_fingerprints
            ),
            role_operation_callback=lambda fingerprint, operation, outcome: (
                operation_record_store.append(
                    "同步操作",
                    role_name_for_fingerprint(fingerprint),
                    f"{operation}－{outcome}",
                )
            ),
            screen_state_provider=lambda fingerprint: (
                reconnect_controller.observe_screen_states(
                    (fingerprint,)
                ).get(fingerprint)
            ),
        ),
    )
    AppContext.register(
        WindowsSmartReconnectController,
        reconnect_controller,
    )
    AppContext.register(SmartReconnectBoundary, reconnect_controller)
    AppContext.register(
        GroupRoleStatusService,
        GroupRoleStatusService(
            group_launch_service,
            Win32WindowBackend(PowerShellLaunchFingerprintResolver()),
            reconnect_failure_status_service,
            screen_states_provider=reconnect_controller.role_screen_states,
            reconnecting_provider=(
                reconnect_controller.reconnecting_fingerprints
            ),
            record_callback=operation_record_store.append,
        ),
    )
    AppContext.register(
        DeferredSyncOperationMonitor,
        DeferredSyncOperationMonitor(
            deferred_sync_service,
            reconnect_controller.reconnecting_fingerprints,
            lambda: tuple(
                item.key.removeprefix("role:")
                for item in reconnect_failure_status_service.snapshot()
                if item.key.startswith("role:")
            ),
            ready_provider=lambda: tuple(
                fingerprint
                for fingerprint, screen_state
                in reconnect_controller.observe_screen_states(
                    deferred_sync_service.pending_targets()
                ).items()
                if screen_state is ReconnectScreenState.CONNECTED
            ),
        ),
    )
    AppContext.register(
        SmartReconnectMonitor,
        SmartReconnectMonitor(reconnect_controller, logger=logger),
    )

    if registry_store.recovered_from_corruption:
        logger.warning(
            "Character window registry was corrupt and has been rebuilt; "
            f"backup={registry_store.corrupt_backup}"
        )
    else:
        logger.info(f"Character window registry loaded: {len(registry.all())} character(s).")

    if character_store.recovered_from_corruption:
        recovery = (
            "recovered from the last valid backup"
            if character_store.recovered_from_backup
            else "rebuilt empty without guessing data"
        )
        logger.warning(
            "Character profiles were corrupt and isolated; "
            f"{recovery}; backup={character_store.corrupt_backup}"
        )
    else:
        logger.info(f"Character profiles loaded: {len(characters)} character(s).")

    if progress_store.recovered_from_corruption:
        logger.warning(
            "Activity progress was corrupt and has been rebuilt; "
            f"backup={progress_store.corrupt_backup}"
        )
    else:
        logger.info(f"Activity progress loaded: {len(progress_service.all())} record(s).")

    if card_history_store.recovered_from_corruption:
        logger.warning(
            "Card history was corrupt and has been rebuilt; "
            f"backup={card_history_store.corrupt_backup}"
        )
    else:
        logger.info(f"Card history loaded: {len(card_history_service.all())} record(s).")

    if card_display_settings_resolution.recovered_from_invalid:
        logger.warning(
            "Card lifetime setting is invalid; using the safe 30-second default."
        )

    keywords = _normalize_window_keywords(config.get(TARGET_WINDOW_KEY, []))
    if keywords:
        AppContext.register(
            ExternalAdapter,
            WindowsWindowAdapter(
                title_keywords=keywords,
                launch_fingerprint=config.get(TARGET_WINDOW_FINGERPRINT_KEY),
            ),
        )

    return paths, logger


def save_registry(logger: LoggerService | None = None) -> None:
    """Persist the current registry without trusting stale handles on next load."""
    store = AppContext.get(WindowRegistryStore)
    registry = AppContext.get(WindowRegistry)
    if store is None or registry is None:
        return
    try:
        store.save(registry)
        if logger is not None:
            logger.info(f"Character window registry saved: {len(registry.all())} character(s).")
    except Exception as exc:
        if logger is not None:
            logger.error(f"Character window registry save failed: {exc}")
        else:
            raise


def shutdown_external_adapter(logger: LoggerService | None = None) -> None:
    """Release the registered adapter without changing the caller's exit path."""
    try:
        adapter = AppContext.get(ExternalAdapter)
        if adapter is not None:
            adapter.shutdown()
    except Exception:
        if logger is not None:
            try:
                logger.error(f"External adapter shutdown failed:\n{traceback.format_exc()}")
            except Exception:
                pass


def shutdown_smart_reconnect_monitor(
    logger: LoggerService | None = None,
) -> None:
    """Stop the daemon monitor before services and logs are released."""
    try:
        monitor = AppContext.get(SmartReconnectMonitor)
        if monitor is not None and not monitor.stop():
            raise RuntimeError("Smart reconnect monitor did not stop in time.")
    except Exception:
        if logger is not None:
            try:
                logger.error(
                    "Smart reconnect monitor shutdown failed:\n"
                    f"{traceback.format_exc()}"
                )
            except Exception:
                pass


def registry_status() -> dict[str, object]:
    registry = AppContext.get(WindowRegistry)
    store = AppContext.get(WindowRegistryStore)
    if registry is None or store is None:
        return {"loaded": False, "count": 0, "recovered": False, "characters": []}
    return {
        "loaded": True,
        "count": len(registry.all()),
        "recovered": bool(store.recovered_from_corruption),
        "backup": str(store.corrupt_backup) if store.corrupt_backup else None,
        "characters": [record.to_dict() for record in registry.all()],
    }


def detect_target_window() -> dict[str, object]:
    adapter = AppContext.get(ExternalAdapter)
    if adapter is None:
        return {
            "configured": False,
            "safe": False,
            "code": "window.not_configured",
            "message": "尚未設定遊戲主視窗關鍵字；不會執行任何遊戲操作。",
            "details": None,
        }
    result = adapter.health_check()
    return {
        "configured": True,
        "safe": bool(result.success),
        "code": result.code,
        "message": result.message,
        "details": dict(result.details) if result.details is not None else None,
    }


def publish_target_window_observation(
    detection: dict[str, object],
) -> TargetWindowObservation:
    """Publish only the player-safe target-window fact to SP2."""
    observation = TargetWindowObservation.from_detection(detection)
    event_bus = AppContext.get(EventBus)
    if event_bus is None:
        raise RuntimeError("EventBus is unavailable.")
    event_bus.publish(TARGET_WINDOW_OBSERVED_EVENT, observation)
    return observation


def detect_background_capabilities() -> dict[str, object]:
    adapter = AppContext.get(ExternalAdapter)
    handle = None
    if isinstance(adapter, WindowsWindowAdapter) and adapter.last_match is not None:
        handle = adapter.last_match.handle
    return BackgroundCapabilityProbe(WindowsBackgroundCaptureBackend()).run(handle).to_dict()


def _self_check_items(status: dict[str, object]) -> list[dict[str, object]]:
    report = status.get("self_check", [])
    if isinstance(report, dict):
        report = report.get("checks", [])
    if not isinstance(report, list):
        return []
    return [item for item in report if isinstance(item, dict)]


def format_self_check(status: dict[str, object]) -> tuple[str, str]:
    passed = bool(status.get("self_check_passed", False))
    lines: list[str] = []
    for item in _self_check_items(status):
        name = str(item.get("name", "unknown"))
        item_passed = bool(item.get("passed", False))
        message = str(item.get("message", ""))
        lines.append(f"{'✓' if item_passed else '✗'} {name}：{message}")
    if not lines:
        lines.append("✗ self_check：沒有取得檢查結果。")
        passed = False
    return ("自我檢查通過" if passed else "自我檢查發現問題", "\n".join(lines))


def format_window_status(status: dict[str, object]) -> str:
    item = status.get("target_window", {})
    if not isinstance(item, dict):
        return "主視窗狀態：無法取得；操作保持停用。"
    safe = bool(item.get("safe", False))
    return (
        f"{'✓' if safe else '—'} 主視窗："
        f"{'可安全辨識（同步輸入仍需玩家手動執行）' if safe else '不可操作'}\n"
        f"代碼：{item.get('code', 'window.unknown')}\n"
        f"說明：{item.get('message', '')}"
    )


def format_background_status(status: dict[str, object]) -> str:
    report = status.get("background_capabilities", {})
    capabilities = report.get("capabilities", {}) if isinstance(report, dict) else {}
    if not isinstance(capabilities, dict):
        capabilities = {}
    labels = {
        "background_capture": "被遮擋時讀取畫面",
        "background_input": "非前景背景操作",
        "minimized_input": "最小化背景操作",
    }
    states = {
        "supported": "支援", "unsupported": "不支援", "unknown": "無法確認",
        "untested": "尚未測試", "error": "測試錯誤",
    }
    lines = []
    for key, label in labels.items():
        item = capabilities.get(key, {})
        state = str(item.get("state", "unknown")) if isinstance(item, dict) else "unknown"
        lines.append(f"{label}：{states.get(state, state)}")
    policy_labels = {
        WindowInputPolicy.FOREGROUND_ONLY.value: "僅允許前台",
        WindowInputPolicy.FOREGROUND_BACKGROUND.value: "允許前台與背景",
        WindowInputPolicy.ALL.value: "全部允許（含最小化）",
    }
    policy = str(status.get("input_policy", WindowInputPolicy.ALL.value))
    lines.append(
        "同步輸入：只在玩家明確執行已批准測試時啟用；"
        f"目前權限為「{policy_labels.get(policy, '設定無效')}」。"
    )
    return "\n".join(lines)


def format_registry_status(status: dict[str, object]) -> str:
    item = status.get("window_registry", {})
    if not isinstance(item, dict) or not item.get("loaded"):
        return "角色資料：未載入。"
    count = int(item.get("count", 0))
    recovered = bool(item.get("recovered", False))
    message = f"角色資料：已載入 {count} 個角色。"
    if recovered:
        message += " 本次已從損壞狀態重建。"
    return message + "\n舊視窗紀錄不會在重開後直接視為有效。"


def format_start_status(status: dict[str, object], paths: PathManager) -> str:
    """Build the complete read-only status shown from the player home screen."""
    self_check_headline, self_check_details = format_self_check(status)
    return (
        f"{self_check_headline}\n"
        f"{self_check_details}\n\n"
        f"{format_window_status(status)}\n\n"
        "背景能力\n"
        f"{format_background_status(status)}\n\n"
        f"{format_registry_status(status)}\n\n"
        "同步按鍵不會自行送出；只會由玩家從已確認快捷鍵清單明確執行。\n"
        "智慧重連只依已確認畫面自動監看，未知畫面不會點擊。\n"
        f"紀錄位置：{paths.logs_dir()}"
    )


def create_main_window(status: dict[str, object], paths: PathManager) -> Tk:
    window = Tk()
    window.withdraw()
    window.title(APP_TITLE)
    apply_window_icon(window)
    window.geometry("1040x720")
    window.minsize(900, 620)
    window.update_idletasks()
    window_identity = configure_tk_window_app_identity(
        window,
        taskbar_icon_resource(),
    )
    window.deiconify()

    config = AppContext.get(ConfigManager)
    input_controller = AppContext.get(WindowsInputSyncController)
    pointer_sync_controller = AppContext.get(
        WindowsPointerSyncController
    )
    logger = AppContext.get(LoggerService)
    group_selection_service = AppContext.get(GroupSelectionService)
    group_configuration_service = AppContext.get(
        GroupConfigurationService
    )
    group_launch_service = AppContext.get(GroupLaunchService)
    sync_scope_service = AppContext.get(SyncScopeService)
    workspace_service = AppContext.get(WorkspaceService)
    activity_schedule_view_service = AppContext.get(ActivityScheduleViewService)
    card_view_state_service = AppContext.get(CardViewStateService)
    card_service = AppContext.get(CardService)
    activity_reminder_service = AppContext.get(ActivityReminderService)
    card_display_settings_service = AppContext.get(CardDisplaySettingsService)
    target_window_state_service = AppContext.get(TargetWindowStateService)
    smart_reconnect_monitor = AppContext.get(SmartReconnectMonitor)
    smart_reconnect_controller = AppContext.get(
        WindowsSmartReconnectController
    )
    reconnect_failure_status_service = AppContext.get(
        ReconnectFailureStatusService
    )
    group_role_status_service = AppContext.get(GroupRoleStatusService)
    operation_record_store = AppContext.get(SyncOperationRecordStore)
    deferred_sync_monitor = AppContext.get(DeferredSyncOperationMonitor)
    character_view_service = AppContext.get(CharacterViewService)
    character_detail_view_service = AppContext.get(CharacterDetailViewService)
    character_note_service = AppContext.get(CharacterNoteService)
    home_view: HomeView | None = None
    auto_click_service = AutoClickService(
        Win32CursorClickBackend(),
        schedule=window.after,
        cancel=window.after_cancel,
    )

    def report_refresh_error(error: Exception) -> None:
        if logger is not None:
            logger.error(f"Player view refresh failed and was isolated: {error}")
        messagebox.showerror(
            "輔｜操作未完成",
            "操作未能完成，原本資料保持不變。",
            parent=window,
        )

    def show_start_status() -> None:
        detection = detect_target_window()
        status["target_window"] = detection
        publish_target_window_observation(detection)
        if home_view is not None:
            home_view.refresh_target_window()
        messagebox.showinfo(
            "輔｜目前狀態",
            format_start_status(status, paths),
            parent=window,
        )
    configured_policy = (
        normalize_input_policy(config.get(INPUT_POLICY_KEY))
        if config is not None
        else WindowInputPolicy.ALL
    ) or WindowInputPolicy.ALL

    def selected_group_plan(choice) -> GroupLaunchPlan | None:
        if group_launch_service is None:
            return None
        plan = group_launch_service.plan(choice.name)
        if (
            not plan.ready
            or len(plan.targets) != choice.character_count
        ):
            return None
        return plan

    def apply_group_identity(choice) -> GroupLaunchPlan | None:
        plan = selected_group_plan(choice)
        if plan is None:
            return None
        if input_controller is not None:
            scope = (
                sync_scope_service.scope(choice.name)
                if sync_scope_service is not None
                else None
            )
            if scope is None or not scope.ready:
                return None
            input_controller.set_expected_windows(len(scope.fingerprints))
            input_controller.set_allowed_fingerprints(scope.fingerprints)
            if pointer_sync_controller is not None:
                pointer_sync_controller.set_expected_windows(
                    len(scope.fingerprints)
                )
                pointer_sync_controller.set_allowed_fingerprints(
                    scope.fingerprints
                )
        if smart_reconnect_controller is not None:
            smart_reconnect_controller.set_expected_windows(
                choice.character_count
            )
            smart_reconnect_controller.set_group_launch_plan(plan)
        return plan

    def change_input_policy(value: str) -> None:
        policy = normalize_input_policy(value)
        if policy is None or config is None:
            messagebox.showerror(
                "輔｜同步輸入設定",
                "同步輸入權限設定無效，未變更任何操作。",
                parent=window,
            )
            return
        config.set(INPUT_POLICY_KEY, policy.value)
        status["input_policy"] = policy.value

    def change_group(name: str) -> None:
        if (
            group_selection_service is None
            or workspace_service is None
            or config is None
        ):
            messagebox.showerror(
                "輔｜組別",
                "組別資料尚未準備完成，未變更目前組別。",
                parent=window,
            )
            return
        choice = group_selection_service.find(name)
        if choice is None:
            messagebox.showerror(
                "輔｜組別",
                "找不到這個組別，未變更目前組別。",
                parent=window,
            )
            return
        workspace_service.set_current_group(
            group_selection_service.workspace_group(choice)
        )
        workspace_service.set_next_step("查看目前需要注意的內容")
        config.set(CURRENT_GROUP_NAME_KEY, choice.name)
        if keyboard_sync_monitor is not None and keyboard_sync_monitor.enabled:
            keyboard_sync_monitor.stop()
            if mouse_sync_monitor is not None:
                mouse_sync_monitor.stop()
            if home_view is not None:
                home_view.set_keyboard_sync_enabled(False)
            if logger is not None:
                logger.info(
                    "Keyboard synchronization stopped because the group changed."
                )
        if smart_reconnect_monitor is not None and smart_reconnect_monitor.running:
            smart_reconnect_monitor.stop(timeout_seconds=1.0)
            if config is not None:
                config.update_values(
                    {
                        SMART_RECONNECT_ENABLED_KEY: False,
                        SMART_RECONNECT_CONSENT_KEY: False,
                    }
                )
            if home_view is not None:
                home_view.set_smart_reconnect_enabled(False)
            if logger is not None:
                logger.info(
                    "Smart reconnect stopped because the group changed."
                )
        apply_group_identity(choice)
        if group_role_status_service is not None:
            group_role_status_service.clear_cache()

    def stop_group_automation_for_configuration_change() -> None:
        if keyboard_sync_monitor is not None:
            keyboard_sync_monitor.stop()
            if mouse_sync_monitor is not None:
                mouse_sync_monitor.stop()
            if home_view is not None:
                home_view.set_keyboard_sync_enabled(False)
        if smart_reconnect_monitor is not None:
            smart_reconnect_monitor.stop(timeout_seconds=1.0)
            if home_view is not None:
                home_view.set_smart_reconnect_enabled(False)
        if config is not None:
            config.update_values(
                {
                    SMART_RECONNECT_ENABLED_KEY: False,
                    SMART_RECONNECT_CONSENT_KEY: False,
                }
            )

    def group_entries(group_name: str):
        if group_configuration_service is None:
            return ()
        group = group_configuration_service.group(group_name)
        return group.entries if group is not None else ()

    def add_group_shortcuts(group_name: str) -> object:
        if group_configuration_service is None:
            return False
        selected = filedialog.askopenfilenames(
            parent=window,
            title="加入角色到組別",
            filetypes=(("Windows 捷徑", "*.lnk"),),
        )
        if not selected:
            return False
        stop_group_automation_for_configuration_change()
        try:
            added = group_configuration_service.add_shortcuts(
                group_name,
                tuple(Path(path) for path in selected),
            )
        except SyncCycleError:
            return SyncCycleError.player_message
        if home_view is not None:
            home_view.refresh_group_entries()
            home_view.refresh_group_sync_relations()
        if operation_record_store is not None:
            operation_record_store.ensure_daily_file()
        return bool(added)

    def remove_group_shortcut(
        group_name: str,
        entry_id: str,
    ) -> bool:
        if group_configuration_service is None:
            return False
        stop_group_automation_for_configuration_change()
        removed = group_configuration_service.remove_shortcut(
            group_name,
            entry_id,
        )
        if removed and home_view is not None:
            home_view.refresh_group_entries()
            home_view.refresh_group_sync_relations()
        if removed and operation_record_store is not None:
            operation_record_store.ensure_daily_file()
        return removed

    def add_group_sync_relation(
        group_name: str,
        member_entry_id: str,
    ) -> object:
        if group_configuration_service is None:
            return False
        group = group_configuration_service.group(group_name)
        if group is None or not group.entries:
            return False
        stop_group_automation_for_configuration_change()
        try:
            changed = group_configuration_service.add_sync_relation(
                group.entries[0].entry_id,
                member_entry_id,
            )
        except SyncCycleError:
            return SyncCycleError.player_message
        return bool(changed)

    def remove_group_sync_relation(
        group_name: str,
        member_entry_id: str,
    ) -> bool:
        if group_configuration_service is None:
            return False
        group = group_configuration_service.group(group_name)
        if group is None or not group.entries:
            return False
        stop_group_automation_for_configuration_change()
        return group_configuration_service.remove_sync_relation(
            group.entries[0].entry_id,
            member_entry_id,
        )

    def card_display_seconds() -> int:
        if card_display_settings_service is None:
            return 30
        return (
            card_display_settings_service.snapshot().settings.lifetime_seconds
        )

    def update_card_display_seconds(seconds: int) -> None:
        if card_display_settings_service is None:
            raise RuntimeError("card display settings service is unavailable")
        card_display_settings_service.update_lifetime_seconds(seconds)
        messagebox.showinfo(
            "輔｜提醒顯示時間",
            f"新的提醒卡會顯示 {seconds} 秒。",
            parent=window,
        )

    def latest_character_detail(character_id: str):
        if character_detail_view_service is None:
            raise RuntimeError("character detail service is unavailable")
        return character_detail_view_service.get_by_identity(character_id)

    def open_character_detail(character_id: str, detail) -> None:
        if character_note_service is None:
            messagebox.showerror(
                "輔｜角色資料",
                "角色備註服務尚未準備完成。",
                parent=window,
            )
            return

        def save_note(note: str):
            character_note_service.set_note(character_id, note)
            return latest_character_detail(character_id)

        def clear_note():
            character_note_service.clear_note(character_id)
            return latest_character_detail(character_id)

        def note_error(_error: Exception) -> None:
            messagebox.showerror(
                "輔｜角色備註",
                "備註未能保存，原本內容保持不變。",
                parent=window,
            )

        detail_window = CharacterDetailWindow(
            window,
            detail,
            on_save_note=save_note,
            on_clear_note=clear_note,
            on_error=note_error,
        )
        child = detail_window.open()
        apply_window_icon(child)

    def current_input_policy() -> WindowInputPolicy | None:
        return (
            normalize_input_policy(config.get(INPUT_POLICY_KEY))
            if config is not None
            else None
        )

    def log_keyboard_sync_result(result) -> None:
        if logger is not None:
            logger.info(
                "Keyboard synchronized input completed; "
                f"key={result.approved_key}; policy={result.policy}; "
                f"eligible={result.eligible_windows}; sent={result.sent_windows}; "
                f"failures={','.join(result.failure_codes) or 'none'}"
            )

    keyboard_sync_monitor = (
        KeyboardSyncMonitor(
            input_controller,
            policy_provider=current_input_policy,
            schedule=window.after,
            cancel=window.after_cancel,
            result_callback=log_keyboard_sync_result,
        )
        if input_controller is not None
        else None
    )
    mouse_sync_monitor = (
        MouseSyncMonitor(
            pointer_sync_controller,
            policy_provider=current_input_policy,
            schedule=window.after,
            cancel=window.after_cancel,
        )
        if pointer_sync_controller is not None
        else None
    )

    def change_keyboard_sync(enabled: bool) -> bool:
        if (
            keyboard_sync_monitor is None
            or mouse_sync_monitor is None
            or input_controller is None
            or pointer_sync_controller is None
        ):
            messagebox.showerror(
                "輔｜同步輸入",
                "同步輸入尚未正確設定，沒有啟用。",
                parent=window,
            )
            return False
        if not enabled:
            keyboard_sync_monitor.stop()
            mouse_sync_monitor.stop()
            return True

        state = workspace_service.snapshot() if workspace_service is not None else None
        group_name = (
            state.current_group.name
            if state is not None and state.current_group is not None
            else None
        )
        choice = (
            group_selection_service.find(group_name)
            if group_selection_service is not None
            else None
        )
        if choice is None or choice.character_count <= 1:
            messagebox.showerror(
                "輔｜同步輸入",
                "請先選擇至少有 2 個視窗設定的組別；目前沒有啟用。",
                parent=window,
            )
            return False
        plan = apply_group_identity(choice)
        if plan is None:
            messagebox.showerror(
                "輔｜同步輸入",
                "目前組別無法完整對應到唯一遊戲視窗；沒有啟用。",
                parent=window,
            )
            return False
        if current_input_policy() is None:
            messagebox.showerror(
                "輔｜同步輸入",
                "允許範圍尚未正確設定；目前沒有啟用。",
                parent=window,
            )
            return False
        keyboard_sync_monitor.start()
        mouse_sync_monitor.start()
        if logger is not None:
            logger.info(
                "Keyboard synchronization enabled; "
                f"group={choice.name}; expected={choice.character_count}"
            )
        return True

    def change_smart_reconnect(enabled: bool) -> bool:
        if (
            config is None
            or smart_reconnect_monitor is None
            or smart_reconnect_controller is None
        ):
            messagebox.showerror(
                "輔｜智慧重連",
                "智慧重連尚未正確設定；目前維持安全停止。",
                parent=window,
            )
            return False
        if enabled:
            state = (
                workspace_service.snapshot()
                if workspace_service is not None
                else None
            )
            group_name = (
                state.current_group.name
                if state is not None and state.current_group is not None
                else None
            )
            choice = (
                group_selection_service.find(group_name)
                if group_selection_service is not None
                else None
            )
            if choice is None or choice.character_count <= 0:
                messagebox.showerror(
                    "輔｜智慧重連",
                    "請先選擇已有視窗設定的組別；目前維持安全停止。",
                    parent=window,
                )
                return False
            plan = apply_group_identity(choice)
            if plan is None:
                messagebox.showerror(
                    "輔｜智慧重連",
                    "目前組別無法完整對應到唯一遊戲視窗；維持安全停止。",
                    parent=window,
                )
                return False
            config.update_values(
                {
                    SMART_RECONNECT_ENABLED_KEY: True,
                    SMART_RECONNECT_CONSENT_KEY: True,
                }
            )
            smart_reconnect_monitor.start()
            if logger is not None:
                logger.info("Smart reconnect explicitly enabled by the player.")
            return True

        config.update_values(
            {
                SMART_RECONNECT_ENABLED_KEY: False,
                SMART_RECONNECT_CONSENT_KEY: False,
            }
        )
        stopped = smart_reconnect_monitor.stop(timeout_seconds=1.0)
        if logger is not None:
            logger.info(
                "Smart reconnect explicitly disabled by the player; "
                f"worker_stopped={stopped}"
            )
        return True

    def change_auto_click(
        enabled: bool,
        interval_ms: int,
        button: str,
        repeat_forever: bool,
        repeat_count: int,
    ) -> bool:
        settings = AutoClickSettings(
            interval_ms=interval_ms,
            button=button,
            repeat_forever=repeat_forever,
            repeat_count=repeat_count,
        )
        if enabled:
            return auto_click_service.start(settings)
        auto_click_service.stop()
        return True

    group_choices = (
        group_selection_service.choices()
        if group_selection_service is not None
        else ()
    )
    workspace_state = (
        workspace_service.snapshot()
        if workspace_service is not None
        else WorkspaceState()
    )
    character_choices = (
        CharacterDetailChoiceService(
            character_detail_view_service,
            open_character_detail,
        ).all()
        if character_detail_view_service is not None
        else ()
    )
    home_view = HomeView(
        window,
        status,
        on_start=show_start_status,
        input_policy=configured_policy.value,
        on_input_policy_change=change_input_policy,
        keyboard_sync_enabled=False,
        on_keyboard_sync_change=change_keyboard_sync,
        group_choices=group_choices,
        current_group_name=(
            workspace_state.current_group.name
            if workspace_state.current_group is not None
            else None
        ),
        on_group_change=change_group,
        group_entries_provider=group_entries,
        on_add_group_shortcuts=add_group_shortcuts,
        on_remove_group_shortcut=remove_group_shortcut,
        group_sync_choices_provider=(
            group_configuration_service.available_sync_members
            if group_configuration_service is not None
            else None
        ),
        group_sync_relations_provider=(
            group_configuration_service.explicit_sync_members
            if group_configuration_service is not None
            else None
        ),
        on_add_group_sync_relation=add_group_sync_relation,
        on_remove_group_sync_relation=remove_group_sync_relation,
        workspace_state=workspace_state,
        workspace_state_provider=(
            workspace_service.snapshot
            if workspace_service is not None
            else None
        ),
        activity_schedule=(
            activity_schedule_view_service.snapshot()
            if activity_schedule_view_service is not None
            else None
        ),
        activity_schedule_provider=(
            activity_schedule_view_service.snapshot
            if activity_schedule_view_service is not None
            else None
        ),
        card_view_state=(
            card_view_state_service.snapshot()
            if card_view_state_service is not None
            else None
        ),
        card_view_state_provider=(
            card_view_state_service.snapshot
            if card_view_state_service is not None
            else None
        ),
        target_window_state=(
            target_window_state_service.snapshot()
            if target_window_state_service is not None
            else None
        ),
        target_window_state_provider=(
            target_window_state_service.snapshot
            if target_window_state_service is not None
            else None
        ),
        characters=(
            character_view_service.all()
            if character_view_service is not None
            else ()
        ),
        character_choices=character_choices,
        smart_reconnect_enabled=bool(
            status.get("smart_reconnect_enabled", False)
        ),
        on_smart_reconnect_change=change_smart_reconnect,
        reconnect_failure_messages_provider=(
            lambda: tuple(
                item.message
                for item in reconnect_failure_status_service.snapshot()
                if item.key.startswith("group:")
            )
            if reconnect_failure_status_service is not None
            else None
        ),
        group_role_status_provider=(
            group_role_status_service.snapshot
            if group_role_status_service is not None
            else None
        ),
        on_group_role_action=(
            lambda action_id: group_role_status_service.activate_or_launch(
                home_view.current_group_name if home_view is not None else None,
                action_id,
            )
            if group_role_status_service is not None
            else None
        ),
        operation_record_lines_provider=(
            operation_record_store.player_lines
            if operation_record_store is not None
            else None
        ),
        operation_record_files_provider=(
            operation_record_store.daily_files
            if operation_record_store is not None
            else None
        ),
        on_open_operation_record_file=(
            operation_record_store.open_daily_file
            if operation_record_store is not None
            else None
        ),
        operation_record_search=(
            operation_record_store.search
            if operation_record_store is not None
            else None
        ),
        auto_click_running=False,
        on_auto_click_change=change_auto_click,
        card_display_seconds_provider=card_display_seconds,
        on_card_display_seconds_update=update_card_display_seconds,
        on_refresh_error=report_refresh_error,
    )
    home_view.build()
    auto_click_service.subscribe(
        lambda snapshot: home_view.set_auto_click_running(
            snapshot.running,
            snapshot.sent_count,
        )
    )
    auto_click_hotkey_monitor = AutoClickHotkeyMonitor(
        home_view.toggle_auto_click_from_hotkey,
        schedule=window.after,
        cancel=window.after_cancel,
    )
    auto_click_hotkey_monitor.start()

    def current_group_name() -> str | None:
        state = (
            workspace_service.snapshot()
            if workspace_service is not None
            else None
        )
        return (
            state.current_group.name
            if state is not None and state.current_group is not None
            else None
        )

    group_role_status_monitor = (
        GroupRoleStatusMonitor(
            group_role_status_service,
            current_group_name,
        )
        if group_role_status_service is not None
        else None
    )
    if group_role_status_monitor is not None:
        group_role_status_monitor.start()
    if deferred_sync_monitor is not None:
        deferred_sync_monitor.start()

    reconnect_status_refresh_id: str | None = None

    def refresh_reconnect_status() -> None:
        nonlocal reconnect_status_refresh_id
        home_view.refresh_reconnect_failures()
        home_view.refresh_group_role_statuses()
        reconnect_status_refresh_id = window.after(
            1000,
            refresh_reconnect_status,
        )

    refresh_reconnect_status()

    overlay_runtime = None
    expiry_monitor = None
    activity_reminder_monitor = None
    if card_service is not None and card_view_state_service is not None:
        overlay_layout = CardOverlayLayoutService(
            card_view_state_service,
            WindowsWorkAreaReader(),
            CardSize(width=360, height=140),
            right_margin=20,
            bottom_margin=20,
            gap=12,
        )
        overlay_runtime = build_windows_card_overlay_runtime(
            window,
            card_service,
            overlay_layout,
            TkCardTextSettings(
                background="#FFFFFF",
                foreground="#182433",
                muted_foreground="#617083",
                accent="#2474C6",
            ),
        )
        overlay_runtime.start()
        expiry_monitor = CardExpiryMonitor(card_service, window.after)
        expiry_monitor.start()
        card_service.subscribe(home_view.refresh_cards)
        if activity_reminder_service is not None:
            activity_reminder_monitor = ActivityReminderMonitor(
                activity_reminder_service,
                window.after,
                window.after_cancel,
            )
            activity_reminder_monitor.start()

    def close_window() -> None:
        auto_click_hotkey_monitor.stop()
        auto_click_service.stop()
        if group_role_status_monitor is not None:
            group_role_status_monitor.stop(timeout_seconds=1.0)
        if deferred_sync_monitor is not None:
            deferred_sync_monitor.stop(timeout_seconds=1.0)
        if reconnect_status_refresh_id is not None:
            try:
                window.after_cancel(reconnect_status_refresh_id)
            except TclError:
                pass
        if card_service is not None:
            card_service.unsubscribe(home_view.refresh_cards)
        if expiry_monitor is not None:
            expiry_monitor.stop()
        if activity_reminder_monitor is not None:
            activity_reminder_monitor.stop()
        if overlay_runtime is not None:
            overlay_runtime.stop()
        if keyboard_sync_monitor is not None:
            keyboard_sync_monitor.stop()
        if mouse_sync_monitor is not None:
            mouse_sync_monitor.stop()
        if window_identity is not None:
            window_identity.clear()
        window.destroy()

    window.protocol("WM_DELETE_WINDOW", close_window)
    window._card_overlay_runtime = overlay_runtime
    window._card_expiry_monitor = expiry_monitor
    window._activity_reminder_monitor = activity_reminder_monitor
    window._auto_click_service = auto_click_service
    window._auto_click_hotkey_monitor = auto_click_hotkey_monitor
    window._group_role_status_monitor = group_role_status_monitor
    window._deferred_sync_monitor = deferred_sync_monitor
    window._windows_app_identity = window_identity
    return window


def write_self_check_report(status: dict[str, object], paths: PathManager) -> Path:
    report_path = paths.data_dir() / "self_check.json"
    report_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def write_target_desktop_report(
    payload: dict[str, object], paths: PathManager
) -> Path:
    report_path = paths.data_dir() / TARGET_DESKTOP_REPORT_FILENAME
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report_path


def run(
    *,
    self_check_only: bool = False,
    target_desktop_verify_only: bool = False,
    root: Path | None = None,
) -> int:
    paths: PathManager | None = None
    logger: LoggerService | None = None
    try:
        configure_process_app_identity()
        paths, logger = build_services(root=root)
        if target_desktop_verify_only:
            try:
                verification = TargetDesktopVerifier.for_real_windows().verify()
                payload = verification.to_dict()
                exit_code = 0 if verification.passed else 3
            except Exception:
                payload = {
                    "passed": False,
                    "failure_codes": ["verifier_execution_failed"],
                    "raw_arguments_emitted": False,
                    "captured_pixels_persisted": False,
                    "input_sent": False,
                }
                exit_code = 4
            report_path = write_target_desktop_report(payload, paths)
            logger.info(
                "Read-only target-desktop verification "
                f"{'passed' if payload['passed'] else 'failed'}; "
                f"report={report_path}"
            )
            return exit_code

        status = Bootstrap(context=AppContext).start()
        status["window_registry"] = registry_status()
        status["target_window"] = detect_target_window()
        publish_target_window_observation(status["target_window"])
        status["background_capabilities"] = detect_background_capabilities()
        config = AppContext.get(ConfigManager)
        status["input_policy"] = (
            normalize_input_policy(config.get(INPUT_POLICY_KEY)).value
            if config is not None
            and normalize_input_policy(config.get(INPUT_POLICY_KEY)) is not None
            else WindowInputPolicy.ALL.value
        )
        # Permission from an older process is never reused. Every program
        # launch starts in a hard-stopped state and requires a fresh click on
        # the enable button before any game window may be inspected/clicked.
        if config is not None:
            config.update_values(
                {
                    SMART_RECONNECT_ENABLED_KEY: False,
                    SMART_RECONNECT_CONSENT_KEY: False,
                }
            )
        status["smart_reconnect_enabled"] = False
        write_self_check_report(status, paths)

        if self_check_only:
            return 0 if bool(status.get("self_check_passed", False)) else 2

        window = create_main_window(status, paths)
        window.mainloop()
        logger.info(f"FLASH {MILESTONE} closed normally.")
        return 0
    except Exception as exc:
        details = traceback.format_exc()
        if logger is not None:
            logger.error(f"FLASH startup failed: {exc}\n{details}")
        else:
            fallback = Path.home() / "FLASH_startup_error.log"
            try:
                fallback.write_text(details, encoding="utf-8")
            except OSError:
                pass

        if self_check_only or target_desktop_verify_only:
            return 1
        try:
            root_window = Tk()
            root_window.withdraw()
            messagebox.showerror("輔｜啟動失敗", f"輔無法啟動。錯誤已寫入紀錄檔。\n\n原因：{exc}", parent=root_window)
            root_window.destroy()
        except Exception:
            print(details, file=sys.stderr)
        return 1
    finally:
        try:
            shutdown_smart_reconnect_monitor(logger)
        except Exception:
            if logger is not None:
                logger.error(
                    "Smart reconnect final shutdown failed:\n"
                    f"{traceback.format_exc()}"
                )
        finally:
            try:
                save_registry(logger)
            except Exception:
                if logger is not None:
                    logger.error(
                        f"Registry final save failed:\n{traceback.format_exc()}"
                    )
            finally:
                try:
                    shutdown_external_adapter(logger)
                finally:
                    close_logger(logger)


def close_logger(logger: LoggerService | None) -> None:
    """Release logger file handles without replacing the application's result."""
    if logger is None:
        return
    close = getattr(logger, "close", None)
    if not callable(close):
        return
    try:
        close()
    except Exception:
        try:
            print(
                f"Logger final shutdown failed:\n{traceback.format_exc()}",
                file=sys.stderr,
            )
        except Exception:
            pass


def main() -> None:
    arguments = set(sys.argv[1:])
    target_desktop_verify_only = TARGET_DESKTOP_VERIFY_ARGUMENT in arguments
    raise SystemExit(
        run(
            self_check_only=(
                SELF_CHECK_ARGUMENT in arguments
                and not target_desktop_verify_only
            ),
            target_desktop_verify_only=target_desktop_verify_only,
        )
    )


if __name__ == "__main__":
    main()
