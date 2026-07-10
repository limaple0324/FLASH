"""FLASH SP1 desktop entrypoint."""

from __future__ import annotations

import sys
import traceback
from pathlib import Path
from tkinter import BOTH, LEFT, X, Button, Frame, Label, Tk, messagebox

from config.config_manager import ConfigManager
from config.path_manager import PathManager
from core.bootstrap import Bootstrap
from services.app_context import AppContext
from services.event_bus import EventBus
from services.logger_service import LoggerService

APP_TITLE = "輔｜FLASH SP1"


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
    return paths, logger


def format_self_check(status: dict[str, object]) -> tuple[str, str]:
    """Return a user-facing summary and detail text for the SP1 self-check."""
    passed = bool(status.get("self_check_passed", False))
    report = status.get("self_check", {})
    checks = report.get("checks", []) if isinstance(report, dict) else []

    lines: list[str] = []
    for item in checks:
        if not isinstance(item, dict):
            continue
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


def create_main_window(status: dict[str, object], paths: PathManager) -> Tk:
    """Create the persistent SP1 verification window."""
    window = Tk()
    window.title(APP_TITLE)
    window.geometry("720x520")
    window.minsize(620, 430)

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
        text="檢查明細",
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
        wraplength=650,
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


def run() -> int:
    paths: PathManager | None = None
    logger: LoggerService | None = None
    try:
        paths, logger = build_services()
        status = Bootstrap(context=AppContext).start()
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

        try:
            root = Tk()
            root.withdraw()
            messagebox.showerror(
                "輔｜啟動失敗",
                "FLASH 無法啟動。錯誤已寫入紀錄檔。\n\n"
                f"原因：{exc}",
                parent=root,
            )
            root.destroy()
        except Exception:
            print(details, file=sys.stderr)
        return 1


def main() -> None:
    raise SystemExit(run())


if __name__ == "__main__":
    main()
