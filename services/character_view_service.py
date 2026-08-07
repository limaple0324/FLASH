"""把 SP1 視窗角色資料與 SP2 穩定角色資料轉成唯讀玩家資料。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping

from core.window_registry import WindowRegistry
from domain.character import Character, character_priority_key
from services.identity_data_transaction_coordinator import (
    IdentityDataResource,
    IdentityDataTransaction,
    IdentityDataTransactionCoordinator,
)


@dataclass(frozen=True, slots=True)
class PlayerCharacterView:
    """不含角色識別碼與視窗技術資料的唯讀快照。"""

    display_name: str
    group: str | None
    level: int | None
    importance: str | None
    role: str | None
    note: str | None


class CharacterViewService:
    """依固定角色身分組合資料，不用顯示名稱進行猜測。"""

    def __init__(
        self,
        registry: WindowRegistry,
        characters: Iterable[Character],
        coordinator: IdentityDataTransactionCoordinator,
        *,
        confirmed_group_orders: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        if not isinstance(registry, WindowRegistry):
            raise TypeError("registry must be WindowRegistry.")
        if not isinstance(coordinator, IdentityDataTransactionCoordinator):
            raise TypeError("coordinator must be IdentityDataTransactionCoordinator.")
        self._registry = registry
        self._coordinator = coordinator
        self._confirmed_group_orders = {
            group_name: tuple(order)
            for group_name, order in (confirmed_group_orders or {}).items()
        }
        self._characters: dict[str, Character] = {}
        for character in characters:
            if not isinstance(character, Character):
                raise TypeError("characters must contain only Character values.")
            if character.character_id in self._characters:
                raise ValueError(
                    f"Duplicate stable character ID: {character.character_id}"
                )
            self._characters[character.character_id] = character

    def replace_characters(
        self,
        characters: Iterable[Character],
    ) -> None:
        self._coordinator.execute(
            lambda transaction: self.stage_replace(transaction, characters)
        )

    def stage_replace(
        self,
        transaction: IdentityDataTransaction,
        characters: Iterable[Character],
    ) -> None:
        self._coordinator.require_transaction(transaction)
        replacement: dict[str, Character] = {}
        for character in characters:
            if not isinstance(character, Character):
                raise TypeError("characters must contain only Character values.")
            if character.character_id in replacement:
                raise ValueError(
                    f"Duplicate stable character ID: {character.character_id}"
                )
            replacement[character.character_id] = character
        if replacement == self._characters:
            return
        transaction.stage_memory(
            IdentityDataResource.CHARACTER_VIEW_CACHE,
            lambda: dict(self._characters),
            lambda: self._install_characters(replacement),
            self._restore_characters,
        )

    def profiles_in_transaction(
        self,
        transaction: IdentityDataTransaction,
    ) -> tuple[Character, ...]:
        self._coordinator.require_transaction(transaction)
        return tuple(self._characters.values())

    def _install_characters(self, replacement: dict[str, Character]) -> None:
        self._characters = dict(replacement)

    def _restore_characters(self, snapshot: object) -> None:
        if not isinstance(snapshot, dict) or any(
            not isinstance(key, str) or not isinstance(value, Character)
            for key, value in snapshot.items()
        ):
            raise TypeError("invalid character-view snapshot")
        self._characters = dict(snapshot)

    def all_with_identities(
        self,
        group_name: str | None = None,
    ) -> tuple[tuple[str, PlayerCharacterView], ...]:
        """提供內部服務安全配對；角色識別不得傳給顯示層。"""
        return self._coordinator.snapshot(
            lambda: self._all_with_identities_unlocked(group_name)
        )

    def _all_with_identities_unlocked(
        self,
        group_name: str | None,
    ) -> tuple[tuple[str, PlayerCharacterView], ...]:
        snapshots: list[tuple[str, PlayerCharacterView]] = []
        records = tuple(
            record
            for record in self._registry.all()
            if group_name is None or record.group == group_name
        )
        ordered_records = sorted(
            records,
            key=self._record_priority_key,
        )
        for record in ordered_records:
            character = self._characters.get(record.character_id)
            snapshots.append(
                (
                    record.character_id,
                    PlayerCharacterView(
                        display_name=record.display_name,
                        group=record.group,
                        level=character.level if character is not None else None,
                        importance=(
                            character.importance.value
                            if character is not None
                            else None
                        ),
                        role=record.role,
                        note=record.note,
                    ),
                )
            )
        return tuple(snapshots)

    def _record_priority_key(self, record) -> tuple[int, int, int, str]:
        character = self._characters.get(record.character_id)
        if character is None:
            return (
                len(self._characters) + 3,
                len(self._characters) + 3,
                0,
                record.character_id,
            )
        base_rank, negative_level, stable_identity = character_priority_key(
            character
        )
        fixed_order = self._confirmed_group_orders.get(record.group or "")
        if fixed_order is None:
            fixed_rank = len(self._characters) + 3
        else:
            try:
                fixed_rank = fixed_order.index(record.display_name)
            except ValueError:
                fixed_rank = len(fixed_order)
        return (
            base_rank,
            fixed_rank,
            negative_level,
            stable_identity,
        )

    def all(
        self,
        group_name: str | None = None,
    ) -> tuple[PlayerCharacterView, ...]:
        return self._coordinator.snapshot(
            lambda: self._all_unlocked(group_name)
        )

    def _all_unlocked(
        self,
        group_name: str | None,
    ) -> tuple[PlayerCharacterView, ...]:
        return tuple(
            snapshot
            for _character_id, snapshot
            in self._all_with_identities_unlocked(group_name)
        )
