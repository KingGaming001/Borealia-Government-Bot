# commands/status.py
# ------------------------------------------------------------
# /status
# Shows the current configuration for this guild (ephemeral).
# Admin-only:
# - Discord administrators OR configured admin role.
# ------------------------------------------------------------

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from config_store import get_settings, is_admin


class StatusCommand(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="status",
        description="Show the current Borealia Government bot configuration for this server"
    )
    async def status(self, interaction: discord.Interaction):
        # Must be used in a server
        if not interaction.guild:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.",
                ephemeral=True
            )
            return

        # Load settings from DB
        settings = get_settings(self.bot.db, interaction.guild.id)

        # Permission check (admins or admin role)
        if not is_admin(interaction, settings):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True
            )
            return

        # If not configured
        if not settings:
            await interaction.response.send_message(
                "⚠️ The Borealia Government bot is not yet configured for this server. "
                "An administrator can run the **/setup** command to configure it.",
                ephemeral=True
            )
            return

        # Helpers: convert stored IDs into mentions (or warnings)
        def channel_mention(channel_id: int | None) -> str:
            if not channel_id:
                return "⚠️ Not configured"
            ch = interaction.guild.get_channel(int(channel_id))
            return ch.mention if ch else f"⚠️ Channel not found ({channel_id})"

        def role_mention(role_id: int | None) -> str:
            if not role_id:
                return "⚠️ Not configured"
            role = interaction.guild.get_role(int(role_id))
            return role.mention if role else f"⚠️ Role not found ({role_id})"

        # Build embed
        embed = discord.Embed(
            title="📊 Borealia Government Bot Configuration Status",
            description="Current server configuration saved via `/setup`",
            color=discord.Color.blue()
        )

        embed.add_field(
            name="Channels",
            value=(
                f"• **Nominees Channel:** {channel_mention(settings.get('nominees_channel_id'))}\n"
                f"• **Elections Channel:** {channel_mention(settings.get('elections_channel_id'))}\n"
                f"• **Laws Channel:** {channel_mention(settings.get('laws_channel_id'))}\n"
                f"• **Log Channel:** {channel_mention(settings.get('log_channel_id'))}"
            ),
            inline=False
        )

        embed.add_field(
            name="Roles",
            value=(
                f"• **Voter Role:** {role_mention(settings.get('voter_role_id'))}\n"
                f"• **Admin Role:** {role_mention(settings.get('admin_role_id'))}"
            ),
            inline=False
        )

        # Missing config hint (added ONCE)
        missing_fields = []
        for key in [
            "nominees_channel_id",
            "elections_channel_id",
            "laws_channel_id",
            "voter_role_id",
            "admin_role_id"
        ]:
            if not settings.get(key):
                missing_fields.append(key.replace("_", " ").title())

        if missing_fields:
            embed.add_field(
                name="⚠️ Incomplete Configuration",
                value=(
                    "The following required fields are missing:\n"
                    + "\n".join(f"• {field}" for field in missing_fields)
                    + "\n\nAn administrator can run **/setup** to update the configuration."
                ),
                inline=False
            )

        # Send ONCE
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(StatusCommand(bot))
