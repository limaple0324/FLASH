import pytest

from ui.card_content_renderer import CardContent
from ui.tk_card_presenter import TkCardContentPresenter, TkCardTextSettings


def _content(*, next_step="返回競技場繼續守紀"):
    return CardContent(
        group_name="14支",
        activity_name="守紀",
        current_progress="120古正在進行第二次所羅門",
        next_step=next_step,
    )


def _settings(**changes):
    values = {
        "background": "temporary-background",
        "foreground": "temporary-foreground",
        "font_family": "temporary-font",
        "font_size": 12,
        "horizontal_padding": 10,
        "vertical_padding": 8,
        "line_spacing": 4,
    }
    values.update(changes)
    return TkCardTextSettings(**values)


class FakeWidget:
    def __init__(self, factory, kind, parent, options):
        self.factory = factory
        self.kind = kind
        self.parent = parent
        self.options = dict(options)
        self.visible = False
        self.pack_options = None

    def configure(self, **options):
        self.options.update(options)

    def pack(self, **options):
        self.visible = True
        self.pack_options = options
        self.factory.pack_events.append(self)

    def pack_forget(self):
        self.visible = False


class RecordingWidgetFactory:
    def __init__(self):
        self.widgets = []
        self.pack_events = []

    def _create(self, kind, parent, options):
        widget = FakeWidget(self, kind, parent, options)
        self.widgets.append(widget)
        return widget

    def frame(self, parent, **options):
        return self._create("frame", parent, options)

    def label(self, parent, **options):
        return self._create("label", parent, options)


class FakeWindow:
    pass


def _presenter(settings=None):
    factory = RecordingWidgetFactory()
    presenter = TkCardContentPresenter(
        settings or _settings(),
        widget_factory=factory,
    )
    return presenter, factory


def test_first_render_builds_real_frame_and_four_text_labels():
    presenter, factory = _presenter()
    window = FakeWindow()

    presenter.render(window, _content())

    frame, *labels = factory.widgets
    assert frame.kind == "frame"
    assert frame.options == {"background": "temporary-background"}
    assert frame.pack_options == {
        "fill": "both",
        "expand": True,
    }
    assert [label.options["text"] for label in labels] == [
        "14支",
        "守紀",
        "120古正在進行第二次所羅門",
        "返回競技場繼續守紀",
    ]
    assert all(label.visible for label in labels)
    assert all(label.options["font"] == ("temporary-font", 12) for label in labels)
    assert labels[0].pack_options["pady"] == (8, 4)
    assert labels[-1].pack_options["pady"] == (0, 8)
    assert all(label.pack_options["padx"] == 10 for label in labels)


def test_missing_next_step_is_hidden_without_creating_placeholder_text():
    presenter, factory = _presenter()

    presenter.render(FakeWindow(), _content(next_step=None))

    next_step_label = factory.widgets[-1]
    assert next_step_label.options["text"] == ""
    assert next_step_label.visible is False


def test_update_reuses_widgets_and_refreshes_changed_text():
    presenter, factory = _presenter()
    window = FakeWindow()
    presenter.render(window, _content())
    initial_widgets = tuple(factory.widgets)

    presenter.render(
        window,
        CardContent(
            group_name="14支",
            activity_name="守紀",
            current_progress="已恢復登入",
            next_step="返回守紀畫面",
        ),
    )

    assert tuple(factory.widgets) == initial_widgets
    assert factory.widgets[3].options["text"] == "已恢復登入"
    assert factory.widgets[4].options["text"] == "返回守紀畫面"


def test_field_order_and_spacing_are_controlled_by_settings():
    presenter, factory = _presenter(
        _settings(
            field_order=(
                "activity_name",
                "group_name",
                "next_step",
                "current_progress",
            ),
            line_spacing=7,
        )
    )
    presenter.render(FakeWindow(), _content())

    packed_labels = [widget for widget in factory.pack_events if widget.kind == "label"]
    assert [label.options["text"] for label in packed_labels] == [
        "守紀",
        "14支",
        "返回競技場繼續守紀",
        "120古正在進行第二次所羅門",
    ]
    assert [label.pack_options["pady"] for label in packed_labels] == [
        (8, 7),
        (0, 7),
        (0, 7),
        (0, 8),
    ]


def test_invalid_visual_settings_are_rejected():
    with pytest.raises(ValueError, match="font_size"):
        _settings(font_size=0)
    with pytest.raises(ValueError, match="field_order"):
        _settings(field_order=("group_name",) * 4)
    with pytest.raises(ValueError, match="line_spacing"):
        _settings(line_spacing=-1)


def test_invalid_content_is_rejected_before_widgets_are_created():
    presenter, factory = _presenter()

    with pytest.raises(TypeError, match="CardContent"):
        presenter.render(FakeWindow(), object())

    assert factory.widgets == []
