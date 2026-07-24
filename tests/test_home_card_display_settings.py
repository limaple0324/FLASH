from ui import home


class _FakeWidget:
    created: list["_FakeWidget"] = []

    def __init__(self, parent=None, **kwargs) -> None:
        self.parent = parent
        self.options = dict(kwargs)
        self.value = ""
        self.created.append(self)

    def pack(self, **_kwargs) -> None:
        pass

    def configure(self, **kwargs) -> None:
        self.options.update(kwargs)

    def get(self) -> str:
        return self.value

    def delete(self, _start, _end) -> None:
        self.value = ""

    def insert(self, _index, value) -> None:
        self.value = str(value)


def _install_fake_widgets(monkeypatch) -> None:
    _FakeWidget.created = []
    monkeypatch.setattr(home, "Frame", _FakeWidget)
    monkeypatch.setattr(home, "Label", _FakeWidget)
    monkeypatch.setattr(home, "Button", _FakeWidget)
    monkeypatch.setattr(home, "Entry", _FakeWidget)


def _button(text: str) -> _FakeWidget:
    return next(
        widget
        for widget in _FakeWidget.created
        if widget.options.get("text") == text
    )


def test_home_updates_and_refreshes_card_display_seconds(monkeypatch) -> None:
    _install_fake_widgets(monkeypatch)
    state = {"seconds": 30}
    updates = []

    def update(seconds: int) -> None:
        updates.append(seconds)
        state["seconds"] = seconds

    view = home.HomeView(
        None,
        {},
        card_display_seconds_provider=lambda: state["seconds"],
        on_card_display_seconds_update=update,
    )
    view.build()

    assert view._card_display_seconds_entry is not None
    assert view._card_display_seconds_entry.get() == "30"
    view._card_display_seconds_entry.delete(0, "end")
    view._card_display_seconds_entry.insert(0, "75")
    _button("儲存顯示時間").options["command"]()

    assert updates == [75]
    assert view._card_display_seconds_entry.get() == "75"


def test_home_restores_current_seconds_and_reports_invalid_or_failed_updates(
    monkeypatch,
) -> None:
    _install_fake_widgets(monkeypatch)
    errors = []

    def fail_update(_seconds: int) -> None:
        raise OSError("settings write failed")

    view = home.HomeView(
        None,
        {},
        card_display_seconds_provider=lambda: 30,
        on_card_display_seconds_update=fail_update,
        on_card_display_seconds_error=errors.append,
    )
    view.build()
    entry = view._card_display_seconds_entry
    assert entry is not None

    entry.delete(0, "end")
    entry.insert(0, "不是數字")
    view.update_card_display_seconds()

    assert isinstance(errors[0], ValueError)
    assert entry.get() == "30"

    entry.delete(0, "end")
    entry.insert(0, "60")
    view.update_card_display_seconds()

    assert isinstance(errors[1], OSError)
    assert entry.get() == "30"
