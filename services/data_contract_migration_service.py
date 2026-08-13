"""Central version checks for persisted application contracts."""

from __future__ import annotations

from typing import Mapping

from config.config_manager import ConfigManager


class DataContractMigrationService:
    """Normalize known versions and reject unsupported contract drift."""

    SETTINGS_KEY = "data_contract_versions"
    CURRENT_VERSIONS = {
        "role_data": 1,
        "progress": 1,
        "habits": 1,
        "cards": 1,
        "reconnect": 7,
        "legacy_settings": 2,
    }

    def __init__(self, config: ConfigManager) -> None:
        self._config = config
        self._load_state()

    def verify_supported_versions(
        self,
        actual_versions: Mapping[str, int],
    ) -> None:
        for component, expected in self.CURRENT_VERSIONS.items():
            actual = actual_versions.get(component)
            if actual != expected:
                raise RuntimeError(
                    f"data contract version drift: {component}"
                )

    def _load_state(self) -> None:
        raw = self._config.get(self.SETTINGS_KEY, {})
        stored = raw if isinstance(raw, Mapping) else {}
        normalized: dict[str, int] = {}
        for component, current in self.CURRENT_VERSIONS.items():
            version = stored.get(component, current)
            if (
                isinstance(version, bool)
                or not isinstance(version, int)
                or version <= 0
            ):
                version = current
            if version > current:
                raise ValueError(
                    f"unsupported future data contract: {component}"
                )
            normalized[component] = current
        self._config.update_values({self.SETTINGS_KEY: normalized})
