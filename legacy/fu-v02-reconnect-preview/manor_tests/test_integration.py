from __future__ import annotations

import threading
import time
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

import manor_runtime
from manor_assistant.models import RunResult


class FakeWorkflow:
    calls: list[tuple[str, int]] = []

    def __init__(self, _vision, _log) -> None:
        pass

    def run(self, profile, hwnd, _cancelled):
        self.calls.append((profile.crop_key, hwnd))
        return RunResult.success("模擬完成")


class ManorIntegrationTests(unittest.TestCase):
    def test_enabled_binding_runs_immediately_and_uses_same_hwnd(self) -> None:
        FakeWorkflow.calls = []
        stop = threading.Event()
        binding = {
            "profile_key": "role-one",
            "shortcut_path": r"C:\Game\role-one.lnk",
            "shortcut_name": "角色一",
            "manor_enabled": True,
            "manor_crop_key": "rare_tree",
            "manor_quantity": 3,
        }
        with tempfile.TemporaryDirectory() as td:
            manager = manor_runtime.ManorManager(
                stop,
                lambda: {1234: binding},
                __import__("logging").getLogger("test"),
                progress_path=Path(td) / "progress.json",
            )
            with patch.object(manor_runtime, "VisionEngine", lambda: object()), patch.object(manor_runtime, "ManorWorkflow", FakeWorkflow):
                manager.start()
                deadline = time.monotonic() + 2.0
                while not FakeWorkflow.calls and time.monotonic() < deadline:
                    time.sleep(0.02)
                stop.set()
                manager.join(1.0)
        self.assertEqual(FakeWorkflow.calls, [("rare_tree", 1234)])
        state = manager._states["role-one"]
        self.assertEqual(state["status"], "模擬完成")
        self.assertIsNotNone(state["next_due"])

    def test_active_hwnd_gate_is_exact(self) -> None:
        manor_runtime._set_hwnd_active(55, True)
        try:
            self.assertTrue(manor_runtime.is_hwnd_active(55))
            self.assertFalse(manor_runtime.is_hwnd_active(56))
        finally:
            manor_runtime._set_hwnd_active(55, False)

    def test_interrupted_role_is_due_on_next_start(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "progress.json"
            path.write_text(
                '{"profiles":{"role-one":{"status":"執行中","running":true,"next_due":""}}}',
                encoding="utf-8",
            )
            manager = manor_runtime.ManorManager(
                threading.Event(), lambda: {}, __import__("logging").getLogger("test"), progress_path=path
            )
            state = manager._states["role-one"]
            self.assertFalse(state["running"])
            self.assertIsNotNone(state["next_due"])
            self.assertIn("續跑", state["status"])


if __name__ == "__main__":
    unittest.main()
