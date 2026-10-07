"""Pure logic for the server-organizer bot (no Discord imports, so it is unit-testable).

- validate_template: catch mistakes in server_template.json before touching the server
- plan: diff the template against the server -> ordered list of actions
    full=False  additive only (create what is missing)
    full=True   also restyle roles, move channels into place, archive leftovers, delete old roles
- overwrite_spec: who can see / post in a channel, as plain data
- audit: find clutter in an existing server
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

CHANNEL_TYPES = ("text", "voice")
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_NAME = 100
EVERYONE = "@everyone"


@dataclass(frozen=True)
class RoleInfo:
    id: int
    name: str
    managed: bool = False   # bot/integration roles: never touched
    default: bool = False   # @everyone


@dataclass(frozen=True)
class CategoryInfo:
    id: int
    name: str
    other_children: int = 0  # channels of types we do not manage (forum, stage, ...)


@dataclass(frozen=True)
class ChannelInfo:
    id: int
    name: str
    type: str                  # text | voice
    category: str | None = None
    protected: bool = False    # e.g. the Community rules/updates channel: never archived


@dataclass(frozen=True)
class GuildState:
    roles: tuple[RoleInfo, ...] = ()
    categories: tuple[CategoryInfo, ...] = ()
    channels: tuple[ChannelInfo, ...] = ()


@dataclass(frozen=True)
class Action:
    kind: str
    name: str = ""
    parent: str | None = None
    channel_type: str | None = None
    ref: int | None = None  # id of the existing object this acts on

    def describe(self) -> str:
        icon = "🔊" if self.channel_type == "voice" else "#"
        return {
            "create_role": f"สร้างยศ **{self.name}**",
            "update_role": f"ตั้งค่ายศ **{self.name}** ให้ตรงแม่แบบ (สิทธิ์/สี)",
            "reorder_roles": "เรียงลำดับยศตามแม่แบบ",
            "assign_admin": f"ให้ยศ **{self.name}** กับคนที่สั่งคำสั่งนี้ (กันล็อกตัวเองออก)",
            "create_category": f"สร้างหมวด **{self.name}**",
            "update_category": f"ตั้งสิทธิ์การมองเห็นหมวด **{self.name}**",
            "create_channel": f"สร้างช่อง {icon}{self.name} ในหมวด {self.parent}",
            "move_channel": f"ย้ายช่อง {icon}{self.name} ไปหมวด {self.parent}",
            "update_channel": f"ตั้งสิทธิ์ช่อง {icon}{self.name}",
            "archive_channel": f"ย้ายช่องเดิม {icon}{self.name} ไป {self.parent}",
            "delete_category": f"ลบหมวดเดิมที่ว่างแล้ว **{self.name}**",
            "delete_role": f"🗑️ ลบยศเดิม **{self.name}**",
        }[self.kind]


def channel_key(name: str, channel_type: str) -> str:
    """Discord lowercases text channel names and replaces spaces with dashes."""
    n = name.strip().lower()
    return n.replace(" ", "-") if channel_type == "text" else n


def overwrite_spec(visible_to: list[str] | None, read_only: bool = False) -> dict[str, dict[str, bool]]:
    """Channel permission overwrites as {role name or "@everyone": {permission: allow?}}.

    visible_to empty/None = everyone can see it. The bot adds its own overwrite separately.
    """
    spec: dict[str, dict[str, bool]] = {}
    if visible_to:
        spec[EVERYONE] = {"view_channel": False}
        for r in visible_to:
            spec[r] = {"view_channel": True}
    if read_only:
        spec.setdefault(EVERYONE, {}).update(
            send_messages=False, add_reactions=False, create_public_threads=False,
            create_private_threads=False, send_messages_in_threads=False)
    return spec


def validate_template(t: dict, valid_permissions: set[str] | None = None) -> list[str]:
    errors: list[str] = []
    role_names = [r.get("name", "") for r in t.get("roles", [])]
    role_set = {n.lower() for n in role_names}
    seen: set[str] = set()
    for r in t.get("roles", []):
        n = r.get("name", "")
        if not n or len(n) > MAX_NAME:
            errors.append(f"ชื่อยศไม่ถูกต้อง: {n!r}")
        if n.lower() in seen:
            errors.append(f"ยศซ้ำ: {n}")
        seen.add(n.lower())
        if "color" in r and not HEX_COLOR.match(r["color"]):
            errors.append(f"สียศ {n} ต้องเป็น #RRGGBB")
        for p in r.get("permissions", []):
            if valid_permissions is not None and p not in valid_permissions:
                errors.append(f"ยศ {n}: ไม่รู้จักสิทธิ์ {p!r}")
    if t.get("admin_role") and t["admin_role"].lower() not in role_set:
        errors.append(f"admin_role {t['admin_role']!r} ไม่อยู่ใน roles")

    def check_visible(owner: str, vis: list[str]) -> None:
        for v in vis:
            if v.lower() not in role_set:
                errors.append(f"{owner}: visible_to มียศ {v!r} ที่ไม่อยู่ใน roles")

    chan_keys: set[tuple[str, str]] = set()
    cat_names: set[str] = set()
    for c in t.get("categories", []):
        cn = c.get("name", "")
        if not cn or len(cn) > MAX_NAME:
            errors.append(f"ชื่อหมวดไม่ถูกต้อง: {cn!r}")
        if cn.lower() in cat_names:
            errors.append(f"หมวดซ้ำ: {cn}")
        cat_names.add(cn.lower())
        check_visible(f"หมวด {cn}", c.get("visible_to", []))
        for ch in c.get("channels", []):
            n, ty = ch.get("name", ""), ch.get("type", "text")
            if not n or len(n) > MAX_NAME:
                errors.append(f"ชื่อช่องไม่ถูกต้อง: {n!r}")
            if ty not in CHANNEL_TYPES:
                errors.append(f"ช่อง {n}: type ต้องเป็น {CHANNEL_TYPES}")
                continue
            k = (ty, channel_key(n, ty))
            if k in chan_keys:
                errors.append(f"ช่องซ้ำ: {n}")
            chan_keys.add(k)
    arch = t.get("archive_category")
    if arch:
        if not arch.get("name"):
            errors.append("archive_category ต้องมี name")
        elif arch["name"].lower() in cat_names:
            errors.append("archive_category ชื่อซ้ำกับหมวดในแม่แบบ")
        check_visible("archive_category", arch.get("visible_to", []))
    return errors


def template_channels(t: dict) -> dict[tuple[str, str], tuple[dict, dict]]:
    """{(type, key): (category cfg, channel cfg)}"""
    out = {}
    for c in t.get("categories", []):
        for ch in c.get("channels", []):
            ty = ch.get("type", "text")
            out[(ty, channel_key(ch["name"], ty))] = (c, ch)
    return out


def plan(t: dict, s: GuildState, full: bool = False) -> list[Action]:
    """Ordered actions that make the server match the template (execution order = list order).

    Never deletes channels: leftovers are moved to the archive category. Only roles are deleted (full mode).
    """
    acts: list[Action] = []
    roles = {r.name.lower(): r for r in s.roles}
    template_role_names = {r["name"].lower() for r in t.get("roles", [])}

    # roles
    for r in t.get("roles", []):
        have = roles.get(r["name"].lower())
        if have is None:
            acts.append(Action("create_role", r["name"]))
        elif full and not have.managed:
            acts.append(Action("update_role", r["name"], ref=have.id))
    if t.get("roles") and (full or any(a.kind == "create_role" for a in acts)):
        acts.append(Action("reorder_roles"))
    if full and t.get("admin_role"):
        acts.append(Action("assign_admin", t["admin_role"]))

    # categories
    cats = {c.name.lower(): c for c in s.categories}
    wanted = [c["name"] for c in t.get("categories", [])]
    arch = t.get("archive_category") if full else None
    if arch:
        wanted.append(arch["name"])
    for name in wanted:
        have = cats.get(name.lower())
        if have is None:
            acts.append(Action("create_category", name))
        elif full:
            acts.append(Action("update_category", name, ref=have.id))

    # channels
    existing: dict[tuple[str, str], list[ChannelInfo]] = defaultdict(list)
    for ch in s.channels:
        existing[(ch.type, channel_key(ch.name, ch.type))].append(ch)
    keep_ids: set[int] = set()
    for (ty, key), (cat, cfg) in template_channels(t).items():
        found = existing.get((ty, key))
        if not found:
            acts.append(Action("create_channel", cfg["name"], cat["name"], ty))
            continue
        ch = found[0]
        keep_ids.add(ch.id)
        if not full:
            continue
        if (ch.category or "").lower() != cat["name"].lower():
            acts.append(Action("move_channel", ch.name, cat["name"], ty, ch.id))
        else:
            acts.append(Action("update_channel", ch.name, cat["name"], ty, ch.id))

    leaving: set[int] = set()
    if full and arch:
        for ch in s.channels:
            if ch.id in keep_ids or ch.protected:
                continue
            if (ch.category or "").lower() == arch["name"].lower():
                continue  # already archived
            acts.append(Action("archive_channel", ch.name, arch["name"], ch.type, ch.id))
            leaving.add(ch.id)

    # old categories that end up empty
    if full:
        moved_out = leaving | {a.ref for a in acts if a.kind == "move_channel"}
        keep_cats = {n.lower() for n in wanted}
        for c in s.categories:
            if c.name.lower() in keep_cats or c.other_children:
                continue
            stay = [ch for ch in s.channels if (ch.category or "").lower() == c.name.lower() and ch.id not in moved_out]
            if not stay:
                acts.append(Action("delete_category", c.name, ref=c.id))

    # old roles last, so nobody is locked out while the rest is being built
    if full:
        for r in s.roles:
            if r.default or r.managed or r.name.lower() in template_role_names:
                continue
            acts.append(Action("delete_role", r.name, ref=r.id))
    return acts


def gate(mode: str, confirm: str, admin_user_id: str) -> str | None:
    """Why a real (non-preview) one-shot run must be refused, or None if it may proceed.

    mode: preview | setup | reorganize. Only `reorganize` is destructive (deletes old roles).
    """
    if mode not in ("preview", "setup", "reorganize"):
        return f"mode ไม่ถูกต้อง: {mode!r}"
    if mode == "reorganize":
        if confirm != "RESET":
            return "ต้องพิมพ์ confirm = RESET (ตัวพิมพ์ใหญ่) ถึงจะจัด server ใหม่จริง"
        if not (admin_user_id or "").strip().isdigit():
            return "ต้องใส่ admin_user_id (Discord user id ของคุณ) เพื่อให้ยศ Admin ใหม่กับคุณ ไม่งั้นอาจล็อกตัวเองออก"
    return None


def audit(channels: list[dict], now: datetime | None = None, inactive_days: int = 30) -> list[str]:
    """Findings about clutter. Each channel dict: name, type, category (or None), topic, last_activity (datetime|None)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=inactive_days)
    findings: list[str] = []

    cats: dict[str, int] = {}
    for ch in channels:
        if ch["type"] == "category":
            cats.setdefault(ch["name"], 0)
        elif ch.get("category"):
            cats[ch["category"]] = cats.get(ch["category"], 0) + 1
    for name, n in cats.items():
        if n == 0:
            findings.append(f"📁 หมวด **{name}** ว่างเปล่า (ไม่มีช่อง)")

    for ch in channels:
        if ch["type"] == "category":
            continue
        label = f"{'🔊' if ch['type'] == 'voice' else '#'}{ch['name']}"
        if not ch.get("category"):
            findings.append(f"📌 {label} ไม่อยู่ในหมวดใด")
        if ch["type"] == "text" and not (ch.get("topic") or "").strip():
            findings.append(f"📝 {label} ยังไม่มีคำอธิบาย (topic)")
        if ch["type"] == "text":
            last = ch.get("last_activity")
            if last is None:
                findings.append(f"💤 {label} ไม่มีข้อความเลย")
            elif last < cutoff:
                findings.append(f"💤 {label} เงียบมา {(now - last).days} วัน")
    return findings
