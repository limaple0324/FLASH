"""Cumulative FLASH desktop entrypoint."""

from __future__ import annotations

import ctypes
import hashlib
import json
import re
import sys
import traceback
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from time import monotonic
from tkinter import PhotoImage, TclError, Tk, filedialog, messagebox

from adapters.background_capability import BackgroundCapabilityProbe
from adapters.obsidian_page_recognizer import ObsidianPageRecognizer
from adapters.windows_background_capture import (
    Win32PrintWindowProvider,
    WindowsBackgroundCaptureBackend,
)
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
    RegisteredReconnectRole,
    WindowInstanceToken,
    WindowsSmartReconnectController,
)
from adapters.windows_pointer_sync import (
    Win32PointerMessageBackend,
    WindowsPointerSyncController,
)
from adapters.windows_timed_click import WindowsTimedClickBackend
from adapters.windows_sync_calibration import Win32SyncCalibrationBackend
from adapters.windows_target_desktop_verifier import TargetDesktopVerifier
from adapters.windows_window import (
    Win32WindowBackend,
    WindowInfo,
    WindowsWindowAdapter,
    complete_window_instance_identity,
    monitored_window_instance_fingerprint,
)
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
from domain.character import CharacterImportance
from domain.character_game_data_store import CharacterGameDataStore
from domain.confirmed_activity_rules import (
    CONFIRMED_ACTIVITY_RULE_EVENT,
    ConfirmedActivityEvent,
)
from domain.game_shortcuts import CONFIRMED_GAME_SHORTCUTS
from domain.progress import ActivityInterruptionReason, TAIPEI_TIMEZONE
from domain.progress_store import ActivityProgressStore
from habit.service import ActivityOrderHabitService
from habit.store import ActivityOrderHabitStore
from habit.preference_service import PlayerHabitPreferenceService
from habit.preference_store import PlayerHabitStore
from services.activity_progress_monitor import ActivityProgressMonitor
from services.activity_progress_service import (
    ACTIVITY_PROGRESS_CHANGED_EVENT,
    ActivityProgressChange,
    ActivityProgressService,
)
from services.activity_description_service import ActivityDescriptionService
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
from services.feature_card_layout_service import FeatureCardLayoutService
from services.feature_card_settings_batch_service import (
    FeatureCardSettingsBatchService,
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
from services.player_habit_activity_observer import (
    PlayerHabitActivityObserver,
)
from services.character_detail_view_service import CharacterDetailViewService
from services.character_game_data_view_service import (
    CharacterGameDataViewService,
)
from services.character_game_data_update_service import (
    CharacterGameDataUpdateService,
)
from services.character_game_data_capture_service import (
    CharacterGameDataCaptureService,
    RegisteredGameDataWindow,
)
from services.character_detail_choice_service import CharacterDetailChoiceService
from services.character_note_service import CharacterNoteService
from services.character_view_service import CharacterViewService
from services.confirmed_activity_rule_monitor import (
    ConfirmedActivityRuleMonitor,
)
from services.confirmed_activity_rule_service import (
    ConfirmedActivityRuleService,
)
from services.event_bus import EventBus
from services.event_subscription_scope import EventSubscriptionScope
from services.farm_timer_monitor import FarmTimerMonitor
from services.farm_timer_service import (
    FARM_COMPLETED_EVENT,
    FARM_PLANTING_CONFIRMED_EVENT,
    FarmCompleted,
    FarmPlantingConfirmed,
    FarmTimerService,
)
from services.group_selection_service import (
    GroupSelectionService,
    PlayerGroupChoice,
    PlayerGroupMember,
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
    GroupLaunchTarget,
)
from services.group_role_status_monitor import GroupRoleStatusMonitor
from services.group_role_status_service import (
    GROUP_ROLE_STATUS_CHANGED_EVENT,
    GroupRoleStatusChange,
    GroupRoleStatusService,
    ROLE_STATUS_CHECK_DISABLED,
    ROLE_STATUS_CLOSED,
    ROLE_STATUS_DISCONNECTED,
    ROLE_STATUS_FAILED,
    ROLE_STATUS_OPEN,
    ROLE_STATUS_RECONNECTING,
)
from services.group_window_launch_service import (
    GroupWindowLaunchResult,
    GroupWindowLaunchService,
)
from services.ungrouped_window_service import UngroupedWindowService
from services.managed_game_process_service import (
    ManagedGameProcessService,
)
from services.window_size_adjustment_service import (
    WindowSizeAdjustmentResult,
    WindowSizeAdjustmentService,
)
from services.game_time_timed_click_service import (
    DEFAULT_TIMED_CLICK_INTERVAL_MS,
    DEFAULT_TIMED_CLICK_LEAD_MS,
    DEFAULT_TIMED_CLICK_REPEAT_COUNT,
    DEFAULT_TIMED_CLICK_TARGET_TIME,
    GameTimeTimedClickResult,
    GameTimeTimedClickService,
    clamp_time_offset_ms,
)
from services.server_clock import ServerClock, ServerTimeSourceIdentity
from services.server_time_bridge import (
    ProcessMemoryServerTimeReader,
    ServerTimeBridge,
    ServerTimeBridgeServer,
)
from services.keyboard_sync_monitor import (
    KeyboardSyncMonitor,
    Win32KeyboardStateBackend,
)
from services.game_operation_gate import GameOperationGate
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
from services.smart_reconnect_monitor import (
    DEFAULT_SMART_RECONNECT_INTERVAL_MS,
    SmartReconnectMonitor,
    SMART_RECONNECT_MODE_BALANCED,
    normalize_smart_reconnect_mode,
    normalize_smart_reconnect_interval_ms,
)
from services.smart_reconnect_capture_settings_service import (
    SMART_RECONNECT_CAPTURE_MODES_KEY,
    SmartReconnectCaptureSettings,
    SmartReconnectCaptureSettingsService,
)
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
    ResolvedTargetWindows,
    TargetWindowContractService,
)
from services.true_event_card_service import TrueEventCardService
from services.ui_callback_dispatcher import UiCallbackDispatcher
from services.ui_font_service import (
    DEFAULT_CONTENT_FONT_SIZE,
    DEFAULT_SIDEBAR_FONT_SIZE,
    DEFAULT_UI_FONT_ID,
    UIFontService,
    normalize_content_font_size,
    normalize_sidebar_font_size,
    normalize_ui_font_id,
    resolve_ui_font_preferences,
)
from ui.home import HomeView
from ui.home import (
    FeatureCardSettingsSaveResult,
    GroupManagementViewResult,
    SmartReconnectToggleViewResult,
    SyncToggleViewResult,
    UI_THEME_LABELS,
)
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
BACKGROUND_IMAGE_VERIFY_ARGUMENT_PREFIX = "--verify-background-image="
MAIN_INSTANCE_MUTEX_NAME = r"Local\Limaple.Fu.MainInterface"
WINDOWS_ERROR_ALREADY_EXISTS = 183
TARGET_WINDOW_KEY = "target_window_keywords"
TARGET_WINDOW_FINGERPRINT_KEY = "target_window_fingerprint"
INPUT_POLICY_KEY = "input_policy"
SMART_RECONNECT_ENABLED_KEY = "smart_reconnect_enabled"
SMART_RECONNECT_CONSENT_KEY = "smart_reconnect_consent_v1"
SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY = "smart_reconnect_auto_battle_enabled"
SMART_RECONNECT_INTERVAL_MS_KEY = "disconnect_detect_interval_ms"
SMART_RECONNECT_MODE_KEY = "smart_reconnect_mode"
SMART_RECONNECT_STATUS_COLORS_KEY = "smart_reconnect_status_colors"
SMART_RECONNECT_INTERVAL_MIGRATION_KEY = (
    "disconnect_detect_interval_default_v2"
)
UI_THEME_KEY = "ui_theme"
SHOW_HINTS_KEY = "show_hints"
UI_FONT_ID_KEY = "ui_font_id"
UI_SIDEBAR_FONT_SIZE_KEY = "ui_sidebar_font_size"
UI_CONTENT_FONT_SIZE_KEY = "ui_content_font_size"
UI_THEME_CLASSIC_GOLD_MIGRATION_KEY = "ui_theme_classic_gold_migration_v1"
CURRENT_GROUP_NAME_KEY = "current_group_name"
REGISTRY_FILENAME = "window_registry.json"
RECONNECT_STATE_FILENAME = "smart_reconnect_state.json"
GROUP_CONFIGURATION_FILENAME = "group_configuration.json"
OPERATION_RECORD_FILENAME = "operation_records.json"
MANAGED_GAME_PROCESS_FILENAME = "managed_game_processes.json"
OPERATION_RECORD_ARCHIVE_DIRNAME = "角色每日紀錄"
DEFERRED_SYNC_STATE_FILENAME = "deferred_sync_operations.json"
TARGET_DESKTOP_REPORT_FILENAME = "target_desktop_verification.json"
BACKGROUND_IMAGE_VERIFY_REPORT_FILENAME = "background_image_verification.json"
CHARACTER_FILENAME = "characters.json"
CHARACTER_GAME_DATA_FILENAME = "character_game_data.json"
ACTIVITY_PROGRESS_FILENAME = "activity_progress.json"
CARD_HISTORY_FILENAME = "card_history.json"
CARD_PREVIEW_SELECTION_FILENAME = "card_preview_selection.json"
ACTIVITY_REMINDER_STATE_FILENAME = "activity_reminder_state.json"
TRUE_EVENT_CARD_STATE_FILENAME = "true_event_card_state.json"
FARM_TIMER_STATE_FILENAME = "farm_timers.json"
CONFIRMED_ACTIVITY_RULE_STATE_FILENAME = "confirmed_activity_rules.json"
ACTIVITY_ORDER_HABIT_FILENAME = "activity_order_habit.json"
PLAYER_HABIT_FILENAME = "player_habits.json"
SYNC_SELECTED_KEYS_KEY = "sync_selected_keys"
SYNC_KEYS_COLLAPSED_KEY = "sync_keys_collapsed"
GROUP_ROLE_DETAILS_EXPANDED_KEY = "group_role_details_expanded"
FEATURE_HOTKEYS_KEY = "feature_hotkeys"
GAME_TIME_OFFSET_MS_KEY = "game_time_offset_ms"
GAME_TIME_AUTO_UPDATE_KEY = "game_time_auto_update"
TIMED_CLICK_SETTINGS_KEY = "timed_click_settings"
APP_ICON_PNG = Path("assets") / "flash_icon.png"
APP_ICON_ICO = Path("assets") / "flash_icon.ico"
RECONNECT_REFERENCE_DIR = Path("assets") / "reconnect_reference"
OBSIDIAN_REFERENCE_DIR = Path("assets") / "game_data_reference" / "obsidian"
UI_FONT_ASSET_DIR = Path("assets") / "ui_fonts"
BACKGROUND_IMAGE_FILETYPES = (
    (
        "圖片與相機 RAW",
        (
            "*.png *.jpg *.jpeg *.jpe *.jfif *.gif *.bmp *.dib "
            "*.tif *.tiff *.webp *.ico *.heic *.heif *.avif "
            "*.cr2 *.cr3 *.dng *.nef *.nrw *.arw *.srf *.sr2 "
            "*.orf *.rw2 *.raf *.pef *.raw *.3fr *.erf *.mef "
            "*.mos *.mrw *.srw *.x3f *.bay *.dcr *.fff *.iiq "
            "*.k25 *.kdc *.rwl"
        ),
    ),
    ("所有檔案", "*.*"),
)


def _service_running_state(service: object) -> bool:
    for name in ("running", "enabled", "started"):
        value = getattr(service, name, None)
        if type(value) is bool:
            return value
    return False


def stop_input_sync_pair(
    keyboard_monitor: object,
    mouse_monitor: object,
) -> bool:
    """Attempt cleanup of both monitors and report their actual final state."""
    services = (keyboard_monitor, mouse_monitor)
    results = tuple(stop_service(service) for service in services)
    return all(result.success for result in results) and not any(
        _service_running_state(service) for service in services
    )


def resource_path(relative_path: Path) -> Path:
    """Resolve files both from source and from a PyInstaller bundle."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / relative_path
    return Path(__file__).resolve().parent / relative_path


class MainInstanceLock:
    """Retain one cross-process lock until complete application cleanup."""

    def __init__(self, release_callback: Callable[[], object]) -> None:
        self._release_callback = release_callback
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._release_callback()


def acquire_main_instance_lock(
    name: str = MAIN_INSTANCE_MUTEX_NAME,
) -> MainInstanceLock | None:
    """Return None when another ordinary UI process already owns the lock."""
    if sys.platform != "win32":
        return MainInstanceLock(lambda: None)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_mutex = kernel32.CreateMutexW
    create_mutex.argtypes = (
        ctypes.c_void_p,
        ctypes.c_bool,
        ctypes.c_wchar_p,
    )
    create_mutex.restype = ctypes.c_void_p
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (ctypes.c_void_p,)
    close_handle.restype = ctypes.c_bool
    ctypes.set_last_error(0)
    handle = create_mutex(None, False, name)
    if not handle:
        raise OSError(ctypes.get_last_error(), "無法建立主介面執行鎖。")
    if ctypes.get_last_error() == WINDOWS_ERROR_ALREADY_EXISTS:
        close_handle(handle)
        return None
    return MainInstanceLock(lambda: close_handle(handle))


def close_startup_splash() -> None:
    """Close the packaged startup splash safely and idempotently."""
    try:
        import pyi_splash  # type: ignore[import-not-found]
    except ImportError:
        return
    try:
        is_alive = getattr(pyi_splash, "is_alive", None)
        if callable(is_alive) and not is_alive():
            return
        close = getattr(pyi_splash, "close", None)
        if callable(close):
            close()
    except Exception:
        pass


def registered_game_data_target(
    current_group_name: str | None,
    selection: PlayerGroupChoice | None,
    member: PlayerGroupMember | None,
    window: WindowInfo | None,
    screen_state: ReconnectScreenState,
) -> RegisteredGameDataWindow | None:
    """只將目前組別內唯一、完整且已登入的角色交給唯讀資料擷取。"""
    if (
        not isinstance(current_group_name, str)
        or not current_group_name.strip()
        or not isinstance(selection, PlayerGroupChoice)
        or selection.name != current_group_name
        or not isinstance(member, PlayerGroupMember)
        or not isinstance(window, WindowInfo)
        or screen_state is not ReconnectScreenState.CONNECTED
    ):
        return None
    character_id = member.character_id.strip() if isinstance(member.character_id, str) else ""
    role_id = member.role_id.strip() if isinstance(member.role_id, str) else ""
    fingerprint = normalize_launch_fingerprint(window.launch_fingerprint)
    if (
        not character_id
        or not role_id
        or sum(item.entry_id == member.entry_id for item in selection.members) != 1
        or sum(
            isinstance(item.character_id, str)
            and item.character_id.strip() == character_id
            for item in selection.members
        ) != 1
        or sum(
            isinstance(item.role_id, str)
            and item.role_id.strip().casefold() == role_id.casefold()
            for item in selection.members
        ) != 1
        or isinstance(window.handle, bool)
        or window.handle <= 0
        or isinstance(window.process_id, bool)
        or not isinstance(window.process_id, int)
        or window.process_id <= 0
        or not isinstance(window.rect, tuple)
        or len(window.rect) != 4
        or not all(isinstance(value, int) and not isinstance(value, bool) for value in window.rect)
        or window.rect[2] <= window.rect[0]
        or window.rect[3] <= window.rect[1]
        or window.visible is not True
        or window.minimized is not False
        or isinstance(window.thread_id, bool)
        or not isinstance(window.thread_id, int)
        or window.thread_id <= 0
        or not isinstance(window.window_class, str)
        or not window.window_class.strip()
        or isinstance(window.process_lifecycle_token, bool)
        or not isinstance(window.process_lifecycle_token, int)
        or window.process_lifecycle_token <= 0
        or fingerprint is None
    ):
        return None
    return RegisteredGameDataWindow(
        window_handle=window.handle,
        character_id=character_id,
        launch_fingerprint=fingerprint,
        process_id=window.process_id,
        rect=window.rect,
        thread_id=window.thread_id,
        window_class=window.window_class,
        process_lifecycle_token=window.process_lifecycle_token,
    )


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


def apply_smart_reconnect_snapshot_transition(
    enabled: bool,
    controller: object,
    monitor: object,
    *,
    start_monitor: Callable[[object], object],
    stop_monitor: Callable[[object], object],
) -> SmartReconnectToggleViewResult:
    """Bind one run to the safe windows open at the enable click."""
    prepare_snapshot = getattr(
        controller,
        "prepare_execution_snapshot",
        None,
    )
    set_execution_enabled = getattr(
        controller,
        "set_execution_enabled",
        None,
    )
    if not callable(prepare_snapshot) or not callable(set_execution_enabled):
        return SmartReconnectToggleViewResult(False, False, "智慧重連控制器尚未正確設定，沒有啟用。")
    if enabled:
        prepared = prepare_snapshot()
        if not bool(getattr(prepared, "success", False)):
            set_execution_enabled(False)
            message = getattr(prepared, "message", None)
            return SmartReconnectToggleViewResult(
                False,
                False,
                message.strip()
                if isinstance(message, str) and message.strip()
                else "目前遊戲視窗無法通過安全檢查，沒有啟用智慧重連。",
            )
        started = start_monitor(monitor)
        if not bool(getattr(started, "success", False)):
            set_execution_enabled(False)
            return SmartReconnectToggleViewResult(False, False, "智慧重連監看服務未能啟動，沒有啟用。")
        return SmartReconnectToggleViewResult(True, True, "智慧重連已開啟，正在安全監看。")
    stopped = stop_monitor(monitor)
    set_execution_enabled(False)
    if bool(getattr(stopped, "success", False)):
        return SmartReconnectToggleViewResult(True, False, "智慧重連已安全停止。")
    return SmartReconnectToggleViewResult(False, False, "智慧重連監看服務未能停止，未變更設定。")


def apply_smart_reconnect_auto_battle_setting(
    enabled: bool,
    controller: object,
    config: object,
) -> bool:
    """保存子開關；智慧重連總開關仍是另一道必要執行閘。"""
    setter = getattr(controller, "set_auto_battle_enabled", None)
    save = getattr(config, "set", None)
    if not callable(setter) or not callable(save):
        return False
    normalized = enabled is True
    setter(normalized)
    save(SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY, normalized)
    return True


def normalize_smart_reconnect_auto_battle_enabled(value: object) -> bool:
    """Keep an explicit player-off choice; missing or invalid values default on."""

    return value if isinstance(value, bool) else True


def resolve_registered_reconnect_roles(
    characters: object,
    registry_records: object,
    groups: object,
) -> tuple[RegisteredReconnectRole, ...]:
    """Cross-check stable character and registry identities for reconnect."""

    character_items = tuple(characters) if isinstance(characters, (list, tuple)) else ()
    registry_items = (
        tuple(registry_records)
        if isinstance(registry_records, (list, tuple))
        else ()
    )
    group_items = tuple(groups) if isinstance(groups, (list, tuple)) else ()
    by_id = {
        item.character_id: item
        for item in character_items
        if isinstance(getattr(item, "character_id", None), str)
    }
    registry_by_id: dict[str, list[object]] = {}
    for item in registry_items:
        character_id = getattr(item, "character_id", None)
        if isinstance(character_id, str) and character_id.strip():
            registry_by_id.setdefault(character_id.strip(), []).append(item)

    values: dict[str, set[CharacterImportance]] = {}

    def remember(role_id: object, importance: object) -> None:
        if not isinstance(role_id, str) or not isinstance(
            importance,
            CharacterImportance,
        ):
            return
        normalized = role_id.strip()
        if not normalized:
            return
        values.setdefault(normalized.casefold(), set()).add(importance)

    for character_id, character in by_id.items():
        if character.importance is not CharacterImportance.PRIMARY:
            continue
        matches = registry_by_id.get(character_id, [])
        if len(matches) != 1:
            continue
        record = matches[0]
        if (
            getattr(record, "display_name", None)
            != character.display_name
            or getattr(record, "role", None)
            != CharacterImportance.PRIMARY.value
        ):
            continue
        remember(character.display_name, CharacterImportance.PRIMARY)

    for group in group_items:
        for entry in tuple(getattr(group, "entries", ())):
            character = by_id.get(getattr(entry, "entry_id", None))
            if character is None:
                continue
            remember(getattr(entry, "role_id", None), character.importance)

    return tuple(
        RegisteredReconnectRole(role_id, next(iter(importances)))
        for role_id, importances in sorted(values.items())
        if len(importances) == 1
    )


def build_configured_reconnect_plan(
    scope: object,
    groups: object,
    characters: object,
    choices: object,
) -> GroupLaunchPlan | None:
    """Keep every detected entry while granting recovery per identity."""

    if not bool(getattr(scope, "ready", False)):
        return None
    group_items = tuple(groups) if isinstance(groups, (list, tuple)) else ()
    character_items = (
        tuple(characters) if isinstance(characters, (list, tuple)) else ()
    )
    choice_items = tuple(choices) if isinstance(choices, (list, tuple)) else ()
    entries = {}
    shortcut_paths = {}
    role_ids = {}
    for group in group_items:
        for entry in tuple(getattr(group, "entries", ())):
            shortcut_path = str(
                entry.shortcut_path.resolve(strict=False)
            ).casefold()
            if (
                entry.entry_id in shortcut_paths
                and shortcut_paths[entry.entry_id] != shortcut_path
            ):
                return None
            entries.setdefault(entry.entry_id, entry)
            shortcut_paths[entry.entry_id] = shortcut_path
            role_id = entry.role_id.strip()
            if role_id:
                role_ids.setdefault(entry.entry_id, set()).add(role_id)
    entry_ids = tuple(getattr(scope, "entry_ids", ()))
    entry_fingerprints = tuple(
        getattr(scope, "entry_fingerprints", ())
    )
    if tuple(entries) != entry_ids:
        return None

    profiles = {
        character.character_id: character
        for character in character_items
    }
    profile_ids = {}
    for choice in choice_items:
        for member in tuple(getattr(choice, "members", ())):
            if member.character_id:
                profile_ids.setdefault(member.entry_id, set()).add(
                    member.character_id
                )
    targets = []
    for order, (entry_id, fingerprint) in enumerate(
        zip(entry_ids, entry_fingerprints),
        start=1,
    ):
        if fingerprint is None:
            return None
        character_ids = profile_ids.get(entry_id, set())
        entry_role_ids = role_ids.get(entry_id, set())
        recovery_role_id = (
            next(iter(entry_role_ids))
            if len(entry_role_ids) == 1
            and len(character_ids) <= 1
            else ""
        )
        profile = (
            profiles.get(next(iter(character_ids), entry_id))
            if recovery_role_id
            else None
        )
        entry = entries[entry_id]
        targets.append(
            GroupLaunchTarget(
                order,
                entry.display_name,
                entry.shortcut_path,
                fingerprint,
                entry.placement,
                entry.entry_id,
                recovery_role_id,
                profile.level if profile is not None else None,
                profile.importance if profile is not None else None,
            )
        )
    return GroupLaunchPlan("configured", tuple(targets))


def apply_auto_battle_after_game_launch(
    game_was_launched: bool,
    controller: object,
    config: object,
    home_view: object | None = None,
) -> bool:
    """Enable, persist, and reflect auto battle after one real app launch."""

    if game_was_launched is not True:
        return False
    if not apply_smart_reconnect_auto_battle_setting(
        True,
        controller,
        config,
    ):
        return False
    update_view = getattr(
        home_view,
        "set_smart_reconnect_auto_battle_enabled",
        None,
    )
    if callable(update_view):
        update_view(True)
    return True


def group_window_launch_started_game(result: object) -> bool:
    """Accept only a fully successful group operation that launched a game."""

    return bool(
        getattr(result, "success", None) is True
        and getattr(result, "action", None) == "launch"
        and getattr(result, "launched_count", 0) > 0
    )


def group_role_action_started_game(result: object) -> bool:
    """Accept only a successful single-role launch, never an activation."""

    return bool(
        getattr(result, "success", None) is True
        and getattr(result, "action", None) == "launched"
    )


def resolve_group_role_progress_subject_id(
    change: object,
    *,
    group_launch_service: GroupLaunchService,
    group_selection_service: GroupSelectionService,
) -> str | None:
    """Return one stable character subject for one verified group role."""
    if not isinstance(change, GroupRoleStatusChange):
        return None
    group_name = change.group_name.strip()
    fingerprint = normalize_launch_fingerprint(change.current.action_id)
    if not group_name or fingerprint is None:
        return None
    try:
        plan = group_launch_service.plan(group_name)
        choices = group_selection_service.choices()
    except (OSError, TypeError, ValueError):
        return None
    if not isinstance(plan, GroupLaunchPlan) or not plan.ready:
        return None
    target = plan.target_for_fingerprint(fingerprint)
    if target is None or not target.entry_id:
        return None
    if sum(
        item.entry_id == target.entry_id
        for item in plan.targets
    ) != 1:
        return None
    matching_choices = tuple(
        choice
        for choice in choices
        if isinstance(choice.name, str) and choice.name.strip() == group_name
    )
    if len(matching_choices) != 1:
        return None
    matching_members = tuple(
        member
        for member in matching_choices[0].members
        if (
            member.entry_id == target.entry_id
            and isinstance(member.character_id, str)
            and member.character_id.strip()
        )
    )
    if len(matching_members) != 1:
        return None
    return matching_members[0].character_id.strip()


def route_group_role_status_to_activity_progress(
    change: object,
    *,
    activity_progress_service: ActivityProgressService | None,
    subject_id_resolver: Callable[[GroupRoleStatusChange], str | None],
    occurred_at: datetime,
) -> tuple[object, ...]:
    """Apply only verified game-role transitions to matching activity progress."""
    if (
        not isinstance(change, GroupRoleStatusChange)
        or not isinstance(activity_progress_service, ActivityProgressService)
        or occurred_at.tzinfo is None
        or occurred_at.utcoffset() is None
    ):
        return ()
    status = change.current.status
    if status not in {
        ROLE_STATUS_DISCONNECTED,
        ROLE_STATUS_RECONNECTING,
        ROLE_STATUS_CLOSED,
        ROLE_STATUS_OPEN,
    }:
        return ()
    try:
        subject_id = subject_id_resolver(change)
    except (KeyError, TypeError, ValueError):
        return ()
    if not isinstance(subject_id, str) or not subject_id.strip():
        return ()
    if status in {ROLE_STATUS_DISCONNECTED, ROLE_STATUS_RECONNECTING}:
        return activity_progress_service.record_interruption(
            subject_id,
            ActivityInterruptionReason.DISCONNECTED,
            occurred_at,
        )
    if status == ROLE_STATUS_CLOSED:
        return activity_progress_service.record_interruption(
            subject_id,
            ActivityInterruptionReason.GAME_CLOSED,
            occurred_at,
        )
    return activity_progress_service.clear_interruption(
        subject_id,
        occurred_at,
    )


def route_group_role_status_to_farm_timer(
    change: object,
    *,
    farm_timer_service: FarmTimerService | None,
    subject_id_resolver: Callable[[GroupRoleStatusChange], str | None],
    occurred_at: datetime,
) -> bool:
    """只以既有可靠身分將遊戲關閉狀態交給農場計時。"""
    if (
        not isinstance(change, GroupRoleStatusChange)
        or not isinstance(farm_timer_service, FarmTimerService)
        or occurred_at.tzinfo is None
        or occurred_at.utcoffset() is None
    ):
        return False
    try:
        subject_id = subject_id_resolver(change)
    except (KeyError, TypeError, ValueError):
        return False
    if not isinstance(subject_id, str) or not subject_id.strip():
        return False
    return farm_timer_service.handle_role_status(
        subject_id,
        change.current.status,
        occurred_at,
    )


def route_group_role_status_to_confirmed_activity_rules(
    change: object,
    *,
    confirmed_activity_rule_service: ConfirmedActivityRuleService | None,
    subject_id_resolver: Callable[[GroupRoleStatusChange], str | None],
    occurred_at: datetime,
) -> tuple[object, ...]:
    """僅以可靠角色身分更新已定案活動的暫停與登入提示。"""
    if (
        not isinstance(change, GroupRoleStatusChange)
        or not isinstance(
            confirmed_activity_rule_service,
            ConfirmedActivityRuleService,
        )
        or occurred_at.tzinfo is None
        or occurred_at.utcoffset() is None
    ):
        return ()
    try:
        subject_id = subject_id_resolver(change)
    except (KeyError, TypeError, ValueError):
        return ()
    if not isinstance(subject_id, str) or not subject_id.strip():
        return ()
    return confirmed_activity_rule_service.handle_role_status(
        subject_id,
        change.current.status,
        occurred_at,
    )


def refresh_confirmed_activity_group_scope(
    *,
    workspace_service: WorkspaceService | None,
    confirmed_activity_rule_service: ConfirmedActivityRuleService | None,
    logger: LoggerService | None,
) -> bool:
    """只以目前工作區群組更新已定案活動的主動提醒範圍。"""
    if not isinstance(workspace_service, WorkspaceService) or not isinstance(
        confirmed_activity_rule_service,
        ConfirmedActivityRuleService,
    ):
        return False
    current_group = workspace_service.snapshot().current_group
    try:
        saved = (
            confirmed_activity_rule_service.register_group(current_group)
            if current_group is not None
            else confirmed_activity_rule_service.clear_current_group()
        )
    except Exception as error:
        if logger is not None:
            logger.error(
                "Confirmed activity group registration was isolated: "
                f"{error}"
            )
        return False
    if not saved and logger is not None:
        logger.error("Confirmed activity group state could not be saved.")
    return saved


def handle_group_role_status_change(
    change: object,
    *,
    activity_progress_service: ActivityProgressService | None,
    subject_id_resolver: Callable[[GroupRoleStatusChange], str | None],
    occurred_at: datetime,
    logger: LoggerService | None,
    on_role_status_card: Callable[[GroupRoleStatusChange], object] | None,
    on_farm_timer_status: Callable[[GroupRoleStatusChange], object] | None = None,
    on_confirmed_activity_status: (
        Callable[[GroupRoleStatusChange], object] | None
    ) = None,
) -> tuple[object, ...]:
    """Keep the existing role-status card independent from progress persistence."""
    if not isinstance(change, GroupRoleStatusChange):
        return ()
    try:
        progress_changes = route_group_role_status_to_activity_progress(
            change,
            activity_progress_service=activity_progress_service,
            subject_id_resolver=subject_id_resolver,
            occurred_at=occurred_at,
        )
    except Exception as error:
        progress_changes = ()
        if logger is not None:
            try:
                logger.error(
                    "Activity progress interruption routing failed and was "
                    f"isolated: {error}"
                )
            except Exception:
                pass
    for label, callback in (
        ("Farm timer status routing", on_farm_timer_status),
        ("Confirmed activity status routing", on_confirmed_activity_status),
    ):
        if callback is None:
            continue
        try:
            callback(change)
        except Exception as error:
            if logger is not None:
                try:
                    logger.error(f"{label} failed and was isolated: {error}")
                except Exception:
                    pass
    if on_role_status_card is not None:
        on_role_status_card(change)
    return progress_changes


def _connected_sync_fingerprints(
    scoped_fingerprints: tuple[str, ...],
    windows: tuple[WindowInfo, ...],
) -> tuple[str, ...]:
    """依組別固定順序保留目前唯一且已連線的安全身分。"""

    normalized_values = tuple(
        normalize_launch_fingerprint(value)
        for value in scoped_fingerprints
    )
    if (
        not normalized_values
        or any(value is None for value in normalized_values)
        or len(normalized_values) != len(set(normalized_values))
    ):
        return ()
    normalized_scope = tuple(
        value for value in normalized_values if value is not None
    )
    observed: dict[str, int] = {}
    for window in windows:
        fingerprint = normalize_launch_fingerprint(
            getattr(window, "launch_fingerprint", None)
        )
        if fingerprint in normalized_scope:
            observed[fingerprint] = observed.get(fingerprint, 0) + 1
    connected = tuple(
        fingerprint
        for fingerprint in normalized_scope
        if observed.get(fingerprint) == 1
    )
    if not connected or connected[0] != normalized_scope[0]:
        return ()
    return connected


def resolve_complete_sync_instance_windows(
    scoped_entry_ids: tuple[str, ...],
    resolved_entry_ids: tuple[str, ...],
    resolved_windows: tuple[WindowInfo, ...],
    *,
    controller_entry_id: str | None,
) -> tuple[WindowInfo, ...]:
    """Order only complete, one-entry-per-instance sync targets.

    Launcher digests can be shared by many Flash windows.  The target contract
    supplies instance-local digests only after matching each group entry to its
    player-confirmed full window instance; this helper rejects any collapse or
    replacement before the sync controllers receive the collection.
    """

    if (
        not isinstance(controller_entry_id, str)
        or not controller_entry_id.strip()
        or not scoped_entry_ids
        or scoped_entry_ids[0] != controller_entry_id
        or len(resolved_entry_ids) != len(resolved_windows)
        or len(set(scoped_entry_ids)) != len(scoped_entry_ids)
        or len(set(resolved_entry_ids)) != len(resolved_entry_ids)
        or not set(resolved_entry_ids).issubset(scoped_entry_ids)
    ):
        return ()
    by_entry: dict[str, WindowInfo] = {}
    instance_names: set[str] = set()
    instance_keys: set[tuple[object, ...]] = set()
    for entry_id, window in zip(resolved_entry_ids, resolved_windows):
        if not isinstance(entry_id, str) or not entry_id.strip():
            return ()
        identity = complete_window_instance_identity(window)
        if identity is None:
            return ()
        instance_name = identity[0]
        stable_instance = identity[1:6]
        if (
            entry_id in by_entry
            or instance_name in instance_names
            or stable_instance in instance_keys
        ):
            return ()
        by_entry[entry_id] = window
        instance_names.add(instance_name)
        instance_keys.add(stable_instance)
    if controller_entry_id not in by_entry:
        return ()
    return tuple(
        by_entry[entry_id]
        for entry_id in scoped_entry_ids
        if entry_id in by_entry
    )


def resolve_connected_sync_target_contract(
    target_service: TargetWindowContractService | None,
    group_name: object,
) -> tuple[ResolvedTargetWindows, tuple[WindowInfo, ...]]:
    """Resolve one immutable, identity-safe target contract subset.

    Basic keyboard and pointer synchronization intentionally depends only on
    the target-window identity contract.  Screen recognition belongs to smart
    reconnect and must not turn a safely resolved live game window into an
    ineligible basic-input target.
    """

    if target_service is None:
        return ResolvedTargetWindows(()), ()
    resolved = target_service.reconnect_targets(group_name)
    safe_windows = resolve_complete_sync_instance_windows(
        resolved.sync_scope_entry_ids,
        resolved.sync_entry_ids,
        resolved.sync_windows,
        controller_entry_id=resolved.sync_controller_entry_id,
    )
    return resolved, safe_windows


def resolve_complete_reconnect_window_for_entry(
    group_name: str,
    entry_id: str,
    group_launch_service: GroupLaunchService,
    target_service: TargetWindowContractService,
) -> WindowInfo | None:
    """Return the real complete contract window for one unique group entry."""

    if not isinstance(group_name, str) or not group_name.strip():
        return None
    if not isinstance(entry_id, str) or not entry_id.strip():
        return None
    plan = group_launch_service.plan(group_name)
    if not plan.ready:
        return None
    targets = tuple(
        target for target in plan.targets if target.entry_id == entry_id
    )
    if len(targets) != 1:
        return None
    target = targets[0]
    resolved = target_service.reconnect_targets(group_name)
    if tuple(getattr(resolved, "global_failure_codes", ())):
        return None
    if tuple(getattr(resolved, "sync_scope_entry_ids", ())) != tuple(
        item.entry_id for item in plan.targets
    ):
        return None
    contract_windows = tuple(getattr(resolved, "windows", ()))
    contract_entries = tuple(getattr(resolved, "sync_entry_ids", ()))
    contract_sync_windows = tuple(
        getattr(resolved, "sync_windows", ())
    )
    if not (
        contract_entries
        and len(contract_entries)
        == len(contract_windows)
        == len(contract_sync_windows)
    ):
        return None
    matches = tuple(
        window
        for paired_entry_id, window, sync_window in zip(
            contract_entries,
            contract_windows,
            contract_sync_windows,
        )
        if (
            paired_entry_id == entry_id
            and normalize_launch_fingerprint(window.launch_fingerprint)
            == target.fingerprint
            and complete_window_instance_identity(window) is not None
            and complete_window_instance_identity(sync_window) is not None
            and WindowInstanceToken.from_window(window)
            == WindowInstanceToken.from_window(sync_window)
            and monitored_window_instance_fingerprint(window)
            == normalize_launch_fingerprint(sync_window.launch_fingerprint)
        )
    )
    return matches[0] if len(matches) == 1 else None


def auto_read_missing_role_id_once(
    group_name: str,
    entry_id: str,
    group_configuration_service: GroupConfigurationService,
    group_launch_service: GroupLaunchService,
    target_window_contract_service: TargetWindowContractService,
    role_id_template_service: RoleIdTemplateService,
    smart_reconnect_controller: WindowsSmartReconnectController,
    *,
    refresh: Callable[[], None] | None = None,
) -> bool:
    """Read one blank role ID twice from one complete in-game target window."""

    group = group_configuration_service.group(group_name)
    if group is None:
        return False
    entries = tuple(
        entry for entry in group.entries if entry.entry_id == entry_id
    )
    if len(entries) != 1 or entries[0].role_id.strip():
        return False
    entry = entries[0]
    window = resolve_complete_reconnect_window_for_entry(
        group_name,
        entry_id,
        group_launch_service,
        target_window_contract_service,
    )
    if window is None or not window.visible or window.minimized:
        return False
    fingerprint = normalize_launch_fingerprint(window.launch_fingerprint)
    instance = WindowInstanceToken.from_window(window)
    if fingerprint is None or instance is None:
        return False
    screen_state = smart_reconnect_controller.observe_screen_states(
        (fingerprint,),
        candidate_windows=(window,),
    ).get(fingerprint)
    if screen_state not in (
        ReconnectScreenState.CONNECTED,
        ReconnectScreenState.UNKNOWN,
    ):
        return False
    result = role_id_template_service.read_if_missing(
        window.handle,
        existing_role_id=entry.role_id,
    )
    if not result.success:
        return False
    # OCR is intentionally outside the target-contract resolver. Re-resolve
    # after it and require the same complete instance before a value read from
    # an old image can be written into a replacement entry/window.
    refreshed_window = resolve_complete_reconnect_window_for_entry(
        group_name,
        entry_id,
        group_launch_service,
        target_window_contract_service,
    )
    refreshed_fingerprint = (
        normalize_launch_fingerprint(refreshed_window.launch_fingerprint)
        if refreshed_window is not None
        else None
    )
    if (
        refreshed_window is None
        or not refreshed_window.visible
        or refreshed_window.minimized
        or refreshed_fingerprint != fingerprint
        or WindowInstanceToken.from_window(refreshed_window) != instance
    ):
        return False
    refreshed_state = smart_reconnect_controller.observe_screen_states(
        (fingerprint,),
        candidate_windows=(refreshed_window,),
    ).get(fingerprint)
    if refreshed_state not in (
        ReconnectScreenState.CONNECTED,
        ReconnectScreenState.UNKNOWN,
    ):
        return False
    latest_group = group_configuration_service.group(group_name)
    latest_entries = (
        tuple(
            item
            for item in latest_group.entries
            if item.entry_id == entry_id
        )
        if latest_group is not None
        else ()
    )
    if len(latest_entries) != 1 or latest_entries[0].role_id.strip():
        return False
    confirmed_result = role_id_template_service.read_if_missing(
        refreshed_window.handle,
        existing_role_id=latest_entries[0].role_id,
    )
    if not confirmed_result.success or confirmed_result.role_id != result.role_id:
        return False
    final_group = group_configuration_service.group(group_name)
    final_entries = (
        tuple(
            item
            for item in final_group.entries
            if item.entry_id == entry_id
        )
        if final_group is not None
        else ()
    )
    if len(final_entries) != 1 or final_entries[0].role_id.strip():
        return False
    if not group_configuration_service.set_role_id(
        group_name,
        entry_id,
        confirmed_result.role_id,
    ):
        return False
    if refresh is not None:
        refresh()
    return True


class ConnectedSyncTargetContractProvider:
    """Publish identity-safe basic-sync windows from one target contract."""

    def __init__(
        self,
        target_service: TargetWindowContractService | None,
        group_name_provider: Callable[[], object],
    ) -> None:
        self._target_service = target_service
        self._group_name_provider = group_name_provider

    def windows(self) -> tuple[WindowInfo, ...]:
        group_name = self._group_name_provider()
        _resolved, safe_windows = resolve_connected_sync_target_contract(
            self._target_service,
            group_name,
        )
        return safe_windows


def build_services(
    root: Path | None = None,
    card_preview_catalog: CardPreviewCatalog | None = None,
):
    """Create, load, and register the cumulative SP1+SP2 services."""
    AppContext.clear()
    paths = PathManager(root=root)
    logger = LoggerService(paths.log_file("flash.log"))
    config = ConfigManager(paths.config_file("settings.json"))
    ui_font_service = UIFontService(resource_path(UI_FONT_ASSET_DIR))
    background_image_service = BackgroundImageService(
        config,
        paths.data_dir(),
        error_logger=logger.error,
    )
    feature_card_layout_service = FeatureCardLayoutService(config)
    if config.get(UI_THEME_CLASSIC_GOLD_MIGRATION_KEY) is not True:
        config.update_values(
            {
                UI_THEME_KEY: "classic_gold",
                UI_THEME_CLASSIC_GOLD_MIGRATION_KEY: True,
            }
        )
    if config.get(SMART_RECONNECT_INTERVAL_MIGRATION_KEY) is not True:
        current_interval = config.get(SMART_RECONNECT_INTERVAL_MS_KEY)
        values = {SMART_RECONNECT_INTERVAL_MIGRATION_KEY: True}
        if (
            current_interval is None
            or str(current_interval).strip() == "1000"
        ):
            values[SMART_RECONNECT_INTERVAL_MS_KEY] = (
                DEFAULT_SMART_RECONNECT_INTERVAL_MS
            )
        config.update_values(values)
    config.ensure_defaults(
        {
            INPUT_POLICY_KEY: WindowInputPolicy.ALL.value,
            SMART_RECONNECT_ENABLED_KEY: False,
            SMART_RECONNECT_CONSENT_KEY: False,
            SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY: True,
            SMART_RECONNECT_INTERVAL_MS_KEY: (
                DEFAULT_SMART_RECONNECT_INTERVAL_MS
            ),
            SMART_RECONNECT_MODE_KEY: SMART_RECONNECT_MODE_BALANCED,
            SMART_RECONNECT_STATUS_COLORS_KEY: {
                "已開啟": "#26845B",
                "重連中": "#B36A18",
                "重連失敗": "#D64545",
            },
            SMART_RECONNECT_INTERVAL_MIGRATION_KEY: True,
            UI_THEME_KEY: "classic_gold",
            SHOW_HINTS_KEY: False,
            UI_FONT_ID_KEY: DEFAULT_UI_FONT_ID,
            UI_SIDEBAR_FONT_SIZE_KEY: DEFAULT_SIDEBAR_FONT_SIZE,
            UI_CONTENT_FONT_SIZE_KEY: DEFAULT_CONTENT_FONT_SIZE,
            SYNC_SELECTED_KEYS_KEY: ["ESC"],
            SYNC_KEYS_COLLAPSED_KEY: True,
            GROUP_ROLE_DETAILS_EXPANDED_KEY: {},
            FEATURE_HOTKEYS_KEY: {
                "sync": "XBUTTON1",
                "reconnect": "",
                "auto_click": "F1",
            },
            GAME_TIME_OFFSET_MS_KEY: 0,
            GAME_TIME_AUTO_UPDATE_KEY: True,
            TIMED_CLICK_SETTINGS_KEY: {
                "target_time": DEFAULT_TIMED_CLICK_TARGET_TIME,
                "lead_ms": DEFAULT_TIMED_CLICK_LEAD_MS,
                "repeat_count": DEFAULT_TIMED_CLICK_REPEAT_COUNT,
                "repeat_interval_ms": DEFAULT_TIMED_CLICK_INTERVAL_MS,
            },
        }
    )
    saved_auto_battle = normalize_smart_reconnect_auto_battle_enabled(
        config.get(SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY)
    )
    if config.get(SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY) is not saved_auto_battle:
        config.set(
            SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY,
            saved_auto_battle,
        )
    smart_reconnect_capture_settings_service = (
        SmartReconnectCaptureSettingsService(config)
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
    character_game_data_update_service = CharacterGameDataUpdateService(
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
    ungrouped_window_service = UngroupedWindowService(
        group_configuration_service,
        shortcut_fingerprint_resolver,
        synchronized_window_backend,
        screen_states_provider=(
            lambda fingerprints, windows: reconnect_controller.observe_screen_states(
                fingerprints,
                candidate_windows=windows,
            )
        ),
    )
    game_operation_gate = GameOperationGate()
    AppContext.register(GameOperationGate, game_operation_gate)
    target_window_contract_service = TargetWindowContractService(
        group_configuration_service,
        sync_scope_service,
        registry,
        synchronized_window_backend,
        ungrouped_window_service,
    )
    sync_calibration_backend = Win32SyncCalibrationBackend()
    role_id_template_service = RoleIdTemplateService()
    progress_store = ActivityProgressStore(
        paths.data_dir() / ACTIVITY_PROGRESS_FILENAME
    )
    progress_service = ActivityProgressService(progress_store, event_bus)
    activity_schedule_catalog = build_confirmed_activity_catalog()
    activity_description_service = ActivityDescriptionService(
        config,
        activity_schedule_catalog,
    )
    activity_schedule_view_service = ActivityScheduleViewService(
        activity_schedule_catalog,
        activity_description_service,
        progress_service,
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
    card_coordinator = CardCoordinator(
        card_service,
        card_history_service,
        decision_service,
    )
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
    true_event_card_service = TrueEventCardService(
        card_coordinator,
        workspace_service.snapshot,
        progress_service.definition,
        state_path=paths.data_dir() / TRUE_EVENT_CARD_STATE_FILENAME,
        record_callback=operation_record_store.append,
    )
    farm_timer_service = FarmTimerService(
        card_coordinator,
        state_path=paths.data_dir() / FARM_TIMER_STATE_FILENAME,
        record_callback=operation_record_store.append,
    )
    confirmed_activity_rule_service = ConfirmedActivityRuleService(
        card_coordinator,
        state_path=(
            paths.data_dir() / CONFIRMED_ACTIVITY_RULE_STATE_FILENAME
        ),
        event_bus=event_bus,
    )

    refresh_confirmed_activity_group_scope(
        workspace_service=workspace_service,
        confirmed_activity_rule_service=confirmed_activity_rule_service,
        logger=logger,
    )

    AppContext.register(PathManager, paths)
    AppContext.register(LoggerService, logger)
    AppContext.register(ConfigManager, config)
    AppContext.register(UIFontService, ui_font_service)
    AppContext.register(
        SmartReconnectCaptureSettingsService,
        smart_reconnect_capture_settings_service,
    )
    AppContext.register(BackgroundImageService, background_image_service)
    AppContext.register(
        FeatureCardLayoutService,
        feature_card_layout_service,
    )
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
    AppContext.register(
        ActivityDescriptionService,
        activity_description_service,
    )
    AppContext.register(ActivityScheduleViewService, activity_schedule_view_service)
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
    AppContext.register(TrueEventCardService, true_event_card_service)
    AppContext.register(FarmTimerService, farm_timer_service)
    AppContext.register(
        ConfirmedActivityRuleService,
        confirmed_activity_rule_service,
    )

    def role_name_for_fingerprint(fingerprint: str) -> str:
        state = workspace_service.snapshot()
        current_name = (
            state.current_group.name
            if state.current_group is not None
            else None
        )
        if current_name is not None:
            current_plan = group_launch_service.plan(current_name)
            current_target = (
                current_plan.target_for_fingerprint(fingerprint)
                if current_plan.ready
                else None
            )
            if current_target is not None:
                return current_target.display_name
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

    def current_group_name() -> str | None:
        state = workspace_service.snapshot()
        return (
            state.current_group.name
            if state.current_group is not None
            else None
        )

    def current_sync_target_windows() -> tuple[WindowInfo, ...]:
        return connected_sync_contract_provider.windows()

    AppContext.register(SyncOperationRecordStore, operation_record_store)

    def registered_reconnect_roles() -> tuple[RegisteredReconnectRole, ...]:
        return resolve_registered_reconnect_roles(
            character_store.load(),
            registry.all(),
            group_configuration_service.groups(),
        )

    def current_reconnect_target_windows() -> ResolvedTargetWindows:
        return target_window_contract_service.configured_reconnect_targets()

    reconnect_controller = WindowsSmartReconnectController.for_real_windows(
        reference_dir=resource_path(RECONNECT_REFERENCE_DIR),
        state_path=paths.data_dir() / RECONNECT_STATE_FILENAME,
        window_backend=synchronized_window_backend,
        capture_settings=(
            smart_reconnect_capture_settings_service.snapshot()
        ),
        require_expected_window_count=False,
        operation_gate=game_operation_gate,
        auto_battle_enabled=(
            normalize_smart_reconnect_auto_battle_enabled(
                config.get(SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY)
            )
        ),
        failure_status_service=reconnect_failure_status_service,
        failure_record_callback=lambda role_name, detail: (
            operation_record_store.append(
                "智慧重連",
                role_name,
                detail,
            )
        ),
        registered_role_provider=registered_reconnect_roles,
        target_windows_provider=current_reconnect_target_windows,
    )
    connected_sync_contract_provider = ConnectedSyncTargetContractProvider(
        target_window_contract_service,
        current_group_name,
    )

    def current_deferred_sync_screen_state(
        fingerprint: str,
    ) -> ReconnectScreenState:
        """Recognize screens only for the final deferred-delivery safety gate."""

        normalized = normalize_launch_fingerprint(fingerprint)
        group_name = current_group_name()
        if normalized is None or target_window_contract_service is None:
            return ReconnectScreenState.UNKNOWN
        try:
            resolved = target_window_contract_service.reconnect_targets(
                group_name
            )
            safe_windows = resolve_complete_sync_instance_windows(
                resolved.sync_scope_entry_ids,
                resolved.sync_entry_ids,
                resolved.sync_windows,
                controller_entry_id=resolved.sync_controller_entry_id,
            )
            allowed = {
                safe_fingerprint
                for window in safe_windows
                if (
                    safe_fingerprint := normalize_launch_fingerprint(
                        window.launch_fingerprint
                    )
                )
                is not None
            }
            if normalized not in allowed:
                return ReconnectScreenState.UNKNOWN
            observed = reconnect_controller.observe_window_instance_states(
                resolved.windows
            )
            return observed.get(normalized, ReconnectScreenState.UNKNOWN)
        except Exception:
            return ReconnectScreenState.UNKNOWN

    def registered_game_data_window(window_handle: int) -> RegisteredGameDataWindow | None:
        group_name = current_group_name()
        choice = group_selection_service.find(group_name)
        if choice is None or target_window_contract_service is None:
            return None
        candidates = target_window_contract_service.reconnect_targets(
            group_name
        ).windows
        matches = tuple(
            window
            for window in candidates
            if isinstance(window, WindowInfo) and window.handle == window_handle
        )
        if len(matches) != 1:
            return None
        window = matches[0]
        fingerprint = normalize_launch_fingerprint(window.launch_fingerprint)
        group = group_configuration_service.group(group_name)
        plan = group_launch_service.plan(group_name)
        if (
            fingerprint is None
            or group is None
            or not plan.ready
        ):
            return None
        members = tuple(
            member
            for member in choice.members
            if any(
                entry.entry_id == member.entry_id
                and (
                    target := next(
                        (
                            item
                            for item in plan.targets
                            if str(item.shortcut_path.resolve(strict=False)).casefold()
                            == str(entry.shortcut_path.resolve(strict=False)).casefold()
                        ),
                        None,
                    )
                ) is not None
                and normalize_launch_fingerprint(target.fingerprint) == fingerprint
                for entry in group.entries
            )
        )
        if len(members) != 1:
            return None
        state = reconnect_controller.observe_screen_states(
            (fingerprint,),
            candidate_windows=(window,),
        ).get(fingerprint, ReconnectScreenState.UNKNOWN)
        return registered_game_data_target(
            group_name,
            choice,
            members[0],
            window,
            state,
        )

    character_game_data_capture_service = None
    try:
        obsidian_recognizer = ObsidianPageRecognizer(
            reference_dir=resource_path(OBSIDIAN_REFERENCE_DIR),
        )
        if not obsidian_recognizer.ready:
            raise RuntimeError("黑曜石可靠參考圖不完整。")
        character_game_data_capture_service = CharacterGameDataCaptureService(
            Win32PrintWindowProvider(),
            obsidian_recognizer,
            character_game_data_update_service,
            registered_game_data_window,
        )
    except Exception as error:
        logger.warning(f"黑曜石唯讀資料擷取未啟用：{error}")
    else:
        AppContext.register(
            CharacterGameDataCaptureService,
            character_game_data_capture_service,
        )
    AppContext.register(UngroupedWindowService, ungrouped_window_service)
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
            operation_record_store.append_deferred(
                "同步衝突",
                role_name_for_fingerprint(record.target_id),
                (
                    f"已執行 {record.active_operation}；"
                    f"略過 {record.skipped_operation}"
                ),
            ),
        )
    )
    AppContext.register(
        WindowsInputSyncController,
        WindowsInputSyncController.for_real_windows(
            window_backend=synchronized_window_backend,
            target_windows_provider=current_sync_target_windows,
            operation_gate=game_operation_gate,
            require_expected_window_count=False,
            conflict_arbiter=sync_conflict_arbiter,
            deferred_service=deferred_sync_service,
            reconnecting_provider=(
                reconnect_controller.reconnecting_fingerprints
            ),
            role_operation_callback=lambda fingerprint, operation, outcome: (
                operation_record_store.append_deferred(
                    "同步操作",
                    role_name_for_fingerprint(fingerprint),
                    f"{operation}－{outcome}",
                )
            ),
            deferred_screen_state_provider=(
                current_deferred_sync_screen_state
            ),
        ),
    )
    AppContext.register(
        WindowsPointerSyncController,
        WindowsPointerSyncController.for_real_windows(
            window_backend=synchronized_window_backend,
            target_windows_provider=current_sync_target_windows,
            operation_gate=game_operation_gate,
            require_expected_window_count=False,
            conflict_arbiter=sync_conflict_arbiter,
            deferred_service=deferred_sync_service,
            reconnecting_provider=(
                reconnect_controller.reconnecting_fingerprints
            ),
            role_operation_callback=lambda fingerprint, operation, outcome: (
                operation_record_store.append_deferred(
                    "同步操作",
                    role_name_for_fingerprint(fingerprint),
                    f"{operation}－{outcome}",
                )
            ),
            deferred_screen_state_provider=(
                current_deferred_sync_screen_state
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
            record_callback=operation_record_store.append_deferred,
            target_snapshot_provider=lambda group_name: (
                target_window_contract_service.snapshot(
                    group_name,
                    expanded_sync_scope=False,
                )
            ),
            operation_gate=game_operation_gate,
            event_bus=event_bus,
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
        SmartReconnectMonitor(
            reconnect_controller,
            logger=logger,
            monitor_interval_ms=normalize_smart_reconnect_interval_ms(
                config.get(SMART_RECONNECT_INTERVAL_MS_KEY)
            ),
            monitor_mode=normalize_smart_reconnect_mode(
                config.get(SMART_RECONNECT_MODE_KEY),
            ),
        ),
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


def register_server_time_services(
    group_window_backend,
    registry: WindowRegistry,
    *,
    managed_process_service=None,
    group_configuration_service=None,
    group_launch_service=None,
    start_listener: bool = True,
    start_memory_reader: bool = True,
    listener_port: int = 37842,
) -> tuple[ServerClock, ServerTimeBridge]:
    """接上唯讀遊戲時間服務，不建立任何遊戲操作能力。"""

    def revalidated_source(identity: ServerTimeSourceIdentity) -> bool:
        """只接受可由既有組別與角色資料唯一重建的現行視窗。"""
        if (
            group_configuration_service is None
            or group_launch_service is None
            or managed_process_service is None
        ):
            return False
        records = getattr(managed_process_service, "_records", None)
        if not isinstance(records, dict):
            return False
        try:
            groups = tuple(group_configuration_service.groups())
        except Exception:
            return False
        candidates: dict[tuple[str, str], tuple[set[str], GroupLaunchTarget]] = {}
        for group in groups:
            try:
                plan = group_launch_service.plan(group.name)
            except Exception:
                return False
            if not plan.ready:
                continue
            target = plan.target_for_fingerprint(identity.fingerprint)
            if target is None or not target.entry_id:
                continue
            key = (target.entry_id, target.fingerprint)
            if key not in candidates:
                candidates[key] = (set(), target)
            candidates[key][0].add(group.name)
        if len(candidates) != 1:
            return False
        group_names, target = next(iter(candidates.values()))
        try:
            registered = registry.get(target.entry_id)
        except KeyError:
            return False
        if registered.display_name != target.display_name:
            return False
        managed_matches = tuple(
            record
            for record in records.values()
            if getattr(record, "launch_fingerprint", None)
            == identity.fingerprint
            and getattr(record, "group_name", None) in group_names
            and getattr(record, "role_name", None) == target.display_name
        )
        return len(managed_matches) == 1

    def valid_source(identity: ServerTimeSourceIdentity) -> bool:
        if group_window_backend is None:
            return False
        try:
            windows = tuple(group_window_backend.list_windows())
        except Exception:
            return False
        matches = tuple(
            window
            for window in windows
            if window.handle == identity.handle
            and window.process_id == identity.process_id
            and window.thread_id == identity.thread_id
            and window.process_lifecycle_token == identity.lifecycle
            and window.launch_fingerprint == identity.fingerprint
        )
        if len(matches) != 1:
            return False
        registry_match = any(
            record.confirmed
            and record.handle == identity.handle
            and record.process_id == identity.process_id
            for record in registry.all()
        )
        records = getattr(managed_process_service, "_records", None)
        if isinstance(records, dict) and registry_match:
            managed_matches = tuple(
                record
                for record in records.values()
                if getattr(record, "window_handle", None) == identity.handle
                and getattr(record, "process_id", None) == identity.process_id
                and getattr(record, "launch_fingerprint", None)
                == identity.fingerprint
            )
            if len(managed_matches) == 1:
                return True
        return revalidated_source(identity)

    def resolve_transport_source(process_id: int) -> ServerTimeSourceIdentity | None:
        if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
            return None
        if group_window_backend is None:
            return None
        try:
            windows = tuple(group_window_backend.list_windows())
        except Exception:
            return None
        identities: list[ServerTimeSourceIdentity] = []
        for window in windows:
            if getattr(window, "process_id", None) != process_id:
                continue
            try:
                identity = ServerTimeSourceIdentity(
                    handle=window.handle,
                    process_id=window.process_id,
                    thread_id=window.thread_id,
                    lifecycle=window.process_lifecycle_token,
                    fingerprint=window.launch_fingerprint,
                )
            except (AttributeError, TypeError, ValueError):
                continue
            if valid_source(identity):
                identities.append(identity)
        return identities[0] if len(identities) == 1 else None

    def current_server_time_windows() -> tuple[object, ...]:
        """每輪只交出當下可由正式身分鏈唯一確認的遊戲視窗。"""
        if group_window_backend is None:
            return ()
        try:
            windows = tuple(group_window_backend.list_windows())
        except Exception:
            return ()
        accepted: list[object] = []
        for window in windows:
            try:
                identity = ServerTimeSourceIdentity(
                    handle=window.handle,
                    process_id=window.process_id,
                    thread_id=window.thread_id,
                    lifecycle=window.process_lifecycle_token,
                    fingerprint=window.launch_fingerprint,
                )
            except (AttributeError, TypeError, ValueError):
                continue
            if valid_source(identity):
                accepted.append(window)
        return tuple(accepted)

    server_clock = ServerClock(source_validator=valid_source)
    server_time_bridge = ServerTimeBridge(
        server_clock,
        source_validator=valid_source,
        transport_identity_resolver=resolve_transport_source,
    )
    AppContext.register(ServerClock, server_clock)
    AppContext.register(ServerTimeBridge, server_time_bridge)
    bridge_server = ServerTimeBridgeServer(
        server_time_bridge,
        port=listener_port,
    )
    if start_listener:
        bridge_server.start()
    AppContext.register(ServerTimeBridgeServer, bridge_server)
    memory_reader = ProcessMemoryServerTimeReader(
        current_server_time_windows,
        server_time_bridge,
    )
    if start_memory_reader:
        memory_reader.start()
    AppContext.register(ProcessMemoryServerTimeReader, memory_reader)
    return server_clock, server_time_bridge


def shutdown_server_time_services() -> None:
    """停止唯讀時間橋接接收器，不觸碰任何遊戲視窗。"""
    memory_reader = AppContext.get(ProcessMemoryServerTimeReader)
    if memory_reader is not None:
        memory_reader.stop()
    bridge_server = AppContext.get(ServerTimeBridgeServer)
    if bridge_server is not None:
        bridge_server.stop()


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


def shutdown_ui_font_service(
    logger: LoggerService | None = None,
) -> bool:
    """Release every process-private UI font before the logger closes."""
    service = AppContext.get(UIFontService)
    if service is None:
        return True
    try:
        closed = service.close()
    except Exception:
        closed = False
    if not closed and logger is not None:
        try:
            logger.error("私有介面字體資源未能完整解除。")
        except Exception:
            pass
    return closed


def shutdown_event_subscriptions(
    logger: LoggerService | None = None,
) -> bool:
    """Detach long-lived event listeners before the service registry is released."""
    try:
        state_service = AppContext.get(TargetWindowStateService)
        if state_service is not None and not state_service.close():
            raise RuntimeError("Target-window listeners were not detached.")
        return True
    except Exception:
        if logger is not None:
            try:
                logger.error(
                    "Event subscription shutdown failed:\n"
                    f"{traceback.format_exc()}"
                )
            except Exception:
                pass
        return False


def shutdown_sync_controllers(
    logger: LoggerService | None = None,
) -> bool:
    """Stop delayed input queues and ensure no callback survives shutdown."""
    stopped = True
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
            stopped = False
    return stopped


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
    ui_font_service = AppContext.get(UIFontService)
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
    activity_description_service = AppContext.get(ActivityDescriptionService)
    activity_progress_service = AppContext.get(ActivityProgressService)
    event_bus = AppContext.get(EventBus)
    card_view_state_service = AppContext.get(CardViewStateService)
    card_service = AppContext.get(CardService)
    card_coordinator = AppContext.get(CardCoordinator)
    activity_reminder_service = AppContext.get(ActivityReminderService)
    true_event_card_service = AppContext.get(TrueEventCardService)
    farm_timer_service = AppContext.get(FarmTimerService)
    confirmed_activity_rule_service = AppContext.get(
        ConfirmedActivityRuleService
    )
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
    smart_reconnect_capture_settings_service = AppContext.get(
        SmartReconnectCaptureSettingsService
    )
    game_operation_gate = AppContext.get(GameOperationGate)
    reconnect_failure_status_service = AppContext.get(
        ReconnectFailureStatusService
    )
    group_role_status_service = AppContext.get(GroupRoleStatusService)
    operation_record_store = AppContext.get(SyncOperationRecordStore)
    registry = AppContext.get(WindowRegistry)
    registry_store = AppContext.get(WindowRegistryStore)
    ungrouped_window_service = AppContext.get(UngroupedWindowService)
    sync_session_state = {"enabled": False}
    deferred_sync_monitor = AppContext.get(DeferredSyncOperationMonitor)
    character_view_service = AppContext.get(CharacterViewService)
    character_detail_view_service = AppContext.get(CharacterDetailViewService)
    character_note_service = AppContext.get(CharacterNoteService)
    character_store = AppContext.get(CharacterStore)
    group_character_registration_service = AppContext.get(
        GroupCharacterRegistrationService
    )
    background_image_service = AppContext.get(BackgroundImageService)
    feature_card_layout_service = AppContext.get(
        FeatureCardLayoutService
    )
    player_habit_service = AppContext.get(PlayerHabitPreferenceService)
    player_habit_activity_observer = (
        PlayerHabitActivityObserver(
            player_habit_service,
            activity_name_provider=lambda activity_id: (
                activity_progress_service.definition(activity_id).name
            ),
        )
        if player_habit_service is not None
        and activity_progress_service is not None
        else None
    )
    home_view: HomeView | None = None
    tray_controller: SystemTrayController | None = None

    def current_workspace_group_name() -> str | None:
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

    def current_sync_target_windows() -> tuple[WindowInfo, ...]:
        """Return only registered targets currently confirmed as connected."""
        _resolved, connected = resolve_connected_sync_target_contract(
            target_window_contract_service,
            current_workspace_group_name(),
        )
        return connected

    def mark_tray_operations_running() -> None:
        if tray_controller is not None:
            tray_controller.mark_operations_running()

    ui_callback_dispatcher = UiCallbackDispatcher(
        window.after,
        window.after_cancel,
    )

    def dispatch_to_main_window(callback) -> object | None:
        return ui_callback_dispatcher.dispatch(callback)

    group_window_backend = (
        AppContext.get(Win32WindowBackend)
        if group_launch_service is not None
        else None
    )
    managed_process_service = (
        ManagedGameProcessService(
            paths.data_dir() / MANAGED_GAME_PROCESS_FILENAME,
            group_window_backend,
            record_callback=(
                operation_record_store.append
                if operation_record_store is not None
                else None
            ),
        )
        if group_window_backend is not None
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
            managed_process_service=managed_process_service,
        )
        if group_launch_service is not None
        else None
    )

    register_server_time_services(
        group_window_backend,
        registry,
        managed_process_service=managed_process_service,
        group_configuration_service=group_configuration_service,
        group_launch_service=group_launch_service,
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
            operation_gate=game_operation_gate,
            server_clock=AppContext.get(ServerClock),
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
        operation_gate=game_operation_gate,
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

    group_window_operation_lease = {"value": None}

    def release_group_window_operation(expected_lease=None) -> None:
        lease = group_window_operation_lease.get("value")
        if expected_lease is not None and lease is not expected_lease:
            return
        group_window_operation_lease["value"] = None
        if lease is not None:
            lease.release()

    def complete_group_window_launch(
        result: GroupWindowLaunchResult,
    ) -> None:
        apply_auto_battle_after_game_launch(
            group_window_launch_started_game(result),
            smart_reconnect_controller,
            config,
            home_view,
        )
        if (
            result.action == "stop"
            and operation_record_store is not None
        ):
            operation_record_store.append(
                "停止全部",
                "全部受管遊戲",
                result.player_message,
            )
        if home_view is None:
            return
        home_view.set_group_launch_state(False, result.player_message)
        home_view.refresh_group_role_statuses()

    def start_group_window_operation(operation: str, starter) -> bool:
        if group_window_operation_lease.get("value") is not None:
            return False
        lease = (
            game_operation_gate.acquire(
                operation,
                timeout_seconds=0,
            )
            if game_operation_gate is not None
            else None
        )
        if game_operation_gate is not None and lease is None:
            return False
        group_window_operation_lease["value"] = lease

        def complete(result: GroupWindowLaunchResult) -> None:
            release_group_window_operation(lease)
            complete_group_window_launch(result)

        try:
            started = bool(starter(complete))
        except Exception:
            release_group_window_operation()
            raise
        if not started:
            release_group_window_operation()
        return started

    def launch_group_and_restore(group_name: str) -> bool:
        if group_window_launch_service is None:
            return False
        return start_group_window_operation(
            "group-launch",
            lambda complete: group_window_launch_service.start(
                group_name,
                complete,
            ),
        )

    def restore_group_positions(group_name: str) -> bool:
        if group_window_launch_service is None:
            return False
        return start_group_window_operation(
            "group-position-restore",
            lambda complete: group_window_launch_service.start_restore(
                group_name,
                complete,
            ),
        )

    def stop_all_managed_games(_group_name: str) -> object:
        def failure(message: str) -> str:
            if operation_record_store is not None:
                operation_record_store.append(
                    "停止全部",
                    "全部受管遊戲",
                    message,
                )
            return message

        if group_window_launch_service is None:
            return failure("受管遊戲服務尚未準備完成。")
        if not stop_group_automation_for_configuration_change():
            return failure(
                "同步或智慧重連尚未完全停止，沒有關閉遊戲視窗。"
            )
        if (
            group_window_launch_service.running
            and not group_window_launch_service.stop(timeout_seconds=5.0)
        ):
            return failure(
                "整組啟動尚未完全停止，沒有關閉遊戲視窗。"
            )
        return start_group_window_operation(
            "group-stop-all",
            lambda complete: group_window_launch_service.start_stop_all(
                complete,
            ),
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
            group.main_entry.shortcut_path
            if group is not None and group.main_entry is not None
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
        else "classic_gold"
    )
    configured_show_hints = (
        bool(config.get(SHOW_HINTS_KEY, False))
        if config is not None
        else False
    )
    configured_font_preferences = resolve_ui_font_preferences(
        config.get(UI_FONT_ID_KEY) if config is not None else None,
        (
            config.get(UI_SIDEBAR_FONT_SIZE_KEY)
            if config is not None
            else None
        ),
        (
            config.get(UI_CONTENT_FONT_SIZE_KEY)
            if config is not None
            else None
        ),
    )
    if config is not None:
        normalized_font_values = {
            UI_FONT_ID_KEY: configured_font_preferences.font_id,
            UI_SIDEBAR_FONT_SIZE_KEY: (
                configured_font_preferences.sidebar_size
            ),
            UI_CONTENT_FONT_SIZE_KEY: (
                configured_font_preferences.content_size
            ),
        }
        try:
            config.update_values(normalized_font_values)
        except Exception:
            logger.error("介面字體偏好無法寫入，這次仍使用安全預設值。")
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
    configured_sync_keys_collapsed = (
        config.get(SYNC_KEYS_COLLAPSED_KEY, True) is not False
        if config is not None
        else True
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
    feature_card_settings_batch_service = (
        FeatureCardSettingsBatchService(
            config=config,
            feature_card_layout_service=feature_card_layout_service,
            background_image_service=background_image_service,
            configured_feature_hotkeys=configured_feature_hotkeys,
            feature_hotkeys_config_key=FEATURE_HOTKEYS_KEY,
            group_configuration_service=group_configuration_service,
            error_logger=logger.error,
        )
        if (
            config is not None
            and feature_card_layout_service is not None
            and background_image_service is not None
        )
        else None
    )

    def change_selected_sync_keys(keys: tuple[str, ...]) -> bool:
        normalized = list(
            dict.fromkeys(key for key in keys if key in known_sync_keys)
        )
        configured_selected_sync_keys[:] = normalized
        if config is not None:
            config.set(SYNC_SELECTED_KEYS_KEY, normalized)
        return True

    def change_sync_keys_collapsed(collapsed: bool) -> bool:
        if config is not None:
            config.set(SYNC_KEYS_COLLAPSED_KEY, bool(collapsed))
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

    def change_show_hints(show: bool) -> bool:
        if config is None:
            return False
        config.set(SHOW_HINTS_KEY, bool(show))
        return True

    def change_ui_font(font_id: str) -> bool:
        normalized = normalize_ui_font_id(font_id)
        if config is None or normalized != font_id:
            return False
        try:
            config.set(UI_FONT_ID_KEY, normalized)
        except Exception:
            logger.error("介面字體偏好保存失敗。")
            return False
        return True

    def change_sidebar_font_size(size: int) -> bool:
        normalized = normalize_sidebar_font_size(size)
        if config is None or normalized != size:
            return False
        try:
            config.set(UI_SIDEBAR_FONT_SIZE_KEY, normalized)
        except Exception:
            logger.error("左側選單字級偏好保存失敗。")
            return False
        return True

    def change_content_font_size(size: int) -> bool:
        normalized = normalize_content_font_size(size)
        if config is None or normalized != size:
            return False
        try:
            config.set(UI_CONTENT_FONT_SIZE_KEY, normalized)
        except Exception:
            logger.error("內容區字級偏好保存失敗。")
            return False
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

    def save_card_background(managed_path: Path, card_id: str):
        if background_image_service is None:
            raise RuntimeError("background image service is unavailable")
        return background_image_service.commit_prepared_to_card(
            managed_path,
            card_id,
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

    def clear_card_background(card_id: str):
        if background_image_service is None:
            raise RuntimeError("background image service is unavailable")
        return background_image_service.clear_card(card_id)

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
        profiles = {
            character.character_id: character
            for character in (
                character_store.load()
                if character_store is not None
                else ()
            )
        }
        members = {
            member.entry_id: member
            for member in choice.members
        }

        def registered_profile(target):
            member = members.get(target.entry_id)
            character_id = (
                member.character_id
                if member is not None
                else None
            )
            return (
                profiles.get(character_id)
                if character_id is not None
                else None
            )

        def with_registered_profile(target):
            profile = registered_profile(target)
            return replace(
                target,
                registered_level=(
                    profile.level
                    if profile is not None
                    else None
                ),
                importance=(
                    profile.importance
                    if profile is not None
                    else None
                ),
            )

        return GroupLaunchPlan(
            group_name=plan.group_name,
            targets=tuple(
                with_registered_profile(target)
                for target in plan.targets
            ),
            failure_codes=plan.failure_codes,
        )

    def configured_reconnect_plan() -> GroupLaunchPlan | None:
        if (
            target_window_contract_service is None
            or group_configuration_service is None
        ):
            return None
        scope = target_window_contract_service.configured_scope()
        return build_configured_reconnect_plan(
            scope,
            group_configuration_service.groups(),
            (
                character_store.load()
                if character_store is not None
                else ()
            ),
            (
                group_selection_service.choices()
                if group_selection_service is not None
                else ()
            ),
        )

    def write_clipboard(value: str) -> bool:
        try:
            window.clipboard_clear()
            window.clipboard_append(value)
            window.update_idletasks()
            return True
        except TclError:
            return False

    if farm_timer_service is not None:
        farm_timer_service.set_clipboard_writer(write_clipboard)

    def scoped_group_entries(group_name: str, entry_ids):
        if group_configuration_service is None:
            return None
        selected = group_configuration_service.group(group_name)
        if selected is None:
            return None
        selected_by_id = {
            entry.entry_id: entry for entry in selected.entries
        }
        candidates: dict[str, list[object]] = {}
        for configured_group in group_configuration_service.groups():
            for entry in configured_group.entries:
                candidates.setdefault(entry.entry_id, []).append(entry)
        resolved = []
        for entry_id in entry_ids:
            entry = selected_by_id.get(entry_id)
            if entry is None:
                matches = candidates.get(entry_id, ())
                if len(matches) != 1:
                    return None
                entry = matches[0]
            resolved.append(entry)
        return tuple(resolved)

    def apply_group_identity(choice) -> GroupLaunchPlan | None:
        nonlocal sync_connected_fingerprints
        nonlocal sync_connected_instance_signature
        plan = selected_group_plan(choice)
        if plan is None:
            return None
        if target_window_contract_service is None:
            return None
        resolved_targets = target_window_contract_service.reconnect_targets(
            choice.name
        )
        scope_entry_ids = resolved_targets.sync_scope_entry_ids
        controller_entry_id = resolved_targets.sync_controller_entry_id
        scoped_entries = scoped_group_entries(
            choice.name,
            scope_entry_ids,
        )
        if (
            scoped_entries is None
            or len(scoped_entries) != len(scope_entry_ids)
        ):
            return None
        instance_windows = resolve_complete_sync_instance_windows(
            scope_entry_ids,
            resolved_targets.sync_entry_ids,
            resolved_targets.sync_windows,
            controller_entry_id=controller_entry_id,
        )
        if not instance_windows:
            return None
        entry_by_id = {
            entry.entry_id: entry for entry in scoped_entries
        }
        instance_by_entry = dict(
            zip(
                resolved_targets.sync_entry_ids,
                resolved_targets.sync_windows,
            )
        )
        target_settings = {
            identity[0]: entry_by_id[entry_id].sync_settings
            for entry_id, window in instance_by_entry.items()
            if entry_id in entry_by_id
            and window in instance_windows
            and (
                identity := complete_window_instance_identity(window)
            ) is not None
        }
        if len(target_settings) != len(instance_windows):
            return None
        instance_fingerprints = tuple(
            complete_window_instance_identity(window)[0]
            for window in instance_windows
        )
        sync_connected_fingerprints = None
        sync_connected_instance_signature = None
        if input_controller is not None:
            input_controller.set_expected_windows(len(instance_windows))
            input_controller.set_allowed_window_instances(instance_windows)
            input_controller.set_target_settings(target_settings)
            input_controller.set_controller_fingerprint(
                instance_fingerprints[0]
            )
            if pointer_sync_controller is not None:
                pointer_sync_controller.set_expected_windows(
                    len(instance_windows)
                )
                pointer_sync_controller.set_allowed_window_instances(
                    instance_windows
                )
                pointer_sync_controller.set_target_settings(
                    target_settings
                )
                pointer_sync_controller.set_controller_fingerprint(
                    instance_fingerprints[0]
                )
        return plan

    sync_connected_fingerprints: tuple[str, ...] | None = None
    sync_connected_instance_signature: tuple[tuple[object, ...], ...] | None = None

    def apply_connected_sync_identity(
        choice,
        connected_windows: tuple[WindowInfo, ...] | None = None,
        *,
        resolved_targets: ResolvedTargetWindows | None = None,
    ) -> bool:
        """Only the currently connected and already scoped windows may sync."""
        if input_controller is None or pointer_sync_controller is None:
            return False
        if (resolved_targets is None) != (connected_windows is None):
            return False
        if resolved_targets is None:
            resolved_targets, connected_windows = (
                resolve_connected_sync_target_contract(
                    target_window_contract_service,
                    choice.name,
                )
            )
        scope_entry_ids = resolved_targets.sync_scope_entry_ids
        controller_entry_id = resolved_targets.sync_controller_entry_id
        scope_windows = resolve_complete_sync_instance_windows(
            scope_entry_ids,
            resolved_targets.sync_entry_ids,
            resolved_targets.sync_windows,
            controller_entry_id=controller_entry_id,
        )
        if not scope_windows:
            return False
        fingerprints = _connected_sync_fingerprints(
            tuple(
                complete_window_instance_identity(window)[0]
                for window in scope_windows
            ),
            tuple(
                connected_windows if connected_windows is not None else ()
            ),
        )
        entries = scoped_group_entries(choice.name, scope_entry_ids)
        if entries is None:
            return False
        entry_by_id = {entry.entry_id: entry for entry in entries}
        window_by_entry = dict(
            zip(
                resolved_targets.sync_entry_ids,
                resolved_targets.sync_windows,
            )
        )
        settings_by_fingerprint = {
            identity[0]: entry_by_id[entry_id].sync_settings
            for entry_id, window in window_by_entry.items()
            if entry_id in entry_by_id
            and window in scope_windows
            and (
                identity := complete_window_instance_identity(window)
            ) is not None
        }
        settings = {
            fingerprint: settings_by_fingerprint[fingerprint]
            for fingerprint in fingerprints
            if fingerprint in settings_by_fingerprint
        }
        if len(settings) != len(fingerprints):
            return False
        connected_windows_by_fingerprint = {
            complete_window_instance_identity(window)[0]: window
            for window in scope_windows
            if complete_window_instance_identity(window) is not None
            and complete_window_instance_identity(window)[0] in fingerprints
        }
        active_windows = tuple(
            connected_windows_by_fingerprint[fingerprint]
            for fingerprint in fingerprints
            if fingerprint in connected_windows_by_fingerprint
        )
        instance_signature = tuple(
            complete_window_instance_identity(window)
            for window in active_windows
        )
        nonlocal sync_connected_fingerprints
        nonlocal sync_connected_instance_signature
        if (
            sync_connected_fingerprints == fingerprints
            and sync_connected_instance_signature == instance_signature
        ):
            return True
        if not fingerprints:
            return False
        for controller in (input_controller, pointer_sync_controller):
            controller.set_expected_windows(len(active_windows))
            controller.set_allowed_window_instances(active_windows)
            controller.set_target_settings(settings)
            controller.set_controller_fingerprint(fingerprints[0])
        sync_connected_fingerprints = fingerprints
        sync_connected_instance_signature = instance_signature
        return True

    def group_identity_failure_message(choice) -> str:
        plan = selected_group_plan(choice)
        if plan is None:
            return "目前組別的遊戲視窗設定尚未完成；維持安全停止。"
        scope = (
            sync_scope_service.scope(choice.name)
            if sync_scope_service is not None
            else None
        )
        if scope is None or not scope.ready:
            return "目前組別的同步範圍尚未安全確認；維持安全停止。"
        scoped_entries = scoped_group_entries(choice.name, scope.entry_ids)
        if scoped_entries is None:
            return (
                "目前組別因共用捷徑延伸到其他組別；其中角色在多組設定中"
                "無法唯一對應遊戲視窗。為避免錯誤同步，維持安全停止。"
            )
        return "目前組別無法完整對應到唯一遊戲視窗；維持安全停止。"

    def clear_group_identity() -> None:
        nonlocal sync_connected_fingerprints
        nonlocal sync_connected_instance_signature
        if input_controller is not None:
            input_controller.set_allowed_window_instances(None)
        if pointer_sync_controller is not None:
            pointer_sync_controller.set_allowed_window_instances(None)
        sync_connected_fingerprints = None
        sync_connected_instance_signature = None

    def close_group_operation_gate() -> bool:
        return (
            game_operation_gate is None
            or game_operation_gate.close_and_wait(5.0)
        )

    def reopen_group_operation_gate() -> None:
        if game_operation_gate is not None:
            game_operation_gate.reopen()

    def restore_group_identity(choice) -> bool:
        try:
            if choice is None:
                clear_group_identity()
                return True
            return apply_group_identity(choice) is not None
        except Exception:
            return False

    def restore_published_group(
        previous_group,
        previous_next_step: str,
        previous_config_name,
    ) -> bool:
        try:
            config.set(
                CURRENT_GROUP_NAME_KEY,
                previous_config_name,
            )
            workspace_service.set_current_group(previous_group)
            workspace_service.set_next_step(previous_next_step)
            return True
        except Exception:
            return False

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
        refreshed_choice = (
            group_selection_service.find(choice.name)
            if group_selection_service is not None
            else None
        )
        return group_selection_service.workspace_group(
            refreshed_choice or choice,
            tuple(profiles),
        )

    def change_group(name: str) -> GroupManagementViewResult:
        current_workspace = workspace_service.snapshot()
        current_group = current_workspace.current_group
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
        if not close_group_operation_gate():
            return GroupManagementViewResult(
                False,
                current_name,
                "目前操作尚未安全停止，未切換組別。",
            )
        old_choice = group_selection_service.find(current_name)
        old_config_name = config.get(
            CURRENT_GROUP_NAME_KEY,
            current_name or "",
        )
        try:
            automation_stopped = (
                stop_group_automation_for_configuration_change()
            )
        except Exception:
            automation_stopped = False
        if not automation_stopped:
            if restore_group_identity(old_choice):
                reopen_group_operation_gate()
            return GroupManagementViewResult(
                False,
                current_name,
                "自動操作尚未完全停止，未切換組別。",
            )
        try:
            selected_workspace_group = workspace_group_for_choice(choice)
            identity_ready = apply_group_identity(choice) is not None
            if not identity_ready:
                clear_group_identity()
            config.set(CURRENT_GROUP_NAME_KEY, choice.name)
            workspace_service.set_current_group(
                selected_workspace_group
            )
            workspace_service.set_next_step("查看目前需要注意的內容")
        except Exception:
            rollback_ready = restore_group_identity(old_choice)
            if not rollback_ready:
                try:
                    clear_group_identity()
                    rollback_ready = True
                except Exception:
                    rollback_ready = False
            publication_restored = restore_published_group(
                current_group,
                current_workspace.next_step,
                old_config_name,
            )
            if rollback_ready and publication_restored:
                reopen_group_operation_gate()
            return GroupManagementViewResult(
                False,
                current_name,
                "組別身分無法完整重新綁定，未切換組別。",
            )
        reopen_group_operation_gate()
        refresh_confirmed_activity_group_scope(
            workspace_service=workspace_service,
            confirmed_activity_rule_service=confirmed_activity_rule_service,
            logger=logger,
        )
        if group_role_status_service is not None:
            group_role_status_service.clear_cache()
        refresh_character_data(choice.name)
        if not identity_ready:
            return GroupManagementViewResult(
                True,
                choice.name,
                "已切換目前組別；此組視窗身分尚未完整，"
                "同步與智慧重連已保持停用。",
            )
        return GroupManagementViewResult(True, choice.name)

    def stop_group_automation_for_configuration_change() -> bool:
        sync_session_state["enabled"] = False
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
        # 智慧重連綁定的是啟用當下視窗快照；組別編修不得停止或重綁它。
        stopped = sync_stopped
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
        current_workspace = workspace_service.snapshot()
        current_group = current_workspace.current_group
        current_name = (
            current_group.name if current_group is not None else None
        )
        old_choice = group_selection_service.find(current_name)
        old_config_name = config.get(
            CURRENT_GROUP_NAME_KEY,
            current_name or "",
        )
        if not close_group_operation_gate():
            return GroupManagementViewResult(
                False,
                current_name,
                "目前操作尚未安全停止，組別設定未套用。",
            )
        try:
            if choice is not None:
                selected_workspace_group = workspace_group_for_choice(choice)
                if apply_group_identity(choice) is None:
                    raise RuntimeError("group_identity_unresolved")
            else:
                clear_group_identity()
                selected_workspace_group = None
            config.set(
                CURRENT_GROUP_NAME_KEY,
                choice.name if choice is not None else "",
            )
            workspace_service.set_current_group(selected_workspace_group)
            workspace_service.set_next_step(
                "查看目前需要注意的內容"
                if choice is not None
                else "選擇組別"
            )
        except Exception:
            rollback_ready = restore_group_identity(old_choice)
            publication_restored = restore_published_group(
                current_group,
                current_workspace.next_step,
                old_config_name,
            )
            if rollback_ready and publication_restored:
                reopen_group_operation_gate()
            return GroupManagementViewResult(
                False,
                current_name,
                "組別身分無法完整重新綁定，設定未套用。",
            )
        reopen_group_operation_gate()
        refresh_confirmed_activity_group_scope(
            workspace_service=workspace_service,
            confirmed_activity_rule_service=confirmed_activity_rule_service,
            logger=logger,
        )
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

    def save_feature_card_settings(
        *,
        card_id: str,
        title: str,
        reset_title: bool,
        pending_background_path: Path | None,
        clear_background: bool,
        hotkey_feature: str | None,
        hotkey: str,
        group_name: str | None,
    ) -> FeatureCardSettingsSaveResult:
        if feature_card_settings_batch_service is None:
            return FeatureCardSettingsSaveResult(
                False,
                "卡片設定服務尚未準備完成，全部設定均未變更。",
            )
        result = feature_card_settings_batch_service.save(
            card_id=card_id,
            title=title,
            reset_title=reset_title,
            pending_background_path=pending_background_path,
            clear_background=clear_background,
            hotkey_feature=hotkey_feature,
            hotkey=hotkey,
            group_name=group_name,
        )
        return FeatureCardSettingsSaveResult(
            succeeded=result.succeeded,
            message=result.message,
            preference=result.preference,
            background_path=result.background_path,
            hotkey=result.hotkey,
        )

    def group_entries(group_name: str):
        if group_configuration_service is None:
            return ()
        group = group_configuration_service.group(group_name)
        return group.entries if group is not None else ()

    def group_role_details_expanded(entry_id: str) -> bool:
        if config is None or not isinstance(entry_id, str):
            return False
        raw = config.get(GROUP_ROLE_DETAILS_EXPANDED_KEY, {})
        return bool(
            isinstance(raw, dict)
            and raw.get(entry_id) is True
        )

    def change_group_role_details_expanded(
        entry_id: str,
        expanded: bool,
    ) -> bool:
        if (
            config is None
            or not isinstance(entry_id, str)
            or not entry_id.strip()
        ):
            return False
        raw = config.get(GROUP_ROLE_DETAILS_EXPANDED_KEY, {})
        values = dict(raw) if isinstance(raw, dict) else {}
        if expanded:
            values[entry_id.strip()] = True
        else:
            values.pop(entry_id.strip(), None)
        config.set(GROUP_ROLE_DETAILS_EXPANDED_KEY, values)
        return True

    def reorder_group_entries(
        group_name: str,
        entry_ids: tuple[str, ...],
    ) -> object:
        if group_configuration_service is None:
            return "組別設定尚未準備完成。"
        if (
            group_window_launch_service is not None
            and group_window_launch_service.running
        ):
            return "整組啟動正在進行中，未變更角色順序。"
        if not stop_group_automation_for_configuration_change():
            return "自動操作尚未完全停止，未變更角色順序。"
        try:
            changed = group_configuration_service.reorder_group_entries(
                group_name,
                entry_ids,
            )
        except GroupMasterLockedError:
            return GroupMasterLockedError.player_message
        if not changed:
            return "角色順序沒有變更。"
        result = finish_group_management(group_name)
        if result.success and operation_record_store is not None:
            operation_record_store.append(
                "組別設定",
                group_name,
                "角色啟動順序已更新",
            )
        return True if result.success else result.message

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

    def add_ungrouped_window_to_group(
        fingerprint: str,
        group_name: str,
    ) -> GroupManagementViewResult:
        current_name = current_group_name()
        if (
            group_configuration_service is None
            or ungrouped_window_service is None
        ):
            return GroupManagementViewResult(
                False,
                current_name,
                "未分組視窗服務尚未準備完成，沒有加入任何組別。",
            )
        try:
            shortcut_path = ungrouped_window_service.shortcut_for(fingerprint)
        except Exception:
            shortcut_path = None
        if shortcut_path is None:
            return GroupManagementViewResult(
                False,
                current_name,
                "未分組視窗已變更，沒有加入任何組別。",
            )
        if not stop_group_automation_for_configuration_change():
            return GroupManagementViewResult(
                False,
                current_name,
                "自動操作尚未完全停止，未加入角色。",
            )
        try:
            added = group_configuration_service.add_shortcuts(
                group_name,
                (shortcut_path,),
            )
        except (SyncCycleError, GroupMasterLockedError) as error:
            return GroupManagementViewResult(
                False,
                current_name,
                error.player_message,
            )
        except Exception:
            return GroupManagementViewResult(
                False,
                current_name,
                "加入組別時發生錯誤，原本設定已保留。",
            )
        if not added:
            return GroupManagementViewResult(
                False,
                current_name,
                "捷徑已在所選組別，沒有變更。",
            )
        refresh_warning = False
        try:
            refresh_character_data(group_name)
        except Exception:
            refresh_warning = True
        refreshed = finish_group_management(current_name)
        if not refreshed.success:
            refresh_warning = True
        if operation_record_store is not None:
            operation_record_store.ensure_daily_file()
        message = f"{shortcut_path.name} 已加入 {group_name}。"
        if refresh_warning:
            message += "目前組別顯示尚未完整重新套用，操作保持安全停止。"
        return GroupManagementViewResult(
            True,
            refreshed.current_group_name,
            message,
        )

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

    def refresh_group_sync_identity(group_name: str) -> bool:
        if (
            group_selection_service is None
            or config is None
            or config.get(CURRENT_GROUP_NAME_KEY, "") != group_name
        ):
            return False
        choice = group_selection_service.find(group_name)
        if choice is None or not close_group_operation_gate():
            return False
        applied = False
        try:
            applied = apply_group_identity(choice) is not None
        except Exception:
            applied = False
        if applied:
            reopen_group_operation_gate()
            return True
        try:
            clear_group_identity()
        except Exception:
            return False
        reopen_group_operation_gate()
        return False

    def capture_sync_base_point(group_name: str) -> str:
        if (
            group_configuration_service is None
            or sync_calibration_backend is None
        ):
            return "主基準點功能尚未準備完成。"
        group = group_configuration_service.group(group_name)
        if group is None or group.main_entry is None:
            return "目前組別沒有可用的主窗口。"
        window_info = resolve_complete_reconnect_window_for_entry(
            group_name,
            group.main_entry.entry_id,
            group_launch_service,
            target_window_contract_service,
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
        window_info = resolve_complete_reconnect_window_for_entry(
            group_name,
            entry_id,
            group_launch_service,
            target_window_contract_service,
        )
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
    ) -> str:
        if (
            role_id_template_service is None
            or group_configuration_service is None
        ):
            return "角色ID校正尚未準備完成。"
        window_info = resolve_complete_reconnect_window_for_entry(
            group_name,
            entry_id,
            group_launch_service,
            target_window_contract_service,
        )
        if window_info is None:
            return "無法唯一確認角色窗口，未校正角色ID。"
        result = role_id_template_service.calibrate(
            window_info.handle,
            entry_id=entry_id,
        )
        if result.success:
            group_configuration_service.set_role_id(
                group_name,
                entry_id,
                result.role_id,
            )
            refresh_group_sync_identity(group_name)
        return result.message

    def read_role_id(group_name: str, entry_id: str) -> str:
        if (
            role_id_template_service is None
            or group_configuration_service is None
        ):
            return "角色ID讀取尚未準備完成。"
        window_info = resolve_complete_reconnect_window_for_entry(
            group_name,
            entry_id,
            group_launch_service,
            target_window_contract_service,
        )
        if window_info is None:
            return "無法唯一確認角色窗口，未讀取角色ID。"
        result = role_id_template_service.read(
            window_info.handle,
            entry_id=entry_id,
        )
        if result.success:
            group_configuration_service.set_role_id(
                group_name,
                entry_id,
                result.role_id,
            )
            refresh_group_sync_identity(group_name)
            return f"已讀取遊戲內角色ID：{result.role_id}"
        return result.message

    def add_group_sync_relation(
        group_name: str,
        member_entry_id: str,
    ) -> object:
        if group_configuration_service is None:
            return False
        group = group_configuration_service.group(group_name)
        if group is None or group.main_entry is None:
            return False
        if not stop_group_automation_for_configuration_change():
            return "自動操作尚未完全停止，未加入同步關係。"
        try:
            changed = group_configuration_service.add_sync_relation(
                group.main_entry.entry_id,
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
        if group is None or group.main_entry is None:
            return False
        if not stop_group_automation_for_configuration_change():
            return False
        return group_configuration_service.remove_sync_relation(
            group.main_entry.entry_id,
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

    def remove_habit_observation(observation_id: str):
        if player_habit_service is None:
            raise RuntimeError("player habit service is unavailable")
        removed = player_habit_service.remove_observation(observation_id)
        if removed and operation_record_store is not None:
            operation_record_store.append(
                "玩家習慣",
                "系統",
                "已刪除一筆玩家習慣觀察紀錄",
            )
        return player_habit_service.settings_view()

    def clear_habit_preferences():
        if player_habit_service is None:
            raise RuntimeError("player habit service is unavailable")
        removed = player_habit_service.clear_all()
        if removed and operation_record_store is not None:
            operation_record_store.append(
                "玩家習慣",
                "系統",
                f"已清除 {removed} 筆玩家習慣資料",
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

        if home_view is None:
            return
        home_view.show_character_detail(
            detail,
            on_save_note=save_note,
            on_clear_note=clear_note,
            on_error=note_error,
        )

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

    sync_source_handle_cache: dict[str, object] = {
        "group_name": None,
        "expires_at": 0.0,
        "handles": (),
    }

    def current_target_handles() -> tuple[int, ...]:
        """Keep hot input polling within the current group without rescanning."""
        nonlocal sync_connected_fingerprints
        nonlocal sync_connected_instance_signature
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
        now = monotonic()
        if (
            sync_source_handle_cache["group_name"] == group_name
            and now < float(sync_source_handle_cache["expires_at"])
        ):
            return tuple(sync_source_handle_cache["handles"])
        resolved_targets, connected_windows = (
            resolve_connected_sync_target_contract(
                target_window_contract_service,
                group_name,
            )
        )
        handles = tuple(window.handle for window in connected_windows)
        if sync_session_state["enabled"]:
            choice = (
                group_selection_service.find(group_name)
                if group_selection_service is not None
                else None
            )
            if choice is None or not apply_connected_sync_identity(
                choice,
                connected_windows,
                resolved_targets=resolved_targets,
            ):
                sync_session_state["enabled"] = False
                sync_connected_fingerprints = None
                sync_connected_instance_signature = None
                if input_controller is not None:
                    input_controller.set_allowed_window_instances(None)
                if pointer_sync_controller is not None:
                    pointer_sync_controller.set_allowed_window_instances(None)
                if (
                    keyboard_sync_monitor is not None
                    and mouse_sync_monitor is not None
                ):
                    stop_input_sync_pair(
                        keyboard_sync_monitor,
                        mouse_sync_monitor,
                    )
                handles = ()
        sync_source_handle_cache.update(
            {
                "group_name": group_name,
                "expires_at": now + 0.25,
                "handles": handles,
            }
        )
        return handles

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
            execution_enabled_provider=lambda: bool(
                sync_session_state["enabled"]
            ),
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
            execution_enabled_provider=lambda: bool(
                sync_session_state["enabled"]
            ),
        )
        if pointer_sync_controller is not None
        else None
    )

    def direct_auto_click_enabled() -> bool:
        return (
            bool(sync_session_state["enabled"])
            and
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

    def change_keyboard_sync(enabled: bool) -> SyncToggleViewResult:
        nonlocal sync_connected_fingerprints
        nonlocal sync_connected_instance_signature
        if (
            keyboard_sync_monitor is None
            or mouse_sync_monitor is None
            or input_controller is None
            or pointer_sync_controller is None
        ):
            return SyncToggleViewResult(
                False,
                False,
                "同步輸入尚未正確設定，沒有啟用。",
            )
        auto_click_service.invalidate_direct_sync()
        if not enabled:
            sync_session_state["enabled"] = False
            input_controller.set_allowed_window_instances(None)
            pointer_sync_controller.set_allowed_window_instances(None)
            sync_connected_fingerprints = None
            sync_connected_instance_signature = None
            cleanup_stopped = stop_input_sync_pair(
                keyboard_sync_monitor,
                mouse_sync_monitor,
            )
            if logger is not None and not cleanup_stopped:
                logger.warning(
                    "Synchronized input delivery was disabled, but one "
                    "monitor did not finish cleanup."
                )
            # The shared execution gate is already closed, so no queued or
            # still-polling monitor can deliver another game input.
            return SyncToggleViewResult(
                True,
                False,
                (
                    "同步已停止。"
                    if cleanup_stopped
                    else "同步已停止；背景清理仍在完成中。"
                ),
            )

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
        if choice is None:
            return SyncToggleViewResult(
                False,
                False,
                "請先選擇至少有 2 個視窗設定的組別；目前沒有啟用。",
            )
        if not apply_connected_sync_identity(choice):
            return SyncToggleViewResult(
                False,
                False,
                group_identity_failure_message(choice),
            )
        if current_input_policy() is None:
            return SyncToggleViewResult(
                False,
                False,
                "允許範圍尚未正確設定；目前沒有啟用。",
            )
        keyboard_started = start_service(keyboard_sync_monitor)
        mouse_started = start_service(mouse_sync_monitor)
        if not keyboard_started.success or not mouse_started.success:
            sync_session_state["enabled"] = False
            stop_service(keyboard_sync_monitor)
            stop_service(mouse_sync_monitor)
            failed_parts = "、".join(
                name
                for name, started in (
                    ("鍵盤監看", keyboard_started),
                    ("滑鼠監看", mouse_started),
                )
                if not started.success
            )
            return SyncToggleViewResult(
                False,
                False,
                f"{failed_parts}未能啟動；同步沒有啟用。",
            )
        sync_session_state["enabled"] = True
        if logger is not None:
            logger.info(
                "Keyboard synchronization enabled for connected group windows; "
                f"group={choice.name}"
            )
        mark_tray_operations_running()
        return SyncToggleViewResult(
            True,
            True,
            "同步中｜同步左鍵、拖曳與已確認快捷鍵",
        )

    def change_smart_reconnect(
        enabled: bool,
    ) -> SmartReconnectToggleViewResult:
        if (
            config is None
            or smart_reconnect_monitor is None
            or smart_reconnect_controller is None
            or workspace_service is None
            or group_selection_service is None
        ):
            return SmartReconnectToggleViewResult(
                False,
                False,
                "智慧重連服務尚未正確設定，沒有啟用。",
            )
        if enabled:
            if not close_group_operation_gate():
                return SmartReconnectToggleViewResult(
                    False,
                    False,
                    "目前組別的安全視窗身分尚未完成，智慧重連未啟用。",
                )
            transition = SmartReconnectToggleViewResult(
                False,
                False,
                "目前組別的安全視窗身分尚未完成，智慧重連未啟用。",
            )
            try:
                plan = configured_reconnect_plan()
                identity_ready = plan is not None
                if identity_ready:
                    smart_reconnect_controller.set_group_launch_plan(plan)
                    transition = apply_smart_reconnect_snapshot_transition(
                        True,
                        smart_reconnect_controller,
                        smart_reconnect_monitor,
                        start_monitor=start_service,
                        stop_monitor=lambda _monitor: stop_service(
                            smart_reconnect_monitor,
                            timeout_seconds=1.0,
                        ),
                    )
            except Exception:
                identity_ready = False
            if not identity_ready or not transition.success:
                try:
                    smart_reconnect_controller.set_group_launch_plan(None)
                    reopen_group_operation_gate()
                except Exception:
                    pass
                return transition
            try:
                reopen_group_operation_gate()
            except Exception:
                gate_closed = close_group_operation_gate()
                apply_smart_reconnect_snapshot_transition(
                    False,
                    smart_reconnect_controller,
                    smart_reconnect_monitor,
                    start_monitor=start_service,
                    stop_monitor=lambda _monitor: stop_service(
                        smart_reconnect_monitor,
                        timeout_seconds=1.0,
                    ),
                )
                smart_reconnect_controller.set_group_launch_plan(None)
                return SmartReconnectToggleViewResult(
                    False,
                    False,
                    "安全操作閘門無法開啟，智慧重連已停止。",
                )
            try:
                config.update_values(
                    {
                        SMART_RECONNECT_ENABLED_KEY: True,
                        SMART_RECONNECT_CONSENT_KEY: True,
                    }
                )
            except Exception:
                gate_closed = close_group_operation_gate()
                apply_smart_reconnect_snapshot_transition(
                    False,
                    smart_reconnect_controller,
                    smart_reconnect_monitor,
                    start_monitor=start_service,
                    stop_monitor=lambda _monitor: stop_service(
                        smart_reconnect_monitor,
                        timeout_seconds=1.0,
                    ),
                )
                try:
                    config.update_values(
                        {
                            SMART_RECONNECT_ENABLED_KEY: False,
                            SMART_RECONNECT_CONSENT_KEY: False,
                        }
                    )
                except Exception:
                    pass
                try:
                    smart_reconnect_controller.set_group_launch_plan(None)
                except Exception:
                    pass
                if gate_closed:
                    try:
                        reopen_group_operation_gate()
                    except Exception:
                        pass
                return SmartReconnectToggleViewResult(
                    False,
                    False,
                    "智慧重連設定無法保存，監看已停止。",
                )
            if logger is not None:
                logger.info("Smart reconnect explicitly enabled by the player.")
            mark_tray_operations_running()
            return transition

        transition = apply_smart_reconnect_snapshot_transition(
            False,
            smart_reconnect_controller,
            smart_reconnect_monitor,
            start_monitor=start_service,
            stop_monitor=lambda _monitor: stop_service(
                smart_reconnect_monitor,
                timeout_seconds=1.0,
            ),
        )
        stopped = transition.success
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
        return transition

    def change_smart_reconnect_auto_battle(enabled: bool) -> bool:
        """子開關永遠不會自行開啟智慧重連總開關。"""
        if config is None or smart_reconnect_controller is None:
            return False
        return apply_smart_reconnect_auto_battle_setting(
            enabled,
            smart_reconnect_controller,
            config,
        )

    def change_smart_reconnect_interval(interval_ms: int) -> bool:
        if config is None or smart_reconnect_monitor is None:
            return False
        normalized = normalize_smart_reconnect_interval_ms(
            interval_ms,
            default=0,
        )
        if normalized <= 0:
            return False
        if not smart_reconnect_monitor.set_monitor_interval_ms(normalized):
            return False
        config.set(SMART_RECONNECT_INTERVAL_MS_KEY, normalized)
        if logger is not None:
            logger.info(
                "Smart reconnect monitoring interval changed; "
                f"interval_ms={normalized}"
            )
        return True

    def change_smart_reconnect_status_colors(value: object) -> bool:
        if config is None or not isinstance(value, dict):
            return False
        normalized: dict[str, str] = {}
        for name in ("已開啟", "重連中", "重連失敗"):
            color = value.get(name)
            if not isinstance(color, str) or not re.fullmatch(
                r"#[0-9A-Fa-f]{6}", color
            ):
                return False
            normalized[name] = color.upper()
        config.set(SMART_RECONNECT_STATUS_COLORS_KEY, normalized)
        return True

    def change_smart_reconnect_capture_modes(modes: object) -> bool:
        if (
            smart_reconnect_capture_settings_service is None
            or smart_reconnect_controller is None
        ):
            return False
        try:
            settings = (
                smart_reconnect_capture_settings_service.update(modes)
            )
            smart_reconnect_controller.set_capture_settings(settings)
        except Exception as error:
            if logger is not None:
                logger.error(
                    "Smart reconnect capture modes were not changed; "
                    f"error_type={type(error).__name__}"
                )
            return False
        if logger is not None:
            logger.info(
                "Smart reconnect capture modes changed; "
                f"visible={settings.visible}; "
                f"obscured={settings.obscured}; "
                f"minimized={settings.minimized}"
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
            started = auto_click_service.start(settings)
            if started:
                mark_tray_operations_running()
            return started
        auto_click_service.stop()
        return True

    raw_timed_click_settings = (
        config.get(TIMED_CLICK_SETTINGS_KEY, {})
        if config is not None
        else {}
    )
    if not isinstance(raw_timed_click_settings, dict):
        raw_timed_click_settings = {}
    raw_configured_timed_click_target = (
        raw_timed_click_settings.get("target_time", "")
        if isinstance(raw_timed_click_settings.get("target_time", ""), str)
        else ""
    )
    configured_timed_click_target = (
        raw_configured_timed_click_target.strip()
        or DEFAULT_TIMED_CLICK_TARGET_TIME
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
        DEFAULT_TIMED_CLICK_LEAD_MS,
        -5_000,
        5_000,
    )
    configured_timed_click_repeat = bounded_timed_setting(
        "repeat_count",
        DEFAULT_TIMED_CLICK_REPEAT_COUNT,
        1,
        10,
    )
    configured_timed_click_interval = bounded_timed_setting(
        "repeat_interval_ms",
        DEFAULT_TIMED_CLICK_INTERVAL_MS,
        0,
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
        if result.success:
            mark_tray_operations_running()
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

    def activate_or_launch_group_role(action_id: str) -> object | None:
        if group_role_status_service is None:
            return None
        result = group_role_status_service.activate_or_launch(
            home_view.current_group_name if home_view is not None else None,
            action_id,
        )
        apply_auto_battle_after_game_launch(
            group_role_action_started_game(result),
            smart_reconnect_controller,
            config,
            home_view,
        )
        return result

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
        sync_keys_collapsed=configured_sync_keys_collapsed,
        on_sync_keys_collapsed_change=change_sync_keys_collapsed,
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
        on_stop_all_managed_games=stop_all_managed_games,
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
        group_role_details_expanded_provider=(
            group_role_details_expanded
        ),
        on_group_role_details_expanded_change=(
            change_group_role_details_expanded
        ),
        on_reorder_group_entries=reorder_group_entries,
        group_master_locked_provider=group_master_locked,
        on_group_master_locked_change=change_group_master_locked,
        on_add_group_shortcuts=add_group_shortcuts,
        ungrouped_windows_provider=ungrouped_window_service.snapshot,
        on_add_ungrouped_window_to_group=add_ungrouped_window_to_group,
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
        activity_description_choices=(
            activity_description_service.choices()
            if activity_description_service is not None
            else ()
        ),
        activity_description_choices_provider=(
            activity_description_service.choices
            if activity_description_service is not None
            else None
        ),
        on_activity_description_change=(
            activity_description_service.set_description
            if activity_description_service is not None
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
        on_card_action=(
            lambda card_id, action_id: handle_card_action(
                card_id,
                action_id,
            )
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
        smart_reconnect_auto_battle_enabled=(
            normalize_smart_reconnect_auto_battle_enabled(
                config.get(SMART_RECONNECT_AUTO_BATTLE_ENABLED_KEY)
                if config is not None
                else None
            )
        ),
        smart_reconnect_runtime_status=(
            smart_reconnect_monitor.runtime_status
            if smart_reconnect_monitor is not None
            else None
        ),
        smart_reconnect_status_colors=(
            config.get(SMART_RECONNECT_STATUS_COLORS_KEY)
            if config is not None else None
        ),
        on_smart_reconnect_status_colors_change=(
            change_smart_reconnect_status_colors
        ),
        on_smart_reconnect_change=change_smart_reconnect,
        on_smart_reconnect_auto_battle_change=(
            change_smart_reconnect_auto_battle
        ),
        smart_reconnect_interval_ms=(
            smart_reconnect_monitor.monitor_interval_ms
            if smart_reconnect_monitor is not None
            else DEFAULT_SMART_RECONNECT_INTERVAL_MS
        ),
        on_smart_reconnect_interval_change=(
            change_smart_reconnect_interval
        ),
        smart_reconnect_capture_modes=(
            smart_reconnect_capture_settings_service.snapshot().to_dict()
            if smart_reconnect_capture_settings_service is not None
            else SmartReconnectCaptureSettings().to_dict()
        ),
        on_smart_reconnect_capture_modes_change=(
            change_smart_reconnect_capture_modes
        ),
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
            activate_or_launch_group_role
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
        on_remove_habit_observation=remove_habit_observation,
        on_clear_habit_preferences=clear_habit_preferences,
        theme_name=configured_theme,
        on_theme_change=change_ui_theme,
        show_hints=configured_show_hints,
        on_show_hints_change=change_show_hints,
        ui_font_choices=(
            ui_font_service.choices
            if ui_font_service is not None
            else ()
        ),
        ui_font_id=configured_font_preferences.font_id,
        sidebar_font_size=configured_font_preferences.sidebar_size,
        content_font_size=configured_font_preferences.content_size,
        ui_font_failure_message=(
            ui_font_service.result.message
            if ui_font_service is not None
            and not ui_font_service.result.success
            else ""
        ),
        on_ui_font_change=change_ui_font,
        on_sidebar_font_size_change=change_sidebar_font_size,
        on_content_font_size_change=change_content_font_size,
        feature_card_preference_provider=(
            feature_card_layout_service.preference
            if feature_card_layout_service is not None
            else None
        ),
        feature_card_order_provider=(
            feature_card_layout_service.order_for
            if feature_card_layout_service is not None
            else None
        ),
        on_feature_card_collapsed_change=(
            feature_card_layout_service.set_collapsed
            if feature_card_layout_service is not None
            else None
        ),
        on_feature_card_order_change=(
            feature_card_layout_service.reorder
            if feature_card_layout_service is not None
            else None
        ),
        on_feature_card_title_change=(
            feature_card_layout_service.set_title
            if feature_card_layout_service is not None
            else None
        ),
        on_feature_card_title_reset=(
            feature_card_layout_service.reset_title
            if feature_card_layout_service is not None
            else None
        ),
        on_save_feature_card_settings=save_feature_card_settings,
        card_background_provider=(
            background_image_service.current_card_background
            if background_image_service is not None
            else None
        ),
        on_save_card_background=save_card_background,
        on_clear_card_background=clear_card_background,
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
    closing = False
    game_data_read_after_id: str | None = None
    game_data_read_cursor = 0

    def schedule_registered_obsidian_poll() -> None:
        nonlocal game_data_read_after_id
        if closing:
            game_data_read_after_id = None
            return
        try:
            game_data_read_after_id = window.after(
                1500,
                poll_registered_obsidian_once,
            )
        except TclError:
            game_data_read_after_id = None

    def poll_registered_obsidian_once() -> None:
        nonlocal game_data_read_after_id, game_data_read_cursor
        game_data_read_after_id = None
        stage = "resolve_candidates"
        candidate_index = -1
        try:
            capture_service = AppContext.get(CharacterGameDataCaptureService)
            candidates = current_sync_target_windows()
            if capture_service is not None and candidates:
                candidate_index = game_data_read_cursor % len(candidates)
                selected = candidates[candidate_index]
                game_data_read_cursor += 1
                home_view.set_game_data_read_status("安全讀取中")
                stage = "capture"
                result = capture_service.read(selected.handle)
                if result.status.value in {"updated", "unchanged"}:
                    if result.status.value == "updated":
                        stage = "refresh"
                        refresh_character_data(current_group_name())
                    page = result.page.data if result.page is not None else None
                    opened_page = getattr(page, "opened_page", None)
                    home_view.set_game_data_read_status(
                        f"已確認黑曜石第 {opened_page} 頁"
                        if isinstance(opened_page, int) else "尚未安全讀取"
                    )
                else:
                    home_view.set_game_data_read_status("尚未安全讀取")
            else:
                home_view.set_game_data_read_status("尚未安全讀取")
        except Exception as error:
            home_view.set_game_data_read_status("尚未安全讀取")
            if logger is not None:
                logger.error(
                    "Obsidian read-only polling cycle failed safely; "
                    f"error_type={type(error).__name__}; "
                    f"stage={stage}; candidate_index={candidate_index}"
                )
        finally:
            schedule_registered_obsidian_poll()

    if AppContext.get(CharacterGameDataCaptureService) is not None:
        schedule_registered_obsidian_poll()
    def activity_progress_changed_handler(change: object) -> None:
        def apply_change() -> None:
            home_view.refresh_activity_schedule()
            if (
                true_event_card_service is not None
                and isinstance(change, ActivityProgressChange)
            ):
                true_event_card_service.handle_activity_progress(change)
            if (
                player_habit_activity_observer is not None
                and isinstance(change, ActivityProgressChange)
            ):
                try:
                    recorded = player_habit_activity_observer.handle(change)
                except Exception as error:
                    if logger is not None:
                        logger.error(
                            "Player habit activity observation failed and "
                            f"was isolated: {error}"
                        )
                else:
                    if recorded:
                        home_view.refresh_habit_settings()

        dispatch_to_main_window(apply_change)

    def group_role_status_changed_handler(change: object) -> None:
        occurred_at = datetime.now(TAIPEI_TIMEZONE)
        subject_id_resolver = (
            lambda item: resolve_group_role_progress_subject_id(
                item,
                group_launch_service=group_launch_service,
                group_selection_service=group_selection_service,
            )
        )
        handle_group_role_status_change(
            change,
            activity_progress_service=activity_progress_service,
            subject_id_resolver=subject_id_resolver,
            occurred_at=occurred_at,
            logger=logger,
            on_role_status_card=(
                (
                    lambda item: dispatch_to_main_window(
                        lambda: true_event_card_service.handle_role_status(item)
                    )
                )
                if true_event_card_service is not None
                else None
            ),
            on_farm_timer_status=(
                lambda item: route_group_role_status_to_farm_timer(
                    item,
                    farm_timer_service=farm_timer_service,
                    subject_id_resolver=subject_id_resolver,
                    occurred_at=occurred_at,
                )
            ),
            on_confirmed_activity_status=(
                lambda item: route_group_role_status_to_confirmed_activity_rules(
                    item,
                    confirmed_activity_rule_service=(
                        confirmed_activity_rule_service
                    ),
                    subject_id_resolver=subject_id_resolver,
                    occurred_at=occurred_at,
                )
            ),
        )

    def farm_planting_confirmed_handler(event: object) -> None:
        if (
            farm_timer_service is not None
            and isinstance(event, FarmPlantingConfirmed)
        ):
            dispatch_to_main_window(lambda: farm_timer_service.start(event))

    def farm_completed_handler(event: object) -> None:
        if (
            farm_timer_service is not None
            and isinstance(event, FarmCompleted)
        ):
            dispatch_to_main_window(lambda: farm_timer_service.complete(event))

    def confirmed_activity_rule_event_handler(event: object) -> None:
        if (
            confirmed_activity_rule_service is not None
            and isinstance(event, ConfirmedActivityEvent)
        ):
            dispatch_to_main_window(
                lambda: confirmed_activity_rule_service.handle(event)
            )

    event_subscription_scope = (
        EventSubscriptionScope(event_bus)
        if event_bus is not None
        else None
    )
    if event_subscription_scope is not None:
        event_subscription_scope.subscribe(
            ACTIVITY_PROGRESS_CHANGED_EVENT,
            activity_progress_changed_handler,
        )
        event_subscription_scope.subscribe(
            GROUP_ROLE_STATUS_CHANGED_EVENT,
            group_role_status_changed_handler,
        )
        event_subscription_scope.subscribe(
            FARM_PLANTING_CONFIRMED_EVENT,
            farm_planting_confirmed_handler,
        )
        event_subscription_scope.subscribe(
            FARM_COMPLETED_EVENT,
            farm_completed_handler,
        )
        event_subscription_scope.subscribe(
            CONFIRMED_ACTIVITY_RULE_EVENT,
            confirmed_activity_rule_event_handler,
        )
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
        return current_workspace_group_name()

    role_id_auto_read_id: str | None = None
    role_id_auto_read_cursor = 0

    def refresh_auto_read_role_id(group_name: str) -> None:
        refresh_group_sync_identity(group_name)
        if home_view is not None:
            home_view.refresh_group_entries()
            home_view.refresh_group_role_statuses()

    def auto_read_missing_role_id() -> None:
        """只讀取一個可見且已進遊戲的空白角色名稱。"""
        nonlocal role_id_auto_read_id, role_id_auto_read_cursor
        try:
            group_name = current_group_name()
            if (
                group_name is None
                or group_configuration_service is None
                or role_id_template_service is None
                or smart_reconnect_controller is None
            ):
                return
            group = group_configuration_service.group(group_name)
            if group is None:
                return
            missing_entries = tuple(
                entry
                for entry in group.entries
                if not entry.role_id.strip()
            )
            if not missing_entries:
                return
            entry = missing_entries[
                role_id_auto_read_cursor % len(missing_entries)
            ]
            role_id_auto_read_cursor += 1
            auto_read_missing_role_id_once(
                group_name,
                entry.entry_id,
                group_configuration_service,
                group_launch_service,
                target_window_contract_service,
                role_id_template_service,
                smart_reconnect_controller,
                refresh=lambda: refresh_auto_read_role_id(group_name),
            )
        except Exception:
            if logger is not None:
                logger.warning("自動讀取遊戲內角色名稱失敗。")
        finally:
            try:
                role_id_auto_read_id = window.after(
                    1000,
                    auto_read_missing_role_id,
                )
            except TclError:
                role_id_auto_read_id = None

    auto_read_missing_role_id()

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
        if farm_timer_service is not None:
            result = farm_timer_service.handle_action(card_id, action_id)
            if result is not None:
                return result
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
    activity_progress_monitor = (
        ActivityProgressMonitor(
            activity_progress_service,
            window.after,
            window.after_cancel,
        )
        if activity_progress_service is not None
        else None
    )
    if activity_progress_monitor is not None:
        start_service(activity_progress_monitor)
    farm_timer_monitor = (
        FarmTimerMonitor(
            farm_timer_service,
            window.after,
            window.after_cancel,
        )
        if farm_timer_service is not None
        else None
    )
    if farm_timer_monitor is not None:
        start_service(farm_timer_monitor)
    confirmed_activity_rule_monitor = (
        ConfirmedActivityRuleMonitor(
            confirmed_activity_rule_service,
            window.after,
            window.after_cancel,
        )
        if confirmed_activity_rule_service is not None
        else None
    )
    if confirmed_activity_rule_monitor is not None:
        start_service(confirmed_activity_rule_monitor)

    reconnect_status_refresh_id: str | None = None

    def refresh_reconnect_status() -> None:
        nonlocal reconnect_status_refresh_id
        home_view.set_smart_reconnect_runtime_status(
            smart_reconnect_monitor.runtime_status
            if smart_reconnect_monitor is not None else None
        )
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

    def stop_all_automation_from_tray() -> bool:
        stopped = stop_group_automation_for_configuration_change()
        if group_window_launch_service is not None:
            launch_stopped = stop_service(
                group_window_launch_service,
                timeout_seconds=1.0,
            )
            stopped = stopped and launch_stopped.success
        if logger is not None:
            logger.info(
                "System tray stop-all completed; "
                f"stopped={stopped}"
            )
        return stopped

    def stop_complete_background_services() -> tuple[str, ...]:
        sync_session_state["enabled"] = False
        failures: list[str] = []

        def stop_named(name: str, service, **kwargs) -> None:
            if service is None:
                return
            result = stop_service(service, **kwargs)
            if not result.success:
                failures.append(name)

        stop_named(
            "group_window_launch",
            group_window_launch_service,
            timeout_seconds=1.0,
        )
        stop_named("feature_hotkey", feature_hotkey_monitor)
        stop_named("group_launch_hotkey", group_launch_hotkey_monitor)
        if not auto_click_service.close(timeout_seconds=1.0):
            failures.append("auto_click")
        stop_named("timed_click", game_time_timed_click_service)
        stop_named("player_habit", player_habit_reminder_monitor)
        stop_named(
            "group_role_status",
            group_role_status_monitor,
            timeout_seconds=1.0,
        )
        stop_named(
            "deferred_sync",
            deferred_sync_monitor,
            timeout_seconds=1.0,
        )
        stop_named("card_expiry", expiry_monitor)
        stop_named("activity_reminder", activity_reminder_monitor)
        stop_named("activity_progress", activity_progress_monitor)
        stop_named("farm_timer", farm_timer_monitor)
        stop_named(
            "confirmed_activity_rules",
            confirmed_activity_rule_monitor,
        )
        stop_named("card_overlay", overlay_runtime)
        stop_named("keyboard_sync", keyboard_sync_monitor)
        stop_named("mouse_sync", mouse_sync_monitor)
        stop_named(
            "smart_reconnect",
            smart_reconnect_monitor,
            timeout_seconds=1.0,
        )
        if not shutdown_sync_controllers(logger):
            failures.append("sync_dispatch")
        if not shutdown_event_subscriptions(logger):
            failures.append("event_subscriptions")
        return tuple(failures)

    def hide_window_to_tray() -> bool:
        if tray_controller is not None and tray_controller.running:
            tray_controller.hide()
            return True
        try:
            window.iconify()
        except TclError:
            return False
        return True

    def close_window() -> bool:
        nonlocal closing
        if closing:
            return False
        if not home_view.prepare_close():
            return False
        closing = True
        ui_callback_dispatcher.pause()
        failures = list(stop_complete_background_services())
        if reconnect_status_refresh_id is not None:
            try:
                window.after_cancel(reconnect_status_refresh_id)
            except TclError:
                pass
        if game_data_read_after_id is not None:
            try:
                window.after_cancel(game_data_read_after_id)
            except TclError:
                pass
        if role_id_auto_read_id is not None:
            try:
                window.after_cancel(role_id_auto_read_id)
            except TclError:
                pass
        if failures:
            closing = False
            ui_callback_dispatcher.resume()
            if card_service is not None:
                card_service.resync(home_view.refresh_cards)
            if logger is not None:
                logger.error(
                    "Complete exit was blocked because services remained active; "
                    f"services={','.join(failures)}"
                )
            messagebox.showerror(
                "輔｜無法完全退出",
                "部分背景服務尚未完全停止，程式仍保持開啟，沒有假裝已退出。",
                parent=window,
            )
            return False
        if tray_controller is not None and not tray_controller.stop(
            timeout_seconds=2.0
        ):
            closing = False
            ui_callback_dispatcher.resume()
            if card_service is not None:
                card_service.resync(home_view.refresh_cards)
            if logger is not None:
                logger.error(
                    "Complete exit was blocked because the system tray "
                    "thread remained active."
                )
            messagebox.showerror(
                "輔｜無法完全退出",
                "系統匣尚未完全停止，程式仍保持開啟，沒有假裝已退出。",
                parent=window,
            )
            return False
        if card_service is not None:
            card_service.unsubscribe(home_view.refresh_cards)
        if (
            event_subscription_scope is not None
            and not event_subscription_scope.close()
        ):
            closing = False
            ui_callback_dispatcher.resume()
            if card_service is not None:
                card_service.subscribe(home_view.refresh_cards)
                card_service.resync(home_view.refresh_cards)
            if logger is not None:
                logger.error(
                    "Main-window event subscriptions were not detached."
                )
            messagebox.showerror(
                "輔｜無法完全退出",
                "部分介面事件尚未停止，程式仍保持開啟，沒有假裝已退出。",
                parent=window,
            )
            return False
        ui_callback_dispatcher.close()
        home_view.dispose()
        if window_identity is not None:
            window_identity.clear()
        window.destroy()
        return True

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
        tooltip=f"輔｜{tray_group_name}",
        on_stop_all=stop_all_automation_from_tray,
        on_exit=close_window,
        operations_stopped=True,
    )
    if tray_controller.start():
        window.bind("<Unmap>", tray_controller.handle_unmap, add="+")
    elif logger is not None:
        logger.error(
            "System tray icon was not started; the main window remains usable."
        )
    window.protocol("WM_DELETE_WINDOW", hide_window_to_tray)
    window._card_overlay_runtime = overlay_runtime
    window._card_expiry_monitor = expiry_monitor
    window._activity_reminder_monitor = activity_reminder_monitor
    window._activity_progress_monitor = activity_progress_monitor
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


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def verify_background_image_runtime(
    source: Path,
    paths: PathManager,
) -> tuple[int, Path]:
    """Exercise the packaged decoder without opening or changing the GUI."""
    source = Path(source).resolve(strict=False)
    service = AppContext.get(BackgroundImageService)
    before_hash = _sha256_file(source)
    result = service.prepare(source)
    after_hash = _sha256_file(source)
    managed_copy_created = bool(
        result.succeeded
        and result.managed_path is not None
        and result.managed_path.is_file()
    )
    source_unchanged = bool(
        before_hash is not None
        and after_hash is not None
        and before_hash == after_hash
    )
    payload = {
        "passed": bool(
            result.succeeded
            and managed_copy_created
            and source_unchanged
        ),
        "source": str(source),
        "source_suffix": source.suffix.casefold(),
        "source_unchanged": source_unchanged,
        "managed_copy_created": managed_copy_created,
        "original_size": (
            list(result.original_size)
            if result.original_size is not None
            else None
        ),
        "message": result.message,
    }
    service.discard_prepared(
        result.managed_path if result.succeeded else None
    )
    report_path = (
        paths.data_dir() / BACKGROUND_IMAGE_VERIFY_REPORT_FILENAME
    )
    report_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return (0 if payload["passed"] else 5), report_path


def _run_application(
    *,
    self_check_only: bool = False,
    target_desktop_verify_only: bool = False,
    background_image_verify_path: Path | None = None,
    root: Path | None = None,
) -> int:
    paths: PathManager | None = None
    logger: LoggerService | None = None
    operation_record_store: SyncOperationRecordStore | None = None
    try:
        if (
            self_check_only
            or target_desktop_verify_only
            or background_image_verify_path is not None
        ):
            close_startup_splash()
        configure_process_app_identity()
        paths, logger = build_services(root=root)
        operation_record_store = AppContext.get(SyncOperationRecordStore)
        if background_image_verify_path is not None:
            exit_code, report_path = verify_background_image_runtime(
                background_image_verify_path,
                paths,
            )
            logger.info(
                "背景圖片執行期驗證"
                f"{'通過' if exit_code == 0 else '失敗'}；"
                f"報告={report_path}"
            )
            return exit_code
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

        ui_font_service = AppContext.get(UIFontService)
        if ui_font_service is not None:
            try:
                ui_font_result = ui_font_service.load_all()
            except Exception:
                ui_font_result = None
            if ui_font_result is None:
                logger.warning(
                    "離線介面字體載入發生未預期錯誤，已使用系統預設字體。"
                )
            elif not ui_font_result.success:
                logger.warning(
                    f"{ui_font_result.message} 原因代碼={ui_font_result.code}"
                )

        window = create_main_window(status, paths)
        close_startup_splash()
        window.mainloop()
        logger.info(f"FLASH {MILESTONE} closed normally.")
        return 0
    except Exception as exc:
        close_startup_splash()
        details = traceback.format_exc()
        if logger is not None:
            logger.error(f"FLASH startup failed: {exc}\n{details}")
        else:
            fallback = Path.home() / "FLASH_startup_error.log"
            try:
                fallback.write_text(details, encoding="utf-8")
            except OSError:
                pass

        if (
            self_check_only
            or target_desktop_verify_only
            or background_image_verify_path is not None
        ):
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
            shutdown_server_time_services()
        except Exception:
            if logger is not None:
                logger.error(
                    "Server time bridge shutdown failed:\n"
                    f"{traceback.format_exc()}"
                )
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
                            try:
                                shutdown_ui_font_service(logger)
                            finally:
                                close_operation_record_store(
                                    operation_record_store,
                                    logger,
                                )
                                close_logger(logger)


def run(
    *,
    self_check_only: bool = False,
    target_desktop_verify_only: bool = False,
    background_image_verify_path: Path | None = None,
    root: Path | None = None,
) -> int:
    ordinary_ui = not (
        self_check_only
        or target_desktop_verify_only
        or background_image_verify_path is not None
    )
    if not ordinary_ui:
        try:
            return _run_application(
                self_check_only=self_check_only,
                target_desktop_verify_only=target_desktop_verify_only,
                background_image_verify_path=background_image_verify_path,
                root=root,
            )
        finally:
            close_startup_splash()
    try:
        instance_lock = acquire_main_instance_lock()
    except OSError:
        close_startup_splash()
        return 1
    if instance_lock is None:
        close_startup_splash()
        return 0
    try:
        return _run_application(root=root)
    finally:
        close_startup_splash()
        instance_lock.release()


def close_operation_record_store(
    store: SyncOperationRecordStore | None,
    logger: LoggerService | None,
) -> None:
    """Flush queued hot-path records before the process exits."""
    if store is None:
        return
    try:
        closed = store.close()
    except Exception:
        closed = False
    if not closed and logger is not None:
        logger.error(
            "Operation record store final flush failed; "
            f"code={store.persistence_failure or 'unknown'}"
        )


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
    raw_arguments = tuple(sys.argv[1:])
    arguments = set(raw_arguments)
    target_desktop_verify_only = TARGET_DESKTOP_VERIFY_ARGUMENT in arguments
    background_image_verify_path = next(
        (
            Path(argument[len(BACKGROUND_IMAGE_VERIFY_ARGUMENT_PREFIX) :])
            for argument in raw_arguments
            if argument.startswith(BACKGROUND_IMAGE_VERIFY_ARGUMENT_PREFIX)
            and argument[len(BACKGROUND_IMAGE_VERIFY_ARGUMENT_PREFIX) :]
        ),
        None,
    )
    raise SystemExit(
        run(
            self_check_only=(
                SELF_CHECK_ARGUMENT in arguments
                and not target_desktop_verify_only
                and background_image_verify_path is None
            ),
            target_desktop_verify_only=target_desktop_verify_only,
            background_image_verify_path=background_image_verify_path,
        )
    )


if __name__ == "__main__":
    main()
