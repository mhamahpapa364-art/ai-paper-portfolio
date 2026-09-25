"""Offline tests for Part 2: rule enforcement, execution, reviews, JSON parsing."""
import copy
import json
import unittest
from datetime import date

from src import manager as mg
from src import portfolio as pf
from src import rules_engine as reng
from src.common import CONFIG
from src.decide import parse_json

RULES = json.load(open(CONFIG / "rules.json", encoding="utf-8"))
OK = lambda t: (True, "")  # noqa: E731


def tgt(t, w, full=True):
    d = {"ticker": t, "weight": w}
    if full:
        d.update(thesis="x", exit_condition="y", expected_outcome="z")
    return d


def live_state():
    prices = {t: {"price": 100.0} for t in ("A", "B", "C", "D", "E", "F", "VOO")}
    s = {"status": "pre-live", "start_capital_thb": 100000, "books": {}, "totals": {"dividends_usd": 0, "fees_usd": 0},
         "peak_value_thb": None}
    pf.seed_books(s, {"A": .2, "B": .2, "C": .2, "D": .2, "E": .1, "CASH": .1}, prices, 33.0, 0.0025, "VOO", "2026-01-02")
    return s, prices


class Validate(unittest.TestCase):
    def test_initial_ok(self):
        s, p = live_state()
        s["books"]["portfolio"] = {"cash_usd": 3000, "holdings": {}}
        d = {"action": "initial", "targets": [tgt(t, .18) for t in "ABCDE"]}
        errs, plan = reng.validate(d, s, p, RULES, OK, date(2026, 1, 2), initial=True)
        self.assertEqual(errs, [])
        self.assertAlmostEqual(sum(plan.values()), .9)

    def test_iron_rules(self):
        s, p = live_state()
        s["books"]["portfolio"] = {"cash_usd": 3000, "holdings": {}}
        cases = {
            "over 25%": [tgt("A", .30)] + [tgt(t, .15) for t in "BCDE"],
            "leverage": [tgt(t, .24) for t in "ABCDE"],
            "short": [tgt("A", -.1)] + [tgt(t, .2) for t in "BCDEF"],
            "too few": [tgt("A", .25), tgt("B", .25), tgt("C", .25)],
            "missing thesis": [tgt("A", .18, full=False)] + [tgt(t, .18) for t in "BCDE"],
        }
        for name, targets in cases.items():
            errs, _ = reng.validate({"targets": targets}, s, p, RULES, OK, date(2026, 1, 2), initial=True)
            self.assertTrue(errs, name)

    def test_ineligible_ticker(self):
        s, p = live_state()
        s["books"]["portfolio"] = {"cash_usd": 3000, "holdings": {}}
        d = {"targets": [tgt(t, .18) for t in "ABCDE"]}
        errs, _ = reng.validate(d, s, p, RULES, lambda t: (t != "E", "leveraged"), date(2026, 1, 2), initial=True)
        self.assertTrue(any("E: not eligible" in e for e in errs))

    def test_hold_is_always_legal(self):
        s, p = live_state()
        errs, plan = reng.validate({"action": "hold"}, s, p, RULES, OK, date(2026, 1, 9))
        self.assertEqual((errs, plan), ([], {}))

    def test_min_holding_and_sell_reason(self):
        s, p = live_state()
        d = {"action": "rebalance", "targets": [tgt(t, .2, False) for t in "ABCD"] + [tgt("F", .1)]}
        errs, _ = reng.validate(d, s, p, RULES, OK, date(2026, 1, 16))
        self.assertTrue(any("without a reason" in e for e in errs))
        d["sells"] = [{"ticker": "E", "reason": "better idea"}]
        errs, _ = reng.validate(d, s, p, RULES, OK, date(2026, 1, 16))
        self.assertTrue(any("minimum" in e for e in errs))
        d["sells"] = [{"ticker": "E", "reason": "fraud found", "thesis_broken": True}]
        errs, _ = reng.validate(d, s, p, RULES, OK, date(2026, 1, 16))
        self.assertEqual(errs, [])

    def test_turnover_cap(self):
        s, p = live_state()
        d = {"action": "rebalance", "targets": [tgt("F", .25), tgt("B", .25), tgt("C", .25), tgt("D", .1), tgt("E", .05)],
             "sells": [{"ticker": t, "reason": "r", "thesis_broken": True} for t in "ADE"]}
        errs, _ = reng.validate(d, s, p, RULES, OK, date(2026, 3, 1))
        self.assertTrue(any("turnover" in e for e in errs))


class SectorCap(unittest.TestCase):
    def test_sector_cap(self):
        s, p = live_state()
        s["books"]["portfolio"] = {"cash_usd": 3000, "holdings": {}}
        d = {"targets": [tgt(t, .18) for t in "ABCDE"]}
        tech = lambda t: "Technology" if t in "ABC" else "Health"  # noqa: E731
        errs, _ = reng.validate(d, s, p, RULES, OK, date(2026, 1, 2), initial=True, sector_of=tech)
        self.assertTrue(any("sector Technology" in e for e in errs))
        errs, _ = reng.validate(d, s, p, RULES, OK, date(2026, 1, 2), initial=True, sector_of=lambda t: t)
        self.assertEqual(errs, [])


class Execute(unittest.TestCase):
    def test_rebalance_keeps_cash_nonnegative_and_charges_fees(self):
        s, p = live_state()
        plan = {"A": .1, "B": .2, "C": .2, "D": .2, "E": .1, "F": .15}
        p2 = copy.deepcopy(p)
        trades = reng.execute(s, plan, p2, 0.0025, "2026-02-01")
        book = s["books"]["portfolio"]
        self.assertGreaterEqual(book["cash_usd"], -1e-9)
        self.assertEqual({t["side"] for t in trades}, {"buy", "sell"})
        w = pf.weights(book, p2)
        self.assertAlmostEqual(w["F"], .15, delta=.01)
        self.assertAlmostEqual(w["A"], .10, delta=.01)


class Reviews(unittest.TestCase):
    def test_record_and_grade(self):
        s, p = live_state()
        d = {"targets": [tgt("A", .2)]}
        mg.record_theses(s, d, [{"ticker": "A", "side": "buy"}], p, "2026-01-02", [4, 12])
        self.assertEqual(len(s["pending_reviews"]), 2)
        p["A"]["price"] = 110.0
        due = mg.due_reviews(s, p, {}, "VOO", date(2026, 2, 1))
        self.assertEqual(len(due), 1)
        self.assertAlmostEqual(due[0]["return_since_entry"], .1)
        orig = mg.SCORECARD_PATH
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as tmp:
            mg.SCORECARD_PATH = pathlib.Path(tmp) / "sc.json"
            card = mg.apply_reviews(s, [{"id": due[0]["id"], "verdict": "correct"}], due, "2026-02-01")
            mg.SCORECARD_PATH = orig
        self.assertEqual(len(s["pending_reviews"]), 1)
        self.assertEqual(mg.hit_rate(card)["hit_rate"], 1.0)


class RuleChanges(unittest.TestCase):
    def test_bounds_and_cooldown(self):
        r = copy.deepcopy(RULES)
        errs, ok = reng.validate_rule_changes({"min_holding_weeks": 6}, r, date(2026, 3, 1))
        self.assertEqual(ok, {"min_holding_weeks": 6})
        errs, ok = reng.validate_rule_changes({"min_holding_weeks": 40}, r, date(2026, 3, 1))
        self.assertEqual(ok, {})
        errs, ok = reng.validate_rule_changes({"max_position_weight": .5}, r, date(2026, 3, 1))
        self.assertEqual(ok, {})
        r["flexible_last_changed"] = "2026-02-20"
        errs, ok = reng.validate_rule_changes({"min_holding_weeks": 6}, r, date(2026, 3, 1))
        self.assertEqual(ok, {})


class Parse(unittest.TestCase):
    def test_parse_with_comments_and_prose(self):
        t = 'Here:\n{"action": "hold", // stay\n "targets": []}\nthanks'
        self.assertEqual(parse_json(t)["action"], "hold")
        self.assertIsNone(parse_json("no json"))


if __name__ == "__main__":
    unittest.main()
