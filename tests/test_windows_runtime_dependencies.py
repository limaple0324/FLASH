import re
from pathlib import Path
from zoneinfo import ZoneInfo


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_requirements_include_windows_time_zone_database() -> None:
    requirements = (
        PROJECT_ROOT / "requirements.txt"
    ).read_text(encoding="utf-8").splitlines()
    package_names = {
        re.split(r"[<>=!~\[]", line.strip(), maxsplit=1)[0].lower()
        for line in requirements
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "tzdata" in package_names


def test_taipei_time_zone_is_available() -> None:
    assert ZoneInfo("Asia/Taipei").key == "Asia/Taipei"
