import pytest

from services.character_detail_view_service import PlayerCharacterDetail
from ui.character_list_window import (
    CharacterListWindow,
    render_character_list,
)


def _detail(
    name: str = "小古",
    group: str | None = "14支",
) -> PlayerCharacterDetail:
    return PlayerCharacterDetail(
        display_name=name,
        group=group,
        level=120,
        importance="主號",
        role="古",
        note="守紀優先",
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


def test_window_opens_once_and_forwards_explicit_selection() -> None:
    master = object()
    factory = RecordingWindowFactory()
    rendered = []
    selected = []
    view = CharacterListWindow(
        master,
        selected.append,
        window_factory=factory,
        renderer=lambda window, details, on_select, close: rendered.append(
            (window, details, on_select, close)
        ),
    )

    view.open((_detail(),))

    window = factory.windows[0]
    assert factory.masters == [master]
    assert window.operations[:2] == [
        ("title", "輔｜組別角色"),
        ("transient", master),
    ]
    assert window.operations[2][0:2] == ("protocol", "WM_DELETE_WINDOW")
    assert rendered[0][1] == (_detail(),)
    rendered[0][2](_detail())
    assert selected == [_detail()]

    with pytest.raises(RuntimeError, match="already open"):
        view.open((_detail("小法"),))

    rendered[0][3]()
    rendered[0][3]()
    assert window.operations.count(("destroy",)) == 1
    assert view.is_open is False


def test_window_rejects_untrusted_values_and_cleans_up_render_failure() -> None:
    factory = RecordingWindowFactory()
    view = CharacterListWindow(
        object(),
        lambda _detail: None,
        window_factory=factory,
        renderer=lambda *_args: (_ for _ in ()).throw(RuntimeError("render failed")),
    )

    with pytest.raises(TypeError, match="PlayerCharacterDetail"):
        view.open((object(),))
    with pytest.raises(RuntimeError, match="render failed"):
        view.open((_detail(),))

    assert factory.windows[0].destroyed is True
    assert view.is_open is False


def test_renderer_groups_player_facing_buttons_without_internal_identifiers() -> None:
    frames = RecordingWidgetFactory()
    labels = RecordingWidgetFactory()
    buttons = RecordingWidgetFactory()
    selected = []
    close_calls = []

    render_character_list(
        FakeWindow(),
        (_detail(), _detail("待整理角色", None)),
        selected.append,
        lambda: close_calls.append("close"),
        frame_factory=frames,
        label_factory=labels,
        button_factory=buttons,
    )

    assert [widget.options["text"] for widget in labels.widgets] == [
        "【14支】",
        "【未分組】",
    ]
    assert [widget.options["text"] for widget in buttons.widgets] == [
        "小古｜120 級｜主號｜古",
        "待整理角色｜120 級｜主號｜古",
        "關閉",
    ]
    assert "character_id" not in " ".join(
        widget.options["text"] for widget in buttons.widgets
    )

    buttons.widgets[1].options["command"]()
    buttons.widgets[-1].options["command"]()
    assert selected == [_detail("待整理角色", None)]
    assert close_calls == ["close"]


def test_renderer_has_clear_empty_state() -> None:
    labels = RecordingWidgetFactory()
    buttons = RecordingWidgetFactory()

    render_character_list(
        FakeWindow(),
        (),
        lambda _detail: None,
        lambda: None,
        frame_factory=RecordingWidgetFactory(),
        label_factory=labels,
        button_factory=buttons,
    )

    assert labels.widgets[0].options["text"] == (
        "目前沒有可顯示的組別與角色資料。"
    )
