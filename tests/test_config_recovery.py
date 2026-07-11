import json

from config.config_manager import ConfigManager


def test_corrupt_config_is_preserved_and_rebuilt(tmp_path):
    config_path = tmp_path / "config" / "settings.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('{"version": ', encoding="utf-8")

    config = ConfigManager(config_path)

    assert config.recovered_from_corruption is True
    assert config.corrupt_backup_path is not None
    assert config.corrupt_backup_path.exists()
    assert config.corrupt_backup_path.read_text(encoding="utf-8") == '{"version": '
    assert json.loads(config_path.read_text(encoding="utf-8")) == {}


def test_non_object_config_is_recovered(tmp_path):
    config_path = tmp_path / "settings.json"
    config_path.write_text('["not", "an", "object"]', encoding="utf-8")

    config = ConfigManager(config_path)

    assert config.recovered_from_corruption is True
    assert config.data == {}


def test_save_replaces_file_without_leaving_temporary_file(tmp_path):
    config_path = tmp_path / "settings.json"
    config = ConfigManager(config_path)

    config.set("workspace_enabled", True)

    assert json.loads(config_path.read_text(encoding="utf-8"))["workspace_enabled"] is True
    assert not config_path.with_suffix(".json.tmp").exists()
