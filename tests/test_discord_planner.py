import json
import unittest
from datetime import datetime, timedelta, timezone

from discord_bot import planner
from discord_bot.planner import Action

ROOT_TEMPLATE = json.load(open("discord_bot/server_template.json", encoding="utf-8"))

T = {
    "default_role": "Member",
    "welcome_channel": "welcome",
    "roles": [{"name": "Member"}, {"name": "Dev", "self_assignable": True}],
    "categories": [
        {"name": "Info", "channels": [{"name": "welcome", "type": "text"}, {"name": "Voice Room", "type": "voice"}]},
    ],
}


class ValidateTests(unittest.TestCase):
    def test_shipped_template_is_valid(self):
        self.assertEqual(planner.validate_template(ROOT_TEMPLATE), [])

    def test_catches_mistakes(self):
        bad = {
            "default_role": "Ghost", "welcome_channel": "nope",
            "roles": [{"name": "A", "color": "red"}, {"name": "a"}],
            "categories": [{"name": "C", "channels": [{"name": "x", "type": "forum"}, {"name": "y"}, {"name": "Y"}]}],
        }
        errs = " | ".join(planner.validate_template(bad))
        for needle in ("ยศซ้ำ", "#RRGGBB", "default_role", "type ต้องเป็น", "ช่องซ้ำ", "welcome_channel"):
            self.assertIn(needle, errs)


class PlanTests(unittest.TestCase):
    def test_empty_server_creates_everything(self):
        acts = planner.plan(T, [], {})
        self.assertEqual([a.kind for a in acts],
                         ["create_role", "create_role", "create_category", "create_channel", "create_channel"])

    def test_idempotent_and_case_insensitive(self):
        existing = {"INFO": {"welcome": "text", "voice room": "voice"}}
        self.assertEqual(planner.plan(T, ["member", "DEV"], existing), [])

    def test_text_channel_name_normalization(self):
        t = {"categories": [{"name": "C", "channels": [{"name": "My Chat", "type": "text"}]}]}
        self.assertEqual(planner.plan(t, [], {"C": {"my-chat": "text"}}), [])

    def test_channel_existing_elsewhere_is_not_recreated(self):
        acts = planner.plan(T, ["Member", "Dev"], {"": {"welcome": "text"}, "Info": {"Voice Room": "voice"}})
        self.assertEqual(acts, [])  # welcome exists (uncategorized) and Voice Room exists; nothing to create

    def test_missing_pieces_only(self):
        acts = planner.plan(T, ["Member"], {"Info": {"welcome": "text"}})
        self.assertEqual(acts, [Action("create_role", "Dev"), Action("create_channel", "Voice Room", "Info", "voice")])


class AuditTests(unittest.TestCase):
    NOW = datetime(2026, 10, 7, tzinfo=timezone.utc)

    def test_findings(self):
        chans = [
            {"name": "Empty", "type": "category"},
            {"name": "Full", "type": "category"},
            {"name": "old", "type": "text", "category": "Full", "topic": "x", "last_activity": self.NOW - timedelta(days=90)},
            {"name": "fresh", "type": "text", "category": "Full", "topic": "x", "last_activity": self.NOW - timedelta(days=1)},
            {"name": "loose", "type": "text", "category": None, "topic": "", "last_activity": None},
            {"name": "vc", "type": "voice", "category": "Full"},
        ]
        out = "\n".join(planner.audit(chans, self.NOW))
        self.assertIn("หมวด **Empty** ว่างเปล่า", out)
        self.assertNotIn("**Full** ว่างเปล่า", out)
        self.assertIn("#old เงียบมา 90 วัน", out)
        self.assertNotIn("#fresh", out)
        self.assertIn("#loose ไม่อยู่ในหมวดใด", out)
        self.assertIn("#loose ยังไม่มีคำอธิบาย", out)
        self.assertIn("#loose ไม่มีข้อความเลย", out)
        self.assertNotIn("🔊vc", out)


if __name__ == "__main__":
    unittest.main()
