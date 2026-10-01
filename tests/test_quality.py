"""Offline tests for decision-quality upgrades: risk read-out, news relevance, verification retry."""
import copy
import unittest
from datetime import date
from unittest import mock

import numpy as np
import pandas as pd

from src import news as nw
from src import risk as rk
from src import run_weekly as rw


def fake_history(n=160, seed=1):
    rng = np.random.default_rng(seed)
    idx = pd.bdate_range("2026-03-02", periods=n)
    mkt = rng.normal(0, 0.01, n)
    tnx = rng.normal(0, 0.05, n)
    def close(r):
        return pd.DataFrame({"Close": 100 * np.cumprod(1 + r), "Open": 100 * np.cumprod(1 + r)}, index=idx)
    return {
        "VOO": close(mkt),
        "A": close(1.5 * mkt - 0.15 * tnx + rng.normal(0, 0.004, n)),   # tech-like, rate sensitive
        "B": close(1.4 * mkt - 0.15 * tnx + rng.normal(0, 0.004, n)),   # moves with A
        "C": close(rng.normal(0, 0.01, n)),                              # unrelated
        "^TNX": pd.DataFrame({"Close": 4.5 + np.cumsum(tnx)}, index=idx),
    }


class RiskTest(unittest.TestCase):
    def test_groups_beta_rates_themes(self):
        h = fake_history()
        out = rk.exposure({"A": 0.4, "B": 0.3, "C": 0.2, "CASH": 0.1}, h,
                          themes={"A": "AI infrastructure", "B": "AI infrastructure", "C": "healthcare"})
        g = out["correlated_groups"]
        self.assertEqual(len(g), 1)
        self.assertEqual(set(g[0]["tickers"]), {"A", "B"})
        self.assertAlmostEqual(g[0]["weight"], 0.7)
        self.assertGreater(out["beta_voo"], 0.8)
        self.assertLess(out["rate_move_per_25bp"], 0)          # falls when yields rise
        self.assertEqual(out["theme_weights"]["AI infrastructure"], 0.7)
        self.assertLess(out["bad_week_1_in_20"], 0)

    def test_not_enough_data(self):
        self.assertIsNone(rk.exposure({}, fake_history()))
        self.assertIsNone(rk.exposure({"A": 1.0}, {k: v.tail(10) for k, v in fake_history().items()}))


class RelevanceTest(unittest.TestCase):
    def test_filters_market_wide_stories(self):
        k = nw.name_keys("AMZN", "Amazon.com Inc")
        self.assertTrue(nw.is_relevant({"headline": "Anthropic commits $11.6B elsewhere", "summary": "Amazon retains AWS deal"}, "AMZN", k))
        self.assertFalse(nw.is_relevant({"headline": "Is Home Depot's next big thing already in its stores?"}, "AMZN", k))
        self.assertTrue(nw.is_relevant({"headline": "Stocks to watch: AMZN, HD"}, "AMZN", k))

    def test_short_tickers_need_context(self):
        k = nw.name_keys("MA", "Mastercard Inc")
        self.assertFalse(nw.is_relevant({"headline": "MA state budget passes"}, "MA", k))
        self.assertTrue(nw.is_relevant({"headline": "Payments (NYSE:MA) rise"}, "MA", k))
        self.assertTrue(nw.is_relevant({"headline": "Mastercard pilots tokenized USD"}, "MA", k))


class VerifyRetryTest(unittest.TestCase):
    def test_no_tool_answer_gets_one_retry(self):
        calls = []

        def fake_decide(mode, ctx, cfg, tools, warnings):
            calls.append(copy.deepcopy(ctx.get("previous_attempt")))
            d = {"action": "hold", "summary_th": "ถือ", "journal": "j", "targets": [],
                 "themes": {"A": "AI infrastructure"}, "checked": [{"ticker": "A", "what": "news", "finding": "ok"}]}
            return d, 0.05, ([] if len(calls) == 1 else [{"tool": "get_news"}])

        state = {"status": "live", "books": {"portfolio": {"cash_usd": 10.0, "holdings": {
            "A": {"shares": 1.0, "cost_usd": 90.0, "opened": "2026-09-24", "sector": "Technology"}}}}}
        cfg = {"price_max_age_days": 5, "leveraged_denylist": [], "journal_entries_in_prompt": 4,
               "monthly_ai_budget_usd": 6.0, "decision_model": "m", "benchmark": "VOO",
               "regime_tickers": {"us10y": "^TNX"}}
        rules = {"flexible": {"regime_cash_guidance": {}}}
        prices = {"A": {"price": 100.0}, "VOO": {"price": 100.0}}
        with mock.patch.object(rw, "decide", fake_decide), \
                mock.patch.object(rw.reng, "validate", return_value=([], {"A": 0.9})), \
                mock.patch.object(rw.reng, "turnover_used", return_value=0.0), \
                mock.patch.object(rw.mg, "append_journal"), mock.patch.object(rw.mg, "due_reviews", return_value=[]), \
                mock.patch.object(rw.mg, "journal", return_value=[]), mock.patch.object(rw.mg, "lessons", return_value=""), \
                mock.patch.object(rw.pf, "snapshot", return_value={"returns": {}, "drawdown_from_peak": 0}), \
                mock.patch.object(rw, "StockTools") as st:
            st.return_value.history = {}
            st.return_value.sector_of.return_value = "Technology"
            out, _ = rw.run_decision("weekly", state, cfg, rules, {}, prices, 33.0,
                                     {"label": "risk-on", "signals": {}}, None, {}, {}, [], [],
                                     date(2026, 9, 26), "2026-09-25", "2026-09", [], False)
        self.assertEqual(len(calls), 2)
        self.assertIsNone(calls[0])
        self.assertIn("no tool calls", calls[1]["rejected_because"][0])
        self.assertTrue(out["unverified_first_attempt"])
        self.assertEqual(out["tools_used"], 1)
        self.assertEqual(out["checked"][0]["ticker"], "A")
        self.assertEqual(state["themes"], {"A": "AI infrastructure"})



class OutcomeRefreshTest(unittest.TestCase):
    def test_only_listed_tickers_change_and_original_kept(self):
        state = {"outcome_refresh": ["A", "B"],
                 "books": {"portfolio": {"holdings": {
                     "A": {"thesis": {"expected_outcome": "ราคาไป $550"}},
                     "B": {"thesis": {"expected_outcome": "ราคาเหนือ $270"}},
                     "C": {"thesis": {"expected_outcome": "เดิม"}}}}},
                 "pending_reviews": [{"ticker": "A", "expected_outcome": "ราคาไป $550"}]}
        done = rw.apply_outcome_updates(state, [
            {"ticker": "A", "expected_outcome": "งบ 15 ต.ค. รายได้โตเกิน 30% และ gross margin เกิน 55%"},
            {"ticker": "C", "expected_outcome": "พยายามแก้ตัวที่ไม่ได้อยู่ในรายการ ต้องไม่เปลี่ยน"},
            {"ticker": "B", "expected_outcome": "สั้น"}], "2026-10-02")
        h = state["books"]["portfolio"]["holdings"]
        self.assertEqual([x["ticker"] for x in done], ["A"])
        self.assertEqual(h["A"]["thesis"]["expected_outcome_original"], "ราคาไป $550")
        self.assertIn("gross margin", state["pending_reviews"][0]["expected_outcome"])
        self.assertEqual(h["C"]["thesis"]["expected_outcome"], "เดิม")
        self.assertEqual(state["outcome_refresh"], ["B"])   # B still pending (answer too short)


if __name__ == "__main__":
    unittest.main()


class EarningsQualityAndRecheck(unittest.TestCase):
    def test_trailing_pe_far_below_forward_is_flagged(self):
        from src.stock_tools import earnings_quality
        self.assertTrue(earnings_quality({"pe_ttm": 17.3, "pe_forward": 25.6}))
        self.assertEqual(earnings_quality({"pe_ttm": 28.9, "pe_forward": 19.0}), [])
        self.assertEqual(earnings_quality({"pe_ttm": -5, "pe_forward": 20}), [])

    def test_thesis_update_only_for_recheck_tickers_and_clears(self):
        from src.run_weekly import apply_thesis_updates
        s = {"valuation_recheck": ["GOOGL"],
             "books": {"portfolio": {"holdings": {"GOOGL": {"thesis": {"thesis": "old", "exit_condition": "x"}},
                                                  "MSFT": {"thesis": {"thesis": "keep"}}}}},
             "pending_reviews": [{"ticker": "GOOGL", "thesis": "old"}]}
        done = apply_thesis_updates(s, [{"ticker": "GOOGL", "thesis": "แก้ thesis ใหม่ด้วย forward PE"},
                                        {"ticker": "MSFT", "thesis": "should not change at all"}], "2026-10-03")
        g = s["books"]["portfolio"]["holdings"]["GOOGL"]["thesis"]
        self.assertEqual([d["ticker"] for d in done], ["GOOGL"])
        self.assertEqual((g["thesis_original"], g["exit_condition"]), ("old", "x"))
        self.assertEqual(s["books"]["portfolio"]["holdings"]["MSFT"]["thesis"]["thesis"], "keep")
        self.assertEqual(s["pending_reviews"][0]["thesis_original"], "old")
        self.assertIsNone(s["valuation_recheck"])
