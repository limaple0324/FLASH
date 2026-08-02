"""Persist player-controlled feature-card layout without changing card behavior."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from config.config_manager import ConfigManager


FEATURE_CARD_LAYOUT_CONFIG_KEY = "feature_card_layout"
FEATURE_CARD_LAYOUT_SCHEMA_VERSION = 1
MAX_CARD_TITLE_LENGTH = 80


@dataclass(frozen=True, slots=True)
class FeatureCardPreference:
    card_id: str
    title: str
    collapsed: bool


class FeatureCardLayoutService:
    """Own card order, collapsed state and player-visible title."""

    def __init__(self, config: ConfigManager) -> None:
        if not isinstance(config, ConfigManager):
            raise TypeError("config must be ConfigManager.")
        self._config = config

    @staticmethod
    def _clean_identifier(value: object, *, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} must be a non-empty string.")
        return value.strip()

    @staticmethod
    def _clean_available_ids(values: Iterable[str]) -> tuple[str, ...]:
        cleaned: list[str] = []
        for value in values:
            card_id = FeatureCardLayoutService._clean_identifier(
                value,
                name="card_id",
            )
            if card_id not in cleaned:
                cleaned.append(card_id)
        return tuple(cleaned)

    def _payload(self) -> dict[str, object]:
        raw = self._config.get(FEATURE_CARD_LAYOUT_CONFIG_KEY, {})
        if not isinstance(raw, Mapping):
            return {
                "schema_version": FEATURE_CARD_LAYOUT_SCHEMA_VERSION,
                "pages": {},
                "cards": {},
            }
        raw_pages = raw.get("pages", {})
        raw_cards = raw.get("cards", {})
        pages = dict(raw_pages) if isinstance(raw_pages, Mapping) else {}
        cards = dict(raw_cards) if isinstance(raw_cards, Mapping) else {}
        return {
            "schema_version": FEATURE_CARD_LAYOUT_SCHEMA_VERSION,
            "pages": pages,
            "cards": cards,
        }

    def _save(self, payload: Mapping[str, object]) -> None:
        self._config.set(
            FEATURE_CARD_LAYOUT_CONFIG_KEY,
            {
                "schema_version": FEATURE_CARD_LAYOUT_SCHEMA_VERSION,
                "pages": dict(payload.get("pages", {})),
                "cards": dict(payload.get("cards", {})),
            },
        )

    def order_for(
        self,
        page: str,
        available_ids: Iterable[str],
    ) -> tuple[str, ...]:
        page_id = self._clean_identifier(page, name="page")
        available = self._clean_available_ids(available_ids)
        payload = self._payload()
        pages = payload["pages"]
        raw_page = pages.get(page_id, {}) if isinstance(pages, Mapping) else {}
        raw_order = (
            raw_page.get("order", ())
            if isinstance(raw_page, Mapping)
            else ()
        )
        saved = (
            tuple(
                value
                for value in raw_order
                if isinstance(value, str) and value in available
            )
            if isinstance(raw_order, (list, tuple))
            else ()
        )
        return tuple(dict.fromkeys((*saved, *available)))

    def preference(
        self,
        card_id: str,
        default_title: str,
    ) -> FeatureCardPreference:
        clean_id = self._clean_identifier(card_id, name="card_id")
        clean_default = self._clean_title(default_title)
        payload = self._payload()
        cards = payload["cards"]
        raw = cards.get(clean_id, {}) if isinstance(cards, Mapping) else {}
        title = (
            raw.get("title")
            if isinstance(raw, Mapping)
            else None
        )
        collapsed = (
            raw.get("collapsed")
            if isinstance(raw, Mapping)
            else False
        )
        return FeatureCardPreference(
            card_id=clean_id,
            title=(
                title.strip()
                if isinstance(title, str)
                and title.strip()
                and len(title.strip()) <= MAX_CARD_TITLE_LENGTH
                else clean_default
            ),
            collapsed=collapsed if isinstance(collapsed, bool) else False,
        )

    def set_collapsed(
        self,
        card_id: str,
        collapsed: bool,
    ) -> FeatureCardPreference:
        if not isinstance(collapsed, bool):
            raise TypeError("collapsed must be bool.")
        clean_id = self._clean_identifier(card_id, name="card_id")
        payload = self._payload()
        cards = dict(payload["cards"])
        raw = cards.get(clean_id, {})
        card_payload = dict(raw) if isinstance(raw, Mapping) else {}
        card_payload["collapsed"] = collapsed
        cards[clean_id] = card_payload
        payload["cards"] = cards
        self._save(payload)
        return FeatureCardPreference(
            card_id=clean_id,
            title=(
                str(card_payload.get("title", "")).strip()
            ),
            collapsed=collapsed,
        )

    def set_title(
        self,
        card_id: str,
        title: str,
    ) -> FeatureCardPreference:
        clean_id = self._clean_identifier(card_id, name="card_id")
        clean_title = self._clean_title(title)
        payload = self._payload()
        cards = dict(payload["cards"])
        raw = cards.get(clean_id, {})
        card_payload = dict(raw) if isinstance(raw, Mapping) else {}
        card_payload["title"] = clean_title
        cards[clean_id] = card_payload
        payload["cards"] = cards
        self._save(payload)
        return FeatureCardPreference(
            card_id=clean_id,
            title=clean_title,
            collapsed=(
                card_payload.get("collapsed")
                if isinstance(card_payload.get("collapsed"), bool)
                else False
            ),
        )

    def reset_title(self, card_id: str) -> None:
        clean_id = self._clean_identifier(card_id, name="card_id")
        payload = self._payload()
        cards = dict(payload["cards"])
        raw = cards.get(clean_id, {})
        if not isinstance(raw, Mapping) or "title" not in raw:
            return
        card_payload = dict(raw)
        card_payload.pop("title", None)
        if card_payload:
            cards[clean_id] = card_payload
        else:
            cards.pop(clean_id, None)
        payload["cards"] = cards
        self._save(payload)

    def reorder(
        self,
        page: str,
        ordered_ids: Iterable[str],
        available_ids: Iterable[str],
    ) -> tuple[str, ...]:
        page_id = self._clean_identifier(page, name="page")
        available = self._clean_available_ids(available_ids)
        ordered = self._clean_available_ids(ordered_ids)
        if len(ordered) != len(available) or set(ordered) != set(available):
            raise ValueError("ordered_ids must contain every available card once.")
        payload = self._payload()
        pages = dict(payload["pages"])
        raw_page = pages.get(page_id, {})
        page_payload = dict(raw_page) if isinstance(raw_page, Mapping) else {}
        page_payload["order"] = list(ordered)
        pages[page_id] = page_payload
        payload["pages"] = pages
        self._save(payload)
        return ordered

    @staticmethod
    def _clean_title(value: object) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("卡片顯示文字不可空白。")
        title = value.strip()
        if len(title) > MAX_CARD_TITLE_LENGTH:
            raise ValueError(
                f"卡片顯示文字不可超過 {MAX_CARD_TITLE_LENGTH} 個字。"
            )
        return title
