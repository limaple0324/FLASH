from core.version import (
    DELIVERY_LABEL,
    DELIVERY_SCOPE,
    DISPLAY_VERSION,
    ENGINE_MILESTONE,
    ENGINE_VERSION,
    MILESTONE,
    PRODUCT_NAME as VERSION_PRODUCT_NAME,
    VERSION,
)
from product.identity import PRODUCT_NAME, TECHNICAL_NAME


def test_product_name_and_technical_name_have_separate_roles():
    assert PRODUCT_NAME == "輔"
    assert TECHNICAL_NAME == "FLASH"
    assert VERSION_PRODUCT_NAME == PRODUCT_NAME


def test_main_window_uses_the_product_name():
    from main import APP_TITLE

    assert APP_TITLE == PRODUCT_NAME


def test_delivery_scope_is_separate_from_the_sp1_engine_contract():
    assert ENGINE_MILESTONE == MILESTONE == "SP1"
    assert ENGINE_VERSION == VERSION == "0.1.2"
    assert DELIVERY_SCOPE == "SP1+SP2+SP3"
    assert DELIVERY_LABEL == "整合工程驗證版"
    assert DISPLAY_VERSION == "整合工程驗證版 SP1+SP2+SP3 (SP1 Engine 0.1.2)"
