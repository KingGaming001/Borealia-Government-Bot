# commands/open_election.py
# ------------------------------------------------------------
# /open_election
# Opens (or re-opens) an election for a specific position.
#
# Why this exists:
# - Regular scheduled elections
# - By-elections (if someone resigns)
#
# What it does:
# 1) Checks bot has been configured (/setup)
# 2_ Checks the user has admin permission (Discord admin OR admin_role)
# 3) Marks the election as OPEN (is_closed = 0)
# 4) Clears all previous votes for that position (always)
# 5) Optionally clears nominees too (clear_nominees=True)
#
# Notes:
# - We DO NOT publish vote counts publicly.
# - This command simply makes the election available for nominations/voting.
# ------------------------------------------------------------

import discord
from discord import app_commands
from discord.ext import commands

from config_store import get_settings, is_admin

class OpenElectionCommand(commands.Cog):
    def __init__(self, bot: commands.Bot):
        # Store bot reference so we can use in bot.db
        self.bot = bot

    @app_commands.command(
        name="open_election",
        description="Open or reopen an election (useful for by-elections)"
    )
    @app_commands.describe(
        position="The position to open the election for (e.g., Prime Minister)",
        clear_nominees="If true, removes all nominees as well as votes"
    )
    async def open_election(
        self,
        interaction: discord.Interaction,
        position: str,
        clear_nominees: bool = False
    ):
        # -----------------------------
        # Must be used in a server
        # -----------------------------
        if not interaction.guild:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.",
                ephemeral=True
            )
            return
        
        guild_id = interaction.guild.id

        # -----------------------------
        # Load config and check permissions
        # -----------------------------
        settings = get_settings(self.bot.db, guild_id)
        if not settings:
            await interaction.response.send_message(
                "❌ The Borealia Government bot is not yet configured for this server. "
                "An administrator can run the /setup command to configure it.",
                ephemeral=True
            )
            return
        
        if not is_admin(interaction, settings):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )
            return
        
        # -----------------------------
        # Update database:
        # - Ensure election row exists
        # - Mark election as OPEN
        # - Clear votes
        # - Optionally clear nominees
        # -----------------------------
        cur = self.bot.db.cursor()

        cur.execute(
            """
            INSERT INTO elections (guild_id, position, is_closed)
            VALUES (?, ?, 0)
            ON CONFLICT(guild_id, position) DO UPDATE SET is_closed = 0
            """,
            (guild_id, position)
        )

        # Clear votes so it's a fresh election cycle
        cur.execute(
            "DELETE FROM votes WHERE guild_id = ? AND position = ?" ,
            (guild_id, position),
        )

        self.bot.db.commit()

        # -----------------------------
        # Optional logging (if log channel configured)
        # This does NOT include vote totals (privacy).
        # -----------------------------
        log_channel_id = settings.get("log_channel_id")
        if log_channel_id:
            log_channel = interaction.guild.get_channel(int(log_channel_id))
            if log_channel:
                embed = discord.Embed(
                    title="🗳️ Election Opened",
                    description=(
                        f"An election for the position of **{position}** has been opened by "
                        f"{interaction.user.mention}."
                    ),
                    color=discord.Color.green()
                )
                if clear_nominees:
                    embed.add_field(
                        name="Nominees Cleared",
                        value="All previous nominees have also been cleared.",
                        inline=False
                    )
                await log_channel.send(embed=embed)

        # -----------------------------
        # Confirm to the admin privattely
        # -----------------------------
        msg = f"✅ The election for **{position}** has been opened."
        if clear_nominees:
            msg += " All previous nominees have also been cleared."

        await interaction.response.send_message(
            msg,
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(OpenElectionCommand(bot))