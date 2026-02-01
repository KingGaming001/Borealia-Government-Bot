# commands/open_election.py
# ------------------------------------------------------------
# /open_election
#
# Schedules an election for a position:
# - Opens nominations immediately (status = SCHEDULED)
# - Voting begins automatically at the specified date/time (start_at)
#
# This command:
# - Requires bot configured via /setup
# - Requires admin (Discord admin OR configured admin role)
# - Clears votes for this position (fresh cycle)
# - Optionally clears nominees too (for by-elections / resets)
# - Posts/updates a nominees list message in the nominees channel
#
# Note: Voting UI is created by the scheduler in main.py when start_at arrives.
# ------------------------------------------------------------

from __future__ import annotations

import discord
from discord import app_commands, Interaction
from discord.ext import commands

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from config_store import get_settings, is_admin

# If tz database isn't available on Windows, tzdata package fixes it.
# Fallback to UTC so the bot still runs.
try:
    LONDON_TZ = ZoneInfo("Europe/London")
except ZoneInfoNotFoundError:
    LONDON_TZ = timezone.utc


def parse_start_time_to_utc(start_time_str: str) -> tuple[datetime, datetime]:
    """
    Accepts:
    - "YYYY-MM-DD HH:MM"
    - "YYYY-MM-DDTHH:MM"
    Returns (local_dt, utc_dt)
    """
    s = start_time_str.strip().replace("T", " ")

    try:
        naive = datetime.strptime(s, "%Y-%m-%d %H:%M")
    except ValueError:
        raise ValueError("Invalid date/time format. Use `YYYY-MM-DD HH:MM` (e.g. `2026-02-01 19:30`).")

    local_dt = naive.replace(tzinfo=LONDON_TZ)
    utc_dt = local_dt.astimezone(timezone.utc)
    return local_dt, utc_dt


def format_dt_for_embed(local_dt: datetime) -> str:
    return local_dt.strftime("%d %b %Y, %H:%M") + " (Europe/London)"


class OpenElectionCommand(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="open_election",
        description="Schedule an election (nominations open now, voting starts at the chosen time)"
    )
    @app_commands.describe(
        position="The position to open the election for (e.g., Prime Minister)",
        start_time="When voting begins (Europe/London). Format: YYYY-MM-DD HH:MM",
        clear_nominees="If true, removes all nominees as well as votes"
    )
    async def open_election(
        self,
        interaction: Interaction,
        position: str,
        start_time: str,
        clear_nominees: bool = False
    ):
        if not interaction.guild:
            await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
            return

        guild_id = interaction.guild.id

        settings = get_settings(self.bot.db, guild_id)
        if not settings:
            await interaction.response.send_message(
                "❌ The Borealia Government bot is not configured. An admin must run **/setup** first.",
                ephemeral=True
            )
            return

        if not is_admin(interaction, settings):
            await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
            return

        nominees_channel_id = settings.get("nominees_channel_id")
        if not nominees_channel_id:
            await interaction.response.send_message(
                "❌ Nominees channel is not configured. Run **/setup** and set the nominees channel.",
                ephemeral=True
            )
            return

        nominees_channel = interaction.guild.get_channel(int(nominees_channel_id))
        if not nominees_channel or not isinstance(nominees_channel, discord.TextChannel):
            await interaction.response.send_message("❌ The configured nominees channel could not be found.", ephemeral=True)
            return

        # Parse start time
        try:
            local_dt, utc_dt = parse_start_time_to_utc(start_time)
        except ValueError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return

        now_utc = datetime.now(timezone.utc)

        # Upsert election as SCHEDULED
        cur = self.bot.db.cursor()
        cur.execute(
            """
            INSERT INTO elections (
                guild_id, position, status, start_at,
                nominee_message_id, vote_message_id,
                created_by, created_at
            )
            VALUES (?, ?, 'SCHEDULED', ?, NULL, NULL, ?, ?)
            ON CONFLICT(guild_id, position) DO UPDATE SET
                status = 'SCHEDULED',
                start_at = excluded.start_at,
                vote_message_id = NULL
            """,
            (guild_id, position, utc_dt.isoformat(), interaction.user.id, now_utc.isoformat())
        )

        # Clear votes for fresh cycle
        cur.execute("DELETE FROM votes WHERE guild_id = ? AND position = ?", (guild_id, position))

        # Optionally clear nominees
        if clear_nominees:
            cur.execute("DELETE FROM nominations WHERE guild_id = ? AND position = ?", (guild_id, position))

        self.bot.db.commit()

        # Fetch nominees for embed
        cur.execute(
            "SELECT user_id, display_name FROM nominations WHERE guild_id = ? AND position = ? ORDER BY display_name ASC",
            (guild_id, position)
        )
        nominees = cur.fetchall()

        embed = discord.Embed(
            title=f"🗳️ Nominations Open — {position}",
            description=f"Nominations are now open.\n**Voting begins:** {format_dt_for_embed(local_dt)}",
            color=discord.Color.gold()
        )

        if nominees:
            for n in nominees:
                embed.add_field(name=str(n["display_name"]), value=f"<@{int(n['user_id'])}>", inline=False)
        else:
            embed.add_field(
                name="No nominees yet",
                value="Use **/nominate** to nominate yourself.",
                inline=False
            )

        # Post/update nominees message
        cur.execute(
            "SELECT nominee_message_id FROM elections WHERE guild_id = ? AND position = ?",
            (guild_id, position)
        )
        row = cur.fetchone()
        nominee_message_id = row["nominee_message_id"] if row else None

        msg_obj = None
        if nominee_message_id:
            try:
                msg_obj = await nominees_channel.fetch_message(int(nominee_message_id))
            except Exception:
                msg_obj = None

        if msg_obj:
            await msg_obj.edit(embed=embed)
        else:
            sent = await nominees_channel.send(embed=embed)
            cur.execute(
                "UPDATE elections SET nominee_message_id = ? WHERE guild_id = ? AND position = ?",
                (sent.id, guild_id, position)
            )
            self.bot.db.commit()

        # Optional log
        log_channel_id = settings.get("log_channel_id")
        if log_channel_id:
            log_channel = interaction.guild.get_channel(int(log_channel_id))
            if log_channel and isinstance(log_channel, discord.TextChannel):
                log_embed = discord.Embed(
                    title="🗓️ Election Scheduled",
                    description=(
                        f"**Position:** {position}\n"
                        f"**Voting begins:** {format_dt_for_embed(local_dt)}\n"
                        f"**Scheduled by:** {interaction.user.mention}"
                    ),
                    color=discord.Color.blurple()
                )
                if clear_nominees:
                    log_embed.add_field(
                        name="Nominees Cleared",
                        value="All previous nominees were cleared.",
                        inline=False
                    )
                await log_channel.send(embed=log_embed)

        await interaction.response.send_message(
            f"✅ Election scheduled for **{position}**.\n🕒 Voting begins: **{format_dt_for_embed(local_dt)}**.",
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(OpenElectionCommand(bot))
