"""Offline tests — no network. Run: python -m unittest discover -s tests -v"""
import copy
import unittest
from datetime import date

import numpy as np
import pandas as pd

from src import market_data as md
from src import portfolio as pf
from src.common import DataError
from src.regime import read_regime

CFG = {
    "regime_tickers": {"market": "VOO", "vix": "^VIX", "us10y": "^TNX",
                       "sectors": {"XLK": "Tech", "XLP": "Staples", "XLU": "Utilities", "XLV": "Health"}},
}


def frame(closes, start="2025-01-01", divs=None, splits=None):
    idx = pd.bdate_range(start, periods=len(closes))
    df = pd.DataFrame({"Close": closes, "Dividends": 0.0, "Stock Splits": 0.0}, index=idx)
    for d, v in (divs or {}).items():
        df.loc[pd.Timestamp(d), "Dividends"] = v
    for d, v in (splits or {}).items():
        df.loc[pd.Timestamp(d), "Stock Splits"] = v
    return df


def empty_state():
    return {"status": "pre-live", "start_capital_thb": 100000, "books": {}, "totals": {"dividends_usd": 0, "fees_usd": 0},
            "peak_value_thb": None}


class PriceGuard(unittest.TestCase):
    def test_stale_and_missing_prices_are_errors(self):
        hist = {"AAA": frame([10, 11], start="2025-01-01")}
        prices, errors = md.latest_prices(["AAA", "BBB"], hist, 5, asof=date(2025, 3, 1))
        self.assertEqual(prices, {})
        self.assertEqual(len(errors), 2)
        with self.assertRaises(DataError):
            md.require_prices(["AAA", "BBB"], prices, errors)

    def test_fresh_price_ok(self):
        hist = {"AAA": frame([10, 12], start="2025-01-01")}
        prices, errors = md.latest_prices(["AAA"], hist, 5, asof=date(2025, 1, 3))
        self.assertEqual(prices["AAA"]["price"], 12)
        self.assertFalse(errors)

    def test_zero_price_rejected(self):
        hist = {"AAA": frame([10, 0.0], start="2025-01-01")}
        prices, errors = md.latest_prices(["AAA"], hist, 5, asof=date(2025, 1, 3))
        self.assertNotIn("AAA", prices)


class Books(unittest.TestCase):
    def setUp(self):
        self.prices = {"AAA": {"price": 100.0}, "BBB": {"price": 50.0}, "VOO": {"price": 500.0}}
        self.state = empty_state()
        pf.seed_books(self.state, {"AAA": 0.5, "BBB": 0.4, "CASH": 0.1}, self.prices, 33.0, 0.0025, "VOO", "2025-01-02")

    def test_seed(self):
        s = self.state
        self.assertEqual(s["status"], "live")
        cap = 100000 / 33.0
        self.assertAlmostEqual(s["start_capital_usd"], cap)
        port = s["books"]["portfolio"]
        self.assertAlmostEqual(port["cash_usd"], cap * 0.1, places=6)
        self.assertAlmostEqual(port["holdings"]["AAA"]["shares"], cap * 0.5 * 0.9975 / 100)
        self.assertAlmostEqual(s["books"]["benchmark"]["cash_usd"], 0, places=6)
        self.assertEqual(s["books"]["shadow"], port)
        self.assertIsNot(s["books"]["shadow"], port)

    def test_fee_makes_day0_slightly_negative(self):
        snap = pf.snapshot(self.state, self.prices, 33.0)
        self.assertLess(snap["returns"]["portfolio_thb"], 0)
        self.assertGreater(snap["returns"]["portfolio_thb"], -0.003)

    def test_fx_decomposition(self):
        snap = pf.snapshot(self.state, self.prices, 36.3)  # baht weakens 10%
        r = snap["returns"]
        self.assertAlmostEqual((1 + r["portfolio_usd"]) * (1 + r["fx_effect"]) - 1, r["portfolio_thb"], places=9)
        self.assertAlmostEqual(r["fx_effect"], 0.1, places=9)

    def test_cannot_overspend(self):
        book = {"cash_usd": 100.0, "holdings": {}}
        with self.assertRaises(ValueError):
            pf.buy(book, "AAA", 150, 10, 0.0025, "2025-01-01")

    def test_sell_all_removes_position(self):
        port = self.state["books"]["portfolio"]
        cash0 = port["cash_usd"]
        sh = port["holdings"]["BBB"]["shares"]
        fee = pf.sell(port, "BBB", sh, 50.0, 0.0025)
        self.assertNotIn("BBB", port["holdings"])
        self.assertAlmostEqual(port["cash_usd"], cash0 + sh * 50 - fee)


class CorporateActions(unittest.TestCase):
    def test_split_then_dividend(self):
        hist = {"AAA": frame([100, 100, 10, 10], start="2025-01-01",
                             splits={"2025-01-03": 10.0}, divs={"2025-01-06": 0.5})}
        book = {"cash_usd": 0.0, "holdings": {"AAA": {"shares": 2.0, "cost_usd": 200, "opened": "x"}}}
        evs = pf.apply_corporate_actions(book, hist, date(2025, 1, 1), date(2025, 1, 6), 0.15)
        self.assertEqual([e["type"] for e in evs], ["split", "dividend"])
        self.assertAlmostEqual(book["holdings"]["AAA"]["shares"], 20.0)
        self.assertAlmostEqual(book["cash_usd"], 20 * 0.5 * 0.85)

    def test_events_not_double_counted(self):
        hist = {"AAA": frame([100, 100, 100], start="2025-01-01", divs={"2025-01-02": 1.0})}
        book = {"cash_usd": 0.0, "holdings": {"AAA": {"shares": 1.0, "cost_usd": 100, "opened": "x"}}}
        pf.apply_corporate_actions(book, hist, date(2025, 1, 1), date(2025, 1, 3), 0.0)
        pf.apply_corporate_actions(book, hist, date(2025, 1, 3), date(2025, 1, 3), 0.0)
        self.assertAlmostEqual(book["cash_usd"], 1.0)


class Regime(unittest.TestCase):
    def test_uptrend_calm_is_risk_on(self):
        up = frame(list(np.linspace(300, 500, 260)))
        hist = {"VOO": up, "^VIX": frame([14] * 30), "^TNX": frame([4.0] * 30),
                "XLK": frame(list(np.linspace(100, 130, 30))), "XLP": frame([50] * 30),
                "XLU": frame([50] * 30), "XLV": frame([50] * 30)}
        r = read_regime(hist, CFG)
        self.assertEqual(r["label"], "risk-on")

    def test_downtrend_fear_is_risk_off(self):
        down = frame(list(np.linspace(500, 350, 260)))
        hist = {"VOO": down, "^VIX": frame([38] * 30), "^TNX": frame(list(np.linspace(4, 4.8, 30))),
                "XLK": frame(list(np.linspace(130, 100, 30))), "XLP": frame(list(np.linspace(50, 55, 30))),
                "XLU": frame(list(np.linspace(50, 54, 30))), "XLV": frame([50] * 30)}
        r = read_regime(hist, CFG)
        self.assertEqual(r["label"], "risk-off")

    def test_missing_data_is_unknown(self):
        self.assertEqual(read_regime({}, CFG)["label"], "unknown")


class Drawdown(unittest.TestCase):
    def test_mdd(self):
        self.assertAlmostEqual(pf.max_drawdown([100, 120, 90, 130, 117]), -0.25)


if __name__ == "__main__":
    unittest.main()
