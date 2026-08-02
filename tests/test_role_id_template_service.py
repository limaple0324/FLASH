from adapters.windows_background_capture import CaptureSample
from services.role_id_template_service import (
    RoleIdTemplateService,
    clean_role_id_text,
    signature_distance,
    signature_from_sample,
)


class CaptureBackend:
    def __init__(self, sample):
        self.sample = sample
        self.calls = []

    def capture_client_region(self, handle, region):
        self.calls.append((handle, region))
        return self.sample


def sample_with_white_pixels(count, *, width=90, height=24):
    pixels = bytearray(b"\x00\x00\x00\xff" * (width * height))
    for index in range(count):
        offset = index * 4
        pixels[offset : offset + 4] = b"\xff\xff\xff\xff"
    return CaptureSample(width, height, bytes(pixels), True)


def test_legacy_role_id_text_cleanup_and_hamming_distance():
    assert clean_role_id_text(" 11I| 角色-甲_1! ") == "角色-甲_1"
    assert signature_distance("1010", "1001") == 2
    assert signature_distance("101", "10100") == 2


def test_signature_uses_legacy_brightness_and_colour_spread_thresholds():
    sample = CaptureSample(
        3,
        1,
        (
            b"\xff\xff\xff\xff"
            b"\x00\x00\xff\xff"
            b"\xa9\xa9\xa9\xff"
        ),
        True,
    )

    width, height, signature, count = signature_from_sample(sample)

    assert (width, height, signature, count) == (3, 1, "100", 1)


def test_calibrate_then_read_matches_same_passive_region(tmp_path):
    backend = CaptureBackend(sample_with_white_pixels(120))
    service = RoleIdTemplateService(
        tmp_path / "role-id.json",
        capture_backend=backend,
    )

    calibrated = service.calibrate(123, "001|角色甲", entry_id="entry-a")
    read = service.read(123, entry_id="entry-a")

    assert calibrated.success is True
    assert calibrated.role_id == "角色甲"
    assert read.success is True
    assert read.role_id == "角色甲"
    assert read.score == 0
    assert backend.calls == [
        (123, (87, 13, 177, 37)),
        (123, (87, 13, 177, 37)),
    ]


def test_role_id_capture_with_too_few_features_fails_without_template(tmp_path):
    service = RoleIdTemplateService(
        tmp_path / "role-id.json",
        capture_backend=CaptureBackend(sample_with_white_pixels(99)),
    )

    result = service.calibrate(123, "角色甲")

    assert result.success is False
    assert "足夠文字特徵" in result.message
    assert not service.path.exists()


def test_role_id_template_is_bound_to_its_configured_role_entry(tmp_path):
    service = RoleIdTemplateService(
        tmp_path / "role-id.json",
        capture_backend=CaptureBackend(sample_with_white_pixels(120)),
    )

    calibrated = service.calibrate("123", "120古", entry_id="entry-a")
    other_entry = service.read("123", entry_id="entry-b")

    assert calibrated.success is True
    assert calibrated.role_id == "120古"
    assert other_entry.success is False
    assert "沒有可用" in other_entry.message
