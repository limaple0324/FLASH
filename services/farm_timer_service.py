"""每個角色獨立的農場成熟、逾時與複製代碼服務。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import threading
import uuid

from cards.models import CardAction, GroupCard
from cards.priority import CardPriorityReason
from domain.activity import ActivityDefinition, ActivityType, ResetRule
from domain.group import CharacterGroup
from services.card_coordinator import CardCoordinator
from services.group_role_status_service import (
    ROLE_STATUS_CLOSED,
    ROLE_STATUS_OPEN,
)


FARM_PLANTING_CONFIRMED_EVENT = "farm_planting_confirmed"
FARM_COMPLETED_EVENT = "farm_completed"
FARM_MATURE_AFTER = timedelta(minutes=40)
FARM_ELEVATED_AFTER = timedelta(minutes=50)
FARM_HIGHEST_RISK_AFTER = timedelta(minutes=55)
FARM_DISAPPEARS_AFTER = timedelta(minutes=60)
COPY_CODE_ACTION_ID = "複製代碼"


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field} must be str.")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must not be empty.")
    return normalized


def _aware(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include timezone information.")
    return value.astimezone(timezone.utc)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:20]


@dataclass(frozen=True, slots=True)
class FarmPlantingConfirmed:
    timer_id: str
    group: CharacterGroup
    character_id: str
    planted_at: datetime
    copy_code: str

    def __post_init__(self) -> None:
        if not isinstance(self.group, CharacterGroup):
            raise TypeError("group must be CharacterGroup.")
        timer_id = _required_text(self.timer_id, "timer_id")
        character_id = _required_text(self.character_id, "character_id")
        copy_code = _required_text(self.copy_code, "copy_code")
        if character_id not in self.group.character_ids:
            raise ValueError("character_id must belong to group.")
        object.__setattr__(self, "timer_id", timer_id)
        object.__setattr__(self, "character_id", character_id)
        object.__setattr__(self, "copy_code", copy_code)
        object.__setattr__(
            self,
            "planted_at",
            _aware(self.planted_at, "planted_at"),
        )


@dataclass(frozen=True, slots=True)
class FarmCompleted:
    timer_id: str
    completed_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "timer_id",
            _required_text(self.timer_id, "timer_id"),
        )
        object.__setattr__(
            self,
            "completed_at",
            _aware(self.completed_at, "completed_at"),
        )


@dataclass(frozen=True, slots=True)
class FarmTimer:
    timer_id: str
    group: CharacterGroup
    character_id: str
    planted_at: datetime
    copy_code: str
    emitted_stage: str = "等待中"
    paused_at: datetime | None = None
    paused_seconds: int = 0

    @property
    def character_name(self) -> str:
        return next(
            character.display_name
            for character in self.group.characters
            if character.character_id == self.character_id
        )

    @property
    def card_id(self) -> str:
        return f"farm:{_digest(self.timer_id)}"

    def elapsed_at(self, now: datetime) -> timedelta:
        current = _aware(now, "now")
        paused_seconds = self.paused_seconds
        if self.paused_at is not None:
            paused_seconds += max(
                0,
                int((current - self.paused_at).total_seconds()),
            )
        return max(
            current - self.planted_at - timedelta(seconds=paused_seconds),
            timedelta(0),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "timer_id": self.timer_id,
            "group": self.group.to_dict(),
            "character_id": self.character_id,
            "planted_at": self.planted_at.isoformat(),
            "copy_code": self.copy_code,
            "emitted_stage": self.emitted_stage,
            "paused_at": (
                self.paused_at.isoformat()
                if self.paused_at is not None
                else None
            ),
            "paused_seconds": self.paused_seconds,
        }


class FarmTimerService:
    """A timer starts only after a typed, confirmed planting event."""

    SCHEMA_VERSION = 2

    def __init__(
        self,
        coordinator: CardCoordinator,
        *,
        state_path: Path | None = None,
        clipboard_writer: Callable[[str], object] | None = None,
        record_callback: Callable[[str, str, str], object] | None = None,
    ) -> None:
        if not isinstance(coordinator, CardCoordinator):
            raise TypeError("coordinator must be CardCoordinator.")
        if clipboard_writer is not None and not callable(clipboard_writer):
            raise TypeError("clipboard_writer must be callable.")
        self._coordinator = coordinator
        self._state_path = Path(state_path) if state_path is not None else None
        self._clipboard_writer = clipboard_writer
        self._record_callback = record_callback
        self._lock = threading.RLock()
        self._timers = {
            timer.timer_id: timer for timer in self._load()
        }

    def set_clipboard_writer(
        self,
        writer: Callable[[str], object] | None,
    ) -> None:
        if writer is not None and not callable(writer):
            raise TypeError("writer must be callable.")
        self._clipboard_writer = writer

    @staticmethod
    def _group_from_payload(payload: object) -> CharacterGroup:
        if not isinstance(payload, Mapping):
            raise ValueError("group must be an object.")
        from domain.character import Character, CharacterImportance

        raw_characters = payload.get("characters")
        if not isinstance(raw_characters, list):
            raise ValueError("group characters must be a list.")
        characters = []
        for raw in raw_characters:
            if not isinstance(raw, Mapping):
                raise ValueError("character must be an object.")
            characters.append(
                Character(
                    character_id=_required_text(
                        raw.get("character_id"),
                        "character_id",
                    ),
                    display_name=_required_text(
                        raw.get("display_name"),
                        "display_name",
                    ),
                    level=raw.get("level"),
                    importance=CharacterImportance(
                        _required_text(
                            raw.get("importance"),
                            "importance",
                        )
                    ),
                )
            )
        return CharacterGroup(
            group_id=_required_text(payload.get("group_id"), "group_id"),
            name=_required_text(payload.get("name"), "group_name"),
            characters=tuple(characters),
        )

    @classmethod
    def _timer_from_payload(cls, payload: object) -> FarmTimer:
        if not isinstance(payload, Mapping):
            raise ValueError("timer must be an object.")
        planted_at = datetime.fromisoformat(
            _required_text(payload.get("planted_at"), "planted_at")
        )
        group = cls._group_from_payload(payload.get("group"))
        character_id = _required_text(
            payload.get("character_id"),
            "character_id",
        )
        if character_id not in group.character_ids:
            raise ValueError("character_id must belong to group.")
        stage = _required_text(
            payload.get("emitted_stage", "等待中"),
            "emitted_stage",
        )
        if stage not in {
            "等待中",
            "已成熟",
            "優先收成",
            "最高風險",
            "作物已消失",
            "已逾時",
        }:
            raise ValueError("emitted_stage is invalid.")
        raw_paused_at = payload.get("paused_at")
        paused_at = (
            _aware(
                datetime.fromisoformat(
                    _required_text(raw_paused_at, "paused_at")
                ),
                "paused_at",
            )
            if raw_paused_at is not None
            else None
        )
        paused_seconds = payload.get("paused_seconds", 0)
        if (
            isinstance(paused_seconds, bool)
            or not isinstance(paused_seconds, int)
            or paused_seconds < 0
        ):
            raise ValueError("paused_seconds is invalid.")
        return FarmTimer(
            timer_id=_required_text(payload.get("timer_id"), "timer_id"),
            group=group,
            character_id=character_id,
            planted_at=_aware(planted_at, "planted_at"),
            copy_code=_required_text(payload.get("copy_code"), "copy_code"),
            emitted_stage=stage,
            paused_at=paused_at,
            paused_seconds=paused_seconds,
        )

    def _load(self) -> tuple[FarmTimer, ...]:
        if self._state_path is None or not self._state_path.is_file():
            return ()
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
            if (
                not isinstance(payload, Mapping)
                or payload.get("schema_version") not in {1, self.SCHEMA_VERSION}
                or not isinstance(payload.get("timers"), list)
            ):
                return ()
            return tuple(
                self._timer_from_payload(item)
                for item in payload["timers"]
            )
        except (
            OSError,
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
        ):
            return ()

    def _save(
        self,
        timers: Mapping[str, FarmTimer] | None = None,
    ) -> None:
        if self._state_path is None:
            return
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_name(
            f".{self._state_path.name}.{uuid.uuid4().hex}.tmp"
        )
        timers = self._timers if timers is None else dict(timers)
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "timers": [
                timers[key].to_dict()
                for key in sorted(timers)
            ],
        }
        try:
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, self._state_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _record(self, character_name: str, detail: str) -> None:
        if self._record_callback is None:
            return
        try:
            self._record_callback("農場提醒", character_name, detail)
        except Exception:
            pass

    def start(self, event: FarmPlantingConfirmed) -> FarmTimer:
        if not isinstance(event, FarmPlantingConfirmed):
            raise TypeError("event must be FarmPlantingConfirmed.")
        timer = FarmTimer(
            timer_id=event.timer_id,
            group=event.group,
            character_id=event.character_id,
            planted_at=event.planted_at,
            copy_code=event.copy_code,
        )
        with self._lock:
            timers = dict(self._timers)
            timers[timer.timer_id] = timer
            self._save(timers)
            self._timers = timers
        self._coordinator.cards.remove(timer.card_id)
        self._record(timer.character_name, "已開始獨立計時")
        return timer

    def complete(self, event: FarmCompleted) -> bool:
        if not isinstance(event, FarmCompleted):
            raise TypeError("event must be FarmCompleted.")
        with self._lock:
            timer = self._timers.get(event.timer_id)
            if timer is None:
                return False
            timers = dict(self._timers)
            timers.pop(event.timer_id)
            self._save(timers)
            self._timers = timers
        self._coordinator.cards.complete(timer.card_id)
        self._record(timer.character_name, "已完成並移除提醒")
        return True

    def poll(self, now: datetime) -> tuple[GroupCard, ...]:
        now = _aware(now, "now")
        emitted: list[GroupCard] = []
        plans: list[
            tuple[FarmTimer, GroupCard, timedelta | None]
        ] = []
        with self._lock:
            timers = dict(self._timers)
            timer_ids = tuple(sorted(self._timers))
            for timer_id in timer_ids:
                timer = self._timers[timer_id]
                elapsed = timer.elapsed_at(now)
                if timer.paused_at is not None:
                    continue
                if elapsed >= FARM_DISAPPEARS_AFTER:
                    stage = "作物已消失"
                    reason = CardPriorityReason.LOSS_RISK
                elif elapsed >= FARM_HIGHEST_RISK_AFTER:
                    stage = "最高風險"
                    reason = CardPriorityReason.LOSS_RISK
                elif elapsed >= FARM_ELEVATED_AFTER:
                    stage = "優先收成"
                    reason = CardPriorityReason.TIME_LIMIT
                elif elapsed >= FARM_MATURE_AFTER:
                    stage = "已成熟"
                    reason = CardPriorityReason.TIME_LIMIT
                else:
                    continue
                if timer.emitted_stage == stage:
                    continue
                card = self._card(timer, stage, reason)
                remaining_time = (
                    FARM_HIGHEST_RISK_AFTER - elapsed
                    if reason is CardPriorityReason.TIME_LIMIT
                    else None
                )
                updated = replace(
                    timer,
                    emitted_stage=stage,
                )
                timers[timer_id] = updated
                plans.append((updated, card, remaining_time))
            if plans:
                self._save(timers)
                self._timers = timers

        for timer, card, remaining_time in plans:
            presented = self._coordinator.submit(
                self._coordinator.candidate_for_card(
                    card,
                    remaining_time=remaining_time,
                ),
                card,
                shown_at=now,
            )
            self._record(timer.character_name, timer.emitted_stage)
            if presented is not None:
                emitted.append(presented)
        return tuple(emitted)

    def handle_role_status(
        self,
        character_id: str,
        status: str,
        occurred_at: datetime,
    ) -> bool:
        """只讓可靠的遊戲關閉暫停該角色農場計時。"""
        character_id = _required_text(character_id, "character_id")
        occurred_at = _aware(occurred_at, "occurred_at")
        if status not in {ROLE_STATUS_CLOSED, ROLE_STATUS_OPEN}:
            return False
        removed_cards: list[str] = []
        with self._lock:
            timers = dict(self._timers)
            changed = False
            for timer_id, timer in tuple(timers.items()):
                if timer.character_id != character_id:
                    continue
                if status == ROLE_STATUS_CLOSED and timer.paused_at is None:
                    timers[timer_id] = replace(timer, paused_at=occurred_at)
                    removed_cards.append(timer.card_id)
                    changed = True
                elif status == ROLE_STATUS_OPEN and timer.paused_at is not None:
                    paused_seconds = timer.paused_seconds + max(
                        0,
                        int((occurred_at - timer.paused_at).total_seconds()),
                    )
                    timers[timer_id] = replace(
                        timer,
                        paused_at=None,
                        paused_seconds=paused_seconds,
                        emitted_stage="等待中",
                    )
                    changed = True
            if not changed:
                return False
            self._save(timers)
            self._timers = timers
        for card_id in removed_cards:
            self._coordinator.cards.remove(card_id)
        return True

    @staticmethod
    def _card(
        timer: FarmTimer,
        stage: str,
        reason: CardPriorityReason,
    ) -> GroupCard:
        return GroupCard(
            card_id=timer.card_id,
            group=timer.group,
            activity=ActivityDefinition(
                activity_id=f"farm:{_digest(timer.timer_id)}",
                name=f"{timer.character_name}－農場{stage}",
                activity_type=ActivityType.PERMANENT,
                reset_rule=ResetRule.NONE,
                applicable_character_ids=(timer.character_id,),
            ),
            current_progress=f"農場作物{stage}",
            affected_character_ids=(timer.character_id,),
            requires_player_action=True,
            next_step="點擊複製代碼",
            priority_reason=reason,
            actions=(
                CardAction(
                    action_id=COPY_CODE_ACTION_ID,
                    label="複製代碼",
                ),
            ),
        )

    def handle_action(self, card_id: str, action_id: str) -> bool | None:
        if action_id != COPY_CODE_ACTION_ID:
            return None
        with self._lock:
            timer = next(
                (
                    item
                    for item in self._timers.values()
                    if item.card_id == card_id
                ),
                None,
            )
        if timer is None or self._clipboard_writer is None:
            return False
        result = self._clipboard_writer(timer.copy_code)
        if result is False:
            return False
        self._record(timer.character_name, "已複製正確代碼")
        return True
