from __future__ import annotations

import ast
from dataclasses import replace
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from runtime_paths import sanitized_record
from v02_game_clock import ClockSample, SourceIdentity
from v02_faithful_game_time import (
    APPROVED_SHORTCUTS,
    ApprovedShortcutCatalog,
    FaithfulConsensus,
    FaithfulSample,
    MultiGameClockSource,
    windows_desktop_known_folder,
)


HERE = Path(__file__).resolve().parent
FLASH_SYNC = HERE / "flash_sync_v02.py"


def identity(hwnd: int = 101, pid: int = 201, label: str = "120古") -> SourceIdentity:
    return SourceIdentity(
        hwnd, pid, 301, 401, "launch:" + label, "A" * 64,
        os.path.normcase(os.path.abspath("GameLoader.exe")),
    )


def sample(source: SourceIdentity, server_ms: int, anchor_ns: int) -> ClockSample:
    return ClockSample(source, server_ms, anchor_ns, 1, 1, 1000, "test")


class InlineThread:
    def __init__(self, *, target, **_kwargs):
        self.target = target
        self.alive = False

    def start(self):
        self.alive = True
        try:
            self.target()
        finally:
            self.alive = False

    def is_alive(self):
        return self.alive


class FakeReader:
    def __init__(self, source, rows):
        self.source = source
        self.rows = rows
        self.native = SimpleNamespace(identity=lambda hwnd: self.source)

    def stream(self, hwnd, cancel, publish):
        if hwnd != self.source.hwnd:
            raise AssertionError("unexpected hwnd")
        for row in self.rows:
            if cancel.is_set():
                return
            publish(*row)


class ShortcutIdentityTests(unittest.TestCase):
    def _catalog(self, rows):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        desktop = Path(root.name)
        for label in APPROVED_SHORTCUTS:
            (desktop / f"{label}.lnk").write_bytes(b"lnk")
        return ApprovedShortcutCatalog(
            lambda arguments: "launch:" + arguments,
            resolver=lambda path: rows[Path(path).stem],
            desktop=str(desktop),
        )

    def test_binding_contains_normalized_target_label_and_session_launch_identity(self):
        rows = {label: ("GameLoader.exe", label) for label in APPROVED_SHORTCUTS}
        bindings = self._catalog(rows).bindings()
        self.assertEqual(tuple(item.label for item in bindings), APPROVED_SHORTCUTS)
        for item in bindings:
            self.assertTrue(item.normalized_target)
            self.assertEqual(item.launch_identity, "launch:" + item.label)
            self.assertFalse(hasattr(item, "arguments"))

    def test_selector_checks_target_and_rejects_duplicate_or_many_to_one(self):
        rows = {label: ("GameLoader.exe", label) for label in APPROVED_SHORTCUTS}
        catalog = self._catalog(rows)
        valid = [identity(index + 1, index + 11, label)
                 for index, label in enumerate(APPROVED_SHORTCUTS)]
        self.assertEqual(tuple(label for label, _item in catalog.select(valid)), APPROVED_SHORTCUTS)
        wrong_target = list(valid)
        wrong_target[0] = replace(wrong_target[0], normalized_target=os.path.abspath("other.exe"))
        self.assertEqual(catalog.select(wrong_target), ())

    def test_selector_ambiguity_fails_closed_without_dict_last_wins(self):
        rows = {label: ("GameLoader.exe", label) for label in APPROVED_SHORTCUTS}
        catalog = self._catalog(rows)
        one = identity(1, 11, "120古")
        duplicate_process = SourceIdentity(
            2, one.pid, one.tid, one.created, one.launch_fingerprint,
            one.image_sha256, one.normalized_target,
        )
        self.assertEqual(catalog.select((one, duplicate_process)), ())

    def test_duplicate_launch_identity_across_different_targets_fails_closed(self):
        rows = {label: (f"{label}.exe", "same") for label in APPROVED_SHORTCUTS}
        self.assertEqual(self._catalog(rows).bindings(), ())

    def test_default_desktop_is_windows_known_folder_and_has_no_profile_fallback(self):
        source = (HERE / "v02_faithful_game_time.py").read_text(encoding="utf-8")
        self.assertIn("SHGetKnownFolderPath", source)
        self.assertNotIn('os.environ.get("USERPROFILE"', source)
        self.assertNotIn("Public\\Desktop", source)

    def test_windows_known_folder_resolves_current_user_desktop_on_host(self):
        self.assertEqual(os.name, "nt")
        desktop = windows_desktop_known_folder()
        self.assertTrue(desktop)
        self.assertTrue(Path(desktop).is_dir())


class ConsensusAndEventTests(unittest.TestCase):
    def test_stale_generation_is_rejected_without_reversing_control_generation(self):
        consensus = FaithfulConsensus()
        consensus.invalidate("120古", 7)
        accepted = consensus.add(FaithfulSample("120古", 6, "12:34", "minute"))
        self.assertFalse(accepted)
        self.assertEqual(consensus.generation["120古"], 7)

    def test_end_to_end_source_requires_three_confirmed_samples_before_anchor(self):
        now = [1_000_000_000]
        source_id = identity()
        rows = [(sample(source_id, 1_700_000_000_000, now[0]), "", now[0])]
        model = MultiGameClockSource(
            lambda: FakeReader(source_id, rows),
            lambda _cancel: (("120古", source_id),),
            monotonic_ns=lambda: now[0],
            thread_factory=InlineThread,
        )
        model.poll()
        self.assertNotIn("120古", model.anchors)

    def test_queue_overflow_sets_out_of_band_fault_and_rotates_epoch(self):
        model = MultiGameClockSource(
            lambda: None, lambda _cancel: (), monotonic_ns=lambda: 1,
            thread_factory=InlineThread,
        )
        epoch = model.event_epoch
        while True:
            try:
                model.events.put_nowait((epoch, "noise", (), 1))
            except queue.Full:
                break
        model._emit("sample", (), 1, epoch)
        self.assertTrue(model.overflow_fault.is_set())
        model.poll()
        self.assertGreater(model.event_epoch, epoch)
        self.assertEqual(model.status, "來源失效")


class PrivacyAndUiTests(unittest.TestCase):
    def test_persistence_scrubs_legacy_and_session_sensitive_identity_fields(self):
        cleaned = sanitized_record({
            "process_command_line": "discard-me",
            "process_identity": "old deterministic digest",
            "command_line": "discard-me",
            "username": "discard-me",
            "password": "discard-me",
            "nested": {"launch_identity": "session-only", "safe": 1},
        })
        self.assertEqual(cleaned, {"nested": {"safe": 1}})

    def test_process_identity_is_process_lifetime_hmac_not_cross_session_sha(self):
        code = (
            "import sys;sys.path.insert(0,r'.');"
            "import dpi_policy;print(dpi_policy.process_identity('C:/GameLoader.exe','opaque-launch-arguments'))"
        )
        env = dict(os.environ)
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        first = subprocess.check_output(
            [sys.executable, "-c", code], cwd=HERE, env=env, text=True,
        ).strip()
        second = subprocess.check_output(
            [sys.executable, "-c", code], cwd=HERE, env=env, text=True,
        ).strip()
        self.assertTrue(first and second)
        self.assertNotEqual(first, second)

    def test_tk_toolbar_boundary_dedupes_both_stringvars(self):
        tree = ast.parse(FLASH_SYNC.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "FlashSyncApp")
        methods = {node.name: ast.unparse(node) for node in cls.body
                   if isinstance(node, ast.FunctionDef)}
        self.assertIn("_set_game_time_source_text", methods)
        self.assertIn("_set_game_time_text", methods)
        self.assertNotIn("game_time_source_text.set", methods["poll_game_clock_acquisition"])
        self.assertNotIn("game_time_text.set", methods["update_estimated_game_time_label"])
        direct_source_sets = [name for name, body in methods.items()
                              if "game_time_source_text.set" in body]
        direct_time_sets = [name for name, body in methods.items()
                            if "game_time_text.set" in body]
        self.assertEqual(direct_source_sets, ["_set_game_time_source_text"])
        self.assertEqual(direct_time_sets, ["_set_game_time_text"])

    def test_timed_status_and_logs_use_only_four_semantic_payloads(self):
        text = FLASH_SYNC.read_text(encoding="utf-8")
        for forbidden in ("定時按下：剩", "第一下觸發時差值", "定時按下：未啟用",
                          "定時按下：時鐘失效", "定時按下：目標時間無效"):
            self.assertNotIn(forbidden, text)
        for required in ("定時按下：已啟用", "定時按下：等待目標時間",
                         "定時按下：已觸發", "定時按下：來源失效"):
            self.assertIn(required, text)

    def test_cross_midnight_target_is_explicitly_next_day(self):
        tree = ast.parse(FLASH_SYNC.read_text(encoding="utf-8"))
        cls = next(node for node in tree.body if isinstance(node, ast.ClassDef)
                   and node.name == "FlashSyncApp")
        method = next(node for node in cls.body if isinstance(node, ast.FunctionDef)
                      and node.name == "_timed_target_remaining_ms")
        namespace = {"DAY_MS": 86_400_000}
        harness = ast.ClassDef(
            name="Harness", bases=[], keywords=[], body=[method], decorator_list=[],
        )
        exec(compile(ast.fix_missing_locations(ast.Module([harness], [])), "timed", "exec"), namespace)
        app = namespace["Harness"]()
        self.assertEqual(app._timed_target_remaining_ms(86_399_000, 0), 1_000)
        self.assertEqual(app._timed_target_remaining_ms(1_000, 500), 86_399_500)


class PackagingTests(unittest.TestCase):
    def test_runtime_asset_manifest_is_complete_and_hashes_every_binary_asset(self):
        path = HERE / "RUNTIME_ASSET_MANIFEST.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        assets = payload["assets"]
        self.assertGreater(len(assets), 0)
        self.assertTrue(any(item["path"].startswith("templates/") for item in assets))
        self.assertTrue(any(item["path"].startswith("manor_assets/") for item in assets))
        self.assertTrue(any(item["path"].startswith("fishing_evidence/") for item in assets))
        for item in assets:
            self.assertEqual(set(item), {"path", "bytes", "sha256"})
            self.assertNotIn(":\\", item["path"])
            self.assertEqual(len(item["sha256"]), 64)

    def test_spec_bundles_runtime_asset_manifest(self):
        self.assertIn("RUNTIME_ASSET_MANIFEST.json",
                      (HERE / "fu_preview.spec").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
