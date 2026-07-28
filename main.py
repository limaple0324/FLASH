"""Cumulative FLASH desktop entrypoint."""

from __future__ import annotations

import json
import sys
import traceback
from datetime import datetime
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
from adapters.windows_smart_reconnect import (
    ReconnectRuntimeStateStore,
    WindowsSmartReconnectController,
)
from adapters.windows_pointer_sync import (
    Win32PointerMessageBackend,
    WindowsPointerSyncController,
)
from adapters.windows_timed_click import WindowsTimedClickBackend
from adapters.windows_sync_calibration import Win32SyncCalibrationBackend
from adapters.windows_target_desktop_verifier import TargetDesktopVerifier
from adapters.windows_window import Win32WindowBackend, WindowsWindowAdapter
from adapters.windows_client_size import Win32WindowClientSizeBackend
from adapters.windows_work_area import WindowsWorkAreaReader
from adapters.windows_system_tray import (
    SystemTrayController,
    WindowsSystemTrayBackend,
)
from cards.history_store import CardHistoryStore
from cards.service import CardService
from cards.settings import (
    DEFAULT_CARD_LIFETIME_SECONDS,
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
from core.version import MILESTONE, PRODUCT_NAME
from core.window_registry_store import WindowRegistryStore
from decision.service import DecisionService
from domain.activity_schedule import (
    ActivityScheduleCatalog,
    build_confirmed_activity_catalog,
)
from domain.character_store import CharacterStore
from domain.character_game_data_store import CharacterGameDataStore
from domain.game_shortcuts import CONFIRMED_GAME_SHORTCUTS
from domain.progress import TAIPEI_TIMEZONE
from domain.progress_store import ActivityProgressStore
from habit.service import ActivityOrderHabitService
from habit.store import ActivityOrderHabitStore
from habit.preference_service import PlayerHabitPreferenceService
from habit.preference_store import PlayerHabitStore
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
from services.card_overlay_selection_assembly import (
    build_windows_card_overlay_selection_coordinator,
)
from services.card_preview_selection_service import (
    CardPreviewSelectionService,
)
from services.card_preview_selection_store import (
    CardPreviewSelectionStore,
)
from services.card_view_state_service import CardViewStateService
from services.player_habit_reminder_monitor import (
    PlayerHabitReminderMonitor,
)
from services.player_habit_reminder_service import (
    PlayerHabitReminderService,
)
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
from services.window_size_adjustment_service import (
    WindowSizeAdjustmentResult,
    WindowSizeAdjustmentService,
)
from services.game_time_timed_click_service import (
    GameTimeTimedClickResult,
    GameTimeTimedClickService,
    clamp_time_offset_ms,
)
from services.keyboard_sync_monitor import (
    KeyboardSyncMonitor,
    Win32KeyboardStateBackend,
)
from services.logger_service import LoggerService
from services.lifecycle_contract import start_service, stop_service
from services.mouse_sync_monitor import (
    MouseSyncMonitor,
    Win32MouseStateBackend,
)
from services.reconnect_failure_status_service import (
    ReconnectFailureStatusService,
)
from services.role_id_template_service import RoleIdTemplateService
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
from services.data_contract_migration_service import (
    DataContractMigrationService,
)
from services.target_window_contract_service import (
    TargetWindowContractService,
)
from ui.home import HomeView
from ui.home import (
    GroupManagementViewResult,
    UI_THEME_LABELS,
    theme_palette,
)
from ui.character_detail_window import CharacterDetailWindow
from ui.builtin_card_preview_catalog import (
    BUILTIN_CARD_PREVIEW_PROFILE_ID,
    build_builtin_card_preview_catalog,
)
from ui.card_preview_settings import CardPreviewCatalog
from workspace.models import WorkspaceState
from workspace.service import WorkspaceService

APP_TITLE = PRODUCT_NAME
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
ROLE_ID_TEMPLATE_FILENAME = "role_id_templates.json"
TARGET_DESKTOP_REPORT_FILENAME = "target_desktop_verification.json"
CHARACTER_FILENAME = "characters.json"
CHARACTER_GAME_DATA_FILENAME = "character_game_data.json"
ACTIVITY_PROGRESS_FILENAME = "activity_progress.json"
CARD_HISTORY_FILENAME = "card_history.json"
CARD_PREVIEW_SELECTION_FILENAME = "card_preview_selection.json"
ACTIVITY_REMINDER_STATE_FILENAME = "activity_reminder_state.json"
ACTIVITY_ORDER_HABIT_FILENAME = "activity_order_habit.json"
PLAYER_HABIT_FILENAME = "player_habits.json"
SYNC_SELECTED_KEYS_KEY = "sync_selected_keys"
FEATURE_HOTKEYS_KEY = "feature_hotkeys"
GAME_TIME_OFFSET_MS_KEY = "game_time_offset_ms"
GAME_TIME_AUTO_UPDATE_KEY = "game_time_auto_update"
TIMED_CLICK_SETTINGS_KEY = "timed_click_settings"
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


def build_services(
    root: Path | None = None,
    card_preview_catalog: CardPreviewCatalog | None = None,
):
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
            GAME_TIME_OFFSET_MS_KEY: 0,
            GAME_TIME_AUTO_UPDATE_KEY: True,
            TIMED_CLICK_SETTINGS_KEY: {
                "target_time": "",
                "lead_ms": 120,
                "repeat_count": 2,
                "repeat_interval_ms": 250,
            },
        }
    )
    data_contract_migration_service = DataContractMigrationService(config)
    data_contract_migration_service.verify_supported_versions(
        {
            "role_data": CharacterStore.SCHEMA_VERSION,
            "progress": ActivityProgressStore.SCHEMA_VERSION,
            "habits": PlayerHabitStore.SCHEMA_VERSION,
            "cards": CardHistoryStore.SCHEMA_VERSION,
            "reconnect": ReconnectRuntimeStateStore.VERSION,
            "legacy_settings": GroupConfigurationService.SCHEMA_VERSION,
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
        configuration=group_configuration_service,
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
    synchronized_window_backend = Win32WindowBackend(
        PowerShellLaunchFingerprintResolver()
    )
    target_window_contract_service = TargetWindowContractService(
        group_configuration_service,
        sync_scope_service,
        registry,
        synchronized_window_backend,
    )
    sync_calibration_backend = Win32SyncCalibrationBackend()
    role_id_template_service = RoleIdTemplateService(
        paths.data_dir() / ROLE_ID_TEMPLATE_FILENAME,
        capture_backend=sync_calibration_backend,
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
    player_habit_store = PlayerHabitStore(
        paths.data_dir() / PLAYER_HABIT_FILENAME
    )
    player_habit_service = PlayerHabitPreferenceService(player_habit_store)
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
                group_selection_service.workspace_group(
                    initial_group_choice,
                    characters,
                )
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
    card_preview_selection_store = CardPreviewSelectionStore(
        paths.data_dir() / CARD_PREVIEW_SELECTION_FILENAME
    )
    preview_catalog = (
        card_preview_catalog
        if card_preview_catalog is not None
        else build_builtin_card_preview_catalog()
    )
    card_preview_selection_service = CardPreviewSelectionService(
        preview_catalog,
        card_preview_selection_store,
    )
    if (
        card_preview_catalog is None
        and not card_preview_selection_store.configured
        and not card_preview_selection_store.recovered_from_corruption
    ):
        card_preview_selection_service.select(
            BUILTIN_CARD_PREVIEW_PROFILE_ID
        )
    if card_preview_selection_service.unavailable_stored_profile_id is not None:
        logger.warning(
            "Card preview selection references an unavailable profile; "
            "the overlay remains disabled."
        )

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
    AppContext.register(
        DataContractMigrationService,
        data_contract_migration_service,
    )
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
    AppContext.register(
        Win32WindowBackend,
        synchronized_window_backend,
    )
    AppContext.register(
        TargetWindowContractService,
        target_window_contract_service,
    )
    AppContext.register(
        Win32SyncCalibrationBackend,
        sync_calibration_backend,
    )
    AppContext.register(RoleIdTemplateService, role_id_template_service)
    AppContext.register(ActivityProgressStore, progress_store)
    AppContext.register(ActivityProgressService, progress_service)
    AppContext.register(ActivityScheduleCatalog, activity_schedule_catalog)
    AppContext.register(ActivityScheduleViewService, activity_schedule_view_service)
    AppContext.register(DecisionService, decision_service)
    AppContext.register(ActivityOrderHabitStore, activity_order_habit_store)
    AppContext.register(ActivityOrderHabitService, activity_order_habit_service)
    AppContext.register(PlayerHabitStore, player_habit_store)
    AppContext.register(PlayerHabitPreferenceService, player_habit_service)
    AppContext.register(WorkspaceService, workspace_service)
    AppContext.register(CardHistoryStore, card_history_store)
    AppContext.register(CardHistoryService, card_history_service)
    AppContext.register(CardService, card_service)
    AppContext.register(
        CardPreviewSelectionStore,
        card_preview_selection_store,
    )
    AppContext.register(CardPreviewCatalog, preview_catalog)
    AppContext.register(
        CardPreviewSelectionService,
        card_preview_selection_service,
    )
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

    def current_target_windows():
        state = workspace_service.snapshot()
        group_name = (
            state.current_group.name
            if state.current_group is not None
            else None
        )
        return target_window_contract_service.windows(group_name)

    AppContext.register(SyncOperationRecordStore, operation_record_store)
    reconnect_controller = WindowsSmartReconnectController.for_real_windows(
        reference_dir=resource_path(RECONNECT_REFERENCE_DIR),
        state_path=paths.data_dir() / RECONNECT_STATE_FILENAME,
        window_backend=synchronized_window_backend,
        target_windows_provider=current_target_windows,
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
            window_backend=synchronized_window_backend,
            target_windows_provider=current_target_windows,
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
            target_windows_provider=current_target_windows,
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
                backend=synchronized_window_backend,
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


def shutdown_event_subscriptions(
    logger: LoggerService | None = None,
) -> None:
    """Detach long-lived event listeners before the service registry is released."""
    try:
        state_service = AppContext.get(TargetWindowStateService)
        if state_service is not None and not state_service.close():
            raise RuntimeError("Target-window listeners were not detached.")
    except Exception:
        if logger is not None:
            try:
                logger.error(
                    "Event subscription shutdown failed:\n"
                    f"{traceback.format_exc()}"
                )
            except Exception:
                pass


def shutdown_sync_controllers(
    logger: LoggerService | None = None,
) -> None:
    """Stop delayed input queues and ensure no callback survives shutdown."""
    for controller_type in (
        WindowsInputSyncController,
        WindowsPointerSyncController,
    ):
        controller = AppContext.get(controller_type)
        if controller is None:
            continue
        try:
            if not controller.close(timeout_seconds=1.0):
                raise RuntimeError(
                    f"{controller_type.__name__} did not stop cleanly."
                )
        except Exception:
            if logger is not None:
                try:
                    logger.error(
                        "Synchronized input shutdown failed:\n"
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
    contract_service = AppContext.get(TargetWindowContractService)
    workspace_service = AppContext.get(WorkspaceService)
    if contract_service is not None and workspace_service is not None:
        workspace = workspace_service.snapshot()
        group_name = (
            workspace.current_group.name
            if workspace.current_group is not None
            else None
        )
        if group_name is not None:
            snapshot = contract_service.snapshot(group_name)
            if snapshot.targets:
                safe_count = len(snapshot.safe_targets)
                target_count = len(snapshot.targets)
                all_safe = (
                    safe_count == target_count
                    and not snapshot.failure_codes
                )
                return {
                    "configured": True,
                    "safe": all_safe,
                    "code": (
                        "window.ready"
                        if all_safe
                        else (
                            "window.partial"
                            if safe_count
                            else "window.offline"
                        )
                    ),
                    "message": (
                        f"目前組別已辨識 {safe_count}/{target_count} 個遊戲視窗。"
                    ),
                    "details": dict(snapshot.to_public_dict()),
                }
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


def format_card_overlay_status(status: dict[str, object]) -> str:
    """把提醒卡樣式檢查結果轉成簡短中文。"""
    check = next(
        (
            item
            for item in _self_check_items(status)
            if item.get("name") == "card_preview_selection"
        ),
        None,
    )
    if check is None:
        return "提醒卡浮層：未取得狀態，目前保持停用。"
    if not bool(check.get("passed", False)):
        return "提醒卡浮層：設定檢查未通過，目前保持停用。"
    message = str(check.get("message", ""))
    if "not configured" in message:
        return "提醒卡浮層：尚未提供候選樣式，因此目前不顯示。"
    if "has not selected" in message:
        return "提醒卡浮層：候選樣式已準備好，尚未選擇。"
    if "ready with selected preview profile" in message:
        return "提醒卡浮層：已選擇樣式，可以顯示。"
    if "selection was corrupt" in message:
        return "提醒卡浮層：選擇資料損壞，已安全停用並保留備份。"
    if "saved preview profile is unavailable" in message:
        return "提醒卡浮層：原先選擇的樣式已不可用，目前保持停用。"
    return "提醒卡浮層：狀態無法判斷，目前保持停用。"


def format_card_display_settings_status(
    status: dict[str, object],
) -> str:
    """把提醒卡顯示時間檢查結果轉成簡短中文。"""
    check = next(
        (
            item
            for item in _self_check_items(status)
            if item.get("name") == "card_display_settings"
        ),
        None,
    )
    if check is None:
        return "提醒卡顯示時間：未取得設定狀態。"
    if not bool(check.get("passed", False)):
        return "提醒卡顯示時間：設定檢查未通過，目前使用安全預設值。"
    message = str(check.get("message", ""))
    seconds = next(
        (part for part in message.split() if part.isdecimal()),
        str(DEFAULT_CARD_LIFETIME_SECONDS),
    )
    if "setting was invalid" in message:
        return f"提醒卡顯示時間：原設定無效，已安全改用 {seconds} 秒。"
    if "uses default" in message:
        return f"提醒卡顯示時間：目前使用預設 {seconds} 秒。"
    if "is configured" in message:
        return f"提醒卡顯示時間：目前設定為 {seconds} 秒。"
    return "提醒卡顯示時間：狀態無法判斷，目前使用安全預設值。"


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
        f"{format_card_overlay_status(status)}\n"
        f"{format_card_display_settings_status(status)}\n\n"
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
    sync_calibration_backend = AppContext.get(
        Win32SyncCalibrationBackend
    )
    role_id_template_service = AppContext.get(RoleIdTemplateService)
    synchronized_window_backend = AppContext.get(Win32WindowBackend)
    workspace_service = AppContext.get(WorkspaceService)
    activity_schedule_view_service = AppContext.get(ActivityScheduleViewService)
    card_view_state_service = AppContext.get(CardViewStateService)
    card_service = AppContext.get(CardService)
    card_coordinator = AppContext.get(CardCoordinator)
    activity_reminder_service = AppContext.get(ActivityReminderService)
    card_display_settings_service = AppContext.get(CardDisplaySettingsService)
    card_preview_selection_store = AppContext.get(
        CardPreviewSelectionStore
    )
    card_preview_catalog = AppContext.get(CardPreviewCatalog)
    card_preview_selection_service = AppContext.get(
        CardPreviewSelectionService
    )
    if (
        card_preview_selection_store is not None
        and card_preview_catalog is not None
    ):
        if (
            len(card_preview_catalog.profiles) == 1
            and card_preview_catalog.profiles[0].profile_id
            == BUILTIN_CARD_PREVIEW_PROFILE_ID
        ):
            card_preview_catalog = build_builtin_card_preview_catalog(
                card_display_scale(window)
            )
        card_preview_selection_service = CardPreviewSelectionService(
            card_preview_catalog,
            card_preview_selection_store,
        )
        AppContext.register(
            CardPreviewSelectionService,
            card_preview_selection_service,
        )
    target_window_state_service = AppContext.get(TargetWindowStateService)
    target_window_contract_service = AppContext.get(
        TargetWindowContractService
    )
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
    player_habit_service = AppContext.get(PlayerHabitPreferenceService)
    home_view: HomeView | None = None

    def dispatch_to_main_window(callback) -> object | None:
        try:
            return window.after(0, callback)
        except TclError:
            return None

    group_window_backend = (
        AppContext.get(Win32WindowBackend)
        if group_launch_service is not None
        else None
    )
    group_window_launch_service = (
        GroupWindowLaunchService(
            group_launch_service,
            group_window_backend,
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
    window_size_adjustment_service = (
        WindowSizeAdjustmentService(
            group_launch_service,
            group_window_backend,
            Win32WindowClientSizeBackend(),
        )
        if group_launch_service is not None
        and group_window_backend is not None
        else None
    )

    def current_timed_click_fingerprints() -> tuple[str, ...]:
        if workspace_service is None or sync_scope_service is None:
            return ()
        state = workspace_service.snapshot()
        group_name = (
            state.current_group.name
            if state.current_group is not None
            else None
        )
        if group_name is None:
            return ()
        scope = sync_scope_service.scope(group_name)
        return scope.fingerprints if scope.ready else ()

    def current_operation_role_name() -> str:
        if workspace_service is None:
            return "目前組別"
        state = workspace_service.snapshot()
        return (
            state.current_group.name
            if state.current_group is not None
            else "目前組別"
        )

    def complete_timed_click_result(
        result: GameTimeTimedClickResult,
    ) -> None:
        if (
            operation_record_store is not None
            and (
                result.action != "poll"
                or not result.success
            )
        ):
            operation_record_store.append(
                "定時按下",
                current_operation_role_name(),
                result.message,
            )
        if home_view is not None:
            home_view.set_timed_click_result(result)

    game_time_timed_click_service = (
        GameTimeTimedClickService(
            WindowsTimedClickBackend(
                group_window_backend,
                Win32PointerMessageBackend(),
            ),
            schedule=window.after,
            cancel=window.after_cancel,
            allowed_fingerprints_provider=current_timed_click_fingerprints,
            result_callback=complete_timed_click_result,
        )
        if group_window_backend is not None
        else None
    )
    if game_time_timed_click_service is not None:
        game_time_timed_click_service.configure_game_time(
            offset_ms=(
                config.get(GAME_TIME_OFFSET_MS_KEY, 0)
                if config is not None
                else 0
            ),
            auto_update=(
                config.get(GAME_TIME_AUTO_UPDATE_KEY, True)
                if config is not None
                else True
            ),
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

    def record_window_size_result(
        result: WindowSizeAdjustmentResult,
        role_name: str,
    ) -> WindowSizeAdjustmentResult:
        if (
            operation_record_store is not None
            and (
                not result.success
                or result.action == "read_main"
                or result.changed_count > 0
            )
        ):
            operation_record_store.append(
                "視窗尺寸",
                role_name,
                result.player_message,
            )
        return result

    def read_main_window_size(
        group_name: str,
    ) -> WindowSizeAdjustmentResult:
        if window_size_adjustment_service is None:
            return WindowSizeAdjustmentResult(
                False,
                "read_main",
                failure_code="group_plan_unavailable",
            )
        group = (
            group_configuration_service.group(group_name)
            if group_configuration_service is not None
            else None
        )
        main_shortcut = (
            group.entries[0].shortcut_path
            if group is not None and group.entries
            else None
        )
        return record_window_size_result(
            window_size_adjustment_service.read_main(
                group_name,
                main_shortcut,
            ),
            group_name,
        )

    def apply_group_window_size(
        group_name: str,
        width: int,
        height: int,
    ) -> WindowSizeAdjustmentResult:
        if window_size_adjustment_service is None:
            return WindowSizeAdjustmentResult(
                False,
                "current_group",
                width=width,
                height=height,
                failure_code="group_plan_unavailable",
            )
        return record_window_size_result(
            window_size_adjustment_service.apply_current_group(
                group_name,
                width,
                height,
            ),
            group_name,
        )

    def apply_all_window_size(
        width: int,
        height: int,
    ) -> WindowSizeAdjustmentResult:
        if window_size_adjustment_service is None:
            return WindowSizeAdjustmentResult(
                False,
                "all",
                width=width,
                height=height,
                failure_code="flash_window_unavailable",
            )
        return record_window_size_result(
            window_size_adjustment_service.apply_all(width, height),
            "全部遊戲視窗",
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
            entry_by_id = {
                entry.entry_id: entry
                for configured_group in (
                    group_configuration_service.groups()
                    if group_configuration_service is not None
                    else ()
                )
                for entry in configured_group.entries
            }
            target_settings = {
                fingerprint: entry_by_id[entry_id].sync_settings
                for entry_id, fingerprint in zip(
                    scope.entry_ids,
                    scope.fingerprints,
                )
                if entry_id in entry_by_id
            }
            input_controller.set_expected_windows(len(scope.fingerprints))
            input_controller.set_allowed_fingerprints(scope.fingerprints)
            input_controller.set_target_settings(target_settings)
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
                pointer_sync_controller.set_target_settings(
                    target_settings
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

    def workspace_group_for_choice(choice):
        profiles = (
            character_store.load()
            if character_store is not None
            else ()
        )
        if group_character_registration_service is not None:
            profiles = group_character_registration_service.ensure_group(
                choice.name,
                profiles,
            )
        if character_view_service is not None:
            character_view_service.replace_characters(profiles)
        return group_selection_service.workspace_group(
            choice,
            tuple(profiles),
        )

    def change_group(name: str) -> GroupManagementViewResult:
        current_group = (
            workspace_service.snapshot().current_group
            if workspace_service is not None
            else None
        )
        current_name = (
            current_group.name
            if current_group is not None
            else None
        )
        if (
            group_selection_service is None
            or workspace_service is None
            or config is None
        ):
            return GroupManagementViewResult(
                False,
                current_name,
                "組別資料尚未準備完成，未變更目前組別。",
            )
        choice = group_selection_service.find(name)
        if choice is None:
            return GroupManagementViewResult(
                False,
                current_name,
                "找不到這個組別，未變更目前組別。",
            )
        if choice.name == current_name:
            return GroupManagementViewResult(True, choice.name)
        if not stop_group_automation_for_configuration_change():
            return GroupManagementViewResult(
                False,
                current_name,
                "自動操作尚未完全停止，未切換組別。",
            )
        selected_workspace_group = workspace_group_for_choice(choice)
        apply_group_identity(choice)
        workspace_service.set_current_group(
            selected_workspace_group
        )
        workspace_service.set_next_step("查看目前需要注意的內容")
        config.set(CURRENT_GROUP_NAME_KEY, choice.name)
        if group_role_status_service is not None:
            group_role_status_service.clear_cache()
        refresh_character_data(choice.name)
        return GroupManagementViewResult(True, choice.name)

    def stop_group_automation_for_configuration_change() -> bool:
        auto_click_service.stop()
        if input_controller is not None:
            input_controller.invalidate_scheduled()
        if pointer_sync_controller is not None:
            pointer_sync_controller.invalidate_scheduled()
        if game_time_timed_click_service is not None:
            game_time_timed_click_service.clear_target(notify=False)
        sync_stopped = True
        keyboard_stopped = None
        mouse_stopped = None
        if keyboard_sync_monitor is not None:
            keyboard_stopped = stop_service(keyboard_sync_monitor)
        if mouse_sync_monitor is not None:
            mouse_stopped = stop_service(mouse_sync_monitor)
        sync_stopped = (
            (keyboard_stopped is None or keyboard_stopped.success)
            and (mouse_stopped is None or mouse_stopped.success)
        )
        if home_view is not None and sync_stopped:
            home_view.set_keyboard_sync_enabled(False)
        reconnect_stopped = None
        if smart_reconnect_monitor is not None:
            reconnect_stopped = stop_service(
                smart_reconnect_monitor,
                timeout_seconds=1.0,
            )
            if home_view is not None and reconnect_stopped.success:
                home_view.set_smart_reconnect_enabled(False)
        if (
            config is not None
            and (
                smart_reconnect_monitor is None
                or reconnect_stopped.success
            )
        ):
            config.update_values(
                {
                    SMART_RECONNECT_ENABLED_KEY: False,
                    SMART_RECONNECT_CONSENT_KEY: False,
                }
            )
        stopped = (
            sync_stopped
            and (
                reconnect_stopped is None
                or reconnect_stopped.success
            )
        )
        if logger is not None and not stopped:
            logger.warning(
                "Group configuration change was blocked because "
                "an automation service did not stop."
            )
        return stopped

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
            workspace_group_for_choice(choice)
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

    def group_configuration_stop_failure(
        current_name: str | None,
    ) -> GroupManagementViewResult:
        return GroupManagementViewResult(
            False,
            current_name,
            "自動操作尚未完全停止，組別設定未變更。",
        )

    def detach_group_entries(
        group_name: str,
        entry_ids,
    ) -> None:
        if group_character_registration_service is not None:
            group_character_registration_service.detach_entries(
                group_name,
                entry_ids,
            )

    def create_group(name: str) -> GroupManagementViewResult:
        if group_configuration_service is None:
            return GroupManagementViewResult(
                False,
                None,
                "組別設定尚未準備完成。",
            )
        current_group = (
            workspace_service.snapshot().current_group
            if workspace_service is not None
            else None
        )
        current_name = (
            current_group.name if current_group is not None else None
        )
        if not stop_group_automation_for_configuration_change():
            return group_configuration_stop_failure(current_name)
        if not group_configuration_service.create_group(name):
            return GroupManagementViewResult(
                False,
                current_name,
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
        if not stop_group_automation_for_configuration_change():
            return group_configuration_stop_failure(old_name)
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
        group = group_configuration_service.group(name)
        removed_entry_ids = (
            tuple(entry.entry_id for entry in group.entries)
            if group is not None
            else ()
        )
        if not stop_group_automation_for_configuration_change():
            return group_configuration_stop_failure(name)
        if not group_configuration_service.delete_group(name):
            return GroupManagementViewResult(
                False,
                name,
                "找不到要刪除的組別。",
            )
        detach_group_entries(name, removed_entry_ids)
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
        if not stop_group_automation_for_configuration_change():
            return group_configuration_stop_failure(name)
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
        previous_entries = {
            group.name: frozenset(
                entry.entry_id for entry in group.entries
            )
            for group in group_configuration_service.groups()
        }
        if not stop_group_automation_for_configuration_change():
            return group_configuration_stop_failure(current_name)
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
        for old_group_name, old_entry_ids in previous_entries.items():
            current_group = group_configuration_service.group(
                old_group_name
            )
            current_entry_ids = (
                frozenset(
                    entry.entry_id for entry in current_group.entries
                )
                if current_group is not None
                else frozenset()
            )
            detach_group_entries(
                old_group_name,
                old_entry_ids - current_entry_ids,
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
        if not stop_group_automation_for_configuration_change():
            return "自動操作尚未完全停止，未加入角色。"
        try:
            added = group_configuration_service.add_shortcuts(
                group_name,
                tuple(Path(path) for path in selected),
            )
        except (SyncCycleError, GroupMasterLockedError) as error:
            return error.player_message
        if added:
            finish_group_management(group_name)
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
        group = group_configuration_service.group(group_name)
        removed_entry = (
            next(
                (
                    entry
                    for entry in group.entries
                    if entry.entry_id == entry_id
                ),
                None,
            )
            if group is not None
            else None
        )
        if not stop_group_automation_for_configuration_change():
            return "自動操作尚未完全停止，未移除角色。"
        try:
            removed = group_configuration_service.remove_shortcut(
                group_name,
                entry_id,
            )
        except GroupMasterLockedError:
            return GroupMasterLockedError.player_message
        if removed and removed_entry is not None:
            detach_group_entries(group_name, (removed_entry.entry_id,))
            finish_group_management(group_name)
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
        if not stop_group_automation_for_configuration_change():
            return "自動操作尚未完全停止，未變更主窗口。"
        try:
            changed = group_configuration_service.set_main_entry(
                group_name,
                entry_id,
            )
        except (SyncCycleError, GroupMasterLockedError) as error:
            return error.player_message
        if changed:
            finish_group_management(group_name)
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
        group = group_configuration_service.group(group_name)
        removed_entry_ids = (
            tuple(entry.entry_id for entry in group.entries)
            if group is not None
            else ()
        )
        if not stop_group_automation_for_configuration_change():
            return group_configuration_stop_failure(group_name)
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
        detach_group_entries(group_name, removed_entry_ids)
        return finish_group_management(group_name)

    def unique_window_for_group_entry(
        group_name: str,
        entry_id: str,
    ):
        if (
            group_configuration_service is None
            or group_launch_service is None
            or synchronized_window_backend is None
        ):
            return None
        group = group_configuration_service.group(group_name)
        plan = group_launch_service.plan(group_name)
        if group is None or not plan.ready:
            return None
        entry = next(
            (
                item
                for item in group.entries
                if item.entry_id == entry_id
            ),
            None,
        )
        if entry is None:
            return None
        normalized_path = str(
            entry.shortcut_path.resolve(strict=False)
        ).casefold()
        target = next(
            (
                item
                for item in plan.targets
                if str(
                    item.shortcut_path.resolve(strict=False)
                ).casefold()
                == normalized_path
            ),
            None,
        )
        if target is None:
            return None
        matches = tuple(
            item
            for item in synchronized_window_backend.list_windows()
            if normalize_launch_fingerprint(item.launch_fingerprint)
            == target.fingerprint
        )
        return matches[0] if len(matches) == 1 else None

    def refresh_group_sync_identity(group_name: str) -> None:
        if group_selection_service is None:
            return
        choice = group_selection_service.find(group_name)
        if choice is not None:
            apply_group_identity(choice)

    def capture_sync_base_point(group_name: str) -> str:
        if (
            group_configuration_service is None
            or sync_calibration_backend is None
        ):
            return "主基準點功能尚未準備完成。"
        group = group_configuration_service.group(group_name)
        if group is None or not group.entries:
            return "目前組別沒有可用的主窗口。"
        window_info = unique_window_for_group_entry(
            group_name,
            group.entries[0].entry_id,
        )
        if window_info is None:
            return "無法唯一確認主窗口，未保存基準點。"
        point = sync_calibration_backend.cursor_client_point(
            window_info.handle
        )
        if point is None:
            return "滑鼠不在主窗口遊戲內容區，未保存基準點。"
        group_configuration_service.set_sync_base_point(
            group_name,
            point,
        )
        return f"已保存主基準點：{point[0]}, {point[1]}"

    def capture_sync_target_point(
        group_name: str,
        entry_id: str,
    ) -> str:
        if (
            group_configuration_service is None
            or sync_calibration_backend is None
        ):
            return "角色偏移功能尚未準備完成。"
        group = group_configuration_service.group(group_name)
        if group is None or group.sync_base_point is None:
            return "請先設定主窗口基準點。"
        entry = next(
            (
                item
                for item in group.entries
                if item.entry_id == entry_id
            ),
            None,
        )
        if entry is None:
            return "找不到這個角色，未保存偏移。"
        window_info = unique_window_for_group_entry(group_name, entry_id)
        if window_info is None:
            return "無法唯一確認角色窗口，未保存偏移。"
        point = sync_calibration_backend.cursor_client_point(
            window_info.handle
        )
        if point is None:
            return "滑鼠不在該角色遊戲內容區，未保存偏移。"
        offset_x = point[0] - group.sync_base_point[0]
        if not stop_group_automation_for_configuration_change():
            return "自動操作尚未完全停止，未保存角色偏移。"
        changed = group_configuration_service.set_sync_target_settings(
            group_name,
            entry_id,
            offset_enabled=True,
            offset_x=offset_x,
            offset_y=0,
            delay_ms=entry.sync_settings.delay_ms,
        )
        if changed:
            refresh_group_sync_identity(group_name)
        return f"已套用角色偏移：X {offset_x}、Y 0"

    def save_sync_target_settings(
        group_name: str,
        entry_id: str,
        enabled: bool,
        offset_x: int,
        offset_y: int,
        delay_ms: int,
    ) -> str:
        if group_configuration_service is None:
            return "同步設定尚未準備完成。"
        if not (-20_000 <= offset_x <= 20_000):
            return "X 偏移必須介於 -20000 到 20000。"
        if not (-20_000 <= offset_y <= 20_000):
            return "Y 偏移必須介於 -20000 到 20000。"
        if not (0 <= delay_ms <= 5_000):
            return "延遲必須介於 0 到 5000 毫秒。"
        if not stop_group_automation_for_configuration_change():
            return "自動操作尚未完全停止，未保存角色偏移與延遲。"
        changed = group_configuration_service.set_sync_target_settings(
            group_name,
            entry_id,
            offset_enabled=enabled,
            offset_x=offset_x,
            offset_y=offset_y,
            delay_ms=delay_ms,
        )
        if not changed:
            return "同步偏移與延遲沒有變更。"
        refresh_group_sync_identity(group_name)
        return "已保存角色偏移與延遲；同步已安全停止，請重新啟用。"

    def clear_sync_target_settings(
        group_name: str,
        entry_id: str,
    ) -> str:
        if group_configuration_service is None:
            return "同步設定尚未準備完成。"
        if not stop_group_automation_for_configuration_change():
            return "自動操作尚未完全停止，未清除角色偏移與延遲。"
        changed = group_configuration_service.clear_sync_target_settings(
            group_name,
            entry_id,
        )
        if not changed:
            return "角色偏移與延遲原本已是清除狀態。"
        refresh_group_sync_identity(group_name)
        return "已清除角色偏移與延遲；同步已安全停止。"

    def calibrate_role_id(
        group_name: str,
        entry_id: str,
        role_id: str,
    ) -> str:
        if (
            role_id_template_service is None
            or group_configuration_service is None
        ):
            return "角色ID校正尚未準備完成。"
        window_info = unique_window_for_group_entry(group_name, entry_id)
        if window_info is None:
            return "無法唯一確認角色窗口，未校正角色ID。"
        result = role_id_template_service.calibrate(
            window_info.handle,
            role_id,
        )
        if result.success:
            group_configuration_service.set_role_id(
                group_name,
                entry_id,
                result.role_id,
            )
        return result.message

    def read_role_id(group_name: str, entry_id: str) -> str:
        if (
            role_id_template_service is None
            or group_configuration_service is None
        ):
            return "角色ID讀取尚未準備完成。"
        window_info = unique_window_for_group_entry(group_name, entry_id)
        if window_info is None:
            return "無法唯一確認角色窗口，未讀取角色ID。"
        result = role_id_template_service.read(window_info.handle)
        if result.success:
            group_configuration_service.set_role_id(
                group_name,
                entry_id,
                result.role_id,
            )
            return f"已讀取角色ID：{result.role_id}"
        return result.message

    def add_group_sync_relation(
        group_name: str,
        member_entry_id: str,
    ) -> object:
        if group_configuration_service is None:
            return False
        group = group_configuration_service.group(group_name)
        if group is None or not group.entries:
            return False
        if not stop_group_automation_for_configuration_change():
            return "自動操作尚未完全停止，未加入同步關係。"
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
        if not stop_group_automation_for_configuration_change():
            return False
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

    def card_preview_choices():
        if card_preview_selection_service is None:
            return ()
        return card_preview_selection_service.available_choices()

    def select_card_preview(profile_id: str):
        if card_preview_selection_service is None:
            raise RuntimeError("提醒卡樣式服務目前不可用。")
        return card_preview_selection_service.select(profile_id)

    def clear_card_preview():
        if card_preview_selection_service is None:
            raise RuntimeError("提醒卡樣式服務目前不可用。")
        return card_preview_selection_service.clear()

    def update_habit_observation_days(days: int):
        if player_habit_service is None:
            raise RuntimeError("player habit service is unavailable")
        memory = player_habit_service.set_observation_days(days)
        if operation_record_store is not None:
            operation_record_store.append(
                "玩家習慣",
                "系統",
                f"觀察天數已調整為 {memory.settings.observation_days} 天",
            )
        return player_habit_service.settings_view()

    def remove_habit_preference(preference_id: str):
        if player_habit_service is None:
            raise RuntimeError("player habit service is unavailable")
        removed = player_habit_service.remove_preference(preference_id)
        if removed and operation_record_store is not None:
            operation_record_store.append(
                "玩家習慣",
                "系統",
                "已刪除一筆玩家確認的偏好",
            )
        return player_habit_service.settings_view()

    def modify_habit_preference(
        preference_id: str,
        values: tuple[str, ...],
    ):
        if player_habit_service is None:
            raise RuntimeError("player habit service is unavailable")
        changed = player_habit_service.modify_preference(
            preference_id,
            values,
            datetime.now(TAIPEI_TIMEZONE),
        )
        if operation_record_store is not None:
            operation_record_store.append(
                "玩家習慣",
                changed.subject,
                f"已修改偏好：{' → '.join(changed.values)}",
            )
        return player_habit_service.settings_view()

    def clear_habit_preferences():
        if player_habit_service is None:
            raise RuntimeError("player habit service is unavailable")
        removed = player_habit_service.clear_preferences()
        if removed and operation_record_store is not None:
            operation_record_store.append(
                "玩家習慣",
                "系統",
                f"已清除 {removed} 筆玩家確認的偏好",
            )
        return player_habit_service.settings_view()

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

    def current_target_handles() -> tuple[int, ...]:
        if (
            target_window_contract_service is None
            or workspace_service is None
        ):
            return ()
        workspace = workspace_service.snapshot()
        group_name = (
            workspace.current_group.name
            if workspace.current_group is not None
            else None
        )
        return tuple(
            window.handle
            for window in target_window_contract_service.windows(group_name)
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
            state_backend=Win32KeyboardStateBackend(
                foreground_handle_provider=(
                    synchronized_window_backend.foreground_handle
                    if synchronized_window_backend is not None
                    else None
                ),
                target_handles_provider=current_target_handles,
            ),
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
            state_backend=Win32MouseStateBackend(
                foreground_handle_provider=(
                    synchronized_window_backend.foreground_handle
                    if synchronized_window_backend is not None
                    else None
                ),
                target_handles_provider=current_target_handles,
            ),
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
            keyboard_stopped = stop_service(keyboard_sync_monitor)
            mouse_stopped = stop_service(mouse_sync_monitor)
            return keyboard_stopped.success and mouse_stopped.success

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
        keyboard_started = start_service(keyboard_sync_monitor)
        mouse_started = start_service(mouse_sync_monitor)
        if not keyboard_started.success or not mouse_started.success:
            stop_service(keyboard_sync_monitor)
            stop_service(mouse_sync_monitor)
            return False
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
            started = start_service(smart_reconnect_monitor)
            if not started.success:
                return False
            config.update_values(
                {
                    SMART_RECONNECT_ENABLED_KEY: True,
                    SMART_RECONNECT_CONSENT_KEY: True,
                }
            )
            if logger is not None:
                logger.info("Smart reconnect explicitly enabled by the player.")
            return True

        stopped_result = stop_service(
            smart_reconnect_monitor,
            timeout_seconds=1.0,
        )
        stopped = stopped_result.success
        if stopped:
            config.update_values(
                {
                    SMART_RECONNECT_ENABLED_KEY: False,
                    SMART_RECONNECT_CONSENT_KEY: False,
                }
            )
        if logger is not None:
            logger.info(
                "Smart reconnect explicitly disabled by the player; "
                f"worker_stopped={stopped}"
            )
        return stopped

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

    raw_timed_click_settings = (
        config.get(TIMED_CLICK_SETTINGS_KEY, {})
        if config is not None
        else {}
    )
    if not isinstance(raw_timed_click_settings, dict):
        raw_timed_click_settings = {}
    configured_timed_click_target = (
        raw_timed_click_settings.get("target_time", "")
        if isinstance(raw_timed_click_settings.get("target_time", ""), str)
        else ""
    )

    def bounded_timed_setting(
        key: str,
        fallback: int,
        minimum: int,
        maximum: int,
    ) -> int:
        try:
            value = int(raw_timed_click_settings.get(key, fallback))
        except (TypeError, ValueError):
            return fallback
        return value if minimum <= value <= maximum else fallback

    configured_timed_click_lead = bounded_timed_setting(
        "lead_ms",
        120,
        0,
        5_000,
    )
    configured_timed_click_repeat = bounded_timed_setting(
        "repeat_count",
        2,
        1,
        10,
    )
    configured_timed_click_interval = bounded_timed_setting(
        "repeat_interval_ms",
        250,
        50,
        3_000,
    )

    def change_game_time_settings(
        offset_ms: int,
        auto_update: bool,
    ):
        if game_time_timed_click_service is None:
            raise RuntimeError("遊戲時間服務尚未準備完成。")
        normalized_offset = clamp_time_offset_ms(offset_ms)
        snapshot = game_time_timed_click_service.configure_game_time(
            offset_ms=normalized_offset,
            auto_update=auto_update,
        )
        if config is not None:
            config.update_values(
                {
                    GAME_TIME_OFFSET_MS_KEY: normalized_offset,
                    GAME_TIME_AUTO_UPDATE_KEY: bool(auto_update),
                }
            )
        return snapshot

    def capture_timed_click_target() -> GameTimeTimedClickResult:
        if game_time_timed_click_service is None:
            return GameTimeTimedClickResult(
                False,
                "capture",
                "定時按下服務尚未準備完成。",
                "timed_click_unavailable",
            )
        return game_time_timed_click_service.capture_target()

    def change_timed_click(
        enabled: bool,
        target_time: str,
        lead_ms: int,
        repeat_count: int,
        repeat_interval_ms: int,
    ) -> GameTimeTimedClickResult:
        if game_time_timed_click_service is None:
            return GameTimeTimedClickResult(
                False,
                "arm" if enabled else "cancel",
                "定時按下服務尚未準備完成。",
                "timed_click_unavailable",
            )
        if not enabled:
            return game_time_timed_click_service.cancel()
        result = game_time_timed_click_service.arm(
            target_time,
            lead_ms=lead_ms,
            repeat_count=repeat_count,
            repeat_interval_ms=repeat_interval_ms,
        )
        if config is not None:
            values = {
                TIMED_CLICK_SETTINGS_KEY: {
                    "target_time": target_time.strip(),
                    "lead_ms": lead_ms,
                    "repeat_count": repeat_count,
                    "repeat_interval_ms": repeat_interval_ms,
                },
            }
            if result.success:
                values[GAME_TIME_AUTO_UPDATE_KEY] = True
            config.update_values(values)
        return result

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
        on_capture_sync_base_point=capture_sync_base_point,
        on_capture_sync_target_point=capture_sync_target_point,
        on_save_sync_target_settings=save_sync_target_settings,
        on_clear_sync_target_settings=clear_sync_target_settings,
        on_calibrate_role_id=calibrate_role_id,
        on_read_role_id=read_role_id,
        on_read_main_window_size=read_main_window_size,
        on_apply_group_window_size=apply_group_window_size,
        on_apply_all_window_size=apply_all_window_size,
        game_time_offset_ms=(
            clamp_time_offset_ms(config.get(GAME_TIME_OFFSET_MS_KEY, 0))
            if config is not None
            else 0
        ),
        game_time_auto_update=(
            bool(config.get(GAME_TIME_AUTO_UPDATE_KEY, True))
            if config is not None
            else True
        ),
        timed_click_target_time=configured_timed_click_target,
        timed_click_lead_ms=configured_timed_click_lead,
        timed_click_repeat_count=configured_timed_click_repeat,
        timed_click_repeat_interval_ms=configured_timed_click_interval,
        game_time_snapshot_provider=(
            game_time_timed_click_service.snapshot
            if game_time_timed_click_service is not None
            else None
        ),
        on_game_time_settings_change=change_game_time_settings,
        on_capture_timed_click_target=capture_timed_click_target,
        on_timed_click_change=change_timed_click,
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
        card_preview_choices_provider=card_preview_choices,
        on_card_preview_select=select_card_preview,
        on_card_preview_clear=clear_card_preview,
        habit_settings_provider=(
            player_habit_service.settings_view
            if player_habit_service is not None
            else None
        ),
        on_habit_observation_days_update=update_habit_observation_days,
        on_modify_habit_preference=modify_habit_preference,
        on_remove_habit_preference=remove_habit_preference,
        on_clear_habit_preferences=clear_habit_preferences,
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
    start_service(feature_hotkey_monitor)
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
    start_service(group_launch_hotkey_monitor)

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

    player_habit_reminder_service = (
        PlayerHabitReminderService(
            player_habit_service,
            card_coordinator,
            card_service,
            current_group_name,
            record_callback=(
                operation_record_store.append
                if operation_record_store is not None
                else None
            ),
        )
        if player_habit_service is not None
        and card_coordinator is not None
        and card_service is not None
        else None
    )

    def handle_card_action(card_id: str, action_id: str) -> object | None:
        if player_habit_reminder_service is None:
            return None
        result = player_habit_reminder_service.handle_action(
            card_id,
            action_id,
            datetime.now(TAIPEI_TIMEZONE),
        )
        if result is not None:
            home_view.refresh_habit_settings()
        return result

    group_role_status_monitor = (
        GroupRoleStatusMonitor(
            group_role_status_service,
            current_group_name,
        )
        if group_role_status_service is not None
        else None
    )
    if group_role_status_monitor is not None:
        start_service(group_role_status_monitor)
    if deferred_sync_monitor is not None:
        start_service(deferred_sync_monitor)

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
    player_habit_reminder_monitor = None
    if card_service is not None and card_view_state_service is not None:
        if card_preview_selection_service is not None:
            overlay_runtime = (
                build_windows_card_overlay_selection_coordinator(
                    window,
                    card_service,
                    card_preview_selection_service,
                    card_view_state_service,
                    WindowsWorkAreaReader(),
                    on_action=handle_card_action,
                )
            )
            start_service(overlay_runtime)
        expiry_monitor = CardExpiryMonitor(
            card_service,
            window.after,
            on_pending_expired=lambda card: operation_record_store.append(
                "提醒卡",
                "系統",
                f"{card.activity.name}－排隊期間已過期，未顯示",
            ),
            cancel=window.after_cancel,
        )
        start_service(expiry_monitor)
        card_service.subscribe(home_view.refresh_cards)
        if activity_reminder_service is not None:
            activity_reminder_monitor = ActivityReminderMonitor(
                activity_reminder_service,
                window.after,
                window.after_cancel,
            )
            start_service(activity_reminder_monitor)
        if player_habit_reminder_service is not None:
            player_habit_reminder_monitor = PlayerHabitReminderMonitor(
                player_habit_reminder_service,
                window.after,
                window.after_cancel,
            )
            start_service(player_habit_reminder_monitor)

    tray_controller: SystemTrayController | None = None

    def close_window() -> None:
        if not home_view.prepare_close():
            return
        if tray_controller is not None:
            tray_controller.stop()
        home_view.dispose()
        if group_window_launch_service is not None:
            stop_service(group_window_launch_service)
        stop_service(feature_hotkey_monitor)
        stop_service(group_launch_hotkey_monitor)
        auto_click_service.close(timeout_seconds=1.0)
        if game_time_timed_click_service is not None:
            stop_service(game_time_timed_click_service)
        if player_habit_reminder_monitor is not None:
            stop_service(player_habit_reminder_monitor)
        if group_role_status_monitor is not None:
            stop_service(
                group_role_status_monitor,
                timeout_seconds=1.0,
            )
        if deferred_sync_monitor is not None:
            stop_service(
                deferred_sync_monitor,
                timeout_seconds=1.0,
            )
        if reconnect_status_refresh_id is not None:
            try:
                window.after_cancel(reconnect_status_refresh_id)
            except TclError:
                pass
        if card_service is not None:
            card_service.unsubscribe(home_view.refresh_cards)
        if expiry_monitor is not None:
            stop_service(expiry_monitor)
        if activity_reminder_monitor is not None:
            stop_service(activity_reminder_monitor)
        if overlay_runtime is not None:
            stop_service(overlay_runtime)
        if keyboard_sync_monitor is not None:
            stop_service(keyboard_sync_monitor)
        if mouse_sync_monitor is not None:
            stop_service(mouse_sync_monitor)
        shutdown_sync_controllers(logger)
        if target_window_state_service is not None:
            target_window_state_service.close()
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
                shutdown_sync_controllers(logger)
            finally:
                try:
                    shutdown_event_subscriptions(logger)
                finally:
                    try:
                        save_registry(logger)
                    except Exception:
                        if logger is not None:
                            logger.error(
                                "Registry final save failed:\n"
                                f"{traceback.format_exc()}"
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
