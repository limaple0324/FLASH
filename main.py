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
from services.app_context import AppContext
from services.event_bus import EventBus
from services.logger_service import LoggerService

APP_TITLE = "輔｜FLASH SP1"
SELF_CHECK_ARGUMENT = "--self-check"
TARGET_WINDOW_KEY = "target_window_keywords"


def _normalize_window_keywords(value: object) -> list[str]:
    """Return a clean keyword list from persisted user configuration."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def build_services(root: Path | None = None):
    """Create and register SP1 services."""
    AppContext.clear()
    paths = PathManager(root=root)
    logger = LoggerService(paths.log_file("flash.log"))
    config = ConfigManager(paths.config_file("settings.json"))
    event_bus = EventBus(logger=logger)

    AppContext.register(PathManager, paths)
    AppContext.register(LoggerService, logger)
    AppContext.register(ConfigManager, config)
    AppContext.register(EventBus, event_bus)

    keywords = _normalize_window_keywords(config.get(TARGET_WINDOW_KEY, []))
    if keywords:
        adapter = WindowsWindowAdapter(title_keywords=keywords)
        AppContext.register(ExternalAdapter, adapter)

    return paths, logger


def detect_target_window() -> dict[str, object]:
    """Run one read-only target-window safety check."""
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
    """Probe read-only background capture after a unique target is selected."""
    adapter = AppContext.get(ExternalAdapter)
    handle = None
    if isinstance(adapter, WindowsWindowAdapter) and adapter.last_match is not None:
        handle = adapter.last_match.handle

    probe = BackgroundCapabilityProbe(WindowsBackgroundCaptureBackend())
    report = probe.run(handle)
    return report.to_dict()


def _self_check_items(status: dict[str, object]) -> list[dict[str, object]]:
    """Normalize current and legacy self-check report shapes."""
    report = status.get("self_check", [])
    if isinstance(report, dict):
        report = report.get("checks", [])
    if not isinstance(report, list):
        return []
    return [item for item in report if isinstance(item, dict)]


def format_self_check(status: dict[str, object]) -> tuple[str, str]:
    """Return a user-facing summary and detail text for the SP1 self-check."""
    passed = bool(status.get("self_check_passed", False))
    checks = _self_check_items(status)

    lines: list[str] = []
    for item in checks:
        name = str(item.get("name", "unknown"))
        item_passed = bool(item.get("passed", False))
        message = str(item.get("message", ""))
        mark = "✓" if item_passed else "✗"
        lines.append(f"{mark} {name}：{message}")

    if not lines:
        lines.append("✗ self_check：沒有取得檢查結果。")
        passed = False

    headline = "自我檢查通過" if passed else "自我檢查發現問題"
    return headline, "\n".join(lines)


def format_window_status(status: dict[str, object]) -> str:
    """Return a concise, safety-first target-window summary."""
    window_status = status.get("target_window", {})
    if not isinstance(window_status, dict):
        return "主視窗狀態：無法取得；操作保持停用。"

    safe = bool(window_status.get("safe", False))
    code = str(window_status.get("code", "window.unknown"))
    message = str(window_status.get("message", ""))
    mark = "✓" if safe else "—"
    safety = "可安全辨識（仍未啟用輸入）" if safe else "不可操作"
    return f"{mark} 主視窗：{safety}\n代碼：{code}\n說明：{message}"


def format_background_status(status: dict[str, object]) -> str:
    """Return the current background capability result in user-facing language."""
    report = status.get("background_capabilities", {})
    if not isinstance(report, dict):
        return "背景能力：無法取得。"
    capabilities = report.get("capabilities", {})
    if not isinstance(capabilities, dict):
        return "背景能力：尚未測試。"

    labels = {
        "background_capture": "被遮擋時讀取畫面",
        "background_input": "非前景背景操作",
        "minimized_input": "最小化背景操作",
    }
    state_text = {
        "supported": "支援",
        "unsupported": "不支援",
        "unknown": "無法確認",
        "untested": "尚未測試",
        "error": "測試錯誤",
    }
    lines = []
    for key, label in labels.items():
        item = capabilities.get(key, {})
        state = str(item.get("state", "unknown")) if isinstance(item, dict) else "unknown"
        lines.append(f"{label}：{state_text.get(state, state)}")
    lines.append("背景輸入目前仍為停用。")
    return "\n".join(lines)


def create_main_window(status: dict[str, object], paths: PathManager) -> Tk:
    """Create the persistent SP1 verification window."""
    window = Tk()
    window.title(APP_TITLE)
    window.geometry("760x700")
    window.minsize(660, 560)

    body = Frame(window, padx=28, pady=24)
    body.pack(fill=BOTH, expand=True)

    headline, details = format_self_check(status)
    passed = bool(status.get("self_check_passed", False))

    Label(body, text="輔", font=("Microsoft JhengHei UI", 24, "bold"), anchor="w").pack(fill=X)
    Label(
        body,
        text=f"FLASH SP1 已啟動｜{headline}",
        font=("Microsoft JhengHei UI", 15, "bold"),
        anchor="w",
        pady=8,
    ).pack(fill=X)
    Label(
        body,
        text=(
            f"版本：{status.get('version', 'unknown')}\n"
            f"階段：{status.get('sprint', 'SP1')}\n"
            f"整體狀態：{'核心檢查正常' if passed else '需要檢查下列失敗項目'}"
        ),
        font=("Microsoft JhengHei UI", 10),
        justify=LEFT,
        anchor="nw",
    ).pack(fill=X)

    Label(
        body,
        text="遊戲主視窗（只讀偵測）",
        font=("Microsoft JhengHei UI", 11, "bold"),
        anchor="w",
        pady=(16, 4),
    ).pack(fill=X)
    Label(
        body,
        text=format_window_status(status),
        font=("Microsoft JhengHei UI", 10),
        justify=LEFT,
        anchor="nw",
        wraplength=690,
    ).pack(fill=X)

    Label(
        body,
        text="背景能力（不送出輸入）",
        font=("Microsoft JhengHei UI", 11, "bold"),
        anchor="w",
        pady=(16, 4),
    ).pack(fill=X)
    Label(
        body,
        text=format_background_status(status),
        font=("Microsoft JhengHei UI", 10),
        justify=LEFT,
        anchor="nw",
        wraplength=690,
    ).pack(fill=X)

    Label(
        body,
        text="核心檢查明細",
        font=("Microsoft JhengHei UI", 11, "bold"),
        anchor="w",
        pady=(16, 4),
    ).pack(fill=X)
    Label(
        body,
        text=details,
        font=("Consolas", 9),
        justify=LEFT,
        anchor="nw",
        wraplength=690,
    ).pack(fill=BOTH, expand=True)

    footer = Frame(body)
    footer.pack(side="bottom", fill=X, pady=(16, 0))
    Button(footer, text="關閉", width=12, command=window.destroy).pack(side="right")
    Label(
        footer,
        text=f"紀錄位置：{paths.logs_dir()}",
        font=("Microsoft JhengHei UI", 8),
        anchor="w",
    ).pack(side="left")
    return window


def write_self_check_report(status: dict[str, object], paths: PathManager) -> Path:
    """Persist a machine-readable report for packaged and CI verification."""
    report_path = paths.data_dir() / "self_check.json"
    report_path.write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return report_path


def run(*, self_check_only: bool = False, root: Path | None = None) -> int:
    paths: PathManager | None = None
    logger: LoggerService | None = None
    try:
        paths, logger = build_services(root=root)
        status = Bootstrap(context=AppContext).start()
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
            messagebox.showerror(
                "輔｜啟動失敗",
                "FLASH 無法啟動。錯誤已寫入紀錄檔。\n\n"
                f"原因：{exc}",
                parent=root_window,
            )
            root_window.destroy()
        except Exception:
            print(details, file=sys.stderr)
        return 1


def main() -> None:
    self_check_only = SELF_CHECK_ARGUMENT in sys.argv[1:]
    raise SystemExit(run(self_check_only=self_check_only))


if __name__ == "__main__":
    main()
