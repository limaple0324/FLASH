from domain.soul_stone_store import SoulStoneStore
from main import build_services
from services.app_context import AppContext
from services.soul_stone_service import SoulStoneService


def test_unconfirmed_legacy_character_field_is_not_in_normal_flow(tmp_path) -> None:
    legacy_path = tmp_path / "data" / "soul_stones.json"
    legacy_path.parent.mkdir(parents=True)
    original = '{"schema_version": 1, "records": []}\n'
    legacy_path.write_text(original, encoding="utf-8")

    build_services(tmp_path)

    assert AppContext.get(SoulStoneStore) is None
    assert AppContext.get(SoulStoneService) is None
    assert legacy_path.read_text(encoding="utf-8") == original
