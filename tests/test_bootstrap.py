from config.config_manager import ConfigManager
from config.path_manager import PathManager
from core.bootstrap import Bootstrap
from core.version import MILESTONE, VERSION
from main import build_services
from services.app_context import AppContext
from services.event_bus import EventBus
from services.logger_service import LoggerService


def test_app_context_register_and_get(tmp_path):
    AppContext.clear()
    paths = PathManager(root=tmp_path)
    logger = LoggerService(paths.log_file("test.log"))
    config = ConfigManager(paths.config_file("settings.json"))
    bus = EventBus(logger=logger)

    AppContext.register(PathManager, paths)
    AppContext.register(LoggerService, logger)
    AppContext.register(ConfigManager, config)
    AppContext.register(EventBus, bus)

    assert AppContext.get(PathManager) is paths
    assert AppContext.get(LoggerService) is logger
    assert AppContext.get(ConfigManager) is config
    assert AppContext.get(EventBus) is bus


def test_bootstrap_creates_persistent_files_and_returns_status(tmp_path):
    paths, _logger = build_services(root=tmp_path)
    status = Bootstrap(context=AppContext).start()

    assert status["sprint"] == MILESTONE
    assert status["version"] == VERSION
    assert paths.config_file("settings.json").exists()
    assert paths.log_file("flash.log").exists()
