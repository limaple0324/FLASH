"""FLASH SP1 desktop entrypoint."""

from __future__ import annotations

import json
import sys
import traceback
from pathlib import Path
from tkinter import BOTH, LEFT, X, Button, Frame, Label, Tk, messagebox

from adapters.background_capability import BackgroundCapabilityProbe
from adapters.windows_background_capture import WindowsBackgroundCaptureBackend
from adapters.windows_window import WindowsWindowAdapter
from config.config_manager import ConfigManager
from config.path_manager import PathManager
from core.bootstrap import Bootstrap
from core.sp1_boundaries import ExternalAdapter
from core.window_registry import WindowRegistry
from core.window_registry_store import WindowRegistryStore
from services.app_context import AppContext
from services.event_bus import EventBus
from services.logger_service import LoggerService

APP_TITLE = "輔｜FLASH SP1"
SELF_CHECK_ARGUMENT = "--self-check"
TARGET_WINDOW_KEY = "target_window_keywords"
REGISTRY_FILENAME = "window_registry.json"


def _normalize_window_keywords(value: object) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def build_services(root: Path | None = None):
    """Create, load, and register all SP1 services."""
    AppContext.clear()
    paths = PathManager(root=root)
    logger = LoggerService(paths.log_file("flash.log"))
    config = ConfigManager(paths.config_file("settings.json"))
    event_bus = EventBus(logger=logger)

    registry_store = WindowRegistryStore(paths.data_dir() / REGISTRY_FILENAME)
    registry = registry_store.load()

    AppContext.register(PathManager, paths)
    AppContext.register(LoggerService, logger)
    AppContext.register(ConfigManager, config)
    AppContext.register(EventBus, event_bus)
    AppContext.register(WindowRegistryStore, registry_store)
    AppContext.register(WindowRegistry, registry)

    if registry_store.recovered_from_corruption:
        logger.warning(
            "Character window registry was corrupt and has been rebuilt; "
            f"backup={registry_store.corrupt_backup}"
        )
    else:
        logger.info(f"Character window registry loaded: {len(registry.all())} character(s).")

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
        return "角色註冊表：未載入。"
    count = int(item.get("count", 0))
    recovered = bool(item.get("recovered", False))
    message = f"角色註冊表：已載入 {count} 個角色。"
    if recovered:
        message += " 本次已從損壞狀態重建。"
    return message + "\n舊 Handle 不會在重開後直接視為有效。"


def create_main_window(status: dict[str, object], paths: PathManager) -> Tk:
    window = Tk()
    window.title(APP_TITLE)
    window.geometry("760x760")
    window.minsize(660, 600)
    body = Frame(window, padx=28, pady=24)
    body.pack(fill=BOTH, expand=True)

    headline, details = format_self_check(status)
    passed = bool(status.get("self_check_passed", False))
    Label(body, text="輔", font=("Microsoft JhengHei UI", 24, "bold"), anchor="w").pack(fill=X)
    Label(body, text=f"FLASH SP1 已啟動｜{headline}", font=("Microsoft JhengHei UI", 15, "bold"), anchor="w", pady=8).pack(fill=X)
    Label(body, text=f"版本：{status.get('version', 'unknown')}\n階段：{status.get('sprint', 'SP1')}\n整體狀態：{'核心檢查正常' if passed else '需要檢查下列失敗項目'}", font=("Microsoft JhengHei UI", 10), justify=LEFT, anchor="nw").pack(fill=X)

    for title, text in (
        ("角色註冊表", format_registry_status(status)),
        ("遊戲主視窗（只讀偵測）", format_window_status(status)),
        ("背景能力（不送出輸入）", format_background_status(status)),
    ):
        Label(body, text=title, font=("Microsoft JhengHei UI", 11, "bold"), anchor="w", pady=(14, 4)).pack(fill=X)
        Label(body, text=text, font=("Microsoft JhengHei UI", 10), justify=LEFT, anchor="nw", wraplength=690).pack(fill=X)

    Label(body, text="核心檢查明細", font=("Microsoft JhengHei UI", 11, "bold"), anchor="w", pady=(14, 4)).pack(fill=X)
    Label(body, text=details, font=("Consolas", 9), justify=LEFT, anchor="nw", wraplength=690).pack(fill=BOTH, expand=True)

    footer = Frame(body)
    footer.pack(side="bottom", fill=X, pady=(16, 0))
    Button(footer, text="關閉", width=12, command=window.destroy).pack(side="right")
    Label(footer, text=f"紀錄位置：{paths.logs_dir()}", font=("Microsoft JhengHei UI", 8), anchor="w").pack(side="left")
    return window


def write_self_check_report(status: dict[str, object], paths: PathManager) -> Path:
    report_path = paths.data_dir() / "self_check.json"
    report_path.write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def run(*, self_check_only: bool = False, root: Path | None = None) -> int:
    paths: PathManager | None = None
    logger: LoggerService | None = None
    try:
        paths, logger = build_services(root=root)
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
            messagebox.showerror("輔｜啟動失敗", f"FLASH 無法啟動。錯誤已寫入紀錄檔。\n\n原因：{exc}", parent=root_window)
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
