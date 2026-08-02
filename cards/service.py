"""管理同時可見的組別級提醒卡。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import RLock

from cards.lifecycle import CardLifecycle, _require_aware
from cards.models import GroupCard
from cards.settings import CardDisplaySettings


MAX_VISIBLE_CARDS = 3


class CardCapacityError(RuntimeError):
    """Deprecated compatibility error; extra cards now wait in priority order."""


@dataclass(frozen=True, slots=True)
class CardServiceState:
    schema_version: int
    revision: int
    visible_cards: tuple[GroupCard, ...]
    pending_cards: tuple[GroupCard, ...]

    SCHEMA_VERSION = 1


class CardService:
    def __init__(
        self,
        settings: CardDisplaySettings | None = None,
        *,
        listener_error_callback: Callable[[Exception], None] | None = None,
    ) -> None:
        if settings is not None and not isinstance(settings, CardDisplaySettings):
            raise TypeError("settings must be CardDisplaySettings.")
        self.settings = settings or CardDisplaySettings()
        self._entries: list[CardLifecycle] = []
        self._change_listeners: list[Callable[[], None]] = []
        self._revision = 0
        self._lock = RLock()
        self._listener_error_callback = listener_error_callback

    def subscribe(
        self,
        listener: Callable[[], None],
        *,
        resync: bool = False,
    ) -> bool:
        if not callable(listener):
            raise TypeError("listener must be callable.")
        with self._lock:
            if listener in self._change_listeners:
                added = False
            else:
                self._change_listeners.append(listener)
                added = True
        if resync:
            self.resync(listener)
        return added

    def unsubscribe(self, listener: Callable[[], None]) -> bool:
        with self._lock:
            if listener not in self._change_listeners:
                return False
            self._change_listeners.remove(listener)
            return True

    def _notify_changed(self) -> None:
        with self._lock:
            self._revision += 1
            listeners = tuple(self._change_listeners)
        for listener in listeners:
            self._notify_one(listener)

    def _notify_one(self, listener: Callable[[], None]) -> bool:
        try:
            listener()
            return True
        except Exception as error:
            if self._listener_error_callback is not None:
                try:
                    self._listener_error_callback(error)
                except Exception:
                    pass
            return False

    def resync(self, listener: Callable[[], None] | None = None) -> int:
        """Request a recoverable full snapshot refresh after a missed update."""
        if listener is not None:
            if not callable(listener):
                raise TypeError("listener must be callable.")
            listeners = (listener,)
        else:
            with self._lock:
                listeners = tuple(self._change_listeners)
        return sum(not self._notify_one(item) for item in listeners)

    def snapshot(self) -> CardServiceState:
        with self._lock:
            ordered = self._ordered_entries_unlocked()
            return CardServiceState(
                CardServiceState.SCHEMA_VERSION,
                self._revision,
                tuple(
                    entry.card
                    for entry in ordered[:MAX_VISIBLE_CARDS]
                ),
                tuple(
                    entry.card
                    for entry in ordered[MAX_VISIBLE_CARDS:]
                ),
            )

    @property
    def cards(self) -> tuple[GroupCard, ...]:
        with self._lock:
            return tuple(
                entry.card
                for entry in self._ordered_entries_unlocked()[
                    :MAX_VISIBLE_CARDS
                ]
            )

    @property
    def entries(self) -> tuple[CardLifecycle, ...]:
        with self._lock:
            return tuple(
                self._ordered_entries_unlocked()[:MAX_VISIBLE_CARDS]
            )

    @property
    def pending_entries(self) -> tuple[CardLifecycle, ...]:
        with self._lock:
            return tuple(
                self._ordered_entries_unlocked()[MAX_VISIBLE_CARDS:]
            )

    @property
    def all_cards(self) -> tuple[GroupCard, ...]:
        with self._lock:
            return tuple(entry.card for entry in self._entries)

    def _ordered_entries(self) -> list[CardLifecycle]:
        with self._lock:
            return self._ordered_entries_unlocked()

    def _ordered_entries_unlocked(self) -> list[CardLifecycle]:
        indexed = tuple(enumerate(self._entries))
        return [
            entry
            for _index, entry in sorted(
                indexed,
                key=lambda item: (
                    int(item[1].card.priority_tier),
                    item[1].shown_at,
                    item[0],
                ),
            )
        ]

    def upsert(
        self,
        card: GroupCard,
        shown_at: datetime | None = None,
        *,
        lifetime: timedelta | None = None,
    ) -> GroupCard:
        if not isinstance(card, GroupCard):
            raise TypeError("card must be GroupCard.")

        with self._lock:
            for index, current in enumerate(self._entries):
                if current.card.card_id == card.card_id:
                    self._entries[index] = CardLifecycle(
                        card,
                        current.shown_at,
                        lifetime or current.lifetime,
                    )
                    break
            else:
                shown_at = shown_at or datetime.now(timezone.utc)
                self._entries.append(
                    CardLifecycle(
                        card,
                        shown_at,
                        lifetime or self.settings.lifetime,
                    )
                )
        self._notify_changed()
        return card

    def remove(self, card_id: str) -> GroupCard | None:
        if not isinstance(card_id, str):
            raise TypeError("card_id must be str.")
        card_id = card_id.strip()
        if not card_id:
            raise ValueError("card_id must not be empty.")

        with self._lock:
            removed = None
            for index, entry in enumerate(self._entries):
                if entry.card.card_id == card_id:
                    removed = self._entries.pop(index).card
                    break
        if removed is not None:
            self._notify_changed()
            return removed
        return None

    def remove_expired(self, now: datetime) -> tuple[GroupCard, ...]:
        _require_aware(now, "now")
        with self._lock:
            expired = tuple(
                entry.card
                for entry in self._entries
                if entry.is_expired(now)
            )
            self._entries = [
                entry
                for entry in self._entries
                if not entry.is_expired(now)
            ]
        if expired:
            self._notify_changed()
        return expired

    def complete(self, card_id: str) -> GroupCard | None:
        return self.remove(card_id)
