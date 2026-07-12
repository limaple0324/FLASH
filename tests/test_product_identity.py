from core.version import PRODUCT_NAME as VERSION_PRODUCT_NAME
from product.identity import PRODUCT_NAME, TECHNICAL_NAME


def test_product_name_and_technical_name_have_separate_roles():
    assert PRODUCT_NAME == "輔"
    assert TECHNICAL_NAME == "FLASH"
    assert VERSION_PRODUCT_NAME == PRODUCT_NAME


def test_main_window_uses_the_product_name():
    from main import APP_TITLE

    assert APP_TITLE == PRODUCT_NAME
