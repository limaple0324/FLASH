from main import card_display_scale


class _Window:
    def __init__(self, dpi):
        self.dpi = dpi

    def winfo_fpixels(self, _value):
        return self.dpi


def test_card_display_scale_uses_96_dpi_as_the_100_percent_baseline():
    assert card_display_scale(_Window(96)) == 1.0
    assert card_display_scale(_Window(120)) == 1.25
    assert card_display_scale(_Window(144)) == 1.5


def test_card_display_scale_falls_back_safely():
    assert card_display_scale(_Window(0)) == 1.0
    assert card_display_scale(object()) == 1.0
