from config.config_manager import ConfigManager
from services.feature_card_layout_service import (
    FEATURE_CARD_LAYOUT_CONFIG_KEY,
    FeatureCardLayoutService,
)


def _service(tmp_path):
    config = ConfigManager(tmp_path / "settings.json")
    return config, FeatureCardLayoutService(config)


def test_default_order_and_preferences_do_not_write_configuration(tmp_path):
    config, service = _service(tmp_path)

    assert service.order_for("home", ("home.a", "home.b")) == (
        "home.a",
        "home.b",
    )
    assert service.preference("home.a", "預設標題").title == "預設標題"
    assert service.preference("home.a", "預設標題").collapsed is False
    assert FEATURE_CARD_LAYOUT_CONFIG_KEY not in config.data


def test_reorder_collapse_and_title_survive_restart(tmp_path):
    config, service = _service(tmp_path)
    service.reorder(
        "home",
        ("home.b", "home.a"),
        ("home.a", "home.b"),
    )
    service.set_collapsed("home.b", True)
    service.set_title("home.b", "我的卡片")

    restored = FeatureCardLayoutService(
        ConfigManager(config.config_path)
    )

    assert restored.order_for("home", ("home.a", "home.b")) == (
        "home.b",
        "home.a",
    )
    preference = restored.preference("home.b", "預設標題")
    assert preference.title == "我的卡片"
    assert preference.collapsed is True


def test_new_cards_are_appended_without_losing_saved_order(tmp_path):
    _, service = _service(tmp_path)
    service.reorder(
        "sync",
        ("sync.second", "sync.first"),
        ("sync.first", "sync.second"),
    )

    assert service.order_for(
        "sync",
        ("sync.first", "sync.second", "sync.new"),
    ) == ("sync.second", "sync.first", "sync.new")


def test_invalid_reorder_never_replaces_previous_layout(tmp_path):
    config, service = _service(tmp_path)
    service.reorder(
        "home",
        ("home.b", "home.a"),
        ("home.a", "home.b"),
    )
    before = dict(config.data)

    try:
        service.reorder(
            "home",
            ("home.a",),
            ("home.a", "home.b"),
        )
    except ValueError:
        pass
    else:
        raise AssertionError("invalid order must be rejected")

    assert config.data == before


def test_blank_or_oversized_title_is_rejected_without_saving(tmp_path):
    config, service = _service(tmp_path)

    for value in ("", " " * 4, "字" * 81):
        try:
            service.set_title("home.a", value)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid title must be rejected")

    assert FEATURE_CARD_LAYOUT_CONFIG_KEY not in config.data


def test_reset_title_keeps_collapsed_state(tmp_path):
    _, service = _service(tmp_path)
    service.set_collapsed("home.a", True)
    service.set_title("home.a", "自訂")

    service.reset_title("home.a")

    preference = service.preference("home.a", "預設")
    assert preference.title == "預設"
    assert preference.collapsed is True
