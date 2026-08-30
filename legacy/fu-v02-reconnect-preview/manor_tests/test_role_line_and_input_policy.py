from __future__ import annotations

import inspect
import threading
import unittest

import control_panel
import smart_reconnect
from manor_assistant.vision import (
    ACTION_REFERENCE_ORIGIN,
    Match,
    PLOT_REFERENCE_POINTS,
    USER_PLOT_ORDER,
    VisionEngine,
    user_plot_indices,
)
from manor_assistant.workflow import ManorWorkflow


class RoleLineAndInputPolicyTests(unittest.TestCase):
    def test_special_line_labels_are_exact(self) -> None:
        self.assertEqual(control_panel.line_setting_label(2), "公會專線（第二線）")
        self.assertEqual(control_panel.line_setting_label(8), "郵寄拍賣專線（第八線）")
        self.assertEqual(control_panel.line_setting_label(0), "依最近登入線路")

    def test_user_plot_numbering_is_not_row_major(self) -> None:
        self.assertEqual(
            USER_PLOT_ORDER,
            (0, 1, 3, 6, 2, 4, 7, 10, 5, 8, 11, 13, 9, 12, 14, 15),
        )
        self.assertEqual(user_plot_indices(3), (0, 1, 3))

    def test_harvest_targets_centres_in_user_order(self) -> None:
        vision = object.__new__(VisionEngine)
        action = Match(ACTION_REFERENCE_ORIGIN[0], ACTION_REFERENCE_ORIGIN[1], 1, 1, 1.0, 1.0)
        actual = vision.harvest_points(action)
        expected = [
            (PLOT_REFERENCE_POINTS[index][0] - 28, PLOT_REFERENCE_POINTS[index][1])
            for index in USER_PLOT_ORDER
        ]
        self.assertEqual(actual, expected)

    def test_harvest_pass_uses_user_order_without_sorting(self) -> None:
        source = inspect.getsource(ManorWorkflow._harvest_pass)
        self.assertIn("enumerate(points, start=1)", source)
        self.assertNotIn("only_indices", source)
        self.assertIn("harvest_points", source)
        self.assertNotIn("_wait_plot_state", source)
        self.assertIn("禁止跳到下一格", source)

    def test_harvest_result_accepts_cooldown_as_completed(self) -> None:
        source = inspect.getsource(ManorWorkflow._wait_harvest_result)
        self.assertIn('not in ("empty", "cooldown")', source)
        self.assertIn("for index in USER_PLOT_ORDER", source)

    def test_integrated_manor_never_changes_window_visibility_or_position(self) -> None:
        workflow_source = inspect.getsource(ManorWorkflow)
        self.assertNotIn("promote_offscreen", workflow_source)

    def test_foreground_physical_input_is_hard_disabled(self) -> None:
        self.assertFalse(smart_reconnect.FOREGROUND_PHYSICAL_FALLBACK)

    def test_fixed_line_path_precedes_recent_login_ocr(self) -> None:
        source = inspect.getsource(smart_reconnect.GameWorker.step)
        fixed_at = source.index("fixed_line_no = self.preferred_line_no")
        recent_at = source.index("read_recent_line_detail(frame, header)")
        self.assertLess(fixed_at, recent_at)
        self.assertIn("fixed_line_no or", source)
        self.assertIn("fallback_button = find_line_button(frame, FIXED_LINE_FALLBACK_NO, header)", source)
        self.assertIn("fallback_ocr = find_line_button_ocr(frame, header, FIXED_LINE_FALLBACK_NO)", source)
        self.assertEqual(smart_reconnect.FIXED_LINE_FALLBACK_NO, 1)
        self.assertEqual(smart_reconnect.FIXED_LINE_RETRY_BACKOFF_SECONDS, 1.0)

    def test_each_window_uses_an_independent_worker_thread(self) -> None:
        self.assertTrue(issubclass(smart_reconnect.GameWorker, threading.Thread))
        run_source = inspect.getsource(smart_reconnect.GameWorker.run)
        self.assertIn("while not self.stop_event.is_set()", run_source)

    def test_role_features_are_three_independent_persisted_switches(self) -> None:
        source = inspect.getsource(control_panel.set_feature_switches)
        self.assertIn('item["reconnect_enabled"] = bool(reconnect_enabled)', source)
        self.assertIn('item["manor_enabled"] = bool(manor_enabled)', source)
        self.assertIn('item["fishing_enabled"] = bool(fishing_enabled)', source)
        self.assertIn("saved.update", source)
        self.assertIn("profiles[pkey] = saved", source)

    def test_legacy_bindings_preserve_existing_feature_behaviour(self) -> None:
        source = inspect.getsource(control_panel.save_user_binding_atomic)
        self.assertIn('item.setdefault("reconnect_enabled", True)', source)
        self.assertIn('item.setdefault("manor_enabled", False)', source)
        self.assertIn('item.setdefault("fishing_enabled", bool(str(item.get("fishing_profile_id"', source)

    def test_disabled_reconnect_is_gated_before_disconnect_detection(self) -> None:
        source = inspect.getsource(smart_reconnect.GameWorker.step)
        gate_at = source.index("if not self.reconnect_enabled:")
        detect_at = source.index("self.detect_disconnect_dual")
        self.assertLess(gate_at, detect_at)
        self.assertIn("probe_disc = self.reconnect_enabled", source)
        self.assertIn("startup_probe = self.reconnect_enabled", source)

    def test_disabling_fishing_keeps_assignment_but_removes_active_profile(self) -> None:
        source = inspect.getsource(smart_reconnect.GameWorker.apply_binding)
        self.assertIn("assigned_fishing_profile_id", source)
        self.assertIn('fishing_profile_id = assigned_fishing_profile_id if fishing_enabled else ""', source)
        self.assertIn("assigned_fishing_profile_id, preferred_line_no", source)

    def test_manor_crop_and_quantity_are_saved_before_refresh_can_restore_old_values(self) -> None:
        crop_source = inspect.getsource(control_panel.App.on_manor_crop_changed)
        quantity_source = inspect.getsource(control_panel.App.on_manor_quantity_changed)
        sync_source = inspect.getsource(control_panel.App.sync_manor_panel_to_selection)
        snapshot_source = inspect.getsource(control_panel.App._current_manor_snapshot)
        self.assertIn("delay_ms=0", crop_source)
        self.assertIn("delay_ms=250", quantity_source)
        self.assertIn("manor_dirty_hwnd", sync_source)
        self.assertLess(sync_source.index("manor_dirty_hwnd"), sync_source.index("read_bindings"))
        self.assertIn("self.selected_hwnd()", snapshot_source)


if __name__ == "__main__":
    unittest.main()
