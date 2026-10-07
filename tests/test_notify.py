import os
import unittest
from unittest import mock

from src import common

WEBHOOK = "https://discord.com/api/webhooks/123/abc"


class FakeResp:
    def __init__(self, status=204, body=None):
        self.status_code, self._body, self.text = status, body or {}, ""

    def json(self):
        return self._body


class HtmlToDiscordTests(unittest.TestCase):
    def test_formatting(self):
        out = common.html_to_discord('<b>หัวข้อ</b> <i>x</i> <code>NVDA</code> <a href="https://e.com/a?b=1">ลิงก์</a>')
        self.assertEqual(out, "**หัวข้อ** *x* `NVDA` [ลิงก์](https://e.com/a?b=1)")

    def test_entities_and_unknown_tags(self):
        self.assertEqual(common.html_to_discord("A &amp; B &lt;5% <span>ok</span>"), "A & B <5% ok")


class SplitTests(unittest.TestCase):
    def test_short_message_single_chunk(self):
        self.assertEqual(common.split_message("a\nb"), ["a\nb"])

    def test_chunks_respect_limit_and_keep_content(self):
        text = "\n".join(f"line {i} " + "x" * 80 for i in range(100))
        chunks = common.split_message(text, 500)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertEqual("\n".join(chunks), text)

    def test_overlong_single_line(self):
        chunks = common.split_message("y" * 1200, 500)
        self.assertEqual([len(c) for c in chunks], [500, 500, 200])


class DiscordSendTests(unittest.TestCase):
    def test_missing_or_bad_url_does_not_post(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch("requests.post") as post:
            self.assertFalse(common.discord("hi"))
            post.assert_not_called()
        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": "https://evil.example/hook"}, clear=True), \
                mock.patch("requests.post") as post:
            self.assertFalse(common.discord("hi"))
            post.assert_not_called()

    def test_sends_markdown_and_disables_mentions(self):
        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": WEBHOOK}, clear=True), \
                mock.patch("requests.post", return_value=FakeResp()) as post, \
                mock.patch("time.sleep"):
            self.assertTrue(common.discord("<b>hi</b> @everyone"))
        payload = post.call_args.kwargs["json"]
        self.assertEqual(payload["content"], "**hi** @everyone")
        self.assertEqual(payload["allowed_mentions"], {"parse": []})

    def test_error_status_returns_false(self):
        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": WEBHOOK}, clear=True), \
                mock.patch("requests.post", return_value=FakeResp(404)), mock.patch("time.sleep"):
            self.assertFalse(common.discord("hi"))

    def test_rate_limit_retries_once(self):
        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": WEBHOOK}, clear=True), \
                mock.patch("requests.post", side_effect=[FakeResp(429, {"retry_after": 0.1}), FakeResp()]) as post, \
                mock.patch("time.sleep"):
            self.assertTrue(common.discord("hi"))
        self.assertEqual(post.call_count, 2)


class NotifyTests(unittest.TestCase):
    def test_dry_run_sends_nothing(self):
        with mock.patch.object(common, "discord") as d, mock.patch.object(common, "telegram") as t:
            self.assertTrue(common.notify("x", dry_run=True))
            d.assert_not_called()
            t.assert_not_called()

    def test_no_channel_configured(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertFalse(common.notify("x"))

    def test_discord_only(self):
        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_URL": WEBHOOK}, clear=True), \
                mock.patch.object(common, "discord", return_value=True) as d, \
                mock.patch.object(common, "telegram") as t:
            self.assertTrue(common.notify("x"))
            d.assert_called_once()
            t.assert_not_called()

    def test_both_channels_ok_if_one_succeeds(self):
        env = {"DISCORD_WEBHOOK_URL": WEBHOOK, "TELEGRAM_BOT_TOKEN": "1:a", "TELEGRAM_CHAT_ID": "9"}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(common, "discord", return_value=False), \
                mock.patch.object(common, "telegram", return_value=True):
            self.assertTrue(common.notify("x"))


if __name__ == "__main__":
    unittest.main()
