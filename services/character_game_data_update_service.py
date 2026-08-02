"""四項遊戲資料的唯讀、最新值與部分更新保存服務。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from domain.character_game_data import (
    ArtifactSnapshot,
    CharacterGameData,
    ObsidianSnapshot,
    PetLifeSoulSnapshot,
    PetTalentPageSnapshot,
    PetTalentSnapshot,
)
from domain.character_game_data_store import CharacterGameDataStore


_UNSET = object()


@dataclass(frozen=True, slots=True)
class CharacterGameDataUpdateResult:
    character_id: str
    changed: bool
    record: CharacterGameData
    changed_sections: tuple[str, ...] = ()


class CharacterGameDataUpdateService:
    """將可靠讀到的單頁資料合併到同一角色，不清除未讀頁面。"""

    def __init__(self, store: CharacterGameDataStore):
        if not isinstance(store, CharacterGameDataStore):
            raise TypeError("store must be CharacterGameDataStore.")
        self._store = store

    @property
    def store(self) -> CharacterGameDataStore:
        return self._store

    def update(
        self,
        character_id: str,
        *,
        pet_talent: PetTalentSnapshot | PetTalentPageSnapshot | object = _UNSET,
        obsidian: ObsidianSnapshot | object = _UNSET,
        life_souls: Iterable[PetLifeSoulSnapshot] | PetLifeSoulSnapshot | object = _UNSET,
        artifact: ArtifactSnapshot | object = _UNSET,
        cultivated_pet_count: int | object = _UNSET,
    ) -> CharacterGameDataUpdateResult:
        """套用一個或多個已可靠辨識的區域；未傳入的區域保持原值。"""

        if not isinstance(character_id, str) or not character_id.strip():
            raise ValueError("character_id must be a non-empty string.")
        normalized_character_id = character_id.strip()
        records = self._store.load()
        existing = next(
            (
                item
                for item in records
                if item.character_id == normalized_character_id
            ),
            None,
        )
        current_count = (
            existing.cultivated_pet_count if existing is not None else 0
        )
        next_count = self._validate_count(
            current_count if cultivated_pet_count is _UNSET else cultivated_pet_count
        )

        next_pet_talent = (
            existing.pet_talent if existing is not None else None
        )
        if pet_talent is not _UNSET:
            next_pet_talent = self._merge_pet_talent(
                next_pet_talent,
                pet_talent,
            )

        next_obsidian = existing.obsidian if existing is not None else None
        if obsidian is not _UNSET:
            if not isinstance(obsidian, ObsidianSnapshot):
                raise TypeError("obsidian must be ObsidianSnapshot.")
            if not obsidian.verified:
                raise ValueError(
                    "obsidian update requires stage, opened_nodes, and page shape evidence."
                )
            next_obsidian = obsidian

        next_life_souls = existing.life_souls if existing is not None else ()
        if life_souls is not _UNSET:
            next_life_souls = self._merge_life_souls(
                next_life_souls,
                life_souls,
            )

        next_artifact = existing.artifact if existing is not None else None
        if artifact is not _UNSET:
            if not isinstance(artifact, ArtifactSnapshot):
                raise TypeError("artifact must be ArtifactSnapshot.")
            next_artifact = artifact

        if life_souls is not _UNSET and not next_count:
            raise ValueError(
                "cultivated_pet_count must be set before reading life soul pages."
            )
        next_record = CharacterGameData(
            character_id=normalized_character_id,
            cultivated_pet_count=next_count,
            obsidian=next_obsidian,
            life_souls=next_life_souls,
            pet_talent=next_pet_talent,
            artifact=next_artifact,
        )
        if existing is not None and next_record == existing:
            return CharacterGameDataUpdateResult(
                character_id=next_record.character_id,
                changed=False,
                record=existing,
            )

        next_records = tuple(
            item
            for item in records
            if item.character_id != next_record.character_id
        ) + (next_record,)
        self._store.save(next_records)
        return CharacterGameDataUpdateResult(
            character_id=next_record.character_id,
            changed=True,
            record=next_record,
            changed_sections=self._changed_sections(existing, next_record),
        )

    @staticmethod
    def _validate_count(value: object) -> int:
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
        ):
            raise ValueError("cultivated_pet_count must be a non-negative integer.")
        return value

    @staticmethod
    def _merge_pet_talent(
        existing: PetTalentSnapshot | None,
        incoming: object,
    ) -> PetTalentSnapshot:
        if isinstance(incoming, PetTalentPageSnapshot):
            incoming_pages = (incoming,)
        elif isinstance(incoming, PetTalentSnapshot):
            incoming_pages = incoming.pages
        else:
            raise TypeError(
                "pet_talent must be PetTalentSnapshot or PetTalentPageSnapshot."
            )
        pages = {
            item.page_number: item
            for item in (existing.pages if existing is not None else ())
        }
        for item in incoming_pages:
            pages[item.page_number] = item
        return PetTalentSnapshot(tuple(pages[number] for number in sorted(pages)))

    @staticmethod
    def _merge_life_souls(
        existing: tuple[PetLifeSoulSnapshot, ...],
        incoming: object,
    ) -> tuple[PetLifeSoulSnapshot, ...]:
        if isinstance(incoming, PetLifeSoulSnapshot):
            incoming_items = (incoming,)
        else:
            if isinstance(incoming, (str, bytes)):
                raise TypeError("life_souls must contain PetLifeSoulSnapshot values.")
            try:
                incoming_items = tuple(incoming)  # type: ignore[arg-type]
            except TypeError as error:
                raise TypeError(
                    "life_souls must be a PetLifeSoulSnapshot or iterable."
                ) from error
        if any(not isinstance(item, PetLifeSoulSnapshot) for item in incoming_items):
            raise TypeError("life_souls must contain PetLifeSoulSnapshot values.")
        if any(item.pet_identity is None for item in incoming_items):
            raise ValueError(
                "life soul update requires a reliable pet identity."
            )
        keys = [(item.identity_key, item.page_number) for item in incoming_items]
        if len(keys) != len(set(keys)):
            raise ValueError("Duplicate life soul pages in one update.")

        merged = list(existing)
        for item in incoming_items:
            match_index = next(
                (
                    index
                    for index, current in enumerate(merged)
                    if current.page_number == item.page_number
                    and (
                        current.identity_key == item.identity_key
                        or (
                            current.pet_identity is None
                            and current.pet_name == item.pet_name
                        )
                    )
                ),
                None,
            )
            if match_index is None:
                merged.append(item)
            else:
                merged[match_index] = item
        return tuple(sorted(merged, key=lambda item: (item.identity_key, item.page_number)))

    @staticmethod
    def _changed_sections(
        old: CharacterGameData | None,
        new: CharacterGameData,
    ) -> tuple[str, ...]:
        if old is None:
            return tuple(
                name
                for name, value in (
                    ("寵物天賦", new.pet_talent),
                    ("黑曜石", new.obsidian),
                    ("命魂", new.life_souls),
                    ("魂器", new.artifact),
                )
                if value is not None and value != ()
            )
        changed: list[str] = []
        if old.pet_talent != new.pet_talent:
            changed.append("寵物天賦")
        if old.obsidian != new.obsidian:
            changed.append("黑曜石")
        if old.life_souls != new.life_souls or old.cultivated_pet_count != new.cultivated_pet_count:
            changed.append("命魂")
        if old.artifact != new.artifact:
            changed.append("魂器")
        return tuple(changed)
