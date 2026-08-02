"""保存玩家自訂的活動敘述，不改動活動固定識別或進度。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from config.config_manager import ConfigManager
from domain.activity_schedule import ActivityScheduleCatalog


ACTIVITY_DESCRIPTION_CONFIG_KEY = "activity_descriptions"
ACTIVITY_DESCRIPTION_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ActivityDescriptionChoice:
    activity_id: str
    name: str
    description: str


class ActivityDescriptionService:
    """以活動固定識別保存玩家文字，避免名稱或進度被自訂內容改寫。"""

    def __init__(
        self,
        config: ConfigManager,
        catalog: ActivityScheduleCatalog,
    ) -> None:
        if not isinstance(config, ConfigManager):
            raise TypeError("config must be ConfigManager.")
        if not isinstance(catalog, ActivityScheduleCatalog):
            raise TypeError("catalog must be ActivityScheduleCatalog.")
        self._config = config
        self._catalog = catalog

    def description(self, activity_id: str) -> str:
        clean_id = self._known_activity_id(activity_id)
        descriptions = self._descriptions()
        value = descriptions.get(clean_id, "")
        return value if isinstance(value, str) else ""

    def set_description(
        self,
        activity_id: str,
        description: str,
    ) -> ActivityDescriptionChoice:
        clean_id = self._known_activity_id(activity_id)
        if not isinstance(description, str):
            raise TypeError("description must be str.")
        clean_text = description.strip()
        descriptions = self._descriptions()
        if clean_text:
            descriptions[clean_id] = clean_text
        else:
            descriptions.pop(clean_id, None)
        self._config.set(
            ACTIVITY_DESCRIPTION_CONFIG_KEY,
            {
                "schema_version": ACTIVITY_DESCRIPTION_SCHEMA_VERSION,
                "descriptions": descriptions,
            },
        )
        rule = self._catalog.get(clean_id)
        return ActivityDescriptionChoice(
            activity_id=clean_id,
            name=rule.definition.name,
            description=clean_text,
        )

    def choices(self) -> tuple[ActivityDescriptionChoice, ...]:
        descriptions = self._descriptions()
        return tuple(
            ActivityDescriptionChoice(
                activity_id=rule.activity_id,
                name=rule.definition.name,
                description=(
                    descriptions.get(rule.activity_id, "")
                    if isinstance(descriptions.get(rule.activity_id, ""), str)
                    else ""
                ),
            )
            for rule in self._catalog.all()
        )

    def _known_activity_id(self, activity_id: str) -> str:
        if not isinstance(activity_id, str) or not activity_id.strip():
            raise ValueError("activity_id must be a non-empty string.")
        clean_id = activity_id.strip()
        self._catalog.get(clean_id)
        return clean_id

    def _descriptions(self) -> dict[str, str]:
        raw = self._config.get(ACTIVITY_DESCRIPTION_CONFIG_KEY, {})
        if not isinstance(raw, Mapping):
            return {}
        if raw.get("schema_version") != ACTIVITY_DESCRIPTION_SCHEMA_VERSION:
            return {}
        values = raw.get("descriptions", {})
        if not isinstance(values, Mapping):
            return {}
        known = {rule.activity_id for rule in self._catalog.all()}
        return {
            activity_id: value
            for activity_id, value in values.items()
            if activity_id in known and isinstance(value, str) and value.strip()
        }
