from __future__ import annotations

try:
    import _bootstrap  # noqa: F401
except ModuleNotFoundError:
    from . import _bootstrap  # noqa: F401

import unittest
from datetime import date

from scripts.a_share_panic_index.features.breadth import (
    aggregate_returns,
    breadth_feature_values,
)
from scripts.a_share_panic_index.features.derivatives import (
    annualized_basis,
    mid_or_last,
    select_if_contracts,
)
from scripts.a_share_panic_index.features.liquidity import (
    blended_cumulative_share,
    bootstrap_cumulative_share,
    combine_proxy_curves,
    curves_from_amount_rows,
    liquidity_feature_values,
)
from scripts.a_share_panic_index.scoring import score_from_anchors


class TestFeatures(unittest.TestCase):
    def test_breadth_down_market_scores_higher(self):
        calm = breadth_feature_values(1000, 5000, 0.01, 0.002, 0.006, 10, 1)
        panic = breadth_feature_values(4000, 5000, 0.08, 0.04, -0.025, 10, 100)
        anchors = [[0.3, 10], [0.5, 45], [0.7, 75], [0.95, 99]]
        self.assertGreater(
            score_from_anchors(panic["decline_share"], anchors),
            score_from_anchors(calm["decline_share"], anchors),
        )
        self.assertGreater(
            panic["limit_down_intensity"], calm["limit_down_intensity"]
        )

    def test_empty_breadth_is_rejected(self):
        with self.assertRaises(ValueError):
            aggregate_returns([])

    def test_if_rolls_before_expiry_and_uses_mid_price(self):
        contracts = [
            {"symbol": "IF2608", "last": 4000, "bid": 3999, "ask": 4001, "expiry": "2026-08-21"},
            {"symbol": "IF2609", "last": 3990, "bid": 3989, "ask": 3991, "expiry": "2026-09-18"},
            {"symbol": "IF2610", "last": 3980, "expiry": "2026-10-16"},
        ]
        front, next_contract = select_if_contracts(
            contracts, date(2026, 8, 17), minimum_days_to_expiry=5
        )
        self.assertEqual(front["symbol"], "IF2609")
        self.assertEqual(next_contract["symbol"], "IF2610")
        self.assertEqual(mid_or_last(contracts[0]), 4000)

    def test_annualized_basis_unit_is_decimal(self):
        value = annualized_basis(4000, 3960, 30, 365)
        self.assertAlmostEqual(value, ((4000 / 3960) - 1) * 365 / 30)
        self.assertLess(value, 1)

    def test_bootstrap_curve_projects_full_day_amount_without_claiming_real_data(self):
        share = bootstrap_cumulative_share(6)
        self.assertGreater(share, 0)
        self.assertLess(share, 1)
        current = 500_000_000_000
        self.assertGreater(current / share, current)

    def test_real_proxy_curve_uses_symbol_median(self):
        rows = []
        for symbol, first_amount in (("sh510300", 20.0), ("sz159919", 40.0)):
            for trade_date in ("2026-07-22", "2026-07-23"):
                rows.extend(
                    [
                        {
                            "symbol": symbol,
                            "trade_date": trade_date,
                            "bucket_5m": 1,
                            "amount": first_amount,
                        },
                        {
                            "symbol": symbol,
                            "trade_date": trade_date,
                            "bucket_5m": 48,
                            "amount": 80.0,
                        },
                    ]
                )
        curves = curves_from_amount_rows(rows)
        combined = combine_proxy_curves([item["curve"] for item in curves])
        self.assertEqual(len(curves), 2)
        self.assertAlmostEqual(combined[1], (0.2 + 1 / 3) / 2)
        self.assertEqual(combined[48], 1.0)

    def test_self_curve_blend_reaches_configured_20_60_120_day_weights(self):
        proxy = 0.30
        self_share = 0.50
        self.assertEqual(blended_cumulative_share(proxy, self_share, 20)[2], 0.0)
        self.assertEqual(blended_cumulative_share(proxy, self_share, 60)[2], 0.5)
        self.assertEqual(blended_cumulative_share(proxy, self_share, 120)[2], 0.75)

    def test_liquidity_rejects_negative_increment_and_up_volume_is_not_panic(self):
        with self.assertRaisesRegex(ValueError, "不能为负"):
            liquidity_feature_values(1, 1, -0.01, -1, 1, 1)
        values = liquidity_feature_values(
            1_000, 1_000, 0.01, 200, 100, 100
        )
        self.assertEqual(values["downside_turnover_shock"], 0)
        self.assertEqual(values["amount_acceleration_stress"], 0)


if __name__ == "__main__":
    unittest.main()
