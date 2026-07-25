from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
TOOLS_DIR = REPOSITORY_ROOT / "tools"

OBSOLETE_SCHEDULED_SYNC_TOOLS = (
    TOOLS_DIR / "register_flash_auto_sync_task.ps1",
    TOOLS_DIR / "sync_desktop_from_github.ps1",
)

LEGACY_SYNC_STATUS_CHECKERS = (
    TOOLS_DIR / "檢查輔同步狀態.cmd",
    TOOLS_DIR / "檢查輔同步狀態.ps1",
)


def test_obsolete_scheduled_git_sync_tools_are_not_shipped():
    for path in OBSOLETE_SCHEDULED_SYNC_TOOLS:
        assert not path.exists(), f"obsolete scheduled sync tool remains: {path}"


def test_legacy_sync_status_checkers_remain_available():
    for path in LEGACY_SYNC_STATUS_CHECKERS:
        assert path.is_file(), f"legacy sync status checker is missing: {path}"
