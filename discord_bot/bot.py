"""Server-organizer bot: builds channels/roles from server_template.json, audits clutter, role-picker buttons.

Run once from your own machine (it does not need to stay online for the portfolio alerts, which use a webhook):
    pip install -r discord_bot/requirements.txt
    DISCORD_BOT_TOKEN=... python -m discord_bot.bot

Optional env: DISCORD_GUILD_ID (instant command sync for one server), SERVER_TEMPLATE (path),
ENABLE_WELCOME=1 (welcome + default role; needs the "Server Members Intent" switched on in the Developer Portal).
Never deletes or moves anything that already exists.
"""
from __future__ import annotations

import json
import logging
import os
import sys
from pathlib import Path

import discord
from discord import app_commands

from . import planner

log = logging.getLogger("organizer")
TEMPLATE_PATH = Path(os.environ.get("SERVER_TEMPLATE") or Path(__file__).with_name("server_template.json"))
# A self-assignable role must never carry any of these, otherwise a button press would be privilege escalation.
DANGEROUS = ("administrator", "manage_guild", "manage_roles", "manage_channels", "manage_messages",
             "manage_webhooks", "kick_members", "ban_members", "mention_everyone", "moderate_members")


def load_template() -> dict:
    t = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    errors = planner.validate_template(t)
    if errors:
        raise SystemExit("server_template.json มีข้อผิดพลาด:\n- " + "\n- ".join(errors))
    return t


def clip(text: str, limit: int = 1900) -> str:
    return text if len(text) <= limit else text[: limit - 20] + "\n… (ตัดข้อความ)"


def find_role(guild: discord.Guild, name: str) -> discord.Role | None:
    return discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)


def guild_channels(guild: discord.Guild) -> dict[str, dict[str, str]]:
    existing: dict[str, dict[str, str]] = {"": {}}
    for cat in guild.categories:
        existing.setdefault(cat.name, {})
    for ch in guild.channels:
        if isinstance(ch, discord.CategoryChannel):
            continue
        ty = "text" if isinstance(ch, discord.TextChannel) else "voice" if isinstance(ch, discord.VoiceChannel) else None
        if ty:
            existing.setdefault(ch.category.name if ch.category else "", {})[ch.name] = ty
    return existing


class RoleButton(discord.ui.Button):
    def __init__(self, role_name: str):
        super().__init__(label=role_name, style=discord.ButtonStyle.secondary, custom_id=f"role:{role_name}"[:100])
        self.role_name = role_name

    async def callback(self, interaction: discord.Interaction) -> None:
        role = find_role(interaction.guild, self.role_name)
        if role is None:
            return await interaction.response.send_message("ยังไม่มียศนี้ — ให้แอดมินรัน /setup ก่อน", ephemeral=True)
        if any(getattr(role.permissions, p) for p in DANGEROUS):
            return await interaction.response.send_message("ยศนี้มีสิทธิ์สูง รับเองไม่ได้", ephemeral=True)
        member = interaction.user
        try:
            if role in member.roles:
                await member.remove_roles(role, reason="role panel")
                msg = f"เอายศ **{role.name}** ออกแล้ว"
            else:
                await member.add_roles(role, reason="role panel")
                msg = f"ให้ยศ **{role.name}** แล้ว"
        except discord.Forbidden:
            msg = "บอทไม่มีสิทธิ์ให้ยศนี้ — ให้แอดมินย้ายยศของบอทให้อยู่สูงกว่า"
        await interaction.response.send_message(msg, ephemeral=True)


class RolePanel(discord.ui.View):
    def __init__(self, role_names: list[str]):
        super().__init__(timeout=None)  # persistent: buttons keep working after a bot restart
        for n in role_names[:25]:
            self.add_item(RoleButton(n))


class Organizer(discord.Client):
    def __init__(self, template: dict):
        intents = discord.Intents.default()
        self.welcome = os.environ.get("ENABLE_WELCOME") == "1"
        intents.members = self.welcome
        super().__init__(intents=intents)
        self.template = template
        self.tree = app_commands.CommandTree(self)
        self.self_roles = [r["name"] for r in template.get("roles", []) if r.get("self_assignable")]

    async def setup_hook(self) -> None:
        self.add_view(RolePanel(self.self_roles))
        gid = os.environ.get("DISCORD_GUILD_ID")
        if gid:
            guild = discord.Object(int(gid))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("logged in as %s", self.user)

    async def on_member_join(self, member: discord.Member) -> None:
        t = self.template
        if t.get("default_role"):
            role = find_role(member.guild, t["default_role"])
            if role:
                try:
                    await member.add_roles(role, reason="default role")
                except discord.Forbidden:
                    log.warning("cannot assign default role (bot role too low)")
        ch = discord.utils.get(member.guild.text_channels, name=planner.channel_key(t.get("welcome_channel", ""), "text"))
        if ch and t.get("welcome_message"):
            text = t["welcome_message"].format(mention=member.mention, server=member.guild.name)
            await ch.send(text, allowed_mentions=discord.AllowedMentions(users=[member]))


async def apply(guild: discord.Guild, template: dict, actions: list[planner.Action]) -> list[str]:
    roles_cfg = {r["name"].lower(): r for r in template.get("roles", [])}
    chan_cfg = {(c["name"].lower(), ch["name"].lower()): ch
                for c in template.get("categories", []) for ch in c.get("channels", [])}
    cats = {c.name.lower(): c for c in guild.categories}
    done: list[str] = []
    for a in actions:
        try:
            if a.kind == "create_role":
                cfg = roles_cfg[a.name.lower()]
                color = discord.Colour(int(cfg["color"][1:], 16)) if "color" in cfg else discord.Colour.default()
                await guild.create_role(name=a.name, colour=color, hoist=cfg.get("hoist", False),
                                        mentionable=False, reason="organizer /setup")
            elif a.kind == "create_category":
                cats[a.name.lower()] = await guild.create_category(a.name, reason="organizer /setup")
            else:
                cfg = chan_cfg[(a.parent.lower(), a.name.lower())]
                parent = cats.get(a.parent.lower())
                if a.channel_type == "voice":
                    await guild.create_voice_channel(a.name, category=parent, reason="organizer /setup")
                else:
                    overwrites = {}
                    if cfg.get("read_only"):
                        overwrites = {
                            guild.default_role: discord.PermissionOverwrite(send_messages=False, add_reactions=False,
                                                                            create_public_threads=False),
                            guild.me: discord.PermissionOverwrite(send_messages=True, embed_links=True),
                        }
                    await guild.create_text_channel(a.name, category=parent, topic=cfg.get("topic"),
                                                    overwrites=overwrites, reason="organizer /setup")
            done.append("✅ " + a.describe())
        except discord.Forbidden:
            done.append("❌ " + a.describe() + " — บอทไม่มีสิทธิ์ (ต้องมี Manage Roles / Manage Channels)")
        except discord.HTTPException as e:
            done.append(f"❌ {a.describe()} — {e.status}")
    return done


def build_commands(client: Organizer) -> None:
    admin_only = app_commands.checks.has_permissions(administrator=True)

    @client.tree.command(name="setup", description="สร้างหมวด/ช่อง/ยศตามแม่แบบ (ไม่ลบหรือย้ายของเดิม)")
    @app_commands.describe(preview="True = แค่ดูว่าจะสร้างอะไร (ค่าเริ่มต้น) / False = สร้างจริง")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @admin_only
    async def setup(interaction: discord.Interaction, preview: bool = True) -> None:
        guild = interaction.guild
        actions = planner.plan(client.template, [r.name for r in guild.roles], guild_channels(guild))
        if not actions:
            return await interaction.response.send_message("✨ server ตรงกับแม่แบบอยู่แล้ว ไม่มีอะไรต้องสร้าง", ephemeral=True)
        if preview:
            lines = "\n".join(a.describe() for a in actions)
            return await interaction.response.send_message(
                clip(f"**ตัวอย่าง ({len(actions)} รายการ)** — ยังไม่ได้สร้างจริง\n{lines}\n\nรัน `/setup preview:False` เพื่อสร้าง"),
                ephemeral=True)
        await interaction.response.defer(ephemeral=True, thinking=True)
        results = await apply(guild, client.template, actions)
        await interaction.followup.send(clip("\n".join(results)), ephemeral=True)

    @client.tree.command(name="audit", description="ตรวจความรกของ server (หมวดว่าง ช่องเงียบ ช่องไม่มี topic)")
    @app_commands.describe(days="ช่องที่เงียบเกินกี่วันถือว่าไม่ได้ใช้")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @admin_only
    async def audit(interaction: discord.Interaction, days: app_commands.Range[int, 1, 365] = 30) -> None:
        rows = []
        for ch in interaction.guild.channels:
            if isinstance(ch, discord.CategoryChannel):
                rows.append({"name": ch.name, "type": "category"})
            elif isinstance(ch, (discord.TextChannel, discord.VoiceChannel)):
                is_text = isinstance(ch, discord.TextChannel)
                rows.append({
                    "name": ch.name, "type": "text" if is_text else "voice",
                    "category": ch.category.name if ch.category else None,
                    "topic": ch.topic if is_text else None,
                    "last_activity": (discord.utils.snowflake_time(ch.last_message_id)
                                      if is_text and ch.last_message_id else None),
                })
        findings = planner.audit(rows, inactive_days=days)
        text = "\n".join(findings) if findings else "✨ ไม่พบความรก server เรียบร้อยดี"
        await interaction.response.send_message(clip(f"**ผลตรวจ server**\n{text}"), ephemeral=True)

    @client.tree.command(name="rolepanel", description="โพสต์แผงปุ่มให้สมาชิกกดรับ/เอายศ")
    @app_commands.describe(channel="ช่องที่จะโพสต์ (ค่าเริ่มต้น: ช่องนี้)")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @admin_only
    async def rolepanel(interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        if not client.self_roles:
            return await interaction.response.send_message("แม่แบบไม่มียศที่ตั้ง self_assignable ไว้", ephemeral=True)
        target = channel or interaction.channel
        await target.send("**เลือกยศของคุณ** — กดปุ่มเพื่อรับ กดซ้ำเพื่อเอาออก", view=RolePanel(client.self_roles))
        await interaction.response.send_message(f"โพสต์แผงยศใน {target.mention} แล้ว", ephemeral=True)

    @client.tree.error
    async def on_error(interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.MissingPermissions):
            msg = "คำสั่งนี้สำหรับแอดมินเท่านั้น"
        else:
            log.exception("command failed", exc_info=error)
            msg = "เกิดข้อผิดพลาด ลองใหม่อีกครั้ง"
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("ไม่พบ DISCORD_BOT_TOKEN (สร้างที่ https://discord.com/developers/applications → Bot → Reset Token)")
        return 1
    client = Organizer(load_template())
    build_commands(client)
    client.run(token, log_handler=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
