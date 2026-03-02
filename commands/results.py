# commands/results.py
# ------------------------------------------------------------
# /results
#
# Behaviour:
# - Admin-only
# - Sends election results privately to the command invoker via DM
# - Does NOT change election status
# ------------------------------------------------------------

from __future__ import annotations

import discord
from discord import app_commands, Interaction
from discord.ext import commands

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from config_store import get_settings, is_admin

LONDON_TZ = ZoneInfo("Europe/London")


def utc_iso_to_london_str(iso_utc: str) -> str:
    dt = datetime.fromisoformat(iso_utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(LONDON_TZ)
    return local.strftime("%d %b %Y, %H:%M") + " (Europe/London)"


class DmResultsCommand(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="results",
        description="DM yourself the current election results for a position"
    )
    @app_commands.describe(position="The election position (e.g., Prime Minister)")
    async def results(self, interaction: Interaction, position: str):
        if not interaction.guild:
            await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
            return

        guild_id = interaction.guild.id

        settings = get_settings(self.bot.db, guild_id)
        if not settings:
            await interaction.response.send_message("❌ Bot not configured. Run **/setup** first.", ephemeral=True)
            return

        if not is_admin(interaction, settings):
            await interaction.response.send_message("❌ You do not have permission to do that.", ephemeral=True)
            return

        cur = self.bot.db.cursor()
        cur.execute(
            """
            SELECT status, start_at
            FROM elections
            WHERE guild_id = ? AND position = ?
            """,
            (guild_id, position)
        )
        election = cur.fetchone()

        if not election:
            await interaction.response.send_message(
                f"❌ No election found for **{position}**.",
                ephemeral=True
            )
            return

        current_status = str(election["status"])
        start_at_iso = str(election["start_at"]) if election["start_at"] else None

        cur.execute(
            """
            SELECT user_id, display_name
            FROM nominations
            WHERE guild_id = ? AND position = ?
            ORDER BY display_name ASC
            """,
            (guild_id, position)
        )
        nominees_rows = cur.fetchall()
        nominees = [{"user_id": int(r["user_id"]), "display_name": str(r["display_name"])} for r in nominees_rows]
        name_by_id = {n["user_id"]: n["display_name"] for n in nominees}

        cur.execute(
            """
            SELECT candidate_id, COUNT(*) as votes
            FROM votes
            WHERE guild_id = ? AND position = ?
            GROUP BY candidate_id
            ORDER BY votes DESC
            """,
            (guild_id, position)
        )
        vote_rows = cur.fetchall()

        total_votes = 0
        for r in vote_rows:
            total_votes += int(r["votes"])

        results_lines: list[str] = []
        winner_id = None
        winner_votes = 0

        for r in vote_rows:
            candidate_id = int(r["candidate_id"])
            votes = int(r["votes"])
            display_name = name_by_id.get(candidate_id, f"Unknown Candidate ({candidate_id})")
            vote_share = (votes / total_votes * 100) if total_votes > 0 else 0
            results_lines.append(f"• **{display_name}** — {votes} vote(s) ({vote_share:.1f}%)")

            if winner_id is None:
                winner_id = candidate_id
                winner_votes = votes

        if not results_lines:
            results_lines.append("• *(No votes were recorded.)*")

        voter_role_id = settings.get("voter_role_id")
        num_eligible_voters = 0
        if voter_role_id:
            try:
                voter_role = interaction.guild.get_role(int(voter_role_id))
                if voter_role:
                    num_eligible_voters = len(voter_role.members)
            except Exception:
                pass

        cur.execute(
            """
            SELECT COUNT(DISTINCT voter_id) as voter_count
            FROM votes
            WHERE guild_id = ? AND position = ?
            """,
            (guild_id, position)
        )
        voter_count_row = cur.fetchone()
        num_voters = int(voter_count_row["voter_count"]) if voter_count_row else 0

        turnout_pct = (num_voters / num_eligible_voters * 100) if num_eligible_voters > 0 else 0
        turnout_str = (
            f"{num_voters}/{num_eligible_voters} ({turnout_pct:.1f}%)"
            if num_eligible_voters > 0
            else f"{num_voters} (eligible voters unknown)"
        )

        is_tie = False
        if vote_rows and len(vote_rows) > 1:
            top_votes = int(vote_rows[0]["votes"])
            second_votes = int(vote_rows[1]["votes"])
            if second_votes == top_votes:
                is_tie = True

        dm_embed = discord.Embed(
            title=f"📩 Election Results (Private) — {position}",
            color=discord.Color.blurple()
        )
        dm_embed.add_field(name="Guild", value=interaction.guild.name, inline=False)
        dm_embed.add_field(name="Current status", value=current_status, inline=False)
        if start_at_iso:
            dm_embed.add_field(name="Scheduled vote start", value=utc_iso_to_london_str(start_at_iso), inline=False)
        dm_embed.add_field(name="Voter Turnout", value=turnout_str, inline=False)
        dm_embed.add_field(name="Total votes recorded", value=str(total_votes), inline=False)
        dm_embed.add_field(name="Results", value="\n".join(results_lines), inline=False)

        if winner_id is None:
            dm_embed.add_field(name="Leader", value="No leader (no votes).", inline=False)
        else:
            winner_name = name_by_id.get(winner_id, f"Unknown Candidate ({winner_id})")
            if is_tie:
                dm_embed.add_field(
                    name="Leader",
                    value=f"⚠️ Tie detected at {winner_votes} vote(s). Top candidate: **{winner_name}** (tie-break required).",
                    inline=False
                )
            else:
                dm_embed.add_field(
                    name="Leader",
                    value=f"🏆 **{winner_name}** with {winner_votes} vote(s).",
                    inline=False
                )

        try:
            await interaction.user.send(embed=dm_embed)
            await interaction.response.send_message("✅ I’ve DM’d you the election results.", ephemeral=True)
        except Exception:
            await interaction.response.send_message(
                "⚠️ I couldn’t DM you the results (your DMs may be closed).",
                ephemeral=True
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(DmResultsCommand(bot))
