# commands/setup.py
# ------------------------------------------------------------
# /setup
# Saves the server configuration for the Borealia Government Bot:
# - nominees channel
# - elections channel
# - proposed laws channel
# - log channel
# - voter role
# - admin role
#
# Only discord administrators (or an already-configured admin role)
# can run /setup
# ------------------------------------------------------------

import discord
from discord import app_commands
from discord.ext import commands

from config_store import get_settings, upsert_settings, is_admin

class SetupCommand(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="setup",
        description="Configure Borealia Government bot channels and roles for this server"
    )
    @app_commands.description(
        nominees_channel="Channel where nominations are submitted",
        elections_channel="Channel where election panels + voting are posted",
        laws_channel="Channel where proposed laws are posted",
        log_channel="Staff log channel (optional)",
        voter_role="Role required to vote (e.g. Citizen)",
        admin_role="Role allowed to run admin commands (e.g., King)"
    )
    async def setup(
        self,
        interaction: discord.Interaction,
        nominees_channel: discord.TextChannel,
        elections_channel: discord.TextChannel,
        laws_channel: discord.TextChannel,
        log_channel: discord.TextChannel | None,
        voter_role: discord.Role,
        admin_role: discord.Role
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
        
        # -----------------------------
        # Permission check
        # - Discord admins always allowed
        # - If bot is already configured, the configured admin role can also run setup
        # -----------------------------
        current_settings = get_settings(self.bot.db, interaction.guild.id)
        if not is_admin(interaction, current_settings):
            await interaction.response.send_message(
                "❌ You do not have permission to run this command.",
                ephemeral=True
            )
            return
        
        # -----------------------------
        # Save configuration to the database
        # -----------------------------
        upsert_settings(
            self.bot.db,
            interaction.guild.id,
            nominees_channel_id=nominees_channel.id,
            elections_channel_id=elections_channel.id,
            laws_channel_id=laws_channel.id,
            log_channel_id=log_channel.id if log_channel else None,
            voter_role_id=voter_role.id, # REQUIRED
            admin_role_id=admin_role.id  # REQUIRED
        )

        # -----------------------------
        # Confirm back to the user
        # -----------------------------
        embed = discord.Embed(
            title="✅ Borealia Government Bot Setup Complete",
            description="The bot has been successfully configured for this server.",
            color=discord.Color.green()
        )
        embed.add_field(
            name="Configured Channels",
            value=(
                f"• **Nominees Channel:** {nominees_channel.mention}\n"
                f"• **Elections Channel:** {elections_channel.mention}\n"
                f"• **Proposed Laws Channel:** {laws_channel.mention}\n"
                f"• **Log Channel:** {log_channel.mention if log_channel else 'Not Set'}"
                f"• **Voter Role:** {voter_role.mention}\n"
                f"• **Admin Role:** {admin_role.mention}\n"
            ),
            inline=False
        )

        await interaction.response.send_message(embed=embed, ephemeral=True)
        
        if interaction.user.guild_permissions.administrator:
            await interaction.followup.send(
                "ℹ️ Note: As a server administrator, you can always run /setup again to reconfigure the bot."
            )

        async def setup(bot: commands.Bot):
            await bot.add_cog(SetupCommand(bot))