import json
import unittest
from datetime import datetime, timedelta, timezone

from discord_bot import planner
from discord_bot.planner import (Action, CategoryInfo, ChannelInfo, GuildState, RoleInfo, overwrite_spec)

TEMPLATE = json.load(open("discord_bot/server_template.json", encoding="utf-8"))

T = {
    "admin_role": "Admin",
    "roles": [{"name": "Admin"}, {"name": "Member"}],
    "categories": [
        {"name": "Info", "channels": [{"name": "welcome", "type": "text"}, {"name": "Voice Room", "type": "voice"}]},
    ],
    "archive_category": {"name": "Archive", "visible_to": ["Admin"]},
}
EVERYONE_ROLE = RoleInfo(1, "@everyone", default=True)


def state(roles=(), cats=(), chans=()):
    return GuildState((EVERYONE_ROLE, *roles), tuple(cats), tuple(chans))


def kinds(acts):
    return [a.kind for a in acts]


class ValidateTests(unittest.TestCase):
    def test_shipped_template_is_valid(self):
        self.assertEqual(planner.validate_template(TEMPLATE), [])

    def test_shipped_template_permissions_exist_in_discord_py(self):
        try:
            import discord
        except ImportError:
            self.skipTest("discord.py not installed")
        self.assertEqual(planner.validate_template(TEMPLATE, set(discord.Permissions.VALID_FLAGS)), [])
        # every overwrite flag the bot can emit must also be a real permission
        spec = overwrite_spec(["X"], read_only=True)
        for perms in spec.values():
            for p in perms:
                self.assertIn(p, discord.Permissions.VALID_FLAGS)
                self.assertTrue(hasattr(discord.PermissionOverwrite(), p))

    def test_catches_mistakes(self):
        bad = {
            "admin_role": "Ghost",
            "roles": [{"name": "A", "color": "red", "permissions": ["fly"]}, {"name": "a"}],
            "categories": [{"name": "C", "visible_to": ["Nobody"],
                            "channels": [{"name": "x", "type": "forum"}, {"name": "y"}, {"name": "Y"}]}],
            "archive_category": {"name": "c", "visible_to": ["Nobody"]},
        }
        errs = " | ".join(planner.validate_template(bad, {"administrator"}))
        for needle in ("ยศซ้ำ", "#RRGGBB", "ไม่รู้จักสิทธิ์", "admin_role", "visible_to", "type ต้องเป็น",
                       "ช่องซ้ำ", "archive_category ชื่อซ้ำ"):
            self.assertIn(needle, errs)


class OverwriteTests(unittest.TestCase):
    def test_public_writable(self):
        self.assertEqual(overwrite_spec([], False), {})

    def test_private_to_role(self):
        self.assertEqual(overwrite_spec(["Investor"]),
                         {"@everyone": {"view_channel": False}, "Investor": {"view_channel": True}})

    def test_read_only_blocks_posting_for_everyone_only(self):
        spec = overwrite_spec(["Member"], read_only=True)
        self.assertFalse(spec["@everyone"]["send_messages"])
        self.assertFalse(spec["@everyone"]["view_channel"])
        self.assertEqual(spec["Member"], {"view_channel": True})

    def test_shipped_template_visibility(self):
        cats = {c["name"]: c.get("visible_to", []) for c in TEMPLATE["categories"]}
        self.assertEqual(cats["📈 AI Portfolio"], ["Investor"])      # only Investor (+Admin) sees it
        self.assertEqual(cats["👋 เริ่มต้นที่นี่"], [])               # everyone sees the entry channels
        self.assertTrue(all(v for n, v in cats.items() if n != "👋 เริ่มต้นที่นี่"))
        investor = next(r for r in TEMPLATE["roles"] if r["name"] == "Investor")
        self.assertEqual(investor["permissions"], [])                # Investor is only a visibility key


class AdditivePlanTests(unittest.TestCase):
    def test_empty_server_creates_everything_without_touching_archive(self):
        acts = planner.plan(T, state())
        self.assertEqual(kinds(acts), ["create_role", "create_role", "reorder_roles",
                                       "create_category", "create_channel", "create_channel"])

    def test_idempotent_and_case_insensitive(self):
        s = state([RoleInfo(2, "admin"), RoleInfo(3, "MEMBER")], [CategoryInfo(10, "INFO")],
                  [ChannelInfo(20, "welcome", "text", "INFO"), ChannelInfo(21, "voice room", "voice", "INFO")])
        self.assertEqual(planner.plan(T, s), [])

    def test_never_moves_deletes_or_archives(self):
        s = state([RoleInfo(2, "Old")], [CategoryInfo(10, "Stuff")], [ChannelInfo(20, "welcome", "text", "Stuff"),
                                                                       ChannelInfo(21, "junk", "text", "Stuff")])
        acts = planner.plan(T, s)
        self.assertFalse({"move_channel", "archive_channel", "delete_role", "delete_category"} & set(kinds(acts)))


class FullPlanTests(unittest.TestCase):
    def setUp(self):
        self.s = state(
            roles=[RoleInfo(2, "Admin"), RoleInfo(3, "Old Role"), RoleInfo(4, "Booster", managed=True)],
            cats=[CategoryInfo(10, "Old Cat"), CategoryInfo(11, "Mixed", other_children=1)],
            chans=[ChannelInfo(20, "welcome", "text", "Old Cat"),      # in template, wrong category -> move
                   ChannelInfo(21, "chat", "text", "Old Cat"),         # not in template -> archive
                   ChannelInfo(22, "Voice Room", "voice", "Info"),     # in template, right category -> update
                   ChannelInfo(23, "forum-ish", "text", "Mixed"),      # archive, but category keeps other child
                   ChannelInfo(24, "rules", "text", None, protected=True),   # protected -> untouched
                   ChannelInfo(25, "welcome", "text", None)],          # duplicate of welcome -> archive
        )
        self.s = GuildState(self.s.roles, self.s.categories + (CategoryInfo(12, "Info"),), self.s.channels)
        self.acts = planner.plan(T, self.s, full=True)

    def by(self, kind):
        return [a for a in self.acts if a.kind == kind]

    def test_roles(self):
        self.assertEqual([a.name for a in self.by("delete_role")], ["Old Role"])   # managed + template + @everyone kept
        self.assertEqual([a.name for a in self.by("update_role")], ["Admin"])
        self.assertEqual([a.name for a in self.by("create_role")], ["Member"])
        self.assertEqual([a.name for a in self.by("assign_admin")], ["Admin"])

    def test_channels(self):
        self.assertEqual([(a.ref, a.parent) for a in self.by("move_channel")], [(20, "Info")])
        self.assertEqual([a.ref for a in self.by("update_channel")], [22])
        self.assertEqual(sorted(a.ref for a in self.by("archive_channel")), [21, 23, 25])
        self.assertNotIn(24, [a.ref for a in self.acts])                            # protected channel untouched
        self.assertEqual({a.parent for a in self.by("archive_channel")}, {"Archive"})

    def test_categories(self):
        self.assertEqual([a.name for a in self.by("create_category")], ["Archive"])
        self.assertEqual([a.name for a in self.by("update_category")], ["Info"])
        self.assertEqual([a.name for a in self.by("delete_category")], ["Old Cat"])  # emptied; "Mixed" has other children

    def test_order_keeps_admin_and_deletes_last(self):
        ks = kinds(self.acts)
        self.assertLess(ks.index("assign_admin"), ks.index("delete_role"))
        self.assertLess(max(i for i, k in enumerate(ks) if k in ("archive_channel", "move_channel")),
                        ks.index("delete_role"))
        self.assertEqual(ks[-1], "delete_role")

    def test_already_archived_not_archived_again(self):
        s = GuildState(self.s.roles, self.s.categories + (CategoryInfo(13, "Archive"),),
                       self.s.channels + (ChannelInfo(30, "old", "text", "Archive"),))
        self.assertNotIn(30, [a.ref for a in planner.plan(T, s, full=True) if a.kind == "archive_channel"])

    def test_every_action_describes(self):
        for a in self.acts:
            self.assertTrue(a.describe())


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


class GateTests(unittest.TestCase):
    def test_preview_and_setup_need_nothing(self):
        self.assertIsNone(planner.gate("preview", "", ""))
        self.assertIsNone(planner.gate("setup", "", ""))

    def test_reorganize_needs_confirm_and_admin_id(self):
        self.assertIn("RESET", planner.gate("reorganize", "", "123"))
        self.assertIn("RESET", planner.gate("reorganize", "reset", "123"))
        self.assertIn("admin_user_id", planner.gate("reorganize", "RESET", ""))
        self.assertIn("admin_user_id", planner.gate("reorganize", "RESET", "abc"))
        self.assertIsNone(planner.gate("reorganize", "RESET", " 123456789 "))

    def test_unknown_mode_refused(self):
        self.assertIsNotNone(planner.gate("nuke", "RESET", "1"))
