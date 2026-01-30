# commands/close_election.py
# ------------------------------------------------------------
# /close_election
#
# Behaviour:
# - Admin-only
# - Closes an election early (or closes a scheduled one before it starts)
# - Sets status='CLOSED'
# - Disables the voting dropdown message (if it exists)
# - Sends results privately to the admin via DM
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


class CloseElectionCommand(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="close_election",
        description="Close an election early and DM the results privately to the admin"
    )
    @app_commands.describe(position="The position to close (e.g., Prime Minister)")
    async def close_election(self, interaction: Interaction, position: str):
        if not interaction.guild:
            await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
            return

        guild_id = interaction.guild.id

        # -----------------------------
        # Load settings and permissions
        # -----------------------------
        settings = get_settings(self.bot.db, guild_id)
        if not settings:
            await interaction.response.send_message("❌ Bot not configured. Run **/setup** first.", ephemeral=True)
            return

        if not is_admin(interaction, settings):
            await interaction.response.send_message("❌ You do not have permission to do that.", ephemeral=True)
            return

        # -----------------------------
        # Fetch election row
        # -----------------------------
        cur = self.bot.db.cursor()
        cur.execute(
            """
            SELECT status, start_at, nominee_message_id, vote_message_id
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
        nominee_message_id = election["nominee_message_id"]
        vote_message_id = election["vote_message_id"]

        if current_status == "CLOSED":
            await interaction.response.send_message(
                f"ℹ️ The election for **{position}** is already closed.",
                ephemeral=True
            )
            return

        # -----------------------------
        # Collect nominees
        # -----------------------------
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

        # Map candidate_id -> display name for readable results
        name_by_id = {n["user_id"]: n["display_name"] for n in nominees}

        # -----------------------------
        # Collect votes (private)
        # -----------------------------
        # votes table should have: guild_id, position, voter_id, candidate_id
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
        results_lines: list[str] = []
        winner_id = None
        winner_votes = 0

        for r in vote_rows:
            cid = int(r["candidate_id"])
            v = int(r["votes"])
            total_votes += v

            display = name_by_id.get(cid, f"Unknown Candidate ({cid})")
            results_lines.append(f"• **{display}** — {v} vote(s)")
            if winner_id is None:
                winner_id = cid
                winner_votes = v

        if not results_lines:
            results_lines.append("• *(No votes were recorded.)*")

        # Determine if tie for first place
        is_tie = False
        if vote_rows and len(vote_rows) > 1:
            top = int(vote_rows[0]["votes"])
            second = int(vote_rows[1]["votes"])
            if second == top:
                is_tie = True

        # -----------------------------
        # Mark election as CLOSED
        # -----------------------------
        cur.execute(
            """
            UPDATE elections
            SET status = 'CLOSED'
            WHERE guild_id = ? AND position = ?
            """,
            (guild_id, position)
        )
        self.bot.db.commit()

        # -----------------------------
        # Disable voting UI message (if it exists)
        # -----------------------------
        elections_channel_id = settings.get("elections_channel_id")
        if elections_channel_id and vote_message_id:
            elections_channel = interaction.guild.get_channel(int(elections_channel_id))
            if elections_channel and isinstance(elections_channel, discord.TextChannel):
                try:
                    msg = await elections_channel.fetch_message(int(vote_message_id))

                    closed_embed = discord.Embed(
                        title=f"🗳️ Election Closed — {position}",
                        description="This election has been closed by an administrator.",
                        color=discord.Color.red()
                    )
                    if start_at_iso:
                        closed_embed.add_field(
                            name="Scheduled vote start",
                            value=utc_iso_to_london_str(start_at_iso),
                            inline=False
                        )

                    # Removing the view disables dropdown voting
                    await msg.edit(embed=closed_embed, view=None)
                except Exception:
                    # If message missing or no perms, ignore gracefully
                    pass

        # -----------------------------
        # Optionally update nominees message to show closed
        # -----------------------------
        nominees_channel_id = settings.get("nominees_channel_id")
        if nominees_channel_id and nominee_message_id:
            nominees_channel = interaction.guild.get_channel(int(nominees_channel_id))
            if nominees_channel and isinstance(nominees_channel, discord.TextChannel):
                try:
                    nmsg = await nominees_channel.fetch_message(int(nominee_message_id))

                    embed = discord.Embed(
                        title=f"📝 Nominations Closed — {position}",
                        description="This election has been closed.",
                        color=discord.Color.dark_grey()
                    )
                    if nominees:
                        for n in nominees:
                            embed.add_field(name=n["display_name"], value=f"<@{n['user_id']}>", inline=False)
                    else:
                        embed.add_field(name="No nominees", value="No nominees were recorded.", inline=False)

                    await nmsg.edit(embed=embed)
                except Exception:
                    pass

        # -----------------------------
        # DM the admin results (private)
        # -----------------------------
        dm_embed = discord.Embed(
            title=f"📩 Election Results (Private) — {position}",
            color=discord.Color.blurple()
        )
        dm_embed.add_field(name="Guild", value=interaction.guild.name, inline=False)
        dm_embed.add_field(name="Status when closed", value=current_status, inline=False)
        if start_at_iso:
            dm_embed.add_field(name="Scheduled vote start", value=utc_iso_to_london_str(start_at_iso), inline=False)
        dm_embed.add_field(name="Total votes recorded", value=str(total_votes), inline=False)

        dm_embed.add_field(
            name="Results",
            value="\n".join(results_lines),
            inline=False
        )

        if winner_id is None:
            dm_embed.add_field(name="Winner", value="No winner (no votes).", inline=False)
        else:
            winner_name = name_by_id.get(winner_id, f"Unknown Candidate ({winner_id})")
            if is_tie:
                dm_embed.add_field(
                    name="Winner",
                    value=f"⚠️ Tie detected at {winner_votes} vote(s). Top candidate: **{winner_name}** (tie-break required).",
                    inline=False
                )
            else:
                dm_embed.add_field(
                    name="Winner",
                    value=f"🏆 **{winner_name}** with {winner_votes} vote(s).",
                    inline=False
                )

        try:
            await interaction.user.send(embed=dm_embed)
            dm_note = "✅ I’ve DM’d you the results."
        except Exception:
            dm_note = "⚠️ I couldn’t DM you the results (your DMs may be closed)."

        # -----------------------------
        # Ephemeral confirmation in server
        # -----------------------------
        await interaction.response.send_message(
            f"✅ Election for **{position}** has been closed.\n{dm_note}",
            ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(CloseElectionCommand(bot))
