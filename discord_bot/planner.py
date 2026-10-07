"""Pure logic for the server-organizer bot (no Discord imports, so it is unit-testable).

- validate_template: catch mistakes in server_template.json before touching the server
- plan: diff the template against the server -> list of things to create (never deletes)
- audit: find clutter in an existing server
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable

CHANNEL_TYPES = ("text", "voice")
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")
MAX_NAME = 100


@dataclass(frozen=True)
class Action:
    kind: str  # create_role | create_category | create_channel
    name: str
    parent: str | None = None
    channel_type: str | None = None

    def describe(self) -> str:
        if self.kind == "create_role":
            return f"สร้างยศ **{self.name}**"
        if self.kind == "create_category":
            return f"สร้างหมวด **{self.name}**"
        icon = "🔊" if self.channel_type == "voice" else "#"
        return f"สร้างช่อง {icon}{self.name} ในหมวด {self.parent}"


def channel_key(name: str, channel_type: str) -> str:
    """Discord lowercases text channel names and replaces spaces with dashes."""
    n = name.strip().lower()
    return n.replace(" ", "-") if channel_type == "text" else n


def validate_template(t: dict) -> list[str]:
    errors: list[str] = []
    role_names = [r.get("name", "") for r in t.get("roles", [])]
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
    if t.get("default_role") and t["default_role"] not in role_names:
        errors.append(f"default_role {t['default_role']!r} ไม่อยู่ใน roles")

    chan_keys: set[tuple[str, str]] = set()
    cat_names: set[str] = set()
    for c in t.get("categories", []):
        cn = c.get("name", "")
        if not cn or len(cn) > MAX_NAME:
            errors.append(f"ชื่อหมวดไม่ถูกต้อง: {cn!r}")
        if cn.lower() in cat_names:
            errors.append(f"หมวดซ้ำ: {cn}")
        cat_names.add(cn.lower())
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
    wc = t.get("welcome_channel")
    if wc and ("text", channel_key(wc, "text")) not in chan_keys:
        errors.append(f"welcome_channel {wc!r} ไม่อยู่ในช่อง text ของ template")
    return errors


def plan(template: dict, existing_roles: Iterable[str],
         existing_channels: dict[str, dict[str, str]]) -> list[Action]:
    """What must be created so the server matches the template.

    existing_channels: {category name ("" = no category): {channel name: "text"|"voice"}}
    Matching is case-insensitive; a channel that already exists anywhere is not recreated.
    This never proposes deleting or moving anything.
    """
    have_roles = {r.lower() for r in existing_roles}
    have_cats = {c.lower() for c in existing_channels if c}
    have_chans = {(ty, channel_key(n, ty)) for chans in existing_channels.values() for n, ty in chans.items()}

    actions: list[Action] = []
    for r in template.get("roles", []):
        if r["name"].lower() not in have_roles:
            actions.append(Action("create_role", r["name"]))
    for c in template.get("categories", []):
        if c["name"].lower() not in have_cats:
            actions.append(Action("create_category", c["name"]))
        for ch in c.get("channels", []):
            ty = ch.get("type", "text")
            if (ty, channel_key(ch["name"], ty)) not in have_chans:
                actions.append(Action("create_channel", ch["name"], c["name"], ty))
    return actions


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
