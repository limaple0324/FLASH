import pytest

from ui.soul_stone_editor_window import (
    SoulStoneEditorWindow,
    render_soul_stone_editor,
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
        self.value = ""

    def pack(self, **options):
        self.pack_options = options

    def insert(self, index, value):
        assert index == 0
        self.value = value

    def get(self):
        return self.value


class RecordingWidgetFactory:
    def __init__(self) -> None:
        self.widgets = []

    def __call__(self, parent, **options):
        widget = FakeWidget(parent, **options)
        self.widgets.append(widget)
        return widget


def _editor(**overrides) -> SoulStoneEditorWindow:
    options = {
        "master": object(),
        "on_save": lambda _note: None,
        "on_clear": lambda: None,
        "window_factory": RecordingWindowFactory(),
        "renderer": lambda *_args: None,
    }
    options.update(overrides)
    return SoulStoneEditorWindow(**options)


def test_window_opens_with_player_title_and_normalized_display_name() -> None:
    master = object()
    factory = RecordingWindowFactory()
    rendered = []
    editor = SoulStoneEditorWindow(
        master,
        lambda _note: None,
        lambda: None,
        window_factory=factory,
        renderer=lambda *args: rendered.append(args),
    )

    editor.open(" 小古 ", "本週先保留")

    window = factory.windows[0]
    assert factory.masters == [master]
    assert window.operations[:2] == [
        ("title", "輔｜靈魂石紀錄"),
        ("transient", master),
    ]
    assert window.operations[2][0:2] == ("protocol", "WM_DELETE_WINDOW")
    assert rendered[0][0:3] == (window, "小古", "本週先保留")
    assert editor.is_open is True


@pytest.mark.parametrize(
    ("display_name", "initial_note", "error"),
    [
        (" ", None, ValueError),
        (1, None, TypeError),
        ("小古", 1, TypeError),
    ],
)
def test_window_rejects_invalid_player_facing_values(
    display_name,
    initial_note,
    error,
) -> None:
    editor = _editor()

    with pytest.raises(error):
        editor.open(display_name, initial_note)


def test_window_rejects_duplicate_open_and_closes_once() -> None:
    factory = RecordingWindowFactory()
    editor = _editor(window_factory=factory)

    editor.open("小古")
    with pytest.raises(RuntimeError, match="already open"):
        editor.open("小古")
    editor.close()
    editor.close()

    assert factory.windows[0].operations.count(("destroy",)) == 1
    assert editor.is_open is False


def test_successful_save_closes_window() -> None:
    saved = []
    rendered = []
    editor = _editor(
        on_save=lambda note: saved.append(note),
        renderer=lambda *args: rendered.append(args),
    )
    editor.open("小古")

    rendered[0][3]("稀有靈魂石")

    assert saved == ["稀有靈魂石"]
    assert editor.is_open is False


def test_failed_save_reports_chinese_error_and_keeps_window_open() -> None:
    errors = []
    rendered = []

    def fail(_note):
        raise OSError("disk unavailable")

    editor = _editor(
        on_save=fail,
        renderer=lambda *args: rendered.append(args),
        error_reporter=lambda title, message: errors.append((title, message)),
    )
    editor.open("小古")

    rendered[0][3]("不可保存")

    assert errors == [
        (
            "無法保存靈魂石紀錄",
            "請確認已輸入內容後再試；原本紀錄已保留。",
        )
    ]
    assert editor.is_open is True


def test_successful_clear_closes_and_failed_clear_keeps_window_open() -> None:
    cleared = []
    rendered = []
    editor = _editor(
        on_clear=lambda: cleared.append("clear"),
        renderer=lambda *args: rendered.append(args),
    )
    editor.open("小古")

    rendered[0][4]()

    assert cleared == ["clear"]
    assert editor.is_open is False

    errors = []
    rendered = []

    def fail():
        raise OSError("disk unavailable")

    editor = _editor(
        on_clear=fail,
        renderer=lambda *args: rendered.append(args),
        error_reporter=lambda title, message: errors.append((title, message)),
    )
    editor.open("小古")
    rendered[0][4]()

    assert errors == [
        ("無法清除靈魂石紀錄", "原本紀錄已保留，請稍後再試。")
    ]
    assert editor.is_open is True


def test_renderer_shows_initial_note_and_wires_three_actions() -> None:
    frames = RecordingWidgetFactory()
    labels = RecordingWidgetFactory()
    entries = RecordingWidgetFactory()
    buttons = RecordingWidgetFactory()
    calls = []

    render_soul_stone_editor(
        FakeWindow(),
        "小古",
        "本週先保留",
        lambda note: calls.append(("save", note)),
        lambda: calls.append(("clear",)),
        lambda: calls.append(("close",)),
        frame_factory=frames,
        label_factory=labels,
        entry_factory=entries,
        button_factory=buttons,
    )

    assert labels.widgets[0].options["text"] == "【小古】靈魂石紀錄"
    assert entries.widgets[0].value == "本週先保留"
    assert [button.options["text"] for button in buttons.widgets] == [
        "保存",
        "清除紀錄",
        "取消",
    ]
    entries.widgets[0].value = "更新後"
    for button in buttons.widgets:
        button.options["command"]()
    assert calls == [("save", "更新後"), ("clear",), ("close",)]


def test_renderer_failure_cleans_up_partial_window() -> None:
    factory = RecordingWindowFactory()

    def fail(*_args):
        raise RuntimeError("render failed")

    editor = SoulStoneEditorWindow(
        object(),
        lambda _note: None,
        lambda: None,
        window_factory=factory,
        renderer=fail,
    )

    with pytest.raises(RuntimeError, match="render failed"):
        editor.open("小古")

    assert factory.windows[0].destroyed is True
    assert editor.is_open is False
