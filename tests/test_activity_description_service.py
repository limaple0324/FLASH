from config.config_manager import ConfigManager
from domain.activity_schedule import build_confirmed_activity_catalog
from main import build_services
from services.activity_description_service import ActivityDescriptionService
from services.app_context import AppContext


def _service(tmp_path) -> ActivityDescriptionService:
    return ActivityDescriptionService(
        ConfigManager(tmp_path / "settings.json"),
        build_confirmed_activity_catalog(),
    )


def test_player_description_persists_by_fixed_activity_identity(tmp_path):
    service = _service(tmp_path)

    saved = service.set_description("world-boss", "只使用已確認的 160 角色")
    reloaded = _service(tmp_path)

    assert saved.activity_id == "world-boss"
    assert saved.name == "世界BOSS"
    assert reloaded.description("world-boss") == "只使用已確認的 160 角色"
    assert build_confirmed_activity_catalog().get("world-boss").definition.name == (
        "世界BOSS"
    )


def test_empty_description_clears_only_player_text(tmp_path):
    service = _service(tmp_path)
    service.set_description("quiz-contest", "先看活動狀態")

    cleared = service.set_description("quiz-contest", "  ")

    assert cleared.description == ""
    assert service.description("quiz-contest") == ""
    assert service.choices()[6].activity_id == "quiz-contest"


def test_unknown_activity_cannot_create_a_new_identity(tmp_path):
    service = _service(tmp_path)

    try:
        service.set_description("invented-activity", "不可保存")
    except KeyError as error:
        assert "Unknown scheduled activity" in str(error)
    else:
        raise AssertionError("unknown activity must be rejected")


def test_build_services_registers_restart_safe_description_service(tmp_path):
    build_services(root=tmp_path)
    service = AppContext.get(ActivityDescriptionService)
    service.set_description("academy-duel", "玩家自訂說明")

    build_services(root=tmp_path)
    reloaded = AppContext.get(ActivityDescriptionService)

    assert reloaded.description("academy-duel") == "玩家自訂說明"
