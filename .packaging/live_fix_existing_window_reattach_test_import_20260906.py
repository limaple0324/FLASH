from pathlib import Path

TEST = Path("legacy/fu-v02-reconnect-preview/test_fu_reconnect_integration.py")
text = TEST.read_text(encoding="utf-8")
anchor = "from pathlib import Path\n"
addition = "from pathlib import Path\nfrom datetime import datetime, timezone\n"
if "from datetime import datetime, timezone\n" not in text:
    if text.count(anchor) != 1:
        raise SystemExit(f"test datetime import anchor count={text.count(anchor)}")
    text = text.replace(anchor, addition, 1)
TEST.write_text(text, encoding="utf-8", newline="\n")
print("LIVE_FIX_TEST_IMPORT_APPLIED existing-window reattach datetime fixtures")
