from services.card_preview_selection_service import (
    CardPreviewChoice,
    CardPreviewSelectionState,
)
from ui.home import HomeView


class _Variable:
    def __init__(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class _Label:
    def __init__(self) -> None:
        self.text = ""

    def configure(self, *, text: str) -> None:
        self.text = text


def _view():
    selected = {"value": "compact"}

    def choices() -> tuple[CardPreviewChoice, ...]:
        return (
            CardPreviewChoice(
                "compact",
                "精簡方案",
                selected["value"] == "compact",
            ),
            CardPreviewChoice(
                "roomy",
                "寬鬆方案",
                selected["value"] == "roomy",
            ),
        )

    def choose(profile_id: str) -> CardPreviewSelectionState:
        selected["value"] = profile_id
        return CardPreviewSelectionState(profile_id)

    def clear() -> CardPreviewSelectionState:
        selected["value"] = None
        return CardPreviewSelectionState()

    view = HomeView(
        None,
        {},
        card_preview_choices_provider=choices,
        on_card_preview_select=choose,
        on_card_preview_clear=clear,
    )
    view._card_preview_variable = _Variable("寬鬆方案")
    view._card_preview_status_label = _Label()
    return view, selected


def test_selecting_a_card_style_refreshes_the_visible_selection() -> None:
    view, selected = _view()

    view._apply_card_preview_choice()

    assert selected["value"] == "roomy"
    assert view._card_preview_variable.get() == "寬鬆方案"
    assert view._card_preview_status_label.text == "目前樣式：寬鬆方案"


def test_clearing_a_card_style_persists_the_disabled_state() -> None:
    view, selected = _view()

    view._clear_card_preview_choice()

    assert selected["value"] is None
    assert view._card_preview_status_label.text == "提醒浮層目前已停用。"
