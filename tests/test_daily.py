"""Offline tests for next-open execution, emergency scan and stale prices."""
import copy
import unittest
from datetime import date

import pandas as pd

from src import market_data as md
from src import manager as mg
from src import portfolio as pf
from src import run_daily as rd

CFG = {"fee_rate": 0.0025, "dividend_withholding": 0.15, "review_after_weeks": [4, 12], "benchmark": "VOO",
       "emergency": {"stock_move": 0.10, "market_drop": 0.05, "vix_level": 35},
       "regime_tickers": {"vix": "^VIX"}}


def frame(rows, start="2026-09-21", divs=None):
    idx = pd.bdate_range(start, periods=len(rows))
    df = pd.DataFrame({"Open": [r[0] for r in rows], "Close": [r[1] for r in rows],
                       "Dividends": 0.0, "Stock Splits": 0.0}, index=idx)
    for d, v in (divs or {}).items():
        df.loc[pd.Timestamp(d), "Dividends"] = v
    df.attrs["source"] = "yfinance"
    return df


def state():
    p = {t: {"price": 100.0} for t in ("A", "B", "VOO")}
    s = {"status": "pre-live", "start_capital_thb": 100000, "books": {}, "totals": {"dividends_usd": 0, "fees_usd": 0},
         "peak_value_thb": None}
    pf.seed_books(s, {"A": .5, "B": .4, "CASH": .1}, p, 33.0, 0.0025, "VOO", "2026-09-21")
    return s


class Pending(unittest.TestCase):
    def setUp(self):
        self._j = mg.JOURNAL_PATH
        import tempfile, pathlib
        self.tmp = tempfile.TemporaryDirectory()
        mg.JOURNAL_PATH = pathlib.Path(self.tmp.name) / "j.json"

    def tearDown(self):
        mg.JOURNAL_PATH = self._j
        self.tmp.cleanup()

    def test_fills_at_next_open_not_decision_close(self):
        s = state()
        s["pending_orders"] = {"decided": "2026-09-25", "plan": {"A": .3, "B": .4, "C": .2},
                               "targets": [{"ticker": "C", "thesis": "t", "exit_condition": "e", "expected_outcome": "o"}]}
        # Fri 25 close 100; Mon 28 opens 110 (C), 90 (A)
        hist = {"VOO": frame([(100, 100)] * 5 + [(101, 101)]),
                "A": frame([(100, 100)] * 5 + [(90, 95)]), "B": frame([(100, 100)] * 6),
                "C": frame([(100, 100)] * 5 + [(110, 112)])}
        w = []
        trades = rd.fill_pending(s, hist, CFG, 33.0, "VOO", w)
        self.assertIsNone(s["pending_orders"])
        byt = {t["ticker"]: t for t in trades}
        self.assertEqual(byt["C"]["price"], 110)   # bought at Monday OPEN
        self.assertEqual(byt["A"]["price"], 90)
        self.assertEqual(byt["C"]["date"], "2026-09-28")
        self.assertIn("thesis", s["books"]["portfolio"]["holdings"]["C"])

    def test_waits_when_next_session_not_there(self):
        s = state()
        s["pending_orders"] = {"decided": "2026-09-25", "plan": {"A": .3, "B": .4}}
        hist = {"VOO": frame([(100, 100)] * 5), "A": frame([(100, 100)] * 5), "B": frame([(100, 100)] * 5)}
        self.assertEqual(rd.fill_pending(s, hist, CFG, 33.0, "VOO", []), [])
        self.assertIsNotNone(s["pending_orders"])

    def test_halted_stock_not_traded(self):
        s = state()
        s["pending_orders"] = {"decided": "2026-09-25", "plan": {"A": .1, "B": .4, "C": .4}}
        hist = {"VOO": frame([(100, 100)] * 6), "A": frame([(100, 100)] * 5),  # A has no Monday print
                "B": frame([(100, 100)] * 6), "C": frame([(100, 100)] * 6)}
        w = []
        trades = rd.fill_pending(s, hist, CFG, 33.0, "VOO", w)
        self.assertNotIn("A", {t["ticker"] for t in trades})
        self.assertTrue(any("A" in x for x in w))


    def test_dividend_on_fill_day_goes_to_seller_not_buyer(self):
        s = state()
        s["pending_orders"] = {"decided": "2026-09-25", "plan": {"A": 0.0, "B": .4, "C": .5},
                               "targets": [{"ticker": "C", "thesis": "t", "exit_condition": "e", "expected_outcome": "o"}]}
        hist = {"VOO": frame([(100, 100)] * 6), "B": frame([(100, 100)] * 6),
                "A": frame([(100, 100)] * 6, divs={"2026-09-28": 1.0}),      # ex-date = fill day: seller keeps it
                "C": frame([(100, 100)] * 6, divs={"2026-09-28": 5.0})}      # buyer at the open does NOT get it
        a_shares = s["books"]["portfolio"]["holdings"]["A"]["shares"]
        cash0 = s["books"]["portfolio"]["cash_usd"]
        rd.fill_pending(s, hist, CFG, 33.0, "VOO", [])
        self.assertAlmostEqual(s["totals"]["dividends_usd"], a_shares * 1.0 * 0.85, places=3)
        self.assertEqual(s["last_events_processed"], "2026-09-28")

    def test_weekly_turnover_is_cumulative(self):
        from src import rules_engine as reng
        s = state()
        s["turnover_log"] = [{"date": "2026-09-28", "turnover": 0.25}]
        self.assertAlmostEqual(reng.turnover_used(s, date(2026, 9, 30)), 0.25)
        self.assertAlmostEqual(reng.turnover_used(s, date(2026, 10, 9)), 0.0)


class Drip(unittest.TestCase):
    def test_benchmark_and_shadow_reinvest_dividends(self):
        s = state()
        hist = {"VOO": frame([(100, 100)] * 3, divs={"2026-09-22": 2.0}), "A": frame([(100, 100)] * 3),
                "B": frame([(100, 100)] * 3)}
        v0 = s["books"]["benchmark"]["holdings"]["VOO"]["shares"]
        cash_sh = s["books"]["shadow"]["cash_usd"]
        ev = pf.process_events(s, hist, date(2026, 9, 23), 0.15, [])
        self.assertTrue(any(e["book"] == "benchmark" and e.get("reinvested") for e in ev))
        self.assertAlmostEqual(s["books"]["benchmark"]["cash_usd"], 0.0, places=6)
        self.assertAlmostEqual(s["books"]["benchmark"]["holdings"]["VOO"]["shares"], v0 * (1 + 2 * .85 / 100))
        self.assertAlmostEqual(s["books"]["shadow"]["cash_usd"], cash_sh)  # initial cash untouched

    def test_incomplete_data_postpones(self):
        s = state()
        hist = {"A": frame([(100, 100)] * 3), "B": frame([(100, 100)] * 3), "VOO": frame([(100, 100)] * 3)}
        hist["A"].attrs["source"] = "stooq"
        w = []
        self.assertEqual(pf.process_events(s, hist, date(2026, 9, 23), 0.15, w), [])
        self.assertEqual(s["last_events_processed"], "2026-09-21")
        self.assertTrue(w)


class Scan(unittest.TestCase):
    def test_triggers_and_dedupe(self):
        s = state()
        hist = {"VOO": frame([(100, 100)] * 5 + [(100, 94)]), "A": frame([(100, 100)] * 5 + [(100, 85)]),
                "B": frame([(100, 100)] * 2), "^VIX": frame([(20, 20)] * 5 + [(40, 40)])}
        orig = rd.nw.edgar_filings
        rd.nw.edgar_filings = lambda *a, **k: {"B": [{"form": "8-K", "date": "2026-09-28", "items": "4.02,9.01", "url": "u"}]}
        try:
            trig = rd.scan(s, hist, CFG, date(2026, 9, 28), [])
            types = {(x["type"], x["ticker"]) for x in trig}
            self.assertIn(("big_move", "A"), types)
            self.assertIn(("no_price", "B"), types)
            self.assertIn(("market_shock", "VOO"), types)
            self.assertIn(("market_fear", "^VIX"), types)
            self.assertIn(("sec_filing", "B"), types)
            self.assertEqual(rd.scan(s, hist, CFG, date(2026, 9, 28), []), [])  # same events not re-fired
        finally:
            rd.nw.edgar_filings = orig

    def test_quiet_day(self):
        s = state()
        hist = {"VOO": frame([(100, 100)] * 6), "A": frame([(100, 100)] * 5 + [(100, 104)]), "B": frame([(100, 100)] * 6)}
        orig = rd.nw.edgar_filings
        rd.nw.edgar_filings = lambda *a, **k: {}
        try:
            self.assertEqual(rd.scan(s, hist, CFG, date(2026, 9, 28), []), [])
        finally:
            rd.nw.edgar_filings = orig


class Stale(unittest.TestCase):
    def test_fill_stale(self):
        prices = {"A": {"price": 1, "date": "2026-09-28"}}
        hist = {"B": frame([(100, 50)] * 2)}
        stale = md.fill_stale({"A", "B", "C"}, prices, hist, {"C": {"price": 7, "date": "2026-09-01"}})
        self.assertEqual(sorted(stale), ["B", "C"])
        self.assertTrue(prices["B"]["stale"])
        self.assertEqual(prices["C"]["price"], 7)


if __name__ == "__main__":
    unittest.main()
