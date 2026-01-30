# commands/nominate.py
# ------------------------------------------------------------
# /nominate
#
# New behaviour:
# - Command is ONLY usable when there is at least one election in
#   status = 'SCHEDULED' and start_at is still in the future.
# - Users choose the POSITION from a dropdown of available elections.
# - This command does NOT create voting UI.
# - It ONLY posts/updates the nominees list message in the nominees channel.
# ------------------------------------------------------------

from __future__ import annotations

import discord
from discord import app_commands, Interaction
from discord.ext import commands

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from config_store import get_settings

LONDON_TZ = ZoneInfo("Europe/London")


def utc_iso_to_london_str(iso_utc: str) -> str:
    """
    Convert a stored UTC ISO datetime string to a friendly Europe/London string.
    """
    dt = datetime.fromisoformat(iso_utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(LONDON_TZ)
    return local.strftime("%d %b %Y, %H:%M") + " (Europe/London)"


def build_nominees_embed(position: str, start_at_iso_utc: str | None, nominees: list[dict]) -> discord.Embed:
    """
    Build the nominees embed for the nominees channel.
    """
    desc = "Nominations are open."
    if start_at_iso_utc:
        desc += f"\n**Voting begins:** {utc_iso_to_london_str(start_at_iso_utc)}"

    embed = discord.Embed(
        title=f"🗳️ Nominations — {position}",
        description=desc,
        color=discord.Color.gold()
    )

    if nominees:
        for n in nominees:
            embed.add_field(
                name=n["display_name"],
                value=f"<@{n['user_id']}>",
                inline=False
            )
    else:
        embed.add_field(
            name="No nominees yet",
            value="Be the first to nominate yourself using **/nominate**.",
            inline=False
        )

    return embed


class PositionSelect(discord.ui.Select):
    """
    Dropdown that lists all currently available SCHEDULED elections.
    """
    def __init__(
        self,
        bot: commands.Bot,
        guild_id: int,
        user_id: int,
        ballot_name: str,
        elections: list[dict],
        nominees_channel: discord.TextChannel
    ):
        self.bot = bot
        self.guild_id = guild_id
        self.user_id = user_id
        self.ballot_name = ballot_name
        self.elections = elections
        self.nominees_channel = nominees_channel

        options = []
        for e in elections:
            # Value = position (unique per guild in your elections table)
            label = e["position"]
            # Helpful hint: show when voting begins
            hint = utc_iso_to_london_str(e["start_at"]) if e.get("start_at") else "Start time not set"
            options.append(discord.SelectOption(
                label=label[:100],
                description=f"Voting begins: {hint}"[:100],
                value=label
            ))

        super().__init__(
            placeholder="Choose the position you want to nominate for…",
            min_values=1,
            max_values=1,
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        # Ensure only the original user can use the dropdown
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This dropdown isn’t for you.", ephemeral=True)
            return

        position = self.values[0]

        # Re-check election state/time at the moment of nomination
        now_utc = datetime.now(timezone.utc)
        cur = self.bot.db.cursor()
        cur.execute(
            "SELECT status, start_at, nominee_message_id FROM elections WHERE guild_id = ? AND position = ?",
            (self.guild_id, position)
        )
        election = cur.fetchone()

        if not election:
            await interaction.response.send_message("❌ That election no longer exists.", ephemeral=True)
            return

        if election["status"] != "SCHEDULED":
            await interaction.response.send_message("❌ Nominations are closed for this election.", ephemeral=True)
            return

        # Ensure start_at is still in the future (nominations only before voting begins)
        try:
            start_at = datetime.fromisoformat(election["start_at"])
            if start_at.tzinfo is None:
                start_at = start_at.replace(tzinfo=timezone.utc)
            start_at_utc = start_at.astimezone(timezone.utc)
        except Exception:
            await interaction.response.send_message("❌ Election start time is invalid. Ask an admin to reschedule.", ephemeral=True)
            return

        if start_at_utc <= now_utc:
            await interaction.response.send_message(
                "❌ Voting has already started (or is starting now). Nominations are closed.",
                ephemeral=True
            )
            return

        # Insert nomination (one per user per position)
        cur.execute(
            """
            INSERT INTO nominations (guild_id, position, user_id, display_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, position, user_id)
            DO UPDATE SET display_name = excluded.display_name
            """,
            (self.guild_id, position, self.user_id, self.ballot_name)
        )
        self.bot.db.commit()

        # Fetch updated nominees list
        cur.execute(
            """
            SELECT user_id, display_name
            FROM nominations
            WHERE guild_id = ? AND position = ?
            ORDER BY display_name ASC
            """,
            (self.guild_id, position)
        )
        nominees_rows = cur.fetchall()
        nominees = [{"user_id": int(r["user_id"]), "display_name": str(r["display_name"])} for r in nominees_rows]

        # Update nominees message in nominees channel
        start_at_iso = election["start_at"]
        embed = build_nominees_embed(position, start_at_iso, nominees)

        nominee_message_id = election["nominee_message_id"]
        msg_obj = None
        if nominee_message_id:
            try:
                msg_obj = await self.nominees_channel.fetch_message(int(nominee_message_id))
            except Exception:
                msg_obj = None

        if msg_obj:
            await msg_obj.edit(embed=embed)
        else:
            sent = await self.nominees_channel.send(embed=embed)
            cur.execute(
                "UPDATE elections SET nominee_message_id = ? WHERE guild_id = ? AND position = ?",
                (sent.id, self.guild_id, position)
            )
            self.bot.db.commit()

        # Acknowledge the dropdown interaction by editing the ephemeral message
        await interaction.response.edit_message(
            content=f"✅ You are nominated for **{position}** as **{self.ballot_name}**.\n"
                    f"Nominees list updated in {self.nominees_channel.mention}.",
            view=None
        )


class NominateView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        guild_id: int,
        user_id: int,
        ballot_name: str,
        elections: list[dict],
        nominees_channel: discord.TextChannel
    ):
        super().__init__(timeout=120)
        self.add_item(PositionSelect(bot, guild_id, user_id, ballot_name, elections, nominees_channel))


class NominateCommand(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="nominate", description="Nominate yourself for an active (scheduled) election")
    @app_commands.describe(
        name="How you want your name to appear on the ballot"
    )
    async def nominate(self, interaction: Interaction, name: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
            return

        guild_id = interaction.guild.id

        # Ensure bot is configured and nominees channel exists
        settings = get_settings(self.bot.db, guild_id)
        if not settings:
            await interaction.response.send_message("❌ Bot not configured. Ask an admin to run **/setup**.", ephemeral=True)
            return

        nominees_channel_id = settings.get("nominees_channel_id")
        if not nominees_channel_id:
            await interaction.response.send_message(
                "❌ Nominees channel not configured. Ask an admin to run **/setup**.",
                ephemeral=True
            )
            return

        nominees_channel = interaction.guild.get_channel(int(nominees_channel_id))
        if not nominees_channel or not isinstance(nominees_channel, discord.TextChannel):
            await interaction.response.send_message("❌ Configured nominees channel not found.", ephemeral=True)
            return

        # Find elections currently accepting nominations:
        # - status = SCHEDULED
        # - start_at > now (so voting hasn't begun yet)
        now_utc = datetime.now(timezone.utc)
        cur = self.bot.db.cursor()
        cur.execute(
            """
            SELECT position, start_at
            FROM elections
            WHERE guild_id = ?
              AND status = 'SCHEDULED'
            """,
            (guild_id,)
        )
        rows = cur.fetchall()

        available = []
        for r in rows:
            try:
                start_at = datetime.fromisoformat(str(r["start_at"]))
                if start_at.tzinfo is None:
                    start_at = start_at.replace(tzinfo=timezone.utc)
                start_at_utc = start_at.astimezone(timezone.utc)
            except Exception:
                continue

            if start_at_utc > now_utc:
                available.append({
                    "position": str(r["position"]),
                    "start_at": str(r["start_at"]),
                })

        if not available:
            await interaction.response.send_message(
                "❌ There are no elections currently open for nominations.",
                ephemeral=True
            )
            return

        # Show dropdown to pick the position
        view = NominateView(
            bot=self.bot,
            guild_id=guild_id,
            user_id=interaction.user.id,
            ballot_name=name,
            elections=available,
            nominees_channel=nominees_channel
        )

        embed = discord.Embed(
            title="📝 Nominate Yourself",
            description="Choose which position you want to nominate for from the dropdown below.",
            color=discord.Color.blurple()
        )
        embed.add_field(name="Ballot Name", value=name, inline=False)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(NominateCommand(bot))
