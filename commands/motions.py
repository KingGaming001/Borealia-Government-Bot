# commands/motions.py
# ------------------------------------------------------------
# Parliament Motions (Acts, Resolutions, etc.)
#
# Slash commands and orchestration layer.
# Heavy helpers are in:
# - commands/motion_utils.py
# - commands/motion_views.py
# ------------------------------------------------------------

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3
from typing import Any, cast

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config_store import (
    get_settings,
    has_associate_parliamentarian_role,
    has_king_role,
    has_parliament_role,
    is_admin,
)
from commands.motion_utils import (
    build_assent_decision_embed,
    build_assent_request_embed,
    build_law_embed,
    build_motion_draft_embed,
    build_motion_rollcall_embed,
    build_repeal_embed,
    build_result_embed,
    get_repeal_motion_summary,
    get_repeal_original_proposer,
    iso_now,
    motion_kind,
    motion_value,
    parse_iso_utc,
    proposer_mention,
    tally_motion,
)
from commands.motion_views import MotionVoteView, RoyalAssentView


async def update_rollcall_message(bot: commands.Bot, guild: discord.Guild, motion_id: int, clear_view: bool = False) -> None:
    db: sqlite3.Connection = cast(Any, bot).db
    cur = db.cursor()

    cur.execute("SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?", (guild.id, motion_id))
    motion = cur.fetchone()
    if not motion:
        return

    if not motion["message_channel_id"] or not motion["message_id"]:
        return

    channel = guild.get_channel(int(motion["message_channel_id"]))
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        msg = await channel.fetch_message(int(motion["message_id"]))
    except discord.NotFound:
        return

    tally = tally_motion(db, guild.id, motion_id)
    repeal_motion_summary = get_repeal_motion_summary(db, guild.id, motion) if motion_kind(motion) == "repeal" else None
    repeal_original_proposer = get_repeal_original_proposer(db, guild.id, motion) if motion_kind(motion) == "repeal" else None

    embed = build_motion_rollcall_embed(
        motion_id,
        motion,
        tally,
        guild,
        repeal_motion_summary=repeal_motion_summary,
        repeal_original_proposer=repeal_original_proposer,
    )

    if clear_view:
        await msg.edit(embed=embed, view=None)
    else:
        await msg.edit(embed=embed)


class Motions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db: sqlite3.Connection = cast(Any, bot).db

    async def cog_load(self):
        if not self.motion_scheduler.is_running():
            self.motion_scheduler.start()

    async def cog_unload(self):
        if self.motion_scheduler.is_running():
            self.motion_scheduler.cancel()

    async def on_motion_vote_recorded(self, guild: discord.Guild, motion_id: int) -> None:
        await update_rollcall_message(self.bot, guild, motion_id)

    @tasks.loop(seconds=30)
    async def motion_scheduler(self):
        now_utc = datetime.now(timezone.utc)
        cur = self.db.cursor()
        cur.execute(
            """
            SELECT guild_id, motion_id, closes_at
            FROM motions
            WHERE status = 'VOTING' AND closes_at IS NOT NULL
            """
        )
        rows = cur.fetchall()

        for row in rows:
            closes_at = parse_iso_utc(row["closes_at"])
            if not closes_at or closes_at > now_utc:
                continue

            guild = self.bot.get_guild(int(row["guild_id"]))
            if not guild:
                continue

            await self.close_motion_and_publish_result(guild, int(row["motion_id"]))

    @motion_scheduler.before_loop
    async def before_motion_scheduler(self):
        await self.bot.wait_until_ready()
        await self.restore_open_motion_vote_views()
        await self.restore_pending_assent_views()

    async def restore_open_motion_vote_views(self):
        cur = self.db.cursor()
        cur.execute(
            """
            SELECT guild_id, motion_id, message_channel_id, message_id
            FROM motions
            WHERE status = 'VOTING'
              AND message_channel_id IS NOT NULL
              AND message_id IS NOT NULL
            """
        )
        rows = cur.fetchall()

        for row in rows:
            guild = self.bot.get_guild(int(row["guild_id"]))
            if not guild:
                continue

            channel = guild.get_channel(int(row["message_channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                continue

            try:
                message = await channel.fetch_message(int(row["message_id"]))
                await message.edit(view=MotionVoteView(self.bot, int(row["motion_id"]), self.on_motion_vote_recorded))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue

    async def restore_pending_assent_views(self):
        cur = self.db.cursor()
        cur.execute(
            """
            SELECT guild_id, motion_id, assent_channel_id, assent_message_id
            FROM motions
            WHERE status = 'CLOSED'
              AND royal_assent_status = 'PENDING'
              AND assent_channel_id IS NOT NULL
              AND assent_message_id IS NOT NULL
            """
        )
        rows = cur.fetchall()

        for row in rows:
            guild = self.bot.get_guild(int(row["guild_id"]))
            if not guild:
                continue

            channel = guild.get_channel(int(row["assent_channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                continue

            try:
                message = await channel.fetch_message(int(row["assent_message_id"]))
                await message.edit(view=RoyalAssentView(self.handle_royal_assent, int(row["motion_id"])))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue

    async def post_royal_assent_request(self, guild: discord.Guild, motion) -> None:
        motion_id = int(motion["motion_id"])
        channel = None
        if motion["message_channel_id"]:
            channel = guild.get_channel(int(motion["message_channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return

        embed = build_assent_request_embed(motion_id, motion)
        view = RoyalAssentView(self.handle_royal_assent, motion_id)

        assent_message = None
        if motion_value(motion, "assent_channel_id") and motion_value(motion, "assent_message_id"):
            saved_channel = guild.get_channel(int(motion["assent_channel_id"]))
            if isinstance(saved_channel, discord.TextChannel):
                try:
                    existing = await saved_channel.fetch_message(int(motion["assent_message_id"]))
                    await existing.edit(embed=embed, view=view)
                    assent_message = existing
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    assent_message = None

        if assent_message is None:
            assent_message = await channel.send(embed=embed, view=view)

        cur = self.db.cursor()
        cur.execute(
            """
            UPDATE motions
            SET assent_channel_id = ?, assent_message_id = ?
            WHERE guild_id = ? AND motion_id = ?
            """,
            (channel.id, assent_message.id, guild.id, motion_id),
        )
        self.db.commit()

    async def update_assent_message(self, guild: discord.Guild, motion, decision: str, decider: discord.Member) -> None:
        if not motion_value(motion, "assent_channel_id") or not motion_value(motion, "assent_message_id"):
            return
        channel = guild.get_channel(int(motion["assent_channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            message = await channel.fetch_message(int(motion["assent_message_id"]))
            await message.edit(embed=build_assent_decision_embed(int(motion["motion_id"]), motion, decision, decider), view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    async def get_act(self, guild_id: int, act_id: int):
        cur = self.db.cursor()
        cur.execute(
            """
            SELECT *
            FROM acts
            WHERE guild_id = ? AND act_id = ?
            """,
            (guild_id, act_id),
        )
        return cur.fetchone()

    async def register_enacted_act(self, guild_id: int, motion, enacted_by_user_id: int) -> int:
        cur = self.db.cursor()

        source_motion_id = int(motion["motion_id"])
        cur.execute(
            """
            SELECT act_id
            FROM acts
            WHERE guild_id = ? AND source_motion_id = ?
            """,
            (guild_id, source_motion_id),
        )
        existing = cur.fetchone()
        if existing:
            return int(existing["act_id"])

        cur.execute(
            """
            INSERT INTO acts (guild_id, source_motion_id, title, text, enacted_by_user_id, enacted_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'ENACTED')
            """,
            (guild_id, source_motion_id, motion["title"], motion["text"], enacted_by_user_id, iso_now()),
        )
        self.db.commit()
        lastrowid = cur.lastrowid
        if lastrowid is None:
            raise RuntimeError("Failed to persist enacted act")
        return int(lastrowid)

    async def apply_repeal_to_act(self, guild_id: int, target_act_id: int, repeal_motion_id: int, assenter_user_id: int) -> bool:
        cur = self.db.cursor()
        cur.execute(
            """
            UPDATE acts
            SET status = 'REPEALED',
                repealed_by_motion_id = ?,
                repealed_by_user_id = ?,
                repealed_at = ?
            WHERE guild_id = ?
              AND act_id = ?
              AND status = 'ENACTED'
            """,
            (repeal_motion_id, assenter_user_id, iso_now(), guild_id, target_act_id),
        )
        self.db.commit()
        return cur.rowcount > 0

    async def publish_law_from_motion(self, guild: discord.Guild, motion, decider: discord.Member) -> bool:
        settings = get_settings(self.db, guild.id)
        if not settings or not settings.get("laws_channel_id"):
            return False
        channel = guild.get_channel(int(settings["laws_channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return False
        await channel.send(embed=build_law_embed(int(motion["motion_id"]), motion, decider))
        return True

    async def publish_repeal_from_motion(self, guild: discord.Guild, motion, target_act_id: int, decider: discord.Member) -> bool:
        settings = get_settings(self.db, guild.id)
        if not settings or not settings.get("laws_channel_id"):
            return False
        channel = guild.get_channel(int(settings["laws_channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return False
        repeal_motion_summary = get_repeal_motion_summary(self.db, guild.id, motion)
        repeal_original_proposer = get_repeal_original_proposer(self.db, guild.id, motion)
        await channel.send(
            embed=build_repeal_embed(
                int(motion["motion_id"]),
                motion,
                target_act_id,
                decider,
                repeal_motion_summary=repeal_motion_summary,
                repeal_original_proposer=repeal_original_proposer,
            )
        )
        return True

    async def handle_royal_assent(self, interaction: discord.Interaction, motion_id: int, action: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Server-only.", ephemeral=True)

        settings = get_settings(self.db, interaction.guild.id)
        if not has_king_role(interaction.user, settings):
            return await interaction.response.send_message(
                "❌ Only members with the configured King role can grant or deny Royal Assent.",
                ephemeral=True,
            )

        cur = self.db.cursor()
        cur.execute("SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?", (interaction.guild.id, motion_id))
        motion = cur.fetchone()
        if not motion:
            return await interaction.response.send_message("❌ Motion not found.", ephemeral=True)
        if str(motion["status"]) != "CLOSED":
            return await interaction.response.send_message("❌ Motion is not closed yet.", ephemeral=True)
        if str(motion["royal_assent_status"] or "") != "PENDING":
            return await interaction.response.send_message("ℹ️ Royal Assent has already been finalized for this motion.", ephemeral=True)

        decision = "APPROVED" if action == "approve" else "REJECTED"
        final_result = "PASSED" if decision == "APPROVED" else "FAILED"

        cur.execute(
            """
            UPDATE motions
            SET royal_assent_status = ?,
                royal_assented_by = ?,
                royal_assented_at = ?,
                final_result = ?
            WHERE guild_id = ? AND motion_id = ? AND royal_assent_status = 'PENDING'
            """,
            (decision, interaction.user.id, iso_now(), final_result, interaction.guild.id, motion_id),
        )
        if cur.rowcount == 0:
            self.db.commit()
            return await interaction.response.send_message("ℹ️ Royal Assent has already been finalized for this motion.", ephemeral=True)

        self.db.commit()

        cur.execute("SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?", (interaction.guild.id, motion_id))
        updated_motion = cur.fetchone()
        if not updated_motion:
            return await interaction.response.send_message("❌ Motion state could not be loaded.", ephemeral=True)

        await self.update_assent_message(interaction.guild, updated_motion, decision, interaction.user)
        await update_rollcall_message(self.bot, interaction.guild, motion_id)

        if decision == "APPROVED":
            kind = motion_kind(updated_motion)
            if kind == "repeal":
                target_act_id = motion_value(updated_motion, "target_act_id")
                if not target_act_id:
                    return await interaction.response.send_message(
                        f"✅ Royal Assent approved for motion #{motion_id}, but no target act was set.",
                        ephemeral=True,
                    )

                repealed = await self.apply_repeal_to_act(
                    interaction.guild.id,
                    int(target_act_id),
                    int(updated_motion["motion_id"]),
                    interaction.user.id,
                )

                repeal_posted = await self.publish_repeal_from_motion(
                    interaction.guild,
                    updated_motion,
                    int(target_act_id),
                    interaction.user,
                )

                if repealed and repeal_posted:
                    return await interaction.response.send_message(
                        f"✅ Royal Assent approved for motion #{motion_id}. Act #{int(target_act_id)} has been repealed and posted in laws.",
                        ephemeral=True,
                    )
                if repealed:
                    return await interaction.response.send_message(
                        f"✅ Royal Assent approved for motion #{motion_id}. Act #{int(target_act_id)} has been repealed.",
                        ephemeral=True,
                    )
                return await interaction.response.send_message(
                    f"✅ Royal Assent approved for motion #{motion_id}, but Act #{int(target_act_id)} was already repealed or not found.",
                    ephemeral=True,
                )

            law_posted = await self.publish_law_from_motion(interaction.guild, updated_motion, interaction.user)
            if kind == "act":
                act_id = await self.register_enacted_act(interaction.guild.id, updated_motion, interaction.user.id)
                if law_posted:
                    return await interaction.response.send_message(
                        f"✅ Royal Assent approved for motion #{motion_id}. Enacted as Act #{act_id} and posted in the laws channel.",
                        ephemeral=True,
                    )
                return await interaction.response.send_message(
                    f"✅ Royal Assent approved for motion #{motion_id}. Enacted as Act #{act_id}, but no valid laws channel is configured.",
                    ephemeral=True,
                )

            if law_posted:
                return await interaction.response.send_message(
                    f"✅ Royal Assent approved for motion #{motion_id}. The motion has been posted in the laws channel.",
                    ephemeral=True,
                )
            return await interaction.response.send_message(
                f"✅ Royal Assent approved for motion #{motion_id}, but no valid laws channel is configured.",
                ephemeral=True,
            )

        return await interaction.response.send_message(
            f"✅ Royal Assent rejected for motion #{motion_id}. The motion is now marked as FAILED.",
            ephemeral=True,
        )

    async def close_motion_and_publish_result(self, guild: discord.Guild, motion_id: int):
        cur = self.db.cursor()
        cur.execute("SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?", (guild.id, motion_id))
        motion = cur.fetchone()
        if not motion or motion["status"] != "VOTING":
            return None

        tally = tally_motion(self.db, guild.id, motion_id)
        final_result = tally["result"]
        assent_status = "PENDING" if final_result == "PASSED" else None

        cur.execute(
            """
            UPDATE motions
            SET status = 'CLOSED',
                final_result = ?,
                royal_assent_status = ?,
                royal_assented_by = NULL,
                royal_assented_at = NULL
            WHERE guild_id = ? AND motion_id = ? AND status = 'VOTING'
            """,
            (final_result, assent_status, guild.id, motion_id),
        )
        if cur.rowcount == 0:
            return None
        self.db.commit()

        cur.execute("SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?", (guild.id, motion_id))
        closed_motion = cur.fetchone()
        if not closed_motion:
            return None

        await update_rollcall_message(self.bot, guild, motion_id, clear_view=True)

        if closed_motion["message_channel_id"]:
            channel = guild.get_channel(int(closed_motion["message_channel_id"]))
            if isinstance(channel, discord.TextChannel):
                repeal_motion_summary = get_repeal_motion_summary(self.db, guild.id, closed_motion) if motion_kind(closed_motion) == "repeal" else None
                repeal_original_proposer = get_repeal_original_proposer(self.db, guild.id, closed_motion) if motion_kind(closed_motion) == "repeal" else None
                await channel.send(
                    embed=build_result_embed(
                        motion_id,
                        closed_motion,
                        tally,
                        repeal_motion_summary=repeal_motion_summary,
                        repeal_original_proposer=repeal_original_proposer,
                    )
                )

        if str(closed_motion["royal_assent_status"] or "") == "PENDING":
            await self.post_royal_assent_request(guild, closed_motion)

        return {"motion": closed_motion, "tally": tally}

    @app_commands.command(name="motion_create", description="Create a Parliament motion (draft).")
    @app_commands.guild_only()
    @app_commands.describe(kind="act/resolution/confidence/etc", title="Short title", text="Full text")
    async def motion_create(self, interaction: discord.Interaction, kind: str, title: str, text: str):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Server-only.", ephemeral=True)

        settings = get_settings(self.db, interaction.guild.id)
        has_motion_create_access = isinstance(interaction.user, discord.Member) and (
            has_parliament_role(interaction.user, settings)
            or has_associate_parliamentarian_role(interaction.user, settings)
        )
        if not has_motion_create_access:
            return await interaction.response.send_message(
                "❌ Only users with the Parliament or Associate Parliamentarian role can create drafts.",
                ephemeral=True,
            )

        cur = self.db.cursor()
        cur.execute(
            """
            INSERT INTO motions (guild_id, kind, title, text, created_by, created_at, status, opens_at, closes_at, public_votes, target_act_id)
            VALUES (?, ?, ?, ?, ?, ?, 'DRAFT', NULL, NULL, 1, NULL)
            """,
            (interaction.guild.id, kind, title, text, interaction.user.id, iso_now()),
        )
        self.db.commit()

        lastrowid = cur.lastrowid
        if lastrowid is None:
            return await interaction.response.send_message("❌ Failed to create motion draft.", ephemeral=True)
        motion_id = int(lastrowid)

        cur.execute("SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?", (interaction.guild.id, motion_id))
        motion = cur.fetchone()

        posted_preview = False
        if settings and settings.get("parliament_channel_id"):
            channel = interaction.guild.get_channel(int(settings["parliament_channel_id"]))
            if isinstance(channel, discord.TextChannel) and motion:
                draft_embed = build_motion_draft_embed(motion_id, motion, interaction.user)
                draft_msg = await channel.send(embed=draft_embed)
                cur.execute(
                    """
                    UPDATE motions
                    SET message_channel_id = ?, message_id = ?
                    WHERE guild_id = ? AND motion_id = ?
                    """,
                    (channel.id, draft_msg.id, interaction.guild.id, motion_id),
                )
                self.db.commit()
                posted_preview = True

        await interaction.response.send_message(
            (
                f"✅ Motion #{motion_id} created as **DRAFT**"
                + (" and posted in the Parliament channel." if posted_preview else ".")
                + f"\nUse `/motion_open {motion_id}` to start voting."
            ),
            ephemeral=True,
        )

    @app_commands.command(name="motion_repeal", description="Create a repeal motion for an enacted act.")
    @app_commands.guild_only()
    @app_commands.describe(act_id="The enacted act number to repeal", reason="Reason for repeal")
    async def motion_repeal(self, interaction: discord.Interaction, act_id: int, reason: str):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Server-only.", ephemeral=True)

        settings = get_settings(self.db, interaction.guild.id)
        has_motion_create_access = isinstance(interaction.user, discord.Member) and (
            has_parliament_role(interaction.user, settings)
            or has_associate_parliamentarian_role(interaction.user, settings)
        )
        if not has_motion_create_access:
            return await interaction.response.send_message(
                "❌ Only users with the Parliament or Associate Parliamentarian role can create repeal motions.",
                ephemeral=True,
            )

        act = await self.get_act(interaction.guild.id, act_id)
        if not act:
            return await interaction.response.send_message(f"❌ Act #{act_id} was not found.", ephemeral=True)
        if str(act["status"]) != "ENACTED":
            return await interaction.response.send_message(f"❌ Act #{act_id} is already repealed.", ephemeral=True)

        repeal_title = f"Repeal Act #{act_id} — {act['title']}"
        repeal_text = (
            f"This motion proposes to repeal **Act #{act_id}: {act['title']}**.\n\n"
            f"Reason:\n{reason}\n\n"
            f"Original Act Text:\n{str(act['text'])[:2400]}"
        )

        cur = self.db.cursor()
        cur.execute(
            """
            INSERT INTO motions (guild_id, kind, title, text, created_by, created_at, status, opens_at, closes_at, public_votes, target_act_id)
            VALUES (?, 'repeal', ?, ?, ?, ?, 'DRAFT', NULL, NULL, 1, ?)
            """,
            (interaction.guild.id, repeal_title, repeal_text, interaction.user.id, iso_now(), act_id),
        )
        self.db.commit()

        lastrowid = cur.lastrowid
        if lastrowid is None:
            return await interaction.response.send_message("❌ Failed to create repeal motion draft.", ephemeral=True)
        motion_id = int(lastrowid)

        cur.execute("SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?", (interaction.guild.id, motion_id))
        motion = cur.fetchone()

        posted_preview = False
        if settings and settings.get("parliament_channel_id"):
            channel = interaction.guild.get_channel(int(settings["parliament_channel_id"]))
            if isinstance(channel, discord.TextChannel) and motion:
                draft_embed = build_motion_draft_embed(motion_id, motion, interaction.user)
                draft_msg = await channel.send(embed=draft_embed)
                cur.execute(
                    """
                    UPDATE motions
                    SET message_channel_id = ?, message_id = ?
                    WHERE guild_id = ? AND motion_id = ?
                    """,
                    (channel.id, draft_msg.id, interaction.guild.id, motion_id),
                )
                self.db.commit()
                posted_preview = True

        await interaction.response.send_message(
            (
                f"✅ Repeal motion #{motion_id} created for Act #{act_id} as **DRAFT**"
                + (" and posted in the Parliament channel." if posted_preview else ".")
                + f"\nUse `/motion_open {motion_id}` to start voting."
            ),
            ephemeral=True,
        )

    @app_commands.command(name="motion_open", description="Open voting on a motion and post the roll-call.")
    @app_commands.guild_only()
    @app_commands.describe(motion_id="The motion number")
    async def motion_open(self, interaction: discord.Interaction, motion_id: int):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Server-only.", ephemeral=True)

        settings = get_settings(self.db, interaction.guild.id)
        if not is_admin(interaction, settings):
            return await interaction.response.send_message("❌ Admins only.", ephemeral=True)

        if not settings or not settings.get("parliament_channel_id"):
            return await interaction.response.send_message(
                "❌ Parliament channel not set. Run `/setup` and set `parliament_channel`.",
                ephemeral=True,
            )

        cur = self.db.cursor()
        cur.execute("SELECT status FROM motions WHERE guild_id = ? AND motion_id = ?", (interaction.guild.id, motion_id))
        row = cur.fetchone()
        if not row:
            return await interaction.response.send_message("❌ Motion not found.", ephemeral=True)
        if row["status"] != "DRAFT":
            return await interaction.response.send_message("❌ Motion is not in DRAFT state.", ephemeral=True)

        opens_at = iso_now()
        closes_at = (datetime.now(timezone.utc) + timedelta(hours=24)).replace(microsecond=0).isoformat()

        cur.execute(
            """
            UPDATE motions
            SET status = 'VOTING', opens_at = ?, closes_at = ?
            WHERE guild_id = ? AND motion_id = ?
            """,
            (opens_at, closes_at, interaction.guild.id, motion_id),
        )
        self.db.commit()

        channel = interaction.guild.get_channel(int(settings["parliament_channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Configured parliament channel is invalid.", ephemeral=True)

        cur.execute("SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?", (interaction.guild.id, motion_id))
        motion = cur.fetchone()

        empty_tally = {"yes": [], "no": [], "abstain": [], "result": "TIED"}
        repeal_motion_summary = get_repeal_motion_summary(self.db, interaction.guild.id, motion) if motion_kind(motion) == "repeal" else None
        repeal_original_proposer = get_repeal_original_proposer(self.db, interaction.guild.id, motion) if motion_kind(motion) == "repeal" else None
        embed = build_motion_rollcall_embed(
            motion_id,
            motion,
            empty_tally,
            interaction.guild,
            repeal_motion_summary=repeal_motion_summary,
            repeal_original_proposer=repeal_original_proposer,
        )

        msg = None
        if motion["message_channel_id"] and motion["message_id"] and int(motion["message_channel_id"]) == channel.id:
            try:
                existing = await channel.fetch_message(int(motion["message_id"]))
                await existing.edit(embed=embed, view=MotionVoteView(self.bot, motion_id, self.on_motion_vote_recorded))
                msg = existing
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                msg = None

        if msg is None:
            msg = await channel.send(embed=embed, view=MotionVoteView(self.bot, motion_id, self.on_motion_vote_recorded))

        cur.execute(
            """
            UPDATE motions
            SET message_channel_id = ?, message_id = ?
            WHERE guild_id = ? AND motion_id = ?
            """,
            (channel.id, msg.id, interaction.guild.id, motion_id),
        )
        self.db.commit()

        await update_rollcall_message(self.bot, interaction.guild, motion_id)

        await interaction.response.send_message(
            f"✅ Voting opened for motion #{motion_id}. It will close automatically in 24 hours.",
            ephemeral=True,
        )

    @app_commands.command(name="motion_vote", description="Vote on a Parliament motion (Parliament only).")
    @app_commands.guild_only()
    @app_commands.describe(motion_id="The motion number")
    async def motion_vote(self, interaction: discord.Interaction, motion_id: int):
        view = MotionVoteView(self.bot, motion_id, self.on_motion_vote_recorded)
        await interaction.response.send_message(f"Cast your vote on motion #{motion_id}:", view=view, ephemeral=True)

    @app_commands.command(
        name="motion_repost_vote",
        description="Admin: repost the voting panel for an active motion without clearing votes.",
    )
    @app_commands.guild_only()
    @app_commands.describe(motion_id="The motion number (optional if only one motion is currently in VOTING)")
    async def motion_repost_vote(self, interaction: discord.Interaction, motion_id: int | None = None):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Server-only.", ephemeral=True)

        settings = get_settings(self.db, interaction.guild.id)
        if not is_admin(interaction, settings):
            return await interaction.response.send_message("❌ Admins only.", ephemeral=True)

        if not settings or not settings.get("parliament_channel_id"):
            return await interaction.response.send_message(
                "❌ Parliament channel not set. Run `/setup` and set `parliament_channel`.",
                ephemeral=True,
            )

        channel = interaction.guild.get_channel(int(settings["parliament_channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Configured parliament channel is invalid.", ephemeral=True)

        cur = self.db.cursor()
        if motion_id is None:
            cur.execute(
                """
                SELECT *
                FROM motions
                WHERE guild_id = ? AND status = 'VOTING'
                ORDER BY motion_id ASC
                """,
                (interaction.guild.id,),
            )
            rows = cur.fetchall()
            if not rows:
                return await interaction.response.send_message("❌ No active VOTING motions found.", ephemeral=True)
            if len(rows) > 1:
                ids = ", ".join(str(r["motion_id"]) for r in rows)
                return await interaction.response.send_message(
                    f"❌ Multiple active motions found: {ids}. Please pass **motion_id**.",
                    ephemeral=True,
                )
            motion = rows[0]
            motion_id = int(motion["motion_id"])
        else:
            cur.execute(
                """
                SELECT *
                FROM motions
                WHERE guild_id = ? AND motion_id = ?
                """,
                (interaction.guild.id, motion_id),
            )
            motion = cur.fetchone()
            if not motion:
                return await interaction.response.send_message("❌ Motion not found.", ephemeral=True)
            if motion["status"] != "VOTING":
                return await interaction.response.send_message("❌ Motion is not currently open for voting.", ephemeral=True)

        old_channel_id = motion["message_channel_id"]
        old_message_id = motion["message_id"]

        tally = tally_motion(self.db, interaction.guild.id, motion_id)
        repeal_motion_summary = get_repeal_motion_summary(self.db, interaction.guild.id, motion) if motion_kind(motion) == "repeal" else None
        repeal_original_proposer = get_repeal_original_proposer(self.db, interaction.guild.id, motion) if motion_kind(motion) == "repeal" else None
        embed = build_motion_rollcall_embed(
            motion_id,
            motion,
            tally,
            interaction.guild,
            repeal_motion_summary=repeal_motion_summary,
            repeal_original_proposer=repeal_original_proposer,
        )

        sent = await channel.send(embed=embed, view=MotionVoteView(self.bot, motion_id, self.on_motion_vote_recorded))

        cur.execute(
            """
            UPDATE motions
            SET message_channel_id = ?, message_id = ?
            WHERE guild_id = ? AND motion_id = ?
            """,
            (channel.id, sent.id, interaction.guild.id, motion_id),
        )
        self.db.commit()

        if old_channel_id and old_message_id:
            old_channel = interaction.guild.get_channel(int(old_channel_id))
            if isinstance(old_channel, discord.TextChannel):
                try:
                    old_message = await old_channel.fetch_message(int(old_message_id))
                    await old_message.edit(view=None)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass

        await interaction.response.send_message(
            f"✅ Reposted voting panel for motion #{motion_id}: {sent.jump_url}\n"
            "Existing votes were preserved.",
            ephemeral=True,
        )

    @app_commands.command(name="motion_close", description="Close voting on a motion and publish final result.")
    @app_commands.guild_only()
    @app_commands.describe(motion_id="The motion number")
    async def motion_close(self, interaction: discord.Interaction, motion_id: int):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Server-only.", ephemeral=True)

        settings = get_settings(self.db, interaction.guild.id)
        if not is_admin(interaction, settings):
            return await interaction.response.send_message("❌ Admins only.", ephemeral=True)

        cur = self.db.cursor()
        cur.execute("SELECT status FROM motions WHERE guild_id = ? AND motion_id = ?", (interaction.guild.id, motion_id))
        row = cur.fetchone()
        if not row:
            return await interaction.response.send_message("❌ Motion not found.", ephemeral=True)
        if row["status"] != "VOTING":
            return await interaction.response.send_message("❌ Motion is not currently open for voting.", ephemeral=True)

        result = await self.close_motion_and_publish_result(interaction.guild, motion_id)
        if not result:
            return await interaction.response.send_message("❌ Motion could not be closed.", ephemeral=True)

        tally = result["tally"]
        await interaction.response.send_message(
            f"✅ Motion #{motion_id} closed. Result: **{tally['result']}** "
            f"(Yes {len(tally['yes'])} / No {len(tally['no'])} / Abstain {len(tally['abstain'])}).",
            ephemeral=True,
        )

    @app_commands.command(name="motion_results", description="Show current or final motion results.")
    @app_commands.guild_only()
    @app_commands.describe(motion_id="The motion number")
    async def motion_results(self, interaction: discord.Interaction, motion_id: int):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Server-only.", ephemeral=True)

        cur = self.db.cursor()
        cur.execute("SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?", (interaction.guild.id, motion_id))
        motion = cur.fetchone()
        if not motion:
            return await interaction.response.send_message("❌ Motion not found.", ephemeral=True)

        tally = tally_motion(self.db, interaction.guild.id, motion_id)

        repeal_motion_summary = get_repeal_motion_summary(self.db, interaction.guild.id, motion) if motion_kind(motion) == "repeal" else None
        repeal_original_proposer = get_repeal_original_proposer(self.db, interaction.guild.id, motion) if motion_kind(motion) == "repeal" else None
        embed = build_motion_rollcall_embed(
            motion_id,
            motion,
            tally,
            interaction.guild,
            repeal_motion_summary=repeal_motion_summary,
            repeal_original_proposer=repeal_original_proposer,
        )

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    await bot.add_cog(Motions(bot))
