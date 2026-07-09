"""Application bootstrap flow for FLASH SP1."""

from config.config_manager import ConfigManager
from services.event_bus import EventBus
from services.logger_service import LoggerService


class Bootstrap:
    """Coordinates the minimum startup flow for SP1."""

    def __init__(self, context):
        self.context = context
        self.logger: LoggerService = context.get(LoggerService)
        self.config: ConfigManager = context.get(ConfigManager)
        self.event_bus: EventBus = context.get(EventBus)

    def start(self) -> None:
        self.logger.info("FLASH SP1 bootstrap starting.")
        self.config.ensure_defaults({"version": "0.1.0", "sprint": "SP1", "workspace_enabled": False})
        self.event_bus.publish("startup", {"message": "Application started"})
        self.logger.info("FLASH SP1 bootstrap completed.")
        print("FLASH SP1 bootstrap running.")
