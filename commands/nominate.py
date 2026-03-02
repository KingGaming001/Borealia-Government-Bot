# commands/nominate.py
# ------------------------------------------------------------
# /nominate and /remove_nominee
#
# Keeps election nomination logic here and delegates appointment-
# specific nomination behavior to commands/appointment_nominations.py.
# ------------------------------------------------------------

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord import Interaction, app_commands
from discord.ext import commands

from commands.appointment_nominations import (
    get_open_appointment_targets,
    handle_appointment_nomination,
    remove_open_appointment_nominee,
)
from config_store import (
    get_settings,
    has_associate_parliamentarian_role,
    has_parliament_role,
    is_admin,
)

LONDON_TZ = ZoneInfo("Europe/London")


def utc_iso_to_london_str(iso_utc: str) -> str:
    dt = datetime.fromisoformat(iso_utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(LONDON_TZ)
    return local.strftime("%d %b %Y, %H:%M") + " (Europe/London)"


def build_nominees_embed(position: str, start_at_iso_utc: str | None, nominees: list[dict]) -> discord.Embed:
    desc = "Nominations are open."
    if start_at_iso_utc:
        desc += f"\n**Voting begins:** {utc_iso_to_london_str(start_at_iso_utc)}"

    embed = discord.Embed(
        title=f"🗳️ Nominations — {position}",
        description=desc,
        color=discord.Color.gold(),
    )

    if nominees:
        for nominee in nominees:
            embed.add_field(name=nominee["display_name"], value=f"<@{nominee['user_id']}>", inline=False)
    else:
        embed.add_field(
            name="No nominees yet",
            value="Be the first to nominate yourself using **/nominate**.",
            inline=False,
        )

    return embed


async def refresh_nominees_message(
    bot: commands.Bot,
    nominees_channel: discord.TextChannel,
    guild_id: int,
    position: str,
    start_at_iso_utc: str | None,
    nominee_message_id: int | None,
) -> None:
    cur = bot.db.cursor()
    cur.execute(
        """
        SELECT user_id, display_name
        FROM nominations
        WHERE guild_id = ? AND position = ?
        ORDER BY display_name ASC
        """,
        (guild_id, position),
    )
    nominees_rows = cur.fetchall()
    nominees = [{"user_id": int(r["user_id"]), "display_name": str(r["display_name"])} for r in nominees_rows]

    embed = build_nominees_embed(position, start_at_iso_utc, nominees)

    msg_obj = None
    if nominee_message_id:
        try:
            msg_obj = await nominees_channel.fetch_message(int(nominee_message_id))
        except Exception:
            msg_obj = None

    if msg_obj:
        await msg_obj.edit(embed=embed)
        return

    sent = await nominees_channel.send(embed=embed)
    cur.execute(
        "UPDATE elections SET nominee_message_id = ? WHERE guild_id = ? AND position = ?",
        (sent.id, guild_id, position),
    )
    bot.db.commit()


class PositionSelect(discord.ui.Select):
    def __init__(
        self,
        bot: commands.Bot,
        guild_id: int,
        user_id: int,
        ballot_name: str,
        targets: list[dict],
        nominees_channel: discord.TextChannel,
    ):
        self.bot = bot
        self.guild_id = guild_id
        self.user_id = user_id
        self.ballot_name = ballot_name
        self.nominees_channel = nominees_channel

        options: list[discord.SelectOption] = []
        for target in targets:
            label = target["position"]
            if target["kind"] == "election":
                hint = utc_iso_to_london_str(target["start_at"]) if target.get("start_at") else "Start time not set"
                description = f"Election nomination • Voting begins: {hint}"
                value = f"election|{label}"
            else:
                if target.get("nomination_closes_at"):
                    close_hint = utc_iso_to_london_str(target["nomination_closes_at"])
                    description = f"Appointment nomination • Closes: {close_hint}"
                else:
                    description = "Appointment nomination • No public vote"
                value = f"appointment|{label}"

            options.append(
                discord.SelectOption(
                    label=label[:100],
                    description=description[:100],
                    value=value[:100],
                )
            )

        super().__init__(
            placeholder="Choose the position you want to nominate for…",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ This dropdown isn’t for you.", ephemeral=True)
            return

        try:
            nomination_kind, position = self.values[0].split("|", 1)
        except ValueError:
            await interaction.response.send_message("❌ Invalid nomination selection.", ephemeral=True)
            return

        if nomination_kind == "appointment":
            ok, message = await handle_appointment_nomination(
                bot=self.bot,
                nominees_channel=self.nominees_channel,
                guild_id=self.guild_id,
                user_id=self.user_id,
                ballot_name=self.ballot_name,
                position=position,
            )
            if ok:
                await interaction.response.edit_message(content=message, view=None)
            else:
                await interaction.response.send_message(message, ephemeral=True)
            return

        now_utc = datetime.now(timezone.utc)
        cur = self.bot.db.cursor()
        cur.execute(
            "SELECT status, start_at, nominee_message_id FROM elections WHERE guild_id = ? AND position = ?",
            (self.guild_id, position),
        )
        election = cur.fetchone()

        if not election:
            await interaction.response.send_message("❌ That election no longer exists.", ephemeral=True)
            return

        if election["status"] != "SCHEDULED":
            await interaction.response.send_message("❌ Nominations are closed for this election.", ephemeral=True)
            return

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
                ephemeral=True,
            )
            return

        cur.execute(
            """
            INSERT INTO nominations (guild_id, position, user_id, display_name)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, position, user_id)
            DO UPDATE SET display_name = excluded.display_name
            """,
            (self.guild_id, position, self.user_id, self.ballot_name),
        )
        self.bot.db.commit()

        await refresh_nominees_message(
            bot=self.bot,
            nominees_channel=self.nominees_channel,
            guild_id=self.guild_id,
            position=position,
            start_at_iso_utc=election["start_at"],
            nominee_message_id=election["nominee_message_id"],
        )

        await interaction.response.edit_message(
            content=(
                f"✅ You are nominated for **{position}** as **{self.ballot_name}**.\n"
                f"Nominees list updated in {self.nominees_channel.mention}."
            ),
            view=None,
        )


class NominateView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        guild_id: int,
        user_id: int,
        ballot_name: str,
        targets: list[dict],
        nominees_channel: discord.TextChannel,
    ):
        super().__init__(timeout=120)
        self.add_item(PositionSelect(bot, guild_id, user_id, ballot_name, targets, nominees_channel))


class NominateCommand(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="nominate", description="Nominate yourself for an open election or appointment position")
    @app_commands.describe(name="How you want your name to appear on the ballot")
    async def nominate(self, interaction: Interaction, name: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        settings = get_settings(self.bot.db, guild_id)
        if not settings:
            await interaction.response.send_message("❌ Bot not configured. Ask an admin to run **/setup**.", ephemeral=True)
            return

        parliament_role_configured = bool(settings.get("parliament_role_id"))
        associate_role_configured = bool(settings.get("associate_parliamentarian_role_id"))
        if parliament_role_configured or associate_role_configured:
            allowed_to_nominate = (
                interaction.user.guild_permissions.administrator
                or has_parliament_role(interaction.user, settings)
                or has_associate_parliamentarian_role(interaction.user, settings)
            )
            if not allowed_to_nominate:
                await interaction.response.send_message(
                    "❌ You must have the configured Parliament role or Associate Parliamentarian role to nominate.",
                    ephemeral=True,
                )
                return

        nominees_channel_id = settings.get("nominees_channel_id")
        nominees_channel = interaction.guild.get_channel(int(nominees_channel_id)) if nominees_channel_id else None
        if not nominees_channel or not isinstance(nominees_channel, discord.TextChannel):
            await interaction.response.send_message("❌ Configured nominees channel not found.", ephemeral=True)
            return

        now_utc = datetime.now(timezone.utc)
        cur = self.bot.db.cursor()
        cur.execute(
            """
            SELECT position, start_at
            FROM elections
            WHERE guild_id = ? AND status = 'SCHEDULED'
            """,
            (guild_id,),
        )

        available_targets: list[dict] = []
        for row in cur.fetchall():
            try:
                start_at = datetime.fromisoformat(str(row["start_at"]))
                if start_at.tzinfo is None:
                    start_at = start_at.replace(tzinfo=timezone.utc)
                if start_at.astimezone(timezone.utc) <= now_utc:
                    continue
            except Exception:
                continue

            available_targets.append(
                {
                    "kind": "election",
                    "position": str(row["position"]),
                    "start_at": str(row["start_at"]),
                }
            )

        available_targets.extend(get_open_appointment_targets(self.bot, guild_id, now_utc))

        if not available_targets:
            await interaction.response.send_message("❌ There are no positions currently open for nominations.", ephemeral=True)
            return

        view = NominateView(
            bot=self.bot,
            guild_id=guild_id,
            user_id=interaction.user.id,
            ballot_name=name,
            targets=available_targets,
            nominees_channel=nominees_channel,
        )

        embed = discord.Embed(
            title="📝 Nominate Yourself",
            description="Choose which position you want to nominate for from the dropdown below.",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Ballot Name", value=name, inline=False)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    @app_commands.command(
        name="remove_nominee",
        description="Remove your own nomination, or remove anyone if you are an admin",
    )
    @app_commands.describe(
        position="The election position (e.g., Prime Minister)",
        candidate="Optional: member to remove (defaults to yourself; admins can remove anyone)",
    )
    async def remove_nominee(
        self,
        interaction: Interaction,
        position: str,
        candidate: discord.Member | None = None,
    ):
        if not interaction.guild:
            await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
            return

        target = candidate or interaction.user

        guild_id = interaction.guild.id
        settings = get_settings(self.bot.db, guild_id)
        if not settings:
            await interaction.response.send_message("❌ Bot not configured. Ask an admin to run **/setup**.", ephemeral=True)
            return

        admin_can_remove_anyone = is_admin(interaction, settings)
        if not admin_can_remove_anyone and interaction.user.id != target.id:
            await interaction.response.send_message(
                "❌ You can only remove your own nomination. Admins can remove anyone.",
                ephemeral=True,
            )
            return

        nominees_channel_id = settings.get("nominees_channel_id")
        nominees_channel = interaction.guild.get_channel(int(nominees_channel_id)) if nominees_channel_id else None
        if not nominees_channel or not isinstance(nominees_channel, discord.TextChannel):
            await interaction.response.send_message("❌ Configured nominees channel not found.", ephemeral=True)
            return

        cur = self.bot.db.cursor()
        cur.execute(
            "SELECT status, start_at, nominee_message_id FROM elections WHERE guild_id = ? AND position = ?",
            (guild_id, position),
        )
        election = cur.fetchone()

        if election and election["status"] == "SCHEDULED":
            now_utc = datetime.now(timezone.utc)
            try:
                start_at = datetime.fromisoformat(election["start_at"])
                if start_at.tzinfo is None:
                    start_at = start_at.replace(tzinfo=timezone.utc)
                start_at_utc = start_at.astimezone(timezone.utc)
            except Exception:
                await interaction.response.send_message(
                    "❌ Election start time is invalid. Ask an admin to reschedule.",
                    ephemeral=True,
                )
                return

            if start_at_utc <= now_utc:
                await interaction.response.send_message(
                    "❌ Voting has already started (or is starting now). Nominations are closed.",
                    ephemeral=True,
                )
                return

            cur.execute(
                "DELETE FROM nominations WHERE guild_id = ? AND position = ? AND user_id = ?",
                (guild_id, position, target.id),
            )
            if cur.rowcount == 0:
                await interaction.response.send_message(
                    f"ℹ️ {target.mention} is not currently nominated for **{position}**.",
                    ephemeral=True,
                )
                return

            self.bot.db.commit()
            await refresh_nominees_message(
                bot=self.bot,
                nominees_channel=nominees_channel,
                guild_id=guild_id,
                position=position,
                start_at_iso_utc=election["start_at"],
                nominee_message_id=election["nominee_message_id"],
            )
            await interaction.response.send_message(
                f"✅ Removed {target.mention} from nominees for **{position}**.",
                ephemeral=True,
            )
            return

        removed = await remove_open_appointment_nominee(
            bot=self.bot,
            nominees_channel=nominees_channel,
            guild_id=guild_id,
            position=position,
            candidate_id=target.id,
        )
        if removed:
            await interaction.response.send_message(
                f"✅ Removed {target.mention} from nominees for **{position}**.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            f"❌ No open nomination track found for **{position}**.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(NominateCommand(bot))
