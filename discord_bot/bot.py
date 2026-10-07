"""Server-organizer bot: builds/reorganizes roles, categories and channels from server_template.json.

It does not need to stay online: run it, use the commands, stop it. (Self-service roles are handled by
Discord's built-in Onboarding, see README.) Portfolio alerts are a separate webhook, not this bot.

    pip install -r discord_bot/requirements.txt
    DISCORD_BOT_TOKEN=... python -m discord_bot.bot

Optional env: DISCORD_GUILD_ID (instant command sync for one server), SERVER_TEMPLATE (path).
Channels are never deleted: leftovers are moved to the archive category.
"""
from __future__ import annotations

import io
import json
import logging
import os
import sys
from pathlib import Path

import discord
from discord import app_commands

from . import planner
from .planner import Action

log = logging.getLogger("organizer")
TEMPLATE_PATH = Path(os.environ.get("SERVER_TEMPLATE") or Path(__file__).with_name("server_template.json"))
CONFIRM_WORD = "RESET"


def load_template() -> dict:
    t = json.loads(TEMPLATE_PATH.read_text(encoding="utf-8"))
    errors = planner.validate_template(t, set(discord.Permissions.VALID_FLAGS))
    if errors:
        raise SystemExit("server_template.json มีข้อผิดพลาด:\n- " + "\n- ".join(errors))
    return t


def find_role(guild: discord.Guild, name: str) -> discord.Role | None:
    return discord.utils.find(lambda r: r.name.lower() == name.lower(), guild.roles)


def read_state(guild: discord.Guild) -> planner.GuildState:
    protected = {c.id for c in (guild.rules_channel, guild.public_updates_channel,
                                getattr(guild, "safety_alerts_channel", None)) if c}
    managed_types = (discord.TextChannel, discord.VoiceChannel)
    roles = tuple(planner.RoleInfo(r.id, r.name, r.managed, r.is_default()) for r in guild.roles)
    cats = tuple(planner.CategoryInfo(c.id, c.name, sum(1 for ch in c.channels if not isinstance(ch, managed_types)))
                 for c in guild.categories)
    chans = tuple(
        planner.ChannelInfo(ch.id, ch.name, "text" if isinstance(ch, discord.TextChannel) else "voice",
                            ch.category.name if ch.category else None, ch.id in protected)
        for ch in guild.channels if isinstance(ch, managed_types))
    return planner.GuildState(roles, cats, chans)


def overwrites_for(guild: discord.Guild, visible_to: list[str], read_only: bool = False) -> dict:
    spec = planner.overwrite_spec(visible_to, read_only)
    out: dict = {}
    for who, perms in spec.items():
        target = guild.default_role if who == planner.EVERYONE else find_role(guild, who)
        if target is not None:
            out[target] = discord.PermissionOverwrite(**perms)
    if spec:  # the bot must keep seeing/posting where it hides things from @everyone
        out[guild.me] = discord.PermissionOverwrite(view_channel=True, send_messages=True, embed_links=True,
                                                    manage_channels=True)
    return out


def role_kwargs(cfg: dict) -> dict:
    perms = discord.Permissions(**{p: True for p in cfg.get("permissions", [])})
    color = discord.Colour(int(cfg["color"][1:], 16)) if "color" in cfg else discord.Colour.default()
    return {"permissions": perms, "colour": color, "hoist": cfg.get("hoist", False), "mentionable": False}


async def apply(guild: discord.Guild, template: dict, actions: list[Action], invoker: discord.abc.User) -> list[str]:
    role_cfg = {r["name"].lower(): r for r in template.get("roles", [])}
    cat_cfg = {c["name"].lower(): c for c in template.get("categories", [])}
    arch = template.get("archive_category")
    if arch:
        cat_cfg[arch["name"].lower()] = {"visible_to": arch.get("visible_to", [])}
    chan_cfg = planner.template_channels(template)
    cats = {c.name.lower(): c for c in guild.categories}
    done: list[str] = []

    for a in actions:
        try:
            if a.kind == "create_role":
                await guild.create_role(name=a.name, reason="organizer", **role_kwargs(role_cfg[a.name.lower()]))
            elif a.kind == "update_role":
                await guild.get_role(a.ref).edit(reason="organizer", **role_kwargs(role_cfg[a.name.lower()]))
            elif a.kind == "reorder_roles":
                top = guild.me.top_role.position
                positions = {r: max(1, top - 1 - i) for i, cfg in enumerate(template["roles"])
                             if (r := find_role(guild, cfg["name"]))}
                await guild.edit_role_positions(positions, reason="organizer")
            elif a.kind == "assign_admin":
                role = find_role(guild, a.name)
                if role and isinstance(invoker, discord.Member):
                    await invoker.add_roles(role, reason="organizer: keep the invoker an admin")
            elif a.kind in ("create_category", "update_category"):
                cfg = cat_cfg[a.name.lower()]
                ov = overwrites_for(guild, cfg.get("visible_to", []))
                if a.kind == "create_category":
                    cats[a.name.lower()] = await guild.create_category(a.name, overwrites=ov, reason="organizer")
                else:
                    await guild.get_channel(a.ref).edit(overwrites=ov, reason="organizer")
            elif a.kind in ("create_channel", "move_channel", "update_channel"):
                cat, cfg = chan_cfg[(a.channel_type, planner.channel_key(a.name, a.channel_type))]
                ov = overwrites_for(guild, cat.get("visible_to", []), cfg.get("read_only", False))
                parent = cats.get(a.parent.lower())
                if a.kind == "create_channel":
                    if a.channel_type == "voice":
                        await guild.create_voice_channel(a.name, category=parent, overwrites=ov, reason="organizer")
                    else:
                        await guild.create_text_channel(a.name, category=parent, topic=cfg.get("topic"),
                                                        overwrites=ov, reason="organizer")
                else:
                    ch = guild.get_channel(a.ref)
                    kw = {"topic": cfg["topic"]} if a.channel_type == "text" and cfg.get("topic") and not ch.topic else {}
                    await ch.edit(category=parent, overwrites=ov, reason="organizer", **kw)
            elif a.kind == "archive_channel":
                ov = overwrites_for(guild, cat_cfg[a.parent.lower()].get("visible_to", []))
                await guild.get_channel(a.ref).edit(category=cats.get(a.parent.lower()), overwrites=ov,
                                                    reason="organizer: archive")
            elif a.kind == "delete_category":
                await guild.get_channel(a.ref).delete(reason="organizer: empty old category")
            elif a.kind == "delete_role":
                await guild.get_role(a.ref).delete(reason="organizer: reset roles")
            done.append("✅ " + a.describe())
        except discord.Forbidden:
            done.append("❌ " + a.describe() + " — บอทไม่มีสิทธิ์ (ยศของบอทต้องอยู่สูงกว่า และมี Manage Roles/Channels)")
        except discord.HTTPException as e:
            done.append(f"❌ {a.describe()} — {e.status} {e.text[:80]}")
        except (AttributeError, KeyError) as e:  # object vanished between preview and apply
            done.append(f"⚠️ {a.describe()} — ข้าม ({type(e).__name__})")
    return done


async def reply(interaction: discord.Interaction, text: str, followup: bool = False) -> None:
    """Send text; if it would exceed Discord's 2000-char limit, send it as a file instead."""
    send = interaction.followup.send if followup else interaction.response.send_message
    if len(text) <= 1900:
        await send(text, ephemeral=True)
    else:
        await send("ข้อความยาว แนบเป็นไฟล์", ephemeral=True,
                   file=discord.File(io.BytesIO(text.encode("utf-8")), filename="organizer.txt"))


def build_commands(client: "Organizer") -> None:
    admin_only = app_commands.checks.has_permissions(administrator=True)

    @client.tree.command(name="setup", description="สร้างหมวด/ช่อง/ยศที่ขาดตามแม่แบบ (ไม่แตะของเดิม)")
    @app_commands.describe(preview="True = แค่ดูตัวอย่าง (ค่าเริ่มต้น) / False = สร้างจริง")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @admin_only
    async def setup(interaction: discord.Interaction, preview: bool = True) -> None:
        guild = interaction.guild
        actions = planner.plan(client.template, read_state(guild), full=False)
        if not actions:
            return await reply(interaction, "✨ server มีครบตามแม่แบบแล้ว ไม่มีอะไรต้องสร้าง")
        if preview:
            lines = "\n".join(a.describe() for a in actions)
            return await reply(interaction, f"**ตัวอย่าง ({len(actions)} รายการ)** — ยังไม่ได้ทำจริง\n{lines}\n\nรัน `/setup preview:False` เพื่อสร้าง")
        await interaction.response.defer(ephemeral=True, thinking=True)
        await reply(interaction, "\n".join(await apply(guild, client.template, actions, interaction.user)), followup=True)

    @client.tree.command(name="reorganize", description="จัด server ใหม่ทั้งหมดตามแม่แบบ (ลบยศเดิม ย้ายห้องเดิมไป Archive)")
    @app_commands.describe(preview="True = แค่ดูตัวอย่าง (ค่าเริ่มต้น)",
                           confirm=f"ต้องพิมพ์ {CONFIRM_WORD} ถึงจะทำจริง")
    @app_commands.guild_only()
    @app_commands.default_permissions(administrator=True)
    @admin_only
    async def reorganize(interaction: discord.Interaction, preview: bool = True, confirm: str = "") -> None:
        guild = interaction.guild
        actions = planner.plan(client.template, read_state(guild), full=True)
        deletes = sum(a.kind == "delete_role" for a in actions)
        warn = (f"⚠️ จะ **ลบยศเดิม {deletes} ยศ** — สมาชิกทุกคนจะเสียยศเหล่านั้นทันที (ย้อนคืนไม่ได้) "
                f"และต้องรับยศ Member ใหม่ผ่าน Onboarding\n"
                f"ผู้ที่เป็นแอดมินผ่านยศเดิม (ไม่ใช่เจ้าของ server) จะเสียสิทธิ์ ยกเว้นคุณที่จะได้ยศ Admin ใหม่\n"
                f"ช่องเดิมที่ไม่อยู่ในแม่แบบ **ไม่ถูกลบ** จะถูกย้ายไป Archive")
        if preview or confirm != CONFIRM_WORD:
            lines = "\n".join(a.describe() for a in actions) or "ไม่มีอะไรต้องทำ"
            hint = (f"\n\nรันจริง: `/reorganize preview:False confirm:{CONFIRM_WORD}`" if preview
                    else f"\n\n❌ ยังไม่ได้ทำอะไร — ต้องใส่ confirm เป็น `{CONFIRM_WORD}` (ตัวพิมพ์ใหญ่)")
            return await reply(interaction, f"**ตัวอย่าง ({len(actions)} รายการ)**\n{warn}\n\n{lines}{hint}")
        await interaction.response.defer(ephemeral=True, thinking=True)
        results = await apply(guild, client.template, actions, interaction.user)
        failed = sum(r.startswith("❌") for r in results)
        await reply(interaction, f"เสร็จแล้ว — สำเร็จ {len(results) - failed}, ผิดพลาด {failed}\n" + "\n".join(results), followup=True)

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
        await reply(interaction, "**ผลตรวจ server**\n" + ("\n".join(findings) if findings else "✨ ไม่พบความรก server เรียบร้อยดี"))

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


class Organizer(discord.Client):
    def __init__(self, template: dict):
        super().__init__(intents=discord.Intents.default())  # no privileged intents needed
        self.template = template
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self) -> None:
        gid = os.environ.get("DISCORD_GUILD_ID")
        if gid:
            guild = discord.Object(int(gid))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()

    async def on_ready(self) -> None:
        log.info("logged in as %s", self.user)


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
