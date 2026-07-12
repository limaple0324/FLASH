"""管理同時可見的組別級提醒卡。"""

from cards.models import GroupCard


MAX_VISIBLE_CARDS = 3


class CardCapacityError(RuntimeError):
    """加入第四張新卡時回報，避免擅自淘汰既有提醒。"""


class CardService:
    def __init__(self) -> None:
        self._cards: list[GroupCard] = []

    @property
    def cards(self) -> tuple[GroupCard, ...]:
        return tuple(self._cards)

    def upsert(self, card: GroupCard) -> GroupCard:
        if not isinstance(card, GroupCard):
            raise TypeError("card must be GroupCard.")

        for index, current in enumerate(self._cards):
            if current.card_id == card.card_id:
                self._cards[index] = card
                return card

        if len(self._cards) >= MAX_VISIBLE_CARDS:
            raise CardCapacityError("At most three cards can be visible.")
        self._cards.append(card)
        return card

    def remove(self, card_id: str) -> GroupCard | None:
        if not isinstance(card_id, str):
            raise TypeError("card_id must be str.")
        card_id = card_id.strip()
        if not card_id:
            raise ValueError("card_id must not be empty.")

        for index, card in enumerate(self._cards):
            if card.card_id == card_id:
                return self._cards.pop(index)
        return None
