from cards.service import CardService
from services.card_overlay_layout_service import CardOverlayLayout
from services.card_overlay_sync_service import CardOverlaySyncService


class Layout:
    def __init__(self):
        self.fail = False
        self.calls = 0

    def snapshot(self):
        self.calls += 1
        if self.fail:
            raise RuntimeError("layout fault")
        return CardOverlayLayout()


class Lifecycle:
    def __init__(self):
        self.sync_calls = 0
        self.close_calls = 0
        self.fail_close = False

    def sync(self, _layout):
        self.sync_calls += 1

    def close_all(self):
        self.close_calls += 1
        if self.fail_close:
            raise RuntimeError("close fault")


def test_failed_start_unsubscribes_and_retry_starts_once():
    cards = CardService()
    layout = Layout()
    lifecycle = Lifecycle()
    service = CardOverlaySyncService(cards, layout, lifecycle)
    layout.fail = True

    assert service.start() is False
    assert service.running is False
    assert service.last_error is not None

    layout.fail = False
    assert service.start() is True
    assert service.running is True
    baseline = layout.calls
    cards.resync()
    assert layout.calls == baseline + 1


def test_failed_close_can_be_retried_without_resubscribing():
    cards = CardService()
    layout = Layout()
    lifecycle = Lifecycle()
    service = CardOverlaySyncService(cards, layout, lifecycle)
    assert service.start() is True
    lifecycle.fail_close = True

    assert service.stop() is False
    assert service.running is False
    baseline = layout.calls
    cards.resync()
    assert layout.calls == baseline

    lifecycle.fail_close = False
    assert service.stop() is True
    assert lifecycle.close_calls == 2
