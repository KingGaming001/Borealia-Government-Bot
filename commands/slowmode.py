from __future__ import annotations

from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from config_store import get_settings, is_admin


class SlowmodeCommand(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return

        if not isinstance(message.author, discord.Member):
            return

        if not isinstance(message.channel, discord.TextChannel):
            return

        # Staff with message-management permissions bypass slowmode moderation.
        if message.author.guild_permissions.manage_messages:
            return

        cur = self.bot.db.cursor()
        cur.execute(
            """
            SELECT delay_seconds
            FROM extended_slowmode_channels
            WHERE guild_id = ? AND channel_id = ?
            """,
            (message.guild.id, message.channel.id),
        )
        row = cur.fetchone()
        if not row:
            return

        delay_seconds = int(row["delay_seconds"])
        now = datetime.now(timezone.utc)

        cur.execute(
            """
            SELECT last_message_at
            FROM extended_slowmode_activity
            WHERE guild_id = ? AND channel_id = ? AND user_id = ?
            """,
            (message.guild.id, message.channel.id, message.author.id),
        )
        activity = cur.fetchone()

        if activity:
            last_message_at = datetime.fromisoformat(str(activity["last_message_at"]))
            elapsed = (now - last_message_at).total_seconds()
            if elapsed < delay_seconds:
                remaining = int(delay_seconds - elapsed)
                try:
                    await message.delete()
                except discord.Forbidden:
                    return
                except discord.HTTPException:
                    return

                try:
                    await message.author.send(
                        f"⏳ Extended slowmode is enabled in #{message.channel.name}. "
                        f"Please wait {remaining} more second(s) before sending another message there."
                    )
                except discord.HTTPException:
                    pass
                return

        cur.execute(
            """
            INSERT INTO extended_slowmode_activity (guild_id, channel_id, user_id, last_message_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, channel_id, user_id) DO UPDATE SET
                last_message_at = excluded.last_message_at
            """,
            (message.guild.id, message.channel.id, message.author.id, now.isoformat()),
        )
        self.bot.db.commit()

    @app_commands.command(
        name="slowmode",
        description="Set slowmode for a channel"
    )
    @app_commands.describe(
        channel="The text channel to apply slowmode to",
        time="The amount of time",
        unit="The time unit"
    )
    @app_commands.choices(
        unit=[
            app_commands.Choice(name="seconds", value="seconds"),
            app_commands.Choice(name="minutes", value="minutes"),
            app_commands.Choice(name="hours", value="hours"),
            app_commands.Choice(name="days", value="days"),
        ]
    )
    async def slowmode(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        time: app_commands.Range[int, 0],
        unit: app_commands.Choice[str],
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.",
                ephemeral=True,
            )
            return

        settings = get_settings(self.bot.db, interaction.guild.id)
        if not is_admin(interaction, settings):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        multipliers = {
            "seconds": 1,
            "minutes": 60,
            "hours": 3600,
            "days": 86400,
        }

        delay_seconds = int(time) * multipliers[unit.value]

        # Disable all slowmode for this channel.
        if delay_seconds == 0:
            try:
                await channel.edit(slowmode_delay=0, reason=f"Set by {interaction.user} via /slowmode")
            except discord.Forbidden:
                await interaction.response.send_message(
                    f"❌ I don't have permission to edit {channel.mention}.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException:
                await interaction.response.send_message(
                    "❌ Failed to update slowmode due to a Discord API error.",
                    ephemeral=True,
                )
                return

            cur = self.bot.db.cursor()
            cur.execute(
                "DELETE FROM extended_slowmode_channels WHERE guild_id = ? AND channel_id = ?",
                (interaction.guild.id, channel.id),
            )
            cur.execute(
                "DELETE FROM extended_slowmode_activity WHERE guild_id = ? AND channel_id = ?",
                (interaction.guild.id, channel.id),
            )
            self.bot.db.commit()

            await interaction.response.send_message(
                f"✅ Slowmode disabled for {channel.mention}.",
                ephemeral=True,
            )
            return

        # Native slowmode path (Discord-enforced): up to 6 hours.
        if delay_seconds <= 21600:
            try:
                await channel.edit(slowmode_delay=delay_seconds, reason=f"Set by {interaction.user} via /slowmode")
            except discord.Forbidden:
                await interaction.response.send_message(
                    f"❌ I don't have permission to edit {channel.mention}.",
                    ephemeral=True,
                )
                return
            except discord.HTTPException:
                await interaction.response.send_message(
                    "❌ Failed to update slowmode due to a Discord API error.",
                    ephemeral=True,
                )
                return

            cur = self.bot.db.cursor()
            cur.execute(
                "DELETE FROM extended_slowmode_channels WHERE guild_id = ? AND channel_id = ?",
                (interaction.guild.id, channel.id),
            )
            cur.execute(
                "DELETE FROM extended_slowmode_activity WHERE guild_id = ? AND channel_id = ?",
                (interaction.guild.id, channel.id),
            )
            self.bot.db.commit()

            await interaction.response.send_message(
                f"✅ Native slowmode set for {channel.mention}: **{time} {unit.value}** ({delay_seconds} seconds).",
                ephemeral=True,
            )
            return

        # Extended/custom slowmode path (>6 hours): bot-enforced per-user cooldown.
        try:
            await channel.edit(slowmode_delay=0, reason=f"Set by {interaction.user} via /slowmode (extended)")
        except discord.Forbidden:
            await interaction.response.send_message(
                f"❌ I don't have permission to edit {channel.mention}.",
                ephemeral=True,
            )
            return
        except discord.HTTPException:
            await interaction.response.send_message(
                "❌ Failed to update slowmode due to a Discord API error.",
                ephemeral=True,
            )
            return

        cur = self.bot.db.cursor()
        cur.execute(
            """
            INSERT INTO extended_slowmode_channels (guild_id, channel_id, delay_seconds, enabled_by, enabled_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, channel_id) DO UPDATE SET
                delay_seconds = excluded.delay_seconds,
                enabled_by = excluded.enabled_by,
                enabled_at = excluded.enabled_at
            """,
            (
                interaction.guild.id,
                channel.id,
                delay_seconds,
                interaction.user.id,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        cur.execute(
            "DELETE FROM extended_slowmode_activity WHERE guild_id = ? AND channel_id = ?",
            (interaction.guild.id, channel.id),
        )
        self.bot.db.commit()

        await interaction.response.send_message(
            (
                f"✅ Extended slowmode set for {channel.mention}: **{time} {unit.value}** ({delay_seconds} seconds).\n"
                "Users can send one message per interval in this channel."
            ),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SlowmodeCommand(bot))
