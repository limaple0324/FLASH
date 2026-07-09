"""
FLASH SP1 Bootstrap v0.1
Application entrypoint.
"""

from config.config_manager import ConfigManager
from config.path_manager import PathManager
from core.bootstrap import Bootstrap
from services.app_context import AppContext
from services.event_bus import EventBus
from services.logger_service import LoggerService


def main() -> None:
    paths = PathManager()
    logger = LoggerService(paths.log_file("flash.log"))
    config = ConfigManager(paths.config_file("settings.json"))
    event_bus = EventBus(logger=logger)

    AppContext.register(PathManager, paths)
    AppContext.register(LoggerService, logger)
    AppContext.register(ConfigManager, config)
    AppContext.register(EventBus, event_bus)

    bootstrap = Bootstrap(context=AppContext)
    bootstrap.start()


if __name__ == "__main__":
    main()
