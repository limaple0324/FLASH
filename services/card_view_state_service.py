"""從提醒卡服務建立不可修改的介面快照。"""

from cards.service import CardService
from cards.view_state import CardViewItem, CardViewState


class CardViewStateService:
    def __init__(self, cards: CardService) -> None:
        self._cards = cards

    def snapshot(self) -> CardViewState:
        return CardViewState(
            cards=tuple(
                CardViewItem.from_lifecycle(entry) for entry in self._cards.entries
            )
        )
