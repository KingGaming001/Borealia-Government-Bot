# commands/motions.py
# ------------------------------------------------------------
# Parliament Motions (Acts, Resolutions, etc.)
#
# Features:
# - Parliament-only voting (role required)
# - Public roll-call results (who votes Yes/No/Abstain)
# - Locked votes (cannot change once cast)
# - Simple majority: Yes > No => PASSED; No > Yes => FAILED; tie => TIED
#
# Commands:
# /motion_create
# /motion_open
# /motion_vote
# /motion_close
# /motion_results
# ------------------------------------------------------------

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from config_store import get_settings, is_admin, has_parliament_role

def iso_now() -> str:
    """UTC timestamp string for the database"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def format_voter_list(guild: discord.Guild, user_ids: list[int], limit: int=25) -> str:
    """
    Turns a list of user IDs into mentions, capped for readability.
    """
    if not user_ids:
        return "-"
    
    shown = []
    for uid in user_ids[:limit]:
        member = guild.get_member(uid)
        shown.append(member.mention if member else f"<@{uid}>")

    extra = len(user_ids) - len(shown)
    if extra > 0:
        shown.append(f"+ {extra} more")

    return", ".join(shown)


def tally_motion(cur: discord.abc.Messageable, db, guild_id: int, motion_id: int) -> dict:
    """
    Read all votes and compute:
    - yes/no/abstain voter lists
    - result based on simple majority (yes vs no)
    """
    c = db.cursor()
    c.execute(
        """
        SELECT choice, voter_id
        FROM motion_votes
        WHERE guild_id = ? AND motion_id = ?
        ORDER BY cast_at ASC
        """,
        (guild_id, motion_id)
    )
    rows = c.fetchall()

    yes = [r["voter_id"] for r in rows if r["choice"] == "yes"]
    no = [r["voter_id"] for r in rows if r["choice"] == "no"]
    abstain = [r["voter_id"] for r in rows if r["choice"] == "abstain"]

    if len(yes) > len(no):
        result = "PASSED"
    elif len(no) > len(yes):
        result = "FAILED"
    else:
        result = "TIED"

        return {"yes": yes, "no": no, "abstain": abstain, "result": result}

async def update_rollcall_message(bot: commands.Bot, guild: discord.Guild, motion_id: int) -> None:
    """
    Rebuild the public roll-call message embed and edit it in-place.
    """
    db = bot.db
    cur = db.cursor()

    cur.execute(
        "SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?",
        (guild.id, motion_id)
    )
    motion = cur.fetchone()
    if not motion:
        return
    
    # If no message saved, nothing to update
    if not motion["message_channel_id"] or not motion["motion_id"]:
        return
    
    channel = guild.get_channel(int(motion["message_channel_id"]))
    if not isinstance(channel, discord.TextChannel):
        return
    
    try:
        msg = await channel.fetch_message(int(motion["message_id"]))
    except discord.NotFound:
        return
    
    t = tally_motion(channel, db, guild.id, motion_id)

    embed = discord.Embed(
        title=f"Motion #{motion_id}: {motion['title']}",
        description=motion["text"][:3800]
    )
    embed.add_field(name="Kind", value=motion["kind"], inline=True)
    embed.add_field(name="Status", value=motion["status"], inline=True)

    # Only show final result once closed
    embed.add_field(
        name="Result",
        value=t["result"] if motion["status"] == "CLOSED" else "-",
        inline=True
    )

    embed.add_field(name=f"✅ Yes ({len(t['yes'])})", value=format_voter_list(guild, t["yes"]), inline=False)
    embed.add_field(name=f"❌ No ({len(t['no'])})", value=format_voter_list(guild, t["no"]), inline=False)
    embed.add_field(name=f"⚪ Abstain ({len(t['abstain'])})", value=format_voter_list(guild, t["abstain"]), inline=False)

    if motion["closes_at"]:
        embed.set_footer(text=f"Closes at: {motion['closes_at']}")

    await msg.edit(embed=embed)

class MotionVoteView(discord.ui.View):
    """
    Buttons shown to Parliament members when they run /motion_vote.
    Votes are locked: if you already voted once, you cannot vote again.
    """

    def __init__(self, bot: commands.Bot, motion_id: int):
        super().__init__(timeout=180)
        self.bot = bot
        self.motion_id = motion_id

    async def cast(self, interaction: discord.Interaction, choice: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Server-only.", ephemeral=True)
            
        settings = get_settings(self.bot.db, interaction.guild.id)
        if not settings:
            return await interaction.response.send_message("❌ Server not configured. Run /setup first.", ephemeral=True)
        
        # Parliament-only voting rule
        if not has_parliament_role(interaction.user, settings):
            return await interaction.response.send_message("❌ Only Parliament may vote on motions.", ephemeral=True)
        
        cur = self.bot.db.cursor()

        # Motion must be open for voting
        cur.execute(
            "SELECT status from motions WHERE guild_id = ? AND motion_id = ?",
            (interaction.guild.id, self.motion_id)
        )
        row = cur.fetchone()
        if not row or row["status"] != "VOTING":
            return await interaction.response.send_message("❌ Voting is not open for this motion.", ephemeral=True)
        
        # Locked voting: insert once, fail if already exists
        try:
            cur.execute(
                """
                INSERT INTO motion_votes (guild_id, motion_id, voter_id, choice)
                VALUES (?, ?, ?, ?)
                """,
                (interaction.guild.id, self.motion_id, interaction.user.id, choice)
            )
            self.bot.db.commit()
        except Exception:
            return await interaction.response.send_message("🔒 Your vote is already recorded and locked.", ephemeral=True)
        
        # Update the public roll-call message
        await update_rollcall_message(self.bot, interaction.guild, self.motion_id)

        await interaction.response.send_message("✅ Vote recorded.", ephemeral=True)

    @discord.ui.button(label="Yes", style=discord.ButtonStyle.success)
    async def yes(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cast(interaction, "yes")

    @discord.ui.button(label="No", style=discord.ButtonStyle.danger)
    async def no(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cast(interaction, "no")

    @discord.ui.button(label="Abstain", style=discord.ButtonStyle.secondary)
    async def abstain(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self.cast(interaction, "abstain")

class Motions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
    
    # ----------------------------
    # /motion_create
    # ----------------------------
    @app_commands.command(name="motion_create", description="Create a Parliament motion (draft).")
    @app_commands.guild_only()
    @app_commands.describe(kind="act/resolution/confidence/etc", title="Short title", text="Full text")
    async def motion_create(self, interaction: discord.Interaction, kind: str, title: str, text: str):
        settings = get_settings(self.bot.db, interaction.guild.id)
        if not is_admin(interaction, settings):
            return await interaction.response.send_message("❌ Admins only.", ephemeral=True)

        cur = self.bot.db.cursor()
        cur.execute(
            """
            INSERT INTO motions (guild_id, kind, title, text, status, opens_at, closes_at, created_by, public_votes)
            VALUES (?, ?, ?, ?, 'DRAFT', NULL, NULL, ?, 1)
            """,
            (interaction.guild.id, kind, title, text, interaction.user.id)
        )
        self.bot.db.commit()

        motion_id = cur.lastrowid
        await interaction.response.send_message(
            f"✅ Motion #{motion_id} created as **DRAFT**.\nUse `/motion_open {motion_id}` to start voting.",
            ephemeral=True
        )

    # ----------------------------
    # /motion_open
    # ----------------------------
    @app_commands.command(name="motion_open", description="Open voting on a motion and post the roll-call.")
    @app_commands.guild_only()
    @app_commands.describe(motion_id="The motion number", duration_minutes="How long voting stays open")
    async def motion_open(self, interaction: discord.Interaction, motion_id: int, duration_minutes: int = 60):
        settings = get_settings(self.bot.db, interaction.guild.id)
        if not is_admin(interaction, settings):
            return await interaction.response.send_message("❌ Admins only.", ephemeral=True)

        if not settings or not settings.get("parliament_channel_id"):
            return await interaction.response.send_message(
                "❌ Parliament channel not set. Run `/setup` and set `parliament_channel`.",
                ephemeral=True
            )

        cur = self.bot.db.cursor()

        # Must exist and be draft
        cur.execute(
            "SELECT status FROM motions WHERE guild_id = ? AND motion_id = ?",
            (interaction.guild.id, motion_id)
        )
        row = cur.fetchone()
        if not row:
            return await interaction.response.send_message("❌ Motion not found.", ephemeral=True)
        if row["status"] != "DRAFT":
            return await interaction.response.send_message("❌ Motion is not in DRAFT state.", ephemeral=True)

        opens_at = iso_now()
        closes_at = (datetime.now(timezone.utc) + timedelta(minutes=duration_minutes)).replace(microsecond=0).isoformat()

        cur.execute(
            """
            UPDATE motions
            SET status = 'VOTING', opens_at = ?, closes_at = ?
            WHERE guild_id = ? AND motion_id = ?
            """,
            (opens_at, closes_at, interaction.guild.id, motion_id)
        )
        self.bot.db.commit()

        # Post the public roll-call embed
        channel = interaction.guild.get_channel(int(settings["parliament_channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Configured parliament channel is invalid.", ephemeral=True)

        cur.execute(
            "SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?",
            (interaction.guild.id, motion_id)
        )
        motion = cur.fetchone()

        embed = discord.Embed(
            title=f"Motion #{motion_id}: {motion['title']}",
            description=motion["text"][:3800]
        )
        embed.add_field(name="Kind", value=motion["kind"], inline=True)
        embed.add_field(name="Status", value="VOTING", inline=True)
        embed.set_footer(text=f"Closes at: {motion['closes_at']}")

        msg = await channel.send(embed=embed)

        # Save message reference so we can update it later
        cur.execute(
            """
            UPDATE motions
            SET message_channel_id = ?, message_id = ?
            WHERE guild_id = ? AND motion_id = ?
            """,
            (channel.id, msg.id, interaction.guild.id, motion_id)
        )
        self.bot.db.commit()

        # Fill in initial tallies
        await update_rollcall_message(self.bot, interaction.guild, motion_id)

        await interaction.response.send_message(f"✅ Voting opened for motion #{motion_id}.", ephemeral=True)

    # ----------------------------
    # /motion_vote
    # ----------------------------
    @app_commands.command(name="motion_vote", description="Vote on a Parliament motion (Parliament only).")
    @app_commands.guild_only()
    async def motion_vote(self, interaction: discord.Interaction, motion_id: int):
        # Voting UI is ephemeral so only the user sees the buttons,
        # but the roll-call message is public.
        view = MotionVoteView(self.bot, motion_id)
        await interaction.response.send_message(
            f"Cast your vote on motion #{motion_id}:",
            view=view,
            ephemeral=True
        )

    # ----------------------------
    # /motion_close
    # ----------------------------
    @app_commands.command(name="motion_close", description="Close voting on a motion and publish final result.")
    @app_commands.guild_only()
    async def motion_close(self, interaction: discord.Interaction, motion_id: int):
        settings = get_settings(self.bot.db, interaction.guild.id)
        if not is_admin(interaction, settings):
            return await interaction.response.send_message("❌ Admins only.", ephemeral=True)

        cur = self.bot.db.cursor()
        cur.execute(
            "SELECT status FROM motions WHERE guild_id = ? AND motion_id = ?",
            (interaction.guild.id, motion_id)
        )
        row = cur.fetchone()
        if not row:
            return await interaction.response.send_message("❌ Motion not found.", ephemeral=True)
        if row["status"] != "VOTING":
            return await interaction.response.send_message("❌ Motion is not currently open for voting.", ephemeral=True)

        cur.execute(
            """
            UPDATE motions
            SET status = 'CLOSED'
            WHERE guild_id = ? AND motion_id = ?
            """,
            (interaction.guild.id, motion_id)
        )
        self.bot.db.commit()

        # Update roll-call message to show final result
        await update_rollcall_message(self.bot, interaction.guild, motion_id)

        # Send a small admin confirmation
        t = tally_motion(None, self.bot.db, interaction.guild.id, motion_id)
        await interaction.response.send_message(
            f"✅ Motion #{motion_id} closed. Result: **{t['result']}** "
            f"(Yes {len(t['yes'])} / No {len(t['no'])} / Abstain {len(t['abstain'])}).",
            ephemeral=True
        )

    # ----------------------------
    # /motion_results
    # ----------------------------
    @app_commands.command(name="motion_results", description="Show current or final motion results.")
    @app_commands.guild_only()
    async def motion_results(self, interaction: discord.Interaction, motion_id: int):
        cur = self.bot.db.cursor()
        cur.execute(
            "SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?",
            (interaction.guild.id, motion_id)
        )
        motion = cur.fetchone()
        if not motion:
            return await interaction.response.send_message("❌ Motion not found.", ephemeral=True)

        t = tally_motion(None, self.bot.db, interaction.guild.id, motion_id)

        embed = discord.Embed(
            title=f"Motion #{motion_id}: {motion['title']}",
            description=motion["text"][:3800]
        )
        embed.add_field(name="Kind", value=motion["kind"], inline=True)
        embed.add_field(name="Status", value=motion["status"], inline=True)
        embed.add_field(name="Result", value=t["result"] if motion["status"] == "CLOSED" else "—", inline=True)

        embed.add_field(name=f"✅ Yes ({len(t['yes'])})", value=format_voter_list(interaction.guild, t["yes"]), inline=False)
        embed.add_field(name=f"❌ No ({len(t['no'])})", value=format_voter_list(interaction.guild, t["no"]), inline=False)
        embed.add_field(name=f"⚪ Abstain ({len(t['abstain'])})", value=format_voter_list(interaction.guild, t["abstain"]), inline=False)

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    """Called by main.py's extension loader."""
    await bot.add_cog(Motions(bot))