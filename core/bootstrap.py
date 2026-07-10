"""Application bootstrap flow for FLASH SP1."""

from config.config_manager import ConfigManager
from config.path_manager import PathManager
from core.self_check import SelfCheck
from core.version import MILESTONE, VERSION
from services.event_bus import EventBus
from services.logger_service import LoggerService


class Bootstrap:
    """Coordinates the minimum startup flow for SP1."""

    def __init__(self, context):
        self.context = context
        self.logger: LoggerService = context.get(LoggerService)
        self.config: ConfigManager = context.get(ConfigManager)
        self.paths: PathManager = context.get(PathManager)
        self.event_bus: EventBus = context.get(EventBus)

    def start(self) -> dict[str, object]:
        self.logger.info("FLASH SP1 bootstrap starting.")
        self.config.ensure_defaults(
            {
                "version": VERSION,
                "sprint": MILESTONE,
                "workspace_enabled": False,
            }
        )
        self.event_bus.publish("startup", {"message": "Application started"})

        self_check = SelfCheck(context=self.context, paths=self.paths).run_all()
        if self_check["passed"]:
            self.logger.info("FLASH SP1 self-check passed.")
        else:
            self.logger.error("FLASH SP1 self-check failed.")

        self.logger.info("FLASH SP1 bootstrap completed.")
        return {
            "version": str(self.config.get("version", VERSION)),
            "sprint": str(self.config.get("sprint", MILESTONE)),
            "workspace_enabled": bool(self.config.get("workspace_enabled", False)),
            "self_check_passed": bool(self_check["passed"]),
            "self_check": self_check["checks"],
        }
