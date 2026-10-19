import unittest
from datetime import date

from src import goals

CFG = {"goals": {"horizon_months": 12, "max_drawdown": 0.15, "min_hit_rate": 0.5, "min_graded_reviews": 4}}
R = {"vs_benchmark": 0.01, "vs_shadow": 0.0}


def ev(returns=R, dd=-0.02, hr=None, inc="2026-09-24", asof=date(2026, 10, 6), cfg=CFG):
    return goals.evaluate(cfg, returns, dd, hr or {}, inc, asof)


def row(rep, gid):
    return next(r for r in rep["goals"] if r["id"] == gid)


class GoalsTest(unittest.TestCase):
    def test_none_before_go_live(self):
        self.assertIsNone(ev(returns=None))
        self.assertIsNone(ev(inc=None))
        self.assertEqual(goals.telegram_lines(None), [])
        self.assertIsNone(goals.prompt_text(None, CFG))

    def test_never_traded_is_not_credited_as_beating_shadow(self):
        self.assertEqual(row(ev(), "beat_shadow")["status"], "unknown")

    def test_tiny_shadow_gap_is_noise_not_behind(self):
        # -0.0013% (a few baht on 100k) is rounding noise for a portfolio that has never traded
        self.assertEqual(row(ev(returns={"vs_benchmark": 0.01, "vs_shadow": -1.3e-5}), "beat_shadow")["status"], "unknown")
        self.assertEqual(row(ev(returns={"vs_benchmark": 0.01, "vs_shadow": 0.0004}), "beat_shadow")["status"], "unknown")
        self.assertEqual(row(ev(returns={"vs_benchmark": 0.01, "vs_shadow": -0.002}), "beat_shadow")["status"], "behind")
        self.assertEqual(row(ev(returns={"vs_benchmark": 0.01, "vs_shadow": 0.002}), "beat_shadow")["status"], "on_track")

    def test_beat_and_behind(self):
        rep = ev(returns={"vs_benchmark": -0.02, "vs_shadow": 0.01})
        self.assertEqual(row(rep, "beat_benchmark")["status"], "behind")
        self.assertEqual(row(rep, "beat_shadow")["status"], "on_track")
        self.assertEqual(rep["behind"], ["beat_benchmark"])

    def test_drawdown_limit(self):
        self.assertEqual(row(ev(dd=-0.14), "max_drawdown")["status"], "on_track")
        self.assertEqual(row(ev(dd=-0.16), "max_drawdown")["status"], "behind")

    def test_hit_rate_needs_enough_graded_reviews(self):
        few = {"correct": 1, "wrong": 1, "hit_rate": 0.5}
        self.assertEqual(row(ev(hr=few), "hit_rate")["status"], "unknown")
        enough = {"correct": 1, "wrong": 3, "hit_rate": 0.25}
        self.assertEqual(row(ev(hr=enough), "hit_rate")["status"], "behind")
        good = {"correct": 3, "wrong": 1, "hit_rate": 0.75}
        self.assertEqual(row(ev(hr=good), "hit_rate")["status"], "on_track")

    def test_progress_and_prompt(self):
        rep = ev()
        self.assertEqual(rep["day"], 12)
        self.assertAlmostEqual(rep["progress"], 12 / 365, places=2)
        txt = goals.prompt_text(rep, CFG)
        self.assertIn("beat_shadow=unknown", txt)
        self.assertIn("holding is still the default", txt)

    def test_defaults_when_config_missing(self):
        rep = ev(cfg={})
        self.assertEqual(rep["horizon_days"], 365)


class MonthlyReviewContextTest(unittest.TestCase):
    def test_review_gets_precomputed_performance(self):
        from unittest import mock

        from src import manager
        seen = {}

        def fake_call(system, user, cfg, tools, warnings, max_tokens=None):
            seen["system"], seen["ctx"] = system, __import__("json").loads(user)
            return '{"lessons_md": ""}', 0.01, []

        perf = {"value_thb": 99752.72, "returns": {"portfolio_thb": -0.00247}, "drawdown_from_peak": -0.034,
                "max_drawdown": -0.034}
        with mock.patch.object(manager, "call_claude", fake_call):
            out = manager.monthly_review({"status": "live", "inception_date": "2026-09-24"}, {}, [], "2026-10-17", [],
                                         performance=perf)
        self.assertEqual(seen["ctx"]["performance_now"], perf)
        self.assertIn("performance_now", seen["system"])
        self.assertEqual(out["cost"], 0.01)


if __name__ == "__main__":
    unittest.main()
