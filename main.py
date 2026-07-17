"""FLASH SP1 desktop entrypoint."""

from __future__ import annotations

import ctypes
import json
import sys
import traceback
from pathlib import Path
from tkinter import PhotoImage, TclError, Tk, messagebox
from typing import Protocol

from adapters.background_capability import BackgroundCapabilityProbe
from adapters.windows_background_capture import WindowsBackgroundCaptureBackend
from adapters.windows_work_area import WindowsWorkAreaReader
from adapters.windows_window import WindowsWindowAdapter
from cards.history_store import CardHistoryStore
from cards.service import CardService
from cards.view_state import CardViewState
from config.config_manager import ConfigManager
from config.path_manager import PathManager
from core.bootstrap import Bootstrap
from core.sp1_boundaries import ExternalAdapter
from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from domain.progress_store import ActivityProgressStore
from product.identity import PRODUCT_NAME
from services.activity_progress_service import ActivityProgressService
from services.app_context import AppContext
from services.card_history_service import CardHistoryService
from services.card_coordinator import CardCoordinator
from services.card_expiry_monitor import CardExpiryMonitor
from services.card_overlay_selection_assembly import (
    build_windows_card_overlay_selection_coordinator,
)
from services.card_preview_selection_service import CardPreviewSelectionService
from services.card_preview_selection_store import CardPreviewSelectionStore
from services.card_view_state_service import CardViewStateService
from services.event_bus import EventBus
from services.logger_service import LoggerService
from ui.home import HomeView
from ui.card_preview_settings import CardPreviewCatalog

APP_TITLE = PRODUCT_NAME
SELF_CHECK_ARGUMENT = "--self-check"
TARGET_WINDOW_KEY = "target_window_keywords"
REGISTRY_FILENAME = "window_registry.json"
ACTIVITY_PROGRESS_FILENAME = "activity_progress.json"
CARD_HISTORY_FILENAME = "card_history.json"
CARD_PREVIEW_SELECTION_FILENAME = "card_preview_selection.json"
APP_ICON_PNG = Path("assets") / "flash_icon.png"
APP_ICON_ICO = Path("assets") / "flash_icon.ico"
WINDOWS_APP_USER_MODEL_ID = "limaple0324.FLASH"


class CardOverlayRuntime(Protocol):
    """Optional reminder overlay lifecycle managed by the main window."""

    def start(self) -> None: ...

    def stop(self) -> None: ...


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


def build_services(
    root: Path | None = None,
    *,
    card_preview_catalog: CardPreviewCatalog | None = None,
):
    """Create, load, and register all SP1 services."""
    AppContext.clear()
    paths = PathManager(root=root)
    logger = LoggerService(paths.log_file("flash.log"))
    config = ConfigManager(paths.config_file("settings.json"))
    event_bus = EventBus(logger=logger)

    registry_store = WindowRegistryStore(paths.data_dir() / REGISTRY_FILENAME)
    registry = registry_store.load()
    progress_store = ActivityProgressStore(paths.data_dir() / ACTIVITY_PROGRESS_FILENAME)
    progress_service = ActivityProgressService(progress_store)
    card_history_store = CardHistoryStore(paths.data_dir() / CARD_HISTORY_FILENAME)
    card_history_service = CardHistoryService(card_history_store)
    card_service = CardService()
    card_coordinator = CardCoordinator(card_service, card_history_service)
    card_view_state_service = CardViewStateService(card_service)

    AppContext.register(PathManager, paths)
    AppContext.register(LoggerService, logger)
    AppContext.register(ConfigManager, config)
    AppContext.register(EventBus, event_bus)
    AppContext.register(WindowRegistryStore, registry_store)
    AppContext.register(WindowRegistry, registry)
    AppContext.register(ActivityProgressStore, progress_store)
    AppContext.register(ActivityProgressService, progress_service)
    AppContext.register(CardHistoryStore, card_history_store)
    AppContext.register(CardHistoryService, card_history_service)
    AppContext.register(CardService, card_service)
    AppContext.register(CardCoordinator, card_coordinator)
    AppContext.register(CardViewStateService, card_view_state_service)

    if card_preview_catalog is not None:
        card_preview_selection_store = CardPreviewSelectionStore(
            paths.data_dir() / CARD_PREVIEW_SELECTION_FILENAME
        )
        card_preview_selection_service = CardPreviewSelectionService(
            card_preview_catalog,
            card_preview_selection_store,
        )
        AppContext.register(CardPreviewSelectionStore, card_preview_selection_store)
        AppContext.register(CardPreviewSelectionService, card_preview_selection_service)

        if card_preview_selection_store.recovered_from_corruption:
            logger.warning(
                "Card preview selection was corrupt and has been disabled; "
                f"backup={card_preview_selection_store.corrupt_backup}"
            )
        elif card_preview_selection_service.unavailable_stored_profile_id is not None:
            logger.warning(
                "Card preview selection references an unavailable profile and has been "
                "disabled; "
                f"profile={card_preview_selection_service.unavailable_stored_profile_id}"
            )

    if registry_store.recovered_from_corruption:
        logger.warning(
            "Character window registry was corrupt and has been rebuilt; "
            f"backup={registry_store.corrupt_backup}"
        )
    else:
        logger.info(f"Character window registry loaded: {len(registry.all())} character(s).")

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

    keywords = _normalize_window_keywords(config.get(TARGET_WINDOW_KEY, []))
    if keywords:
        AppContext.register(ExternalAdapter, WindowsWindowAdapter(title_keywords=keywords))

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
    """Turn overlay self-check diagnostics into a concise player-facing summary."""
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


def format_window_status(status: dict[str, object]) -> str:
    item = status.get("target_window", {})
    if not isinstance(item, dict):
        return "主視窗狀態：無法取得；操作保持停用。"
    safe = bool(item.get("safe", False))
    return (
        f"{'✓' if safe else '—'} 主視窗："
        f"{'可安全辨識（仍未啟用輸入）' if safe else '不可操作'}\n"
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
    lines.append("背景輸入目前仍為停用。")
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


def _attach_card_overlay_runtime(window: Tk, runtime: CardOverlayRuntime | None) -> None:
    if runtime is None:
        return

    closed = False

    def close_window() -> None:
        nonlocal closed
        if closed:
            return
        closed = True
        try:
            runtime.stop()
        except Exception as exc:
            window._card_overlay_stop_error = exc
        finally:
            window.destroy()

    window.protocol("WM_DELETE_WINDOW", close_window)
    window._card_overlay_runtime = runtime
    runtime.start()


def _build_registered_card_overlay_runtime(
    window: Tk,
) -> CardOverlayRuntime | None:
    """Build the overlay only when an explicit preview catalog was registered."""
    selection = AppContext.get(CardPreviewSelectionService)
    cards = AppContext.get(CardService)
    card_state = AppContext.get(CardViewStateService)
    if selection is None or cards is None or card_state is None:
        return None

    return build_windows_card_overlay_selection_coordinator(
        window,
        cards,
        selection,
        card_state,
        WindowsWorkAreaReader(),
    )


def create_main_window(
    status: dict[str, object],
    paths: PathManager,
    *,
    card_overlay_runtime: CardOverlayRuntime | None = None,
) -> Tk:
    window = Tk()
    window.title(APP_TITLE)
    apply_window_icon(window)
    window.geometry("760x760")
    window.minsize(660, 600)

    def show_start_status() -> None:
        messagebox.showinfo(
            "輔｜目前狀態",
            (
                "目前可以查看狀態與紀錄。\n\n"
                "遊戲操作尚未啟用，輔不會自動點擊或控制遊戲。\n"
                f"{format_card_overlay_status(status)}\n"
                f"紀錄位置：{paths.logs_dir()}"
            ),
            parent=window,
        )

    card_view_state_service = AppContext.get(CardViewStateService)
    card_preview_selection_service = AppContext.get(CardPreviewSelectionService)
    home_view = HomeView(
        window,
        status,
        on_start=show_start_status,
        card_view_state=CardViewState(),
        card_view_state_provider=(
            card_view_state_service.snapshot
            if card_view_state_service is not None
            else None
        ),
        card_preview_choices_provider=(
            card_preview_selection_service.available_choices
            if card_preview_selection_service is not None
            else None
        ),
        on_card_preview_select=(
            card_preview_selection_service.select
            if card_preview_selection_service is not None
            else None
        ),
    )
    home_view.build()
    window._home_view = home_view
    card_service = AppContext.get(CardService)
    if card_service is not None:
        card_service.subscribe(lambda: window.after_idle(home_view.refresh_cards))
        card_expiry_monitor = CardExpiryMonitor(card_service, window.after)
        card_expiry_monitor.start()
        window._card_expiry_monitor = card_expiry_monitor
    if card_overlay_runtime is None:
        card_overlay_runtime = _build_registered_card_overlay_runtime(window)
    _attach_card_overlay_runtime(window, card_overlay_runtime)
    return window


def write_self_check_report(status: dict[str, object], paths: PathManager) -> Path:
    report_path = paths.data_dir() / "self_check.json"
    report_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def run(
    *,
    self_check_only: bool = False,
    root: Path | None = None,
    card_preview_catalog: CardPreviewCatalog | None = None,
) -> int:
    paths: PathManager | None = None
    logger: LoggerService | None = None
    try:
        apply_windows_app_identity()
        paths, logger = build_services(
            root=root,
            card_preview_catalog=card_preview_catalog,
        )
        status = Bootstrap(context=AppContext).start()
        status["window_registry"] = registry_status()
        status["target_window"] = detect_target_window()
        status["background_capabilities"] = detect_background_capabilities()
        write_self_check_report(status, paths)

        if self_check_only:
            return 0 if bool(status.get("self_check_passed", False)) else 2

        window = create_main_window(status, paths)
        window.mainloop()
        logger.info("FLASH SP1 closed normally.")
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

        if self_check_only:
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
            save_registry(logger)
        except Exception:
            if logger is not None:
                logger.error(f"Registry final save failed:\n{traceback.format_exc()}")


def main() -> None:
    raise SystemExit(run(self_check_only=SELF_CHECK_ARGUMENT in sys.argv[1:]))


if __name__ == "__main__":
    main()
