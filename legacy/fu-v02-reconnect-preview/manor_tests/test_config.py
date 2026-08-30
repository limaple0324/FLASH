from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from manor_assistant.config_store import ConfigStore
from manor_assistant.models import AppConfig, Profile


class ConfigStoreTests(unittest.TestCase):
    def test_round_trip_preserves_profile_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = ConfigStore(Path(directory) / "config.json")
            config = AppConfig(
                profiles=[
                    Profile(
                        shortcut_path=r"C:\Games\角色一.lnk",
                        shortcut_name="角色一",
                        crop_key="rare_crystal",
                        quantity=7,
                    )
                ]
            )
            store.save(config)
            loaded = store.load()
            self.assertEqual(loaded.interval_minutes, 60)
            self.assertEqual(loaded.retry_minutes, 3)
            self.assertEqual(loaded.profiles[0].shortcut_name, "角色一")
            self.assertEqual(loaded.profiles[0].crop.resource, "水晶")
            self.assertEqual(loaded.profiles[0].quantity, 7)


if __name__ == "__main__":
    unittest.main()
