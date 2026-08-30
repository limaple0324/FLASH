from __future__ import annotations

from types import SimpleNamespace
import os
import unittest

from v02_faithful_game_time import (
    APPROVED_SHORTCUTS, ApprovedShortcutCatalog, FaithfulConsensus,
    FaithfulSample, FaithfulScheduler,
)


class SelectorTests(unittest.TestCase):
    def test_only_four_exact_desktop_names_are_resolved_and_selected(self):
        existing = {name + ".lnk" for name in APPROVED_SHORTCUTS}
        old_isfile = os.path.isfile
        os.path.isfile = lambda path: os.path.basename(path) in existing
        try:
            resolver = lambda path: ("GameLoader.exe", "arg=" + os.path.splitext(os.path.basename(path))[0])
            fingerprint = lambda arguments: "fp:" + arguments
            catalog = ApprovedShortcutCatalog(fingerprint, resolver=resolver, desktop="X:\\Desktop")
            identities = [SimpleNamespace(launch_fingerprint="fp:arg=" + name) for name in APPROVED_SHORTCUTS]
            identities.append(SimpleNamespace(launch_fingerprint="fp:arg=not-approved"))
            selected = catalog.select(identities)
        finally:
            os.path.isfile = old_isfile
        self.assertEqual(tuple(label for label, _identity in selected), APPROVED_SHORTCUTS)


class ConsensusTests(unittest.TestCase):
    def test_three_same_generation_samples_group_equal_values_and_list_unreadable(self):
        consensus = FaithfulConsensus()
        for label in ("120古", "120靈", "大排"):
            for _ in range(3):
                consensus.add(FaithfulSample(label, 1, "12:34", "minute"))
        display = consensus.display()
        self.assertEqual(display.groups, (("12:34", ("120古", "120靈", "大排")),))
        self.assertEqual(display.unreadable, ("餐廳",))

    def test_different_values_remain_separate_with_sources(self):
        consensus = FaithfulConsensus()
        for label, value in zip(APPROVED_SHORTCUTS, ("12:34", "12:34", "12:35", "12:36")):
            for _ in range(3):
                consensus.add(FaithfulSample(label, 7, value, "minute"))
        self.assertEqual(consensus.display().groups, (
            ("12:34", ("120古", "120靈")), ("12:35", ("大排",)), ("12:36", ("餐廳",)),
        ))

    def test_generation_change_and_transition_require_fresh_consensus(self):
        consensus = FaithfulConsensus()
        for _ in range(3):
            consensus.add(FaithfulSample("120古", 1, "12:34", "minute"))
        consensus.add(FaithfulSample("120古", 2, "12:35", "minute"))
        self.assertNotIn("120古", consensus.committed)
        for _ in range(2):
            consensus.add(FaithfulSample("120古", 2, "12:35", "minute"))
        self.assertFalse(consensus.resample_all)  # generation changed; no mixed-edge inference


class RateTests(unittest.TestCase):
    def test_injected_counters_enforce_point_two_and_four_hz_and_no_redraw(self):
        now = [0]
        gate = FaithfulScheduler(lambda: now[0])
        self.assertTrue(gate.allow_discovery())
        self.assertTrue(gate.allow_read("120古"))
        self.assertTrue(gate.allow_publish("a"))
        self.assertFalse(gate.allow_discovery())
        self.assertFalse(gate.allow_read("120古"))
        self.assertFalse(gate.allow_publish("a"))
        now[0] += 250_000_000
        self.assertTrue(gate.allow_read("120古"))
        self.assertTrue(gate.allow_publish("b"))
        self.assertFalse(gate.allow_discovery())
        now[0] += 4_750_000_000
        self.assertTrue(gate.allow_discovery())


if __name__ == "__main__":
    unittest.main()
