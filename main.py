"""Cumulative FLASH desktop entrypoint."""

from __future__ import annotations

import ctypes
import json
import sys
import traceback
from pathlib import Path
from tkinter import PhotoImage, TclError, Tk, messagebox

from adapters.background_capability import BackgroundCapabilityProbe
from adapters.windows_background_capture import WindowsBackgroundCaptureBackend
from adapters.windows_input_sync import (
    WindowInputPolicy,
    WindowsInputSyncController,
    normalize_input_policy,
)
from adapters.windows_launch_fingerprint import normalize_launch_fingerprint
from adapters.windows_smart_reconnect import WindowsSmartReconnectController
from adapters.windows_target_desktop_verifier import TargetDesktopVerifier
from adapters.windows_window import WindowsWindowAdapter
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
from core.target_window_observation import TargetWindowObservation
from core.window_registry import WindowRegistry
from core.version import MILESTONE
from core.window_registry_store import WindowRegistryStore
from domain.activity_schedule import (
    ActivityScheduleCatalog,
    build_confirmed_activity_catalog,
)
from domain.character_store import CharacterStore
from domain.progress_store import ActivityProgressStore
from domain.soul_stone_store import SoulStoneStore
from services.activity_progress_service import ActivityProgressService
from services.app_context import AppContext
from services.card_coordinator import CardCoordinator
from services.card_display_settings_service import CardDisplaySettingsService
from services.card_history_service import CardHistoryService
from services.card_view_state_service import CardViewStateService
from services.character_detail_view_service import CharacterDetailViewService
from services.character_view_service import CharacterViewService
from services.event_bus import EventBus
from services.logger_service import LoggerService
from services.smart_reconnect_monitor import SmartReconnectMonitor
from services.soul_stone_service import SoulStoneService
from services.target_window_state_service import (
    TARGET_WINDOW_OBSERVED_EVENT,
    TargetWindowStateService,
)
from ui.home import HomeView
from workspace.service import WorkspaceService

APP_TITLE = "輔"
SELF_CHECK_ARGUMENT = "--self-check"
TARGET_DESKTOP_VERIFY_ARGUMENT = "--verify-target-desktop"
TARGET_WINDOW_KEY = "target_window_keywords"
TARGET_WINDOW_FINGERPRINT_KEY = "target_window_fingerprint"
INPUT_POLICY_KEY = "input_policy"
SMART_RECONNECT_ENABLED_KEY = "smart_reconnect_enabled"
REGISTRY_FILENAME = "window_registry.json"
RECONNECT_STATE_FILENAME = "smart_reconnect_state.json"
TARGET_DESKTOP_REPORT_FILENAME = "target_desktop_verification.json"
CHARACTER_FILENAME = "characters.json"
SOUL_STONE_FILENAME = "soul_stones.json"
ACTIVITY_PROGRESS_FILENAME = "activity_progress.json"
CARD_HISTORY_FILENAME = "card_history.json"
APP_ICON_PNG = Path("assets") / "flash_icon.png"
APP_ICON_ICO = Path("assets") / "flash_icon.ico"
RECONNECT_REFERENCE_DIR = Path("assets") / "reconnect_reference"
WINDOWS_APP_USER_MODEL_ID = "limaple0324.FLASH"


def resource_path(relative_path: Path) -> Path:
    """Resolve files both from source and from a PyInstaller bundle."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root) / relative_path
    return Path(__file__).resolve().parent / relative_path


def apply_windows_app_identity() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        pass


def apply_window_icon(window: Tk) -> None:
    png_path = resource_path(APP_ICON_PNG)
    if png_path.exists():
        try:
            icon = PhotoImage(file=str(png_path))
            window.iconphoto(True, icon)
            window._flash_icon = icon
        except TclError:
            pass

    ico_path = resource_path(APP_ICON_ICO)
    if sys.platform == "win32" and ico_path.exists():
        try:
            window.iconbitmap(default=str(ico_path))
        except TclError:
            pass


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
            SMART_RECONNECT_ENABLED_KEY: True,
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
    soul_stone_store = SoulStoneStore(paths.data_dir() / SOUL_STONE_FILENAME)
    soul_stone_service = SoulStoneService(soul_stone_store)
    character_detail_view_service = CharacterDetailViewService(
        character_view_service,
        soul_stone_service,
    )
    progress_store = ActivityProgressStore(
        paths.data_dir() / ACTIVITY_PROGRESS_FILENAME
    )
    progress_service = ActivityProgressService(progress_store)
    activity_schedule_catalog = build_confirmed_activity_catalog()
    for rule in activity_schedule_catalog.all():
        progress_service.register_definition(rule.definition)
    workspace_service = WorkspaceService()
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
    card_view_state_service = CardViewStateService(card_service)

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
    AppContext.register(SoulStoneStore, soul_stone_store)
    AppContext.register(SoulStoneService, soul_stone_service)
    AppContext.register(CharacterDetailViewService, character_detail_view_service)
    AppContext.register(ActivityProgressStore, progress_store)
    AppContext.register(ActivityProgressService, progress_service)
    AppContext.register(ActivityScheduleCatalog, activity_schedule_catalog)
    AppContext.register(WorkspaceService, workspace_service)
    AppContext.register(CardHistoryStore, card_history_store)
    AppContext.register(CardHistoryService, card_history_service)
    AppContext.register(CardService, card_service)
    AppContext.register(CardDisplaySettingsService, card_display_settings_service)
    AppContext.register(CardCoordinator, card_coordinator)
    AppContext.register(CardViewStateService, card_view_state_service)
    AppContext.register(
        WindowsInputSyncController,
        WindowsInputSyncController.for_real_windows(),
    )
    reconnect_controller = WindowsSmartReconnectController.for_real_windows(
        reference_dir=resource_path(RECONNECT_REFERENCE_DIR),
        state_path=paths.data_dir() / RECONNECT_STATE_FILENAME,
    )
    AppContext.register(
        WindowsSmartReconnectController,
        reconnect_controller,
    )
    AppContext.register(SmartReconnectBoundary, reconnect_controller)
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

    if soul_stone_store.recovered_from_corruption:
        logger.warning(
            "Soul stone records were corrupt and have been isolated; "
            f"backup={soul_stone_store.corrupt_backup}"
        )
    else:
        logger.info(f"Soul stone records loaded: {len(soul_stone_service.all())}.")

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
        "同步按鍵不會自行送出；目前只允許玩家明確執行 B／C 同步測試。\n"
        "智慧重連只依已確認畫面自動監看，未知畫面不會點擊。\n"
        f"紀錄位置：{paths.logs_dir()}"
    )


def create_main_window(status: dict[str, object], paths: PathManager) -> Tk:
    window = Tk()
    window.title(APP_TITLE)
    apply_window_icon(window)
    window.geometry("760x760")
    window.minsize(660, 600)

    def show_start_status() -> None:
        messagebox.showinfo(
            "輔｜目前狀態",
            format_start_status(status, paths),
            parent=window,
        )

    config = AppContext.get(ConfigManager)
    input_controller = AppContext.get(WindowsInputSyncController)
    logger = AppContext.get(LoggerService)
    configured_policy = (
        normalize_input_policy(config.get(INPUT_POLICY_KEY))
        if config is not None
        else WindowInputPolicy.ALL
    ) or WindowInputPolicy.ALL

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

    def test_approved_key(key: str) -> None:
        policy = (
            normalize_input_policy(config.get(INPUT_POLICY_KEY))
            if config is not None
            else None
        )
        if input_controller is None or policy is None:
            messagebox.showerror(
                "輔｜同步輸入測試",
                "同步輸入尚未正確設定，沒有送出任何按鍵。",
                parent=window,
            )
            return
        result = input_controller.send_approved_key(
            key,
            policy=policy,
            execute=True,
        )
        if logger is not None:
            logger.info(
                "Approved input test completed; "
                f"key={result.approved_key}; policy={result.policy}; "
                f"eligible={result.eligible_windows}; sent={result.sent_windows}; "
                f"failures={','.join(result.failure_codes) or 'none'}"
            )
        if result.passed:
            messagebox.showinfo(
                "輔｜同步輸入測試",
                f"{key} 已送達 {result.sent_windows} 個已驗證遊戲視窗。",
                parent=window,
            )
        else:
            messagebox.showerror(
                "輔｜同步輸入測試",
                "同步輸入未完整送達；已停止後續操作。\n"
                f"符合權限：{result.eligible_windows}\n"
                f"成功送達：{result.sent_windows}\n"
                f"錯誤代碼：{', '.join(result.failure_codes) or 'unknown'}",
                parent=window,
            )

    HomeView(
        window,
        status,
        on_start=show_start_status,
        input_policy=configured_policy.value,
        on_input_policy_change=change_input_policy,
        on_test_key=test_approved_key,
    ).build()
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
        apply_windows_app_identity()
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
        status["smart_reconnect_enabled"] = bool(
            config.get(SMART_RECONNECT_ENABLED_KEY, True)
            if config is not None
            else True
        )
        write_self_check_report(status, paths)

        if self_check_only:
            return 0 if bool(status.get("self_check_passed", False)) else 2

        monitor = AppContext.get(SmartReconnectMonitor)
        if status["smart_reconnect_enabled"] and monitor is not None:
            monitor.start()
            logger.info(
                "Smart reconnect monitor enabled with confirmed screen templates."
            )

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
