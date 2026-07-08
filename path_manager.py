
"""
Flash Sync V1.0 alpha Sprint 01 entry
"""
import pathlib, sys
from config.path_manager import PathManager
from config.config_manager import ConfigManager
from services.app_context import AppContext
from services.event_bus import EventBus

def main():
    # init services
    cfg_path = PathManager.config_dir() / "settings.json"
    cfg_mgr = ConfigManager(cfg_path)
    AppContext.register(ConfigManager, cfg_mgr)

    bus = EventBus()
    AppContext.register(EventBus, bus)

    print("Flash Sync V1.0 alpha Sprint 01 skeleton running.")
    # publish a sample event
    bus.publish("startup", {"msg": "Application started"})
    # placeholder for GUI start

if __name__ == "__main__":
    main()
