import os
import unittest
from unittest import mock

from src import decide

CFG = {"decision_model": "claude-opus-5-5", "decision_max_tokens": 1000, "web_search_max_uses": 0,
       "web_search_usd_each": 0.01,
       "pricing_usd_per_mtok": {"claude-opus-5-5": [4, 20, 0.2], "other": [2, 10]}}


class Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body or {}, text

    def json(self):
        return self._body


def ok(stop, usage, content=None):
    return Resp(200, {"stop_reason": stop, "usage": usage,
                      "content": content or [{"type": "text", "text": "{}"}]})


class CostTest(unittest.TestCase):
    def test_cache_read_uses_model_rate(self):
        u = {"input_tokens": 1000, "cache_read_input_tokens": 1_000_000, "output_tokens": 0}
        self.assertAlmostEqual(decide._cost(u, "claude-opus-5-5", CFG), 0.004 + 0.2, places=6)

    def test_default_cache_read_is_tenth_of_input(self):
        u = {"cache_read_input_tokens": 1_000_000}
        self.assertAlmostEqual(decide._cost(u, "other", CFG), 0.2, places=6)

    def test_cache_write_is_1_25x_input_and_output_counts(self):
        u = {"cache_creation_input_tokens": 1_000_000, "output_tokens": 1_000_000}
        self.assertAlmostEqual(decide._cost(u, "claude-opus-5-5", CFG), 5.0 + 20.0, places=6)

    def test_plain_usage_unchanged(self):
        u = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
        self.assertAlmostEqual(decide._cost(u, "claude-opus-5-5", CFG), 24.0, places=6)


@mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "k"})
class LoopCachingTest(unittest.TestCase):
    def test_every_round_sends_top_level_cache_control(self):
        calls = []
        replies = [ok("tool_use", {"input_tokens": 10}, [{"type": "tool_use", "id": "t1", "name": "x", "input": {}}]),
                   ok("end_turn", {"input_tokens": 1, "cache_read_input_tokens": 500})]

        def post(url, json=None, **kw):
            calls.append(json)
            return replies[len(calls) - 1]

        with mock.patch.object(decide.requests, "post", post), \
                mock.patch.object(decide, "run_tool", lambda t, n, i: {"ok": 1}):
            text, cost, log_ = decide.call_claude("sys", "user", CFG, object(), [])
        self.assertEqual(text, "{}")
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(c.get("cache_control") == {"type": "ephemeral"} for c in calls))
        self.assertEqual(len(log_), 1)

    def test_falls_back_without_cache_when_api_rejects_it(self):
        calls, warnings = [], []
        replies = [Resp(400, text='{"error":"cache_control: Extra inputs are not permitted"}'),
                   ok("end_turn", {"input_tokens": 5})]

        def post(url, json=None, **kw):
            calls.append(json)
            return replies[len(calls) - 1]

        with mock.patch.object(decide.requests, "post", post):
            text, cost, _ = decide.call_claude("sys", "user", CFG, None, warnings)
        self.assertEqual(text, "{}")
        self.assertIn("cache_control", calls[0])
        self.assertNotIn("cache_control", calls[1])
        self.assertTrue(any("prompt caching" in w for w in warnings))

    def test_other_400_is_not_retried(self):
        calls, warnings = [], []

        def post(url, json=None, **kw):
            calls.append(json)
            return Resp(400, text="bad model")

        with mock.patch.object(decide.requests, "post", post):
            text, cost, _ = decide.call_claude("sys", "user", CFG, None, warnings)
        self.assertIsNone(text)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
