import pytest

from services.character_detail_view_service import PlayerCharacterDetail
from ui.character_detail_window import (
    CharacterDetailWindow,
    render_character_detail,
)


def _detail() -> PlayerCharacterDetail:
    return PlayerCharacterDetail(
        display_name="小古",
        group="14支",
        level=120,
        importance="主號",
        role="古",
        note="守紀優先",
        soul_stone="本週先保留稀有靈魂石",
    )


class FakeWindow:
    def __init__(self) -> None:
        self.operations = []
        self.destroyed = False

    def title(self, text):
        self.operations.append(("title", text))

    def transient(self, master):
        self.operations.append(("transient", master))

    def protocol(self, name, callback):
        self.operations.append(("protocol", name, callback))

    def destroy(self):
        self.operations.append(("destroy",))
        self.destroyed = True


class RecordingWindowFactory:
    def __init__(self) -> None:
        self.masters = []
        self.windows = []

    def __call__(self, master):
        window = FakeWindow()
        self.masters.append(master)
        self.windows.append(window)
        return window


class FakeWidget:
    def __init__(self, parent, **options) -> None:
        self.parent = parent
        self.options = options
        self.pack_options = None

    def pack(self, **options):
        self.pack_options = options


class RecordingWidgetFactory:
    def __init__(self) -> None:
        self.widgets = []

    def __call__(self, parent, **options):
        widget = FakeWidget(parent, **options)
        self.widgets.append(widget)
        return widget


def test_window_opens_with_player_title_and_safe_close_callback() -> None:
    master = object()
    factory = RecordingWindowFactory()
    rendered = []
    view = CharacterDetailWindow(
        master,
        window_factory=factory,
        renderer=lambda window, detail, close: rendered.append(
            (window, detail, close)
        ),
    )

    view.open(_detail())

    window = factory.windows[0]
    assert factory.masters == [master]
    assert window.operations[:2] == [
        ("title", "輔｜角色詳細資料"),
        ("transient", master),
    ]
    assert window.operations[2][0:2] == ("protocol", "WM_DELETE_WINDOW")
    assert rendered[0][0:2] == (window, _detail())
    assert view.is_open is True

    rendered[0][2]()
    rendered[0][2]()

    assert window.operations.count(("destroy",)) == 1
    assert view.is_open is False


def test_window_rejects_duplicate_open_and_untrusted_detail() -> None:
    view = CharacterDetailWindow(
        object(),
        window_factory=RecordingWindowFactory(),
        renderer=lambda *_args: None,
    )

    with pytest.raises(TypeError, match="PlayerCharacterDetail"):
        view.open(object())

    view.open(_detail())
    with pytest.raises(RuntimeError, match="already open"):
        view.open(_detail())


def test_window_rejects_untrusted_optional_edit_callback() -> None:
    with pytest.raises(TypeError, match="on_edit_soul_stone"):
        CharacterDetailWindow(object(), on_edit_soul_stone=object())


def test_default_renderer_receives_optional_edit_callback(monkeypatch) -> None:
    factory = RecordingWindowFactory()
    edit = lambda: None
    rendered = []

    def record_render(window, detail, close, **options):
        rendered.append((window, detail, close, options))

    monkeypatch.setattr(
        "ui.character_detail_window.render_character_detail",
        record_render,
    )
    view = CharacterDetailWindow(
        object(),
        on_edit_soul_stone=edit,
        window_factory=factory,
    )

    view.open(_detail())

    assert rendered[0][0:2] == (factory.windows[0], _detail())
    assert rendered[0][3] == {"on_edit_soul_stone": edit}


def test_renderer_failure_cleans_up_partial_window() -> None:
    factory = RecordingWindowFactory()

    def fail(*_args):
        raise RuntimeError("render failed")

    view = CharacterDetailWindow(
        object(),
        window_factory=factory,
        renderer=fail,
    )

    with pytest.raises(RuntimeError, match="render failed"):
        view.open(_detail())

    assert factory.windows[0].destroyed is True
    assert view.is_open is False


def test_default_content_is_chinese_and_keeps_visual_factories_replaceable() -> None:
    frame_factory = RecordingWidgetFactory()
    label_factory = RecordingWidgetFactory()
    button_factory = RecordingWidgetFactory()
    close_calls = []

    render_character_detail(
        FakeWindow(),
        _detail(),
        lambda: close_calls.append("close"),
        frame_factory=frame_factory,
        label_factory=label_factory,
        button_factory=button_factory,
    )

    body = frame_factory.widgets[0]
    label = label_factory.widgets[0]
    button = button_factory.widgets[0]
    assert body.options == {"padx": 24, "pady": 20}
    assert label.options["text"] == (
        "【小古】\n"
        "組別：14支\n"
        "等級：120\n"
        "分類：主號\n"
        "定位：古\n"
        "備註：守紀優先\n"
        "靈魂石：本週先保留稀有靈魂石"
    )
    assert button.options["text"] == "關閉"
    button.options["command"]()
    assert close_calls == ["close"]


def test_optional_soul_stone_edit_entry_is_hidden_until_injected() -> None:
    frame_factory = RecordingWidgetFactory()
    label_factory = RecordingWidgetFactory()
    button_factory = RecordingWidgetFactory()
    calls = []

    render_character_detail(
        FakeWindow(),
        _detail(),
        lambda: calls.append("close"),
        on_edit_soul_stone=lambda: calls.append("edit"),
        frame_factory=frame_factory,
        label_factory=label_factory,
        button_factory=button_factory,
    )

    assert [button.options["text"] for button in button_factory.widgets] == [
        "編輯靈魂石",
        "關閉",
    ]
    assert button_factory.widgets[0].pack_options["pady"] == (20, 0)
    assert button_factory.widgets[1].pack_options["pady"] == (8, 0)
    button_factory.widgets[0].options["command"]()
    button_factory.widgets[1].options["command"]()
    assert calls == ["edit", "close"]
