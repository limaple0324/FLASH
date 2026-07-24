"""「輔」的版本與工程階段資訊。"""

from product.identity import PRODUCT_NAME, TECHNICAL_NAME

ENGINE_MILESTONE = "SP1"
ENGINE_VERSION = "0.1.2"

# Keep the original names stable for the SP1 engine and self-check contract.
MILESTONE = ENGINE_MILESTONE
VERSION = ENGINE_VERSION

# Delivery scope is intentionally separate from the engine contract.  The
# integration branch contains cumulative SP1, SP2, and SP3 work, but it is
# still an engineering snapshot rather than a formal release.
DELIVERY_SCOPE = "SP1+SP2+SP3"
DELIVERY_LABEL = "整合工程驗證版"
DISPLAY_VERSION = (
    f"{DELIVERY_LABEL} {DELIVERY_SCOPE} "
    f"({ENGINE_MILESTONE} Engine {ENGINE_VERSION})"
)
