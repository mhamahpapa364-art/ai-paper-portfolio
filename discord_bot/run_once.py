"""One-shot runner: log in, plan (and optionally apply), print, exit. For GitHub Actions or a quick local run.

    DISCORD_BOT_TOKEN=... python -m discord_bot.run_once --mode preview --guild-id 123
    modes: preview (show the full reorganize plan) | setup (create what is missing) | reorganize (needs --confirm RESET and --admin-user-id)
"""
from __future__ import annotations

import argparse
import logging
import os
import sys

import discord

from . import bot, planner

log = logging.getLogger("organizer")


class OneShot(discord.Client):
    def __init__(self, args: argparse.Namespace):
        super().__init__(intents=discord.Intents.default())
        self.args = args
        self.exit_code = 1

    async def on_ready(self) -> None:
        try:
            self.exit_code = await self.work()
        except Exception:  # noqa: BLE001 - report and exit non-zero, never hang the job
            log.exception("run failed")
            self.exit_code = 1
        finally:
            await self.close()

    async def work(self) -> int:
        a = self.args
        guild = self.get_guild(int(a.guild_id))
        if guild is None:
            print("❌ บอทไม่ได้อยู่ใน server นี้ (ตรวจ guild id และว่าเชิญบอทแล้ว)")
            return 1
        template = bot.load_template()
        actions = planner.plan(template, bot.read_state(guild), full=a.mode != "setup")
        print(f"server: {guild.name} — แผน {len(actions)} รายการ ({a.mode})")
        for act in actions:
            print("  •", act.describe())
        if a.mode == "preview":
            print("\nยังไม่ได้ทำอะไรจริง (โหมด preview)")
            return 0
        reason = planner.gate(a.mode, a.confirm, a.admin_user_id)
        if reason:
            print("\n❌ ไม่ทำต่อ:", reason)
            return 1
        invoker = await guild.fetch_member(int(a.admin_user_id)) if a.admin_user_id.strip() else None
        results = await bot.apply(guild, template, actions, invoker)
        print("\nผลลัพธ์")
        for r in results:
            print(" ", r)
        failed = sum(r.startswith("❌") for r in results)
        print(f"\nสำเร็จ {len(results) - failed}, ผิดพลาด {failed}")
        return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("preview", "setup", "reorganize"), default="preview")
    ap.add_argument("--guild-id", required=True)
    ap.add_argument("--admin-user-id", default="")
    ap.add_argument("--confirm", default="")
    args = ap.parse_args(argv)
    if not args.guild_id.strip().isdigit():
        print("❌ guild id ต้องเป็นตัวเลข")
        return 1
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    if not token:
        print("❌ ไม่พบ DISCORD_BOT_TOKEN — ตั้งเป็น environment variable ก่อนรัน (ดู README ข้อ 3; ถ้ารันผ่าน GitHub Actions ให้ใส่เป็น secret)")
        return 1
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    client = OneShot(args)
    client.run(token, log_handler=None)
    return client.exit_code


if __name__ == "__main__":
    sys.exit(main())
