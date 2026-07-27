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
from adapters.windows_system_tray import (
    SystemTrayController,
    WindowsSystemTrayBackend,
)
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
from domain.character_game_data_store import CharacterGameDataStore
from domain.game_shortcuts import CONFIRMED_GAME_SHORTCUTS
from domain.progress_store import ActivityProgressStore
from habit.service import ActivityOrderHabitService
from habit.store import ActivityOrderHabitStore
from services.activity_progress_service import ActivityProgressService
from services.activity_reminder_monitor import ActivityReminderMonitor
from services.activity_reminder_service import ActivityReminderService
from services.activity_schedule_view_service import ActivityScheduleViewService
from services.app_context import AppContext
from services.auto_click_service import (
    Win32AutoClickPointerSourceBackend,
    AutoClickService,
    AutoClickSettings,
    Win32CursorClickBackend,
)
from services.feature_hotkey_monitor import (
    FeatureHotkeyMonitor,
    GroupLaunchHotkeyMonitor,
    normalize_feature_hotkey,
)
from services.background_image_service import BackgroundImageService
from services.card_coordinator import CardCoordinator
from services.card_display_settings_service import CardDisplaySettingsService
from services.card_expiry_monitor import CardExpiryMonitor
from services.card_history_service import CardHistoryService
from services.card_overlay_layout_service import CardOverlayLayoutService
from services.card_overlay_runtime import build_windows_card_overlay_runtime
from services.card_view_state_service import CardViewStateService
from services.character_detail_view_service import CharacterDetailViewService
from services.character_game_data_view_service import (
    CharacterGameDataViewService,
)
from services.character_detail_choice_service import CharacterDetailChoiceService
from services.character_note_service import CharacterNoteService
from services.character_view_service import CharacterViewService
from services.event_bus import EventBus
from services.group_selection_service import (
    GroupSelectionService,
    default_legacy_group_config_path,
)
from services.group_configuration_service import (
    GroupHotkeyConflictError,
    GroupMasterLockedError,
    GroupConfigurationService,
    SyncCycleError,
)
from services.group_character_registration_service import (
    GroupCharacterRegistrationService,
)
from services.group_launch_service import (
    CONFIRMED_GROUP_ORDERS,
    GroupLaunchPlan,
    GroupLaunchService,
)
from services.group_role_status_monitor import GroupRoleStatusMonitor
from services.group_role_status_service import GroupRoleStatusService
from services.group_window_launch_service import (
    GroupWindowLaunchResult,
    GroupWindowLaunchService,
)
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
from ui.home import (
    GroupManagementViewResult,
    UI_THEME_LABELS,
    theme_palette,
)
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
UI_THEME_KEY = "ui_theme"
CURRENT_GROUP_NAME_KEY = "current_group_name"
REGISTRY_FILENAME = "window_registry.json"
RECONNECT_STATE_FILENAME = "smart_reconnect_state.json"
GROUP_CONFIGURATION_FILENAME = "group_configuration.json"
OPERATION_RECORD_FILENAME = "operation_records.json"
OPERATION_RECORD_ARCHIVE_DIRNAME = "角色每日紀錄"
DEFERRED_SYNC_STATE_FILENAME = "deferred_sync_operations.json"
TARGET_DESKTOP_REPORT_FILENAME = "target_desktop_verification.json"
CHARACTER_FILENAME = "characters.json"
CHARACTER_GAME_DATA_FILENAME = "character_game_data.json"
ACTIVITY_PROGRESS_FILENAME = "activity_progress.json"
CARD_HISTORY_FILENAME = "card_history.json"
ACTIVITY_REMINDER_STATE_FILENAME = "activity_reminder_state.json"
ACTIVITY_ORDER_HABIT_FILENAME = "activity_order_habit.json"
SYNC_SELECTED_KEYS_KEY = "sync_selected_keys"
FEATURE_HOTKEYS_KEY = "feature_hotkeys"
APP_ICON_PNG = Path("assets") / "flash_icon.png"
APP_ICON_ICO = Path("assets") / "flash_icon.ico"
RECONNECT_REFERENCE_DIR = Path("assets") / "reconnect_reference"
BACKGROUND_IMAGE_FILETYPES = (
    (
        "圖片與相機 RAW",
        (
            "*.png *.jpg *.jpeg *.jpe *.jfif *.gif *.bmp *.dib "
            "*.tif *.tiff *.webp *.ico *.heic *.heif *.avif "
            "*.cr2 *.cr3 *.dng *.nef *.nrw *.arw *.srf *.sr2 "
            "*.orf *.rw2 *.raf *.pef *.raw *.3fr *.erf *.mef "
            "*.mos *.mrw *.srw *.x3f"
        ),
    ),
    ("所有檔案", "*.*"),
)


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


def card_display_scale(window: Tk) -> float:
    """Return the display scale relative to the 96-DPI card baseline."""
    try:
        pixels_per_inch = float(window.winfo_fpixels("1i"))
    except (AttributeError, TclError, TypeError, ValueError):
        return 1.0
    if pixels_per_inch <= 0:
        return 1.0
    return max(1.0, pixels_per_inch / 96.0)


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
    background_image_service = BackgroundImageService(
        config,
        paths.data_dir(),
    )
    config.ensure_defaults(
        {
            INPUT_POLICY_KEY: WindowInputPolicy.ALL.value,
            SMART_RECONNECT_ENABLED_KEY: False,
            SMART_RECONNECT_CONSENT_KEY: False,
            UI_THEME_KEY: "clear_blue",
            SYNC_SELECTED_KEYS_KEY: ["ESC"],
            FEATURE_HOTKEYS_KEY: {
                "sync": "XBUTTON1",
                "reconnect": "",
                "auto_click": "F1",
            },
        }
    )
    event_bus = EventBus(logger=logger)
    target_window_state_service = TargetWindowStateService(event_bus, logger)
    card_display_settings_resolution = resolve_card_display_settings(config.data)

    registry_store = WindowRegistryStore(paths.data_dir() / REGISTRY_FILENAME)
    registry = registry_store.load()
    character_store = CharacterStore(paths.data_dir() / CHARACTER_FILENAME)
    character_game_data_store = CharacterGameDataStore(
        paths.data_dir() / CHARACTER_GAME_DATA_FILENAME
    )
    character_game_data_view_service = CharacterGameDataViewService(
        character_game_data_store
    )
    characters = character_store.load()
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
        legacy_layout_config_path=legacy_group_config_path,
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
    group_character_registration_service = GroupCharacterRegistrationService(
        registry,
        registry_store,
        character_store,
        group_configuration_service,
    )
    if initial_group_choice is not None:
        characters = group_character_registration_service.ensure_group(
            initial_group_choice.name,
            characters,
        )
    character_view_service = CharacterViewService(
        registry,
        characters,
        confirmed_group_orders=CONFIRMED_GROUP_ORDERS,
    )
    character_detail_view_service = CharacterDetailViewService(
        character_view_service,
        character_game_data_view_service,
    )
    character_note_service = CharacterNoteService(registry, registry_store)
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
        state_path=paths.data_dir() / ACTIVITY_REMINDER_STATE_FILENAME,
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
    AppContext.register(BackgroundImageService, background_image_service)
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
    AppContext.register(CharacterGameDataStore, character_game_data_store)
    AppContext.register(
        CharacterGameDataViewService,
        character_game_data_view_service,
    )
    AppContext.register(CharacterViewService, character_view_service)
    AppContext.register(CharacterDetailViewService, character_detail_view_service)
    AppContext.register(CharacterNoteService, character_note_service)
    AppContext.register(
        GroupCharacterRegistrationService,
        group_character_registration_service,
    )
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
    synchronized_window_backend = Win32WindowBackend(
        PowerShellLaunchFingerprintResolver()
    )
    AppContext.register(
        WindowsInputSyncController,
        WindowsInputSyncController.for_real_windows(
            window_backend=synchronized_window_backend,
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
            window_backend=synchronized_window_backend,
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
            synchronized_window_backend,
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
    character_store = AppContext.get(CharacterStore)
    group_character_registration_service = AppContext.get(
        GroupCharacterRegistrationService
    )
    background_image_service = AppContext.get(BackgroundImageService)
    home_view: HomeView | None = None

    def dispatch_to_main_window(callback) -> object | None:
        try:
            return window.after(0, callback)
        except TclError:
            return None

    group_window_launch_service = (
        GroupWindowLaunchService(
            group_launch_service,
            Win32WindowBackend(PowerShellLaunchFingerprintResolver()),
            completion_dispatch=dispatch_to_main_window,
            record_callback=(
                operation_record_store.append
                if operation_record_store is not None
                else None
            ),
            placement_update_callback=(
                group_configuration_service.update_saved_placements
                if group_configuration_service is not None
                else None
            ),
        )
        if group_launch_service is not None
        else None
    )
    auto_click_service = AutoClickService(
        Win32CursorClickBackend(),
        schedule=window.after,
        cancel=window.after_cancel,
    )
    auto_click_pointer_source_backend = (
        Win32AutoClickPointerSourceBackend()
    )

    def report_refresh_error(error: Exception) -> None:
        if logger is not None:
            logger.error(f"Player view refresh failed and was isolated: {error}")
        messagebox.showerror(
            "輔｜操作未完成",
            "操作未能完成，原本資料保持不變。",
            parent=window,
        )

    def complete_group_window_launch(
        result: GroupWindowLaunchResult,
    ) -> None:
        if home_view is None:
            return
        home_view.set_group_launch_state(False, result.player_message)
        home_view.refresh_group_role_statuses()

    def launch_group_and_restore(group_name: str) -> bool:
        if group_window_launch_service is None:
            return False
        return group_window_launch_service.start(
            group_name,
            complete_group_window_launch,
        )

    def restore_group_positions(group_name: str) -> bool:
        if group_window_launch_service is None:
            return False
        return group_window_launch_service.start_restore(
            group_name,
            complete_group_window_launch,
        )

    def record_group_positions(group_name: str) -> bool:
        if group_window_launch_service is None:
            return False
        if (
            group_configuration_service is not None
            and (
                group_configuration_service.group(group_name) is None
                or group_configuration_service.group(
                    group_name
                ).master_locked
            )
        ):
            return GroupMasterLockedError.player_message
        return group_window_launch_service.start_record(
            group_name,
            complete_group_window_launch,
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
    configured_theme = (
        config.get(UI_THEME_KEY)
        if config is not None
        and config.get(UI_THEME_KEY) in UI_THEME_LABELS
        else "clear_blue"
    )
    known_sync_keys = {
        shortcut.key for shortcut in CONFIRMED_GAME_SHORTCUTS
    }
    raw_selected_sync_keys = (
        config.get(SYNC_SELECTED_KEYS_KEY, ["ESC"])
        if config is not None
        else ["ESC"]
    )
    configured_selected_sync_keys = list(
        dict.fromkeys(
            key
            for key in (
                raw_selected_sync_keys
                if isinstance(raw_selected_sync_keys, list)
                else ["ESC"]
            )
            if isinstance(key, str) and key in known_sync_keys
        )
    )
    raw_feature_hotkeys = (
        config.get(FEATURE_HOTKEYS_KEY, {})
        if config is not None
        else {}
    )
    configured_feature_hotkeys = {
        name: normalize_feature_hotkey(
            raw_feature_hotkeys.get(name)
            if isinstance(raw_feature_hotkeys, dict)
            else ""
        )
        for name in ("sync", "reconnect", "auto_click")
    }

    def change_selected_sync_keys(keys: tuple[str, ...]) -> bool:
        normalized = list(
            dict.fromkeys(key for key in keys if key in known_sync_keys)
        )
        configured_selected_sync_keys[:] = normalized
        if config is not None:
            config.set(SYNC_SELECTED_KEYS_KEY, normalized)
        return True

    def change_feature_hotkey(feature: str, hotkey: str) -> bool:
        if feature not in configured_feature_hotkeys:
            return False
        normalized = normalize_feature_hotkey(hotkey)
        if normalized and any(
            value == normalized
            for name, value in configured_feature_hotkeys.items()
            if name != feature
        ):
            messagebox.showerror(
                "輔｜快捷鍵",
                "這個快捷鍵已用於另一項功能，請選擇不同按鍵。",
                parent=window,
            )
            return False
        if (
            normalized
            and group_configuration_service is not None
            and normalized
            in group_configuration_service.launch_hotkeys().values()
        ):
            messagebox.showerror(
                "輔｜快捷鍵",
                "這個快捷鍵已用於整組啟動，請選擇不同按鍵。",
                parent=window,
            )
            return False
        configured_feature_hotkeys[feature] = normalized
        if config is not None:
            config.set(
                FEATURE_HOTKEYS_KEY,
                dict(configured_feature_hotkeys),
            )
        return True

    def change_ui_theme(theme_name: str) -> bool:
        if config is None or theme_name not in UI_THEME_LABELS:
            return False
        config.set(UI_THEME_KEY, theme_name)
        return True

    def choose_background_source():
        selected = filedialog.askopenfilename(
            parent=window,
            title="選擇背景圖片",
            filetypes=BACKGROUND_IMAGE_FILETYPES,
        )
        if not selected:
            return None
        return Path(selected)

    def prepare_background_image(
        source: Path,
        cancelled,
    ):
        if background_image_service is None:
            raise RuntimeError("background image service is unavailable")
        return background_image_service.prepare(
            source,
            cancelled=cancelled,
        )

    def save_background_image(
        managed_path: Path,
        apply_all: bool,
        pages: tuple[str, ...],
    ):
        if background_image_service is None:
            raise RuntimeError("background image service is unavailable")
        return background_image_service.commit_prepared(
            managed_path,
            apply_all=apply_all,
            pages=pages,
        )

    def discard_background_image(managed_path: Path | None) -> None:
        if background_image_service is not None:
            background_image_service.discard_prepared(managed_path)

    def clear_background_image():
        if background_image_service is None:
            return None
        return background_image_service.clear()

    def clear_page_background(page: str):
        if background_image_service is None:
            raise RuntimeError("background image service is unavailable")
        return background_image_service.clear_page(page)

    def clear_all_backgrounds():
        if background_image_service is None:
            raise RuntimeError("background image service is unavailable")
        return background_image_service.clear_all()

    def export_background_settings():
        if background_image_service is None:
            return None
        selected = filedialog.asksaveasfilename(
            parent=window,
            title="匯出背景設定",
            defaultextension=".zip",
            filetypes=(("背景設定備份", "*.zip"),),
        )
        if not selected:
            return None
        return background_image_service.export_settings(Path(selected))

    def import_background_settings():
        if background_image_service is None:
            return None
        selected = filedialog.askopenfilename(
            parent=window,
            title="匯入背景設定",
            filetypes=(("背景設定備份", "*.zip"),),
        )
        if not selected:
            return None
        return background_image_service.import_settings(Path(selected))

    def update_background_display_settings(
        fill_color: str,
        sidebar_opacity: int,
        panel_opacity: int,
        role_row_opacity: int,
    ):
        if background_image_service is None:
            raise RuntimeError("background image service is unavailable")
        return background_image_service.update_display_settings(
            fill_color=fill_color,
            sidebar_opacity=sidebar_opacity,
            panel_opacity=panel_opacity,
            role_row_opacity=role_row_opacity,
        )

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
            input_controller.set_controller_fingerprint(
                scope.fingerprints[0]
            )
            if pointer_sync_controller is not None:
                pointer_sync_controller.set_expected_windows(
                    len(scope.fingerprints)
                )
                pointer_sync_controller.set_allowed_fingerprints(
                    scope.fingerprints
                )
                pointer_sync_controller.set_controller_fingerprint(
                    scope.fingerprints[0]
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
        auto_click_service.stop()
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
        auto_click_service.stop()
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
        refresh_character_data(choice.name)

    def stop_group_automation_for_configuration_change() -> None:
        auto_click_service.stop()
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

    def finish_group_management(
        selected_name: str | None,
    ) -> GroupManagementViewResult:
        if (
            group_selection_service is None
            or workspace_service is None
            or config is None
        ):
            return GroupManagementViewResult(
                False,
                None,
                "組別資料尚未準備完成。",
            )
        choice = (
            group_selection_service.find(selected_name)
            if selected_name is not None
            else None
        )
        workspace_service.set_current_group(
            group_selection_service.workspace_group(choice)
            if choice is not None
            else None
        )
        workspace_service.set_next_step(
            "查看目前需要注意的內容"
            if choice is not None
            else "選擇組別"
        )
        config.set(
            CURRENT_GROUP_NAME_KEY,
            choice.name if choice is not None else "",
        )
        if choice is not None:
            apply_group_identity(choice)
        if group_role_status_service is not None:
            group_role_status_service.clear_cache()
        if operation_record_store is not None:
            operation_record_store.ensure_daily_file()
        return GroupManagementViewResult(
            True,
            choice.name if choice is not None else None,
        )

    def create_group(name: str) -> GroupManagementViewResult:
        if group_configuration_service is None:
            return GroupManagementViewResult(
                False,
                None,
                "組別設定尚未準備完成。",
            )
        stop_group_automation_for_configuration_change()
        if not group_configuration_service.create_group(name):
            return GroupManagementViewResult(
                False,
                name,
                "已有相同名稱的組別。",
            )
        return finish_group_management(name)

    def rename_group(
        old_name: str,
        new_name: str,
    ) -> GroupManagementViewResult:
        if group_configuration_service is None:
            return GroupManagementViewResult(
                False,
                old_name,
                "組別設定尚未準備完成。",
            )
        stop_group_automation_for_configuration_change()
        if not group_configuration_service.rename_group(
            old_name,
            new_name,
        ):
            return GroupManagementViewResult(
                False,
                old_name,
                "名稱沒有變更，或已有相同名稱的組別。",
            )
        return finish_group_management(new_name)

    def delete_group(name: str) -> GroupManagementViewResult:
        if (
            group_configuration_service is None
            or group_selection_service is None
        ):
            return GroupManagementViewResult(
                False,
                name,
                "組別設定尚未準備完成。",
            )
        stop_group_automation_for_configuration_change()
        if not group_configuration_service.delete_group(name):
            return GroupManagementViewResult(
                False,
                name,
                "找不到要刪除的組別。",
            )
        choices = group_selection_service.choices()
        selected = choices[0].name if choices else None
        return finish_group_management(selected)

    def move_group(
        name: str,
        direction: int,
    ) -> GroupManagementViewResult:
        if group_configuration_service is None:
            return GroupManagementViewResult(
                False,
                name,
                "組別設定尚未準備完成。",
            )
        stop_group_automation_for_configuration_change()
        if not group_configuration_service.move_group(name, direction):
            return GroupManagementViewResult(
                False,
                name,
                "目前已在最上方或最下方。",
            )
        return finish_group_management(name)

    def export_group_configuration() -> object:
        if group_configuration_service is None:
            return "組別設定尚未準備完成。"
        selected = filedialog.asksaveasfilename(
            parent=window,
            title="匯出組別設定",
            defaultextension=".json",
            filetypes=(("JSON 設定檔", "*.json"),),
            initialfile="輔_組別設定.json",
        )
        if not selected:
            return None
        try:
            return group_configuration_service.export_configuration(
                Path(selected)
            )
        except (OSError, UnicodeError, ValueError) as error:
            if logger is not None:
                logger.warning(
                    f"Group configuration export failed: {error}"
                )
            return "組別設定無法匯出。"

    def import_group_configuration() -> object:
        if group_configuration_service is None:
            return GroupManagementViewResult(
                False,
                None,
                "組別設定尚未準備完成。",
            )
        selected = filedialog.askopenfilename(
            parent=window,
            title="匯入組別設定",
            filetypes=(("JSON 設定檔", "*.json"),),
        )
        if not selected:
            return None
        workspace_snapshot = (
            workspace_service.snapshot()
            if workspace_service is not None
            else None
        )
        current_name = (
            workspace_snapshot.current_group.name
            if workspace_snapshot is not None
            and workspace_snapshot.current_group is not None
            else None
        )
        stop_group_automation_for_configuration_change()
        try:
            imported_names = (
                group_configuration_service.import_configuration(
                    Path(selected),
                    reserved_hotkeys=(
                        configured_feature_hotkeys.values()
                    ),
                )
            )
        except SyncCycleError:
            return GroupManagementViewResult(
                False,
                current_name,
                SyncCycleError.player_message,
            )
        except (OSError, UnicodeError, ValueError) as error:
            if logger is not None:
                logger.warning(
                    f"Group configuration import failed: {error}"
                )
            return GroupManagementViewResult(
                False,
                current_name,
                "組別設定無法匯入，原本設定已保留。",
            )
        selected_name = (
            current_name
            if current_name is not None
            and group_configuration_service.group(current_name) is not None
            else imported_names[0]
        )
        result = finish_group_management(selected_name)
        return GroupManagementViewResult(
            result.success,
            result.current_group_name,
            f"已匯入 {len(imported_names)} 個組別；同名組別已更新。",
        )

    def group_launch_hotkey(group_name: str) -> str:
        if group_configuration_service is None:
            return ""
        group = group_configuration_service.group(group_name)
        return group.launch_hotkey if group is not None else ""

    def change_group_launch_hotkey(
        group_name: str,
        hotkey: str,
    ) -> object:
        if group_configuration_service is None:
            return "組別設定尚未準備完成。"
        normalized = normalize_feature_hotkey(hotkey)
        if normalized and normalized in configured_feature_hotkeys.values():
            return "無法設定：這個快捷鍵已被其他功能使用"
        try:
            group_configuration_service.set_launch_hotkey(
                group_name,
                normalized,
            )
        except GroupHotkeyConflictError:
            return GroupHotkeyConflictError.player_message
        return None

    def group_entries(group_name: str):
        if group_configuration_service is None:
            return ()
        group = group_configuration_service.group(group_name)
        return group.entries if group is not None else ()

    def group_master_locked(group_name: str) -> bool:
        if group_configuration_service is None:
            return True
        group = group_configuration_service.group(group_name)
        return group.master_locked if group is not None else True

    def change_group_master_locked(
        group_name: str,
        locked: bool,
    ) -> object:
        if group_configuration_service is None:
            return "組別設定尚未準備完成。"
        if not group_configuration_service.set_master_locked(
            group_name,
            locked,
        ):
            return False
        return (
            "主窗口已上鎖。"
            if locked
            else "主窗口已解鎖，可以調整組別角色。"
        )

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
        except (SyncCycleError, GroupMasterLockedError) as error:
            return error.player_message
        if home_view is not None:
            home_view.refresh_group_entries()
            home_view.refresh_group_sync_relations()
        if operation_record_store is not None:
            operation_record_store.ensure_daily_file()
        return bool(added)

    def remove_group_shortcut(
        group_name: str,
        entry_id: str,
    ) -> object:
        if group_configuration_service is None:
            return False
        stop_group_automation_for_configuration_change()
        try:
            removed = group_configuration_service.remove_shortcut(
                group_name,
                entry_id,
            )
        except GroupMasterLockedError:
            return GroupMasterLockedError.player_message
        if removed and home_view is not None:
            home_view.refresh_group_entries()
            home_view.refresh_group_sync_relations()
        if removed and operation_record_store is not None:
            operation_record_store.ensure_daily_file()
        return removed

    def set_group_main(
        group_name: str,
        entry_id: str,
    ) -> object:
        if group_configuration_service is None:
            return False
        stop_group_automation_for_configuration_change()
        try:
            changed = group_configuration_service.set_main_entry(
                group_name,
                entry_id,
            )
        except (SyncCycleError, GroupMasterLockedError) as error:
            return error.player_message
        if changed and group_role_status_service is not None:
            group_role_status_service.clear_cache()
        return changed

    def clear_group(
        group_name: str,
    ) -> GroupManagementViewResult:
        if group_configuration_service is None:
            return GroupManagementViewResult(
                False,
                group_name,
                "組別設定尚未準備完成。",
            )
        stop_group_automation_for_configuration_change()
        try:
            changed = group_configuration_service.clear_group(group_name)
        except GroupMasterLockedError:
            return GroupManagementViewResult(
                False,
                group_name,
                GroupMasterLockedError.player_message,
            )
        if not changed:
            return GroupManagementViewResult(
                False,
                group_name,
                "目前組別沒有可清空的角色。",
            )
        return finish_group_management(group_name)

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

    def refresh_character_data(group_name: str | None) -> None:
        if (
            character_view_service is None
            or character_detail_view_service is None
        ):
            return
        if (
            group_name
            and character_store is not None
            and group_character_registration_service is not None
        ):
            profiles = group_character_registration_service.ensure_group(
                group_name,
                character_store.load(),
            )
            character_view_service.replace_characters(profiles)
        if home_view is None:
            return
        choices = CharacterDetailChoiceService(
            character_detail_view_service,
            open_character_detail,
        ).all(group_name)
        home_view.set_character_data(
            character_view_service.all(group_name),
            choices,
        )

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
            selected_keys_provider=lambda: tuple(
                configured_selected_sync_keys
            ),
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

    def direct_auto_click_enabled() -> bool:
        return (
            mouse_sync_monitor is not None
            and mouse_sync_monitor.enabled
            and current_input_policy() is not None
        )

    def direct_auto_click_execution_allowed() -> bool:
        return (
            auto_click_service.direct_sync_execution_allowed()
            and direct_auto_click_enabled()
        )

    def deliver_direct_auto_click(source) -> bool:
        if pointer_sync_controller is None:
            return False
        policy = current_input_policy()
        if policy is None:
            return False
        result = pointer_sync_controller.send_click(
            source_handle=source.source_handle,
            x_ratio=source.x_ratio,
            y_ratio=source.y_ratio,
            policy=policy,
            execute=True,
            include_source=True,
            execution_guard=direct_auto_click_execution_allowed,
        )
        deferred_failures = {
            "sync_group_deferred_reconnect",
            "sync_deferred_reconnect",
        }
        accepted = result.passed or (
            result.eligible_windows > 0
            and bool(result.failure_codes)
            and set(result.failure_codes) <= deferred_failures
        )
        if logger is not None and not accepted:
            logger.warning(
                "Direct synchronized continuous click failed; "
                f"eligible={result.eligible_windows}; "
                f"sent={result.sent_windows}; "
                f"failures={','.join(result.failure_codes) or 'none'}"
            )
        return accepted

    if pointer_sync_controller is not None:
        auto_click_service.configure_direct_left_sync(
            source_provider=auto_click_pointer_source_backend.sample,
            eligible=lambda source: (
                pointer_sync_controller.source_is_eligible(
                    source.source_handle
                )
            ),
            deliver=deliver_direct_auto_click,
            enabled=direct_auto_click_enabled,
            block_physical_fallback=lambda source: (
                pointer_sync_controller.source_must_block_physical_fallback(
                    source.source_handle
                )
            ),
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
        auto_click_service.invalidate_direct_sync()
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
    current_character_group = (
        workspace_state.current_group.name
        if workspace_state.current_group is not None
        else None
    )
    character_choices = (
        CharacterDetailChoiceService(
            character_detail_view_service,
            open_character_detail,
        ).all(current_character_group)
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
        selected_sync_keys=tuple(configured_selected_sync_keys),
        on_selected_sync_keys_change=change_selected_sync_keys,
        feature_hotkeys=configured_feature_hotkeys,
        on_feature_hotkey_change=change_feature_hotkey,
        group_choices=group_choices,
        group_choices_provider=(
            group_selection_service.choices
            if group_selection_service is not None
            else None
        ),
        current_group_name=(
            workspace_state.current_group.name
            if workspace_state.current_group is not None
            else None
        ),
        on_group_change=change_group,
        on_launch_group=launch_group_and_restore,
        on_restore_group=restore_group_positions,
        on_record_group_positions=record_group_positions,
        on_create_group=create_group,
        on_rename_group=rename_group,
        on_delete_group=delete_group,
        on_move_group=move_group,
        on_export_group_configuration=export_group_configuration,
        on_import_group_configuration=import_group_configuration,
        group_launch_hotkey_provider=group_launch_hotkey,
        on_group_launch_hotkey_change=change_group_launch_hotkey,
        group_entries_provider=group_entries,
        group_master_locked_provider=group_master_locked,
        on_group_master_locked_change=change_group_master_locked,
        on_add_group_shortcuts=add_group_shortcuts,
        on_remove_group_shortcut=remove_group_shortcut,
        on_set_group_main=set_group_main,
        on_clear_group=clear_group,
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
            character_view_service.all(current_character_group)
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
        theme_name=configured_theme,
        on_theme_change=change_ui_theme,
        background_image_path=(
            background_image_service.current_background()
            if background_image_service is not None
            else None
        ),
        background_fill_color=(
            background_image_service.settings().fill_color
            if background_image_service is not None
            else "#C9A35D"
        ),
        background_settings=(
            background_image_service.settings()
            if background_image_service is not None
            else None
        ),
        background_for_page=(
            background_image_service.current_background
            if background_image_service is not None
            else None
        ),
        background_metadata_provider=(
            background_image_service.metadata
            if background_image_service is not None
            else None
        ),
        background_settings_provider=(
            background_image_service.settings
            if background_image_service is not None
            else None
        ),
        on_choose_background_source=choose_background_source,
        on_prepare_background_image=prepare_background_image,
        on_save_background_image=save_background_image,
        on_discard_background_image=discard_background_image,
        on_clear_background_image=clear_background_image,
        on_clear_page_background=clear_page_background,
        on_clear_all_backgrounds=clear_all_backgrounds,
        on_export_background_settings=export_background_settings,
        on_import_background_settings=import_background_settings,
        on_background_display_settings_update=(
            update_background_display_settings
        ),
        on_refresh_error=report_refresh_error,
    )
    home_view.build()
    refresh_character_data(current_character_group)
    auto_click_service.subscribe(
        lambda snapshot: home_view.set_auto_click_running(
            snapshot.running,
            snapshot.sent_count,
        )
    )
    feature_hotkey_monitor = FeatureHotkeyMonitor(
        {
            "sync": home_view.toggle_keyboard_sync_from_hotkey,
            "reconnect": home_view.toggle_smart_reconnect_from_hotkey,
            "auto_click": home_view.toggle_auto_click_from_hotkey,
        },
        hotkeys_provider=lambda: dict(configured_feature_hotkeys),
        schedule=window.after,
        cancel=window.after_cancel,
    )
    feature_hotkey_monitor.start()
    group_launch_hotkey_monitor = GroupLaunchHotkeyMonitor(
        lambda group_name: launch_group_and_restore(group_name),
        hotkeys_provider=(
            group_configuration_service.launch_hotkeys
            if group_configuration_service is not None
            else (lambda: {})
        ),
        schedule=window.after,
        cancel=window.after_cancel,
    )
    group_launch_hotkey_monitor.start()

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
        overlay_scale = card_display_scale(window)
        overlay_width = round(160 * overlay_scale)
        overlay_height = round(75 * overlay_scale)
        overlay_layout = CardOverlayLayoutService(
            card_view_state_service,
            WindowsWorkAreaReader(),
            CardSize(width=overlay_width, height=overlay_height),
            right_margin=round(12 * overlay_scale),
            bottom_margin=round(12 * overlay_scale),
            gap=round(6 * overlay_scale),
        )
        overlay_runtime = build_windows_card_overlay_runtime(
            window,
            card_service,
            overlay_layout,
            TkCardTextSettings(
                background="#80591F",
                foreground="#FFF2CF",
                muted_foreground="#FFF2CF",
                accent="#FFF2CF",
                title_size=max(10, round(10 * overlay_scale)),
                body_size=max(9, round(9 * overlay_scale)),
                horizontal_padding=max(8, round(8 * overlay_scale)),
                vertical_padding=max(5, round(5 * overlay_scale)),
                card_width=overlay_width,
            ),
        )
        overlay_runtime.start()
        expiry_monitor = CardExpiryMonitor(
            card_service,
            window.after,
            on_pending_expired=lambda card: operation_record_store.append(
                "提醒卡",
                "系統",
                f"{card.activity.name}－排隊期間已過期，未顯示",
            ),
        )
        expiry_monitor.start()
        card_service.subscribe(home_view.refresh_cards)
        if activity_reminder_service is not None:
            activity_reminder_monitor = ActivityReminderMonitor(
                activity_reminder_service,
                window.after,
                window.after_cancel,
            )
            activity_reminder_monitor.start()

    tray_controller: SystemTrayController | None = None

    def close_window() -> None:
        if not home_view.prepare_close():
            return
        if tray_controller is not None:
            tray_controller.stop()
        home_view.dispose()
        if group_window_launch_service is not None:
            group_window_launch_service.stop()
        feature_hotkey_monitor.stop()
        group_launch_hotkey_monitor.stop()
        auto_click_service.close(timeout_seconds=1.0)
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
    tray_group_state = (
        workspace_service.snapshot()
        if workspace_service is not None
        else WorkspaceState()
    )
    tray_group_name = (
        tray_group_state.current_group.name
        if tray_group_state.current_group is not None
        else "尚未設定組別"
    )
    tray_controller = SystemTrayController(
        window,
        WindowsSystemTrayBackend(),
        icon_path=resource_path(APP_ICON_ICO),
        tooltip=f"輔｜{tray_group_name}｜同步安全停止",
        on_close=close_window,
    )
    if tray_controller.start():
        window.bind("<Unmap>", tray_controller.handle_unmap, add="+")
    elif logger is not None:
        logger.error(
            "System tray icon was not started; the main window remains usable."
        )
    window._card_overlay_runtime = overlay_runtime
    window._card_expiry_monitor = expiry_monitor
    window._activity_reminder_monitor = activity_reminder_monitor
    window._auto_click_service = auto_click_service
    window._feature_hotkey_monitor = feature_hotkey_monitor
    window._group_role_status_monitor = group_role_status_monitor
    window._deferred_sync_monitor = deferred_sync_monitor
    window._windows_app_identity = window_identity
    window._system_tray_controller = tray_controller
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
