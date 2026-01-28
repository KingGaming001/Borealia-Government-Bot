# commands/close_election.py
# ------------------------------------------------------------
# /close_election
# Closes an election for a given position and privately sends
# the results to the admin who ran the command (via DM).
#
# What it does:
# 1) Checks bot is configured (/setup)
# 2) Checks the user has admin permission (Discord admin OR admin_role)
# 3) Loads nominees + votes from the database
# 4) Computes totals and determines winner
# 5) Marks the election as CLOSED (is_closed = 1)
# 6) DMs the results to the command user (private)
# 7) Optionally logs "election closed" to a log channel WITHOUT totals
#
# Privacy:
# - Results are sent via DM only
# - No live vote counts are posted publicly
# ------------------------------------------------------------

import discord
from discord import app_commands
from discord.ext import commands
from collections import Counter

from config_store import get_settings, is_admin


class CloseElectionCommand(commands.Cog):
    def __init__(self, bot: commands.Bot):
        # Store bot reference so we can access bot.db
        self.bot = bot

    @app_commands.command(
        name="close_election",
        description="Close an election and DM the results to you privately."
    )
    @app_commands.describe(
        position="The position to close the election for (e.g., Prime Minister)"
    )
    async def close_election(self, interaction: discord.Interaction, position: str):
        # -----------------------------
        # Must be used in a server
        # -----------------------------
        if not interaction.guild:
            await interaction.response.send_message("Use this command in a server.", ephemeral=True)
            return

        guild_id = interaction.guild.id

        # -----------------------------
        # Load config and check permissions
        # -----------------------------
        settings = get_settings(self.bot.db, guild_id)
        if not settings:
            await interaction.response.send_message("⚠️ Bot not configured. Run `/setup` first.", ephemeral=True)
            return

        if not is_admin(interaction, settings):
            await interaction.response.send_message("🚫 You don’t have permission to close elections.", ephemeral=True)
            return

        cur = self.bot.db.cursor()

        # -----------------------------
        # Load nominees
        # -----------------------------
        cur.execute(
            """
            SELECT user_id, display_name
            FROM nominations
            WHERE guild_id=? AND position=?
            """,
            (guild_id, position),
        )
        nominees = cur.fetchall()

        if not nominees:
            await interaction.response.send_message("⚠️ No nominees found for that position.", ephemeral=True)
            return

        # Map candidate_id -> display_name for nice output
        name_by_id = {int(n["user_id"]): n["display_name"] for n in nominees}

        # -----------------------------
        # Load votes
        # -----------------------------
        cur.execute(
            """
            SELECT candidate_id
            FROM votes
            WHERE guild_id=? AND position=?
            """,
            (guild_id, position),
        )
        votes = [int(r["candidate_id"]) for r in cur.fetchall()]

        if not votes:
            await interaction.response.send_message("⚠️ No votes were cast for this election.", ephemeral=True)
            return

        # -----------------------------
        # Tally votes
        # -----------------------------
        counts = Counter(votes)

        # Determine winner (highest vote count)
        winner_id, top_votes = counts.most_common(1)[0]
        winner_name = name_by_id.get(winner_id, f"Unknown ({winner_id})")

        # -----------------------------
        # Mark election as CLOSED (is_closed = 1)
        # Ensures no more nominations/votes after this point.
        # -----------------------------
        cur.execute(
            """
            INSERT INTO elections (guild_id, position, is_closed)
            VALUES (?, ?, 1)
            ON CONFLICT(guild_id, position) DO UPDATE SET is_closed=1
            """,
            (guild_id, position),
        )
        self.bot.db.commit()

        # -----------------------------
        # Build the DM content (private results)
        # -----------------------------
        lines = []
        lines.append(f"🗳️ **Election Results — {position}**")
        lines.append("")

        # Sort nominees alphabetically for a tidy report
        for candidate_id, candidate_name in sorted(name_by_id.items(), key=lambda x: x[1].lower()):
            c = counts.get(candidate_id, 0)
            lines.append(f"• {candidate_name} (<@{candidate_id}>) — **{c}**")

        lines.append("")
        lines.append(f"🏆 **Winner:** {winner_name} (<@{winner_id}>) with **{top_votes}**")

        result_message = "\n".join(lines)

        # -----------------------------
        # DM the results to the admin who closed the election
        # -----------------------------
        try:
            await interaction.user.send(result_message)
        except discord.Forbidden:
            await interaction.response.send_message(
                "⚠️ I couldn't DM you the results (your DMs may be disabled).",
                ephemeral=True
            )
            return

        # -----------------------------
        # Optional: log that the election was closed (no totals)
        # -----------------------------
        log_channel_id = settings.get("log_channel_id")
        if log_channel_id:
            log_channel = interaction.guild.get_channel(int(log_channel_id))
            if log_channel:
                embed = discord.Embed(
                    title="✅ Election Closed",
                    color=discord.Color.gold()
                )
                embed.add_field(name="Position", value=position, inline=False)
                embed.add_field(name="Closed By", value=interaction.user.mention, inline=False)
                # Intentionally not logging vote totals or winner here
                # to keep results private unless you choose otherwise.
                await log_channel.send(embed=embed)

        # -----------------------------
        # Confirm to the admin privately in the channel
        # -----------------------------
        await interaction.response.send_message(
            "✅ Election closed. Results have been sent to your DMs.",
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(CloseElectionCommand(bot))
