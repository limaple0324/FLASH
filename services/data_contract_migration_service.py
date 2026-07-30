"""Central version and migration rules for persisted application contracts."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Mapping

from config.config_manager import ConfigManager


Migration = Callable[[dict[str, object]], dict[str, object]]


@dataclass(frozen=True, slots=True)
class DataContractVersionState:
    schema_version: int
    component_versions: Mapping[str, int]

    SCHEMA_VERSION = 1


class DataContractMigrationService:
    """Reject future data and migrate older payloads one declared step at a time."""

    SETTINGS_KEY = "data_contract_versions"
    CURRENT_VERSIONS = {
        "role_data": 1,
        "progress": 1,
        "habits": 1,
        "cards": 1,
        "reconnect": 6,
        "legacy_settings": 2,
    }

    def __init__(self, config: ConfigManager) -> None:
        self._config = config
        self._state = self._load_state()

    @property
    def state(self) -> DataContractVersionState:
        return self._state

    def current_version(self, component: str) -> int:
        try:
            return self.CURRENT_VERSIONS[component]
        except KeyError as error:
            raise ValueError(
                f"unknown data contract component: {component}"
            ) from error

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

    def migrate_component(
        self,
        component: str,
        payload: Mapping[str, object],
        *,
        migrations: Mapping[int, Migration],
        version_key: str = "schema_version",
    ) -> dict[str, object]:
        return self.migrate_payload(
            component,
            payload,
            current_version=self.current_version(component),
            migrations=migrations,
            version_key=version_key,
        )

    def _load_state(self) -> DataContractVersionState:
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
        return DataContractVersionState(
            DataContractVersionState.SCHEMA_VERSION,
            normalized,
        )

    @staticmethod
    def migrate_payload(
        component: str,
        payload: Mapping[str, object],
        *,
        current_version: int,
        migrations: Mapping[int, Migration],
        version_key: str = "schema_version",
    ) -> dict[str, object]:
        if not isinstance(payload, Mapping):
            raise TypeError(f"{component} payload must be an object.")
        migrated = deepcopy(dict(payload))
        version = migrated.get(version_key)
        if (
            isinstance(version, bool)
            or not isinstance(version, int)
            or version <= 0
        ):
            raise ValueError(f"{component} version is invalid.")
        if version > current_version:
            raise ValueError(f"{component} version is newer than this app.")
        while version < current_version:
            migration = migrations.get(version)
            if migration is None:
                raise ValueError(
                    f"{component} has no migration from version {version}."
                )
            migrated = migration(deepcopy(migrated))
            next_version = migrated.get(version_key)
            if (
                isinstance(next_version, bool)
                or not isinstance(next_version, int)
                or next_version <= version
                or next_version > current_version
            ):
                raise ValueError(f"{component} migration is invalid.")
            version = next_version
        return migrated
