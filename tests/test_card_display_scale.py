from main import card_display_scale, configure_fixed_tk_ui_scaling


class _Window:
    def __init__(self, dpi):
        self.dpi = dpi

    def winfo_fpixels(self, _value):
        return self.dpi


class _TkWindow:
    def __init__(self, scaling):
        self.scaling = scaling
        self.bound = None

    class _Tk:
        def __init__(self, owner):
            self.owner = owner

        def call(self, *args):
            if args[:2] == ("tk", "scaling") and len(args) == 3:
                self.owner.scaling = args[2]
            return self.owner.scaling

    @property
    def tk(self):
        return self._Tk(self)

    def bind(self, sequence, callback, add=None):
        self.bound = (sequence, callback, add)
        return "configure"


def test_card_display_scale_uses_96_dpi_as_the_100_percent_baseline():
    assert card_display_scale(_Window(96)) == 1.0
    assert card_display_scale(_Window(120)) == 1.25
    assert card_display_scale(_Window(144)) == 1.5


def test_card_display_scale_falls_back_safely():
    assert card_display_scale(_Window(0)) == 1.0
    assert card_display_scale(object()) == 1.0


def test_main_tk_scaling_is_restored_after_monitor_change():
    window = _TkWindow(2.0)

    assert configure_fixed_tk_ui_scaling(window) == 2.0
    assert card_display_scale(window) == 1.5
    assert window.bound is not None

    window.scaling = 1.25
    window.bound[1]()

    assert window.scaling == 2.0
    assert card_display_scale(window) == 1.5
