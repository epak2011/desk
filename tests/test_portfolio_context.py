import unittest

import portfolio_context


class PortfolioContextTests(unittest.TestCase):
    def test_blocks_add_when_sector_limit_is_full(self):
        result = portfolio_context.portfolio_recommendation(
            "NVDA", {"action": "enter_now", "entry_size": "normal", "price": 100, "stop": 90},
            {"AMD": {"shares": 400}}, {"AMD": 100}, {"NVDA": "Technology", "AMD": "Technology"},
            account_size=100000, risk_per_trade=.01, max_position_pct=.25,
        )
        self.assertTrue(result["concentration_flag"])
        self.assertEqual(result["incremental_weight_pct"], 0)

    def test_stop_risk_caps_position(self):
        result = portfolio_context.portfolio_recommendation(
            "AAPL", {"action": "enter_now", "entry_size": "full", "price": 100, "stop": 90},
            {}, {}, {"AAPL": "Technology"}, account_size=100000,
            risk_per_trade=.01, max_position_pct=.25,
        )
        self.assertEqual(result["incremental_weight_pct"], 10.0)

    def test_non_actionable_call_recommends_zero(self):
        result = portfolio_context.portfolio_recommendation(
            "AAPL", {"action": "watch"}, {}, {}, {}, account_size=100000,
            risk_per_trade=.01, max_position_pct=.25,
        )
        self.assertEqual(result["suggested_weight_pct"], 0)


if __name__ == "__main__":
    unittest.main()
