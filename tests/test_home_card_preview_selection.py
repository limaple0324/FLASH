from services.card_preview_selection_service import CardPreviewChoice
from ui import home


class _FakeWidget:
    created: list["_FakeWidget"] = []

    def __init__(self, parent=None, **kwargs) -> None:
        self.parent = parent
        self.options = dict(kwargs)
        self.packed = False
        self.created.append(self)

    def pack(self, **_kwargs) -> None:
        self.packed = True

    def pack_forget(self) -> None:
        self.packed = False

    def configure(self, **kwargs) -> None:
        self.options.update(kwargs)


def _install_fake_widgets(monkeypatch) -> None:
    _FakeWidget.created = []
    monkeypatch.setattr(home, "Frame", _FakeWidget)
    monkeypatch.setattr(home, "Label", _FakeWidget)
    monkeypatch.setattr(home, "Button", _FakeWidget)


def test_home_omits_preview_entry_without_explicit_candidates(monkeypatch) -> None:
    _install_fake_widgets(monkeypatch)

    home.HomeView(None, {}).build()

    assert "提醒卡樣式" not in {
        widget.options.get("text") for widget in _FakeWidget.created
    }


def test_home_shows_read_only_group_character_entry_when_provided(monkeypatch) -> None:
    _install_fake_widgets(monkeypatch)
    calls = []

    home.HomeView(
        None,
        {},
        on_show_group_characters=lambda: calls.append("show"),
    ).build()

    button = next(
        widget
        for widget in _FakeWidget.created
        if widget.options.get("text") == "查看組別角色"
    )
    button.options["command"]()

    assert calls == ["show"]


def test_home_selects_candidate_and_refreshes_selected_marker(monkeypatch) -> None:
    _install_fake_widgets(monkeypatch)
    selected_profile_id = None

    def choices() -> tuple[CardPreviewChoice, ...]:
        return (
            CardPreviewChoice("compact", "精簡方案", selected_profile_id == "compact"),
            CardPreviewChoice("roomy", "寬鬆方案", selected_profile_id == "roomy"),
        )

    def select(profile_id: str) -> None:
        nonlocal selected_profile_id
        selected_profile_id = profile_id

    def clear() -> None:
        nonlocal selected_profile_id
        selected_profile_id = None

    view = home.HomeView(
        None,
        {},
        card_preview_choices_provider=choices,
        on_card_preview_select=select,
        on_card_preview_clear=clear,
    )
    view.build()

    assert "提醒卡樣式" in {
        widget.options.get("text") for widget in _FakeWidget.created
    }
    assert view._card_preview_clear_button is not None
    assert view._card_preview_clear_button.packed is False
    view._card_preview_buttons["roomy"].options["command"]()

    assert selected_profile_id == "roomy"
    assert view._card_preview_buttons["compact"].options["text"] == "精簡方案"
    assert view._card_preview_buttons["roomy"].options["text"] == "✓ 寬鬆方案"
    assert view._card_preview_clear_button.packed is True

    view._card_preview_clear_button.options["command"]()

    assert selected_profile_id is None
    assert view._card_preview_buttons["roomy"].options["text"] == "寬鬆方案"
    assert view._card_preview_clear_button.packed is False


def test_home_reports_preview_errors_without_changing_visible_selection(
    monkeypatch,
) -> None:
    _install_fake_widgets(monkeypatch)
    errors: list[tuple[str, str]] = []

    def choices() -> tuple[CardPreviewChoice, ...]:
        return (
            CardPreviewChoice("compact", "精簡方案", True),
            CardPreviewChoice("roomy", "寬鬆方案", False),
        )

    def fail_select(_profile_id: str) -> None:
        raise OSError("selection write failed")

    def fail_clear() -> None:
        raise OSError("selection delete failed")

    view = home.HomeView(
        None,
        {},
        card_preview_choices_provider=choices,
        on_card_preview_select=fail_select,
        on_card_preview_clear=fail_clear,
        on_card_preview_error=lambda action, error: errors.append(
            (action, str(error))
        ),
    )
    view.build()

    view._card_preview_buttons["roomy"].options["command"]()
    view._card_preview_clear_button.options["command"]()

    assert errors == [
        ("select", "selection write failed"),
        ("clear", "selection delete failed"),
    ]
    assert view._card_preview_buttons["compact"].options["text"] == "✓ 精簡方案"
    assert view._card_preview_buttons["roomy"].options["text"] == "寬鬆方案"
    assert view._card_preview_clear_button.packed is True
