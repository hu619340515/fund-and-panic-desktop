from __future__ import annotations

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import unittest

from scripts.a_share_panic_index.scoring import (
    classify_level,
    generalized_mean,
    historical_percentile,
    reference_state,
    score_from_anchors,
    smooth_display,
)


class TestScoring(unittest.TestCase):
    def test_anchor_interpolation_and_direction(self):
        self.assertEqual(score_from_anchors(0, [[0, 10], [1, 90]]), 10)
        self.assertEqual(score_from_anchors(0.5, [[0, 10], [1, 90]]), 50)
        self.assertEqual(score_from_anchors(0.5, [[0, 90], [1, 10]]), 50)
        with self.assertRaises(ValueError):
            score_from_anchors(0.5, [[1, 10], [0, 90]])

    def test_fixed_levels(self):
        expected = {
            0: "极度平静",
            24.99: "极度平静",
            25: "偏平静",
            40: "中性",
            60: "偏恐慌",
            75: "极度恐慌",
            100: "极度恐慌",
        }
        for value, level in expected.items():
            self.assertEqual(classify_level(value), level)

    def test_reference_modes_and_weights(self):
        config = {
            "self_calibration_start_days": 20,
            "same_time_history_days": 60,
            "feature_historical_blend_cap": 0.5,
        }
        self.assertEqual(reference_state(19, config), ("structural_bootstrap", 0.0))
        self.assertEqual(reference_state(20, config), ("self_calibrating", 0.0))
        mode, weight = reference_state(40, config)
        self.assertEqual(mode, "self_calibrating")
        self.assertAlmostEqual(weight, 0.25)
        self.assertEqual(reference_state(60, config), ("same_time_history", 0.5))

    def test_generalized_mean_formula_and_coverage(self):
        values = {name: 50.0 for name in ("a", "b", "c", "d")}
        weights = {name: 0.25 for name in values}
        score, coverage = generalized_mean(values, weights, power=1.5)
        self.assertAlmostEqual(score, 50.0)
        self.assertEqual(coverage, 1.0)
        high, _ = generalized_mean({**values, "a": 90}, weights, power=1.5)
        self.assertGreater(high, score)
        missing, coverage = generalized_mean(
            {"a": 50.0, "b": 50.0, "c": 50.0, "d": None}, weights, power=1.5
        )
        self.assertIsNone(missing)
        self.assertEqual(coverage, 0.75)

    def test_display_smoothing(self):
        self.assertEqual(smooth_display(70, 60, fast_rise_threshold=8), 70)
        self.assertAlmostEqual(smooth_display(65, 60), 63.25)
        self.assertAlmostEqual(smooth_display(50, 60), 56.5)

    def test_historical_percentile_uses_only_supplied_history(self):
        base = historical_percentile(3, [1, 2, 4, 5])
        self.assertEqual(base, 50)
        self.assertNotEqual(base, historical_percentile(3, [1, 2, 4, 5, 100]))


if __name__ == "__main__":
    unittest.main()
