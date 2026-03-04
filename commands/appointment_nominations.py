from __future__ import annotations

# Appointment nominations are intentionally separate from elections:
# no public voting phase, only nomination collection and private admin review.

from datetime import datetime, timezone, timedelta
import sqlite3
from typing import Any, cast
from zoneinfo import ZoneInfo

import discord
from discord import Interaction, app_commands
from discord.ext import commands, tasks

from config_store import get_settings, is_admin

LONDON_TZ = ZoneInfo("Europe/London")


def _db(bot: commands.Bot) -> sqlite3.Connection:
    return cast(Any, bot).db


def utc_iso_to_london_str(iso_utc: str) -> str:
    dt = datetime.fromisoformat(iso_utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(LONDON_TZ)
    return local.strftime("%d %b %Y, %H:%M") + " (Europe/London)"


def build_private_nominee_lines(rows: list[dict]) -> str:
    if not rows:
        return "• *(No nominees yet.)*"
    lines: list[str] = []
    for row in rows:
        lines.append(f"• **{row['display_name']}** (<@{int(row['user_id'])}>)")
    return "\n".join(lines)


async def send_nominee_summary_to_admin_role(
    interaction: Interaction,
    settings: dict,
    dm_embed: discord.Embed,
) -> tuple[int, int, bool]:
    """
    DM appointment nominee summary to members of configured admin role.
    Returns (sent_count, failed_count, invoker_received).
    """
    if not interaction.guild:
        return 0, 0, False

    admin_role_id = settings.get("admin_role_id") if settings else None
    if not admin_role_id:
        return 0, 0, False

    admin_role = interaction.guild.get_role(int(admin_role_id))
    if not admin_role:
        return 0, 0, False

    sent = 0
    failed = 0
    invoker_received = False

    recipients = [member for member in admin_role.members if not member.bot]
    for member in recipients:
        try:
            await member.send(embed=dm_embed)
            sent += 1
            if member.id == interaction.user.id:
                invoker_received = True
        except Exception:
            failed += 1

    return sent, failed, invoker_received


def build_appointment_nominees_embed(
    position: str,
    nominees: list[dict],
    open_status: bool = True,
    closes_at_iso_utc: str | None = None,
) -> discord.Embed:
    title_prefix = "📋 Appointment Nominations" if open_status else "📋 Appointment Nominations Closed"
    desc = (
        "Nominations are open. The Prime Minister/leadership will choose privately."
        if open_status
        else "Nominations are now closed. Selection is handled privately."
    )
    if open_status and closes_at_iso_utc:
        desc += f"\n**Nominations close:** {utc_iso_to_london_str(closes_at_iso_utc)}"

    embed = discord.Embed(
        title=f"{title_prefix} — {position}",
        description=desc,
        color=discord.Color.teal() if open_status else discord.Color.dark_teal(),
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


async def refresh_appointment_nominees_message(
    bot: commands.Bot,
    nominees_channel: discord.TextChannel,
    guild_id: int,
    position: str,
    nominee_message_id: int | None,
    closes_at_iso_utc: str | None = None,
    open_status: bool = True,
) -> None:
    db = _db(bot)
    cur = db.cursor()
    cur.execute(
        """
        SELECT user_id, display_name
        FROM appointment_nominations
        WHERE guild_id = ? AND position = ?
        ORDER BY display_name ASC
        """,
        (guild_id, position),
    )
    nominees_rows = cur.fetchall()
    nominees = [{"user_id": int(r["user_id"]), "display_name": str(r["display_name"])} for r in nominees_rows]

    embed = build_appointment_nominees_embed(
        position,
        nominees,
        open_status=open_status,
        closes_at_iso_utc=closes_at_iso_utc,
    )

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
        "UPDATE appointment_positions SET nominee_message_id = ? WHERE guild_id = ? AND position = ?",
        (sent.id, guild_id, position),
    )
    db.commit()


def get_open_appointment_targets(bot: commands.Bot, guild_id: int, now_utc: datetime) -> list[dict]:
    # Returns only currently-open tracks; expired rows are ignored so the
    # nomination UI can safely show mixed election + appointment options.
    cur = _db(bot).cursor()
    cur.execute(
        """
        SELECT position, nomination_closes_at
        FROM appointment_positions
        WHERE guild_id = ? AND status = 'OPEN'
        ORDER BY position ASC
        """,
        (guild_id,),
    )
    rows = cur.fetchall()

    targets: list[dict] = []
    for row in rows:
        close_at_iso = str(row["nomination_closes_at"]) if row["nomination_closes_at"] else None
        if close_at_iso:
            try:
                close_at = datetime.fromisoformat(close_at_iso)
                if close_at.tzinfo is None:
                    close_at = close_at.replace(tzinfo=timezone.utc)
                if close_at.astimezone(timezone.utc) <= now_utc:
                    continue
            except Exception:
                pass

        targets.append(
            {
                "kind": "appointment",
                "position": str(row["position"]),
                "nomination_closes_at": close_at_iso,
            }
        )

    return targets


async def handle_appointment_nomination(
    bot: commands.Bot,
    nominees_channel: discord.TextChannel,
    guild_id: int,
    user_id: int,
    ballot_name: str,
    position: str,
) -> tuple[bool, str]:
    # Late nominations auto-close stale tracks to keep DB status truthful even
    # if the background auto-closer has not run yet.
    db = _db(bot)
    cur = db.cursor()
    cur.execute(
        "SELECT status, nominee_message_id, nomination_closes_at FROM appointment_positions WHERE guild_id = ? AND position = ?",
        (guild_id, position),
    )
    appointment = cur.fetchone()

    if not appointment or appointment["status"] != "OPEN":
        return False, "❌ Nominations are closed for this appointment position."

    now_utc = datetime.now(timezone.utc)
    close_at_iso = appointment["nomination_closes_at"]
    if close_at_iso:
        try:
            close_at = datetime.fromisoformat(str(close_at_iso))
            if close_at.tzinfo is None:
                close_at = close_at.replace(tzinfo=timezone.utc)
            close_at_utc = close_at.astimezone(timezone.utc)
        except Exception:
            close_at_utc = None

        if close_at_utc and close_at_utc <= now_utc:
            cur.execute(
                """
                UPDATE appointment_positions
                SET status = 'CLOSED', closed_at = ?
                WHERE guild_id = ? AND position = ?
                """,
                (now_utc.isoformat(), guild_id, position),
            )
            db.commit()

            await refresh_appointment_nominees_message(
                bot=bot,
                nominees_channel=nominees_channel,
                guild_id=guild_id,
                position=position,
                nominee_message_id=appointment["nominee_message_id"],
                closes_at_iso_utc=str(close_at_iso),
                open_status=False,
            )
            return False, "❌ Nominations for this appointment position have just closed."

    cur.execute(
        """
        INSERT INTO appointment_nominations (guild_id, position, user_id, display_name)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id, position, user_id)
        DO UPDATE SET display_name = excluded.display_name
        """,
        (guild_id, position, user_id, ballot_name),
    )
    db.commit()

    await refresh_appointment_nominees_message(
        bot=bot,
        nominees_channel=nominees_channel,
        guild_id=guild_id,
        position=position,
        nominee_message_id=appointment["nominee_message_id"],
        closes_at_iso_utc=str(close_at_iso) if close_at_iso else None,
        open_status=True,
    )

    return True, (
        f"✅ You are nominated for **{position}** as **{ballot_name}**.\n"
        f"Nominees list updated in {nominees_channel.mention}. (No public vote for this position.)"
    )


async def remove_open_appointment_nominee(
    bot: commands.Bot,
    nominees_channel: discord.TextChannel,
    guild_id: int,
    position: str,
    candidate_id: int,
) -> bool:
    db = _db(bot)
    cur = db.cursor()
    cur.execute(
        "SELECT status, nominee_message_id, nomination_closes_at FROM appointment_positions WHERE guild_id = ? AND position = ?",
        (guild_id, position),
    )
    appointment = cur.fetchone()
    if not appointment or appointment["status"] != "OPEN":
        return False

    cur.execute(
        "DELETE FROM appointment_nominations WHERE guild_id = ? AND position = ? AND user_id = ?",
        (guild_id, position, candidate_id),
    )
    if cur.rowcount == 0:
        return False

    db.commit()
    await refresh_appointment_nominees_message(
        bot=bot,
        nominees_channel=nominees_channel,
        guild_id=guild_id,
        position=position,
        nominee_message_id=appointment["nominee_message_id"],
        closes_at_iso_utc=str(appointment["nomination_closes_at"]) if appointment["nomination_closes_at"] else None,
        open_status=True,
    )
    return True


class AppointmentNominationsCommand(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db: sqlite3.Connection = _db(bot)

    async def cog_load(self):
        if not self.appointment_nomination_auto_closer.is_running():
            self.appointment_nomination_auto_closer.start()

    async def cog_unload(self):
        if self.appointment_nomination_auto_closer.is_running():
            self.appointment_nomination_auto_closer.cancel()

    @tasks.loop(seconds=30)
    async def appointment_nomination_auto_closer(self):
        # Polling loop closes tracks that have reached nomination_closes_at and
        # refreshes the public nominees embed to show CLOSED state.
        now_utc = datetime.now(timezone.utc)

        cur = self.db.cursor()
        cur.execute(
            """
            SELECT guild_id, position, nominee_message_id, nomination_closes_at
            FROM appointment_positions
            WHERE status = 'OPEN'
              AND nomination_closes_at IS NOT NULL
            """
        )
        rows = cur.fetchall()
        if not rows:
            return

        for row in rows:
            guild_id = int(row["guild_id"])
            position = str(row["position"])
            nominee_message_id = row["nominee_message_id"]
            closes_at_iso = str(row["nomination_closes_at"])

            try:
                closes_at = datetime.fromisoformat(closes_at_iso)
                if closes_at.tzinfo is None:
                    closes_at = closes_at.replace(tzinfo=timezone.utc)
                closes_at_utc = closes_at.astimezone(timezone.utc)
            except Exception:
                continue

            if closes_at_utc > now_utc:
                continue

            cur2 = self.db.cursor()
            cur2.execute(
                """
                UPDATE appointment_positions
                SET status = 'CLOSED',
                    closed_at = ?,
                    closed_by = COALESCE(closed_by, opened_by)
                WHERE guild_id = ? AND position = ? AND status = 'OPEN'
                """,
                (now_utc.isoformat(), guild_id, position)
            )
            if cur2.rowcount == 0:
                continue
            self.db.commit()

            guild = self.bot.get_guild(guild_id)
            if not guild:
                continue
            settings = get_settings(self.db, guild_id)
            if not settings:
                continue

            nominees_channel_id = settings.get("nominees_channel_id")
            if not nominees_channel_id:
                continue
            nominees_channel = guild.get_channel(int(nominees_channel_id))
            if not nominees_channel or not isinstance(nominees_channel, discord.TextChannel):
                continue

            await refresh_appointment_nominees_message(
                bot=self.bot,
                nominees_channel=nominees_channel,
                guild_id=guild_id,
                position=position,
                nominee_message_id=nominee_message_id,
                closes_at_iso_utc=closes_at_iso,
                open_status=False,
            )

    @appointment_nomination_auto_closer.before_loop
    async def before_appointment_nomination_auto_closer(self):
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="open_appointment_nominations",
        description="Open nominations for an appointment position (no public vote)",
    )
    @app_commands.describe(
        position="The appointment position (e.g., Secretary of State for Defence)",
        clear_nominees="If true, removes previous appointment nominees for this position",
        duration_hours="Optional auto-close timer in hours (e.g., 24 or 72)",
    )
    async def open_appointment_nominations(
        self,
        interaction: Interaction,
        position: str,
        clear_nominees: bool = False,
        duration_hours: app_commands.Range[int, 1, 336] | None = None,
    ):
        if not interaction.guild:
            await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        settings = get_settings(self.db, guild_id)
        if not settings:
            await interaction.response.send_message("❌ Bot not configured. Ask an admin to run **/setup**.", ephemeral=True)
            return

        if not is_admin(interaction, settings):
            await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
            return

        nominees_channel_id = settings.get("nominees_channel_id")
        if not nominees_channel_id:
            await interaction.response.send_message("❌ Nominees channel not configured. Ask an admin to run **/setup**.", ephemeral=True)
            return

        nominees_channel = interaction.guild.get_channel(int(nominees_channel_id))
        if not nominees_channel or not isinstance(nominees_channel, discord.TextChannel):
            await interaction.response.send_message("❌ Configured nominees channel not found.", ephemeral=True)
            return

        now_utc = datetime.now(timezone.utc)
        closes_at_iso = (now_utc + timedelta(hours=int(duration_hours))).isoformat() if duration_hours else None

        cur = self.db.cursor()
        cur.execute(
            """
            INSERT INTO appointment_positions (
                guild_id, position, status, nominee_message_id,
                opened_by, opened_at, nomination_closes_at, closed_by, closed_at
            )
            VALUES (?, ?, 'OPEN', NULL, ?, ?, ?, NULL, NULL)
            ON CONFLICT(guild_id, position) DO UPDATE SET
                status = 'OPEN',
                opened_by = excluded.opened_by,
                opened_at = excluded.opened_at,
                nomination_closes_at = excluded.nomination_closes_at,
                closed_by = NULL,
                closed_at = NULL
            """,
            (guild_id, position, interaction.user.id, now_utc.isoformat(), closes_at_iso),
        )

        if clear_nominees:
            cur.execute(
                "DELETE FROM appointment_nominations WHERE guild_id = ? AND position = ?",
                (guild_id, position),
            )

        self.db.commit()

        cur.execute(
            "SELECT nominee_message_id, nomination_closes_at FROM appointment_positions WHERE guild_id = ? AND position = ?",
            (guild_id, position),
        )
        row = cur.fetchone()
        nominee_message_id = row["nominee_message_id"] if row else None
        nomination_closes_at = str(row["nomination_closes_at"]) if row and row["nomination_closes_at"] else None

        await refresh_appointment_nominees_message(
            bot=self.bot,
            nominees_channel=nominees_channel,
            guild_id=guild_id,
            position=position,
            nominee_message_id=nominee_message_id,
            closes_at_iso_utc=nomination_closes_at,
            open_status=True,
        )

        close_note = (
            f"\n⏱️ Auto-close: **{utc_iso_to_london_str(nomination_closes_at)}**."
            if nomination_closes_at
            else ""
        )

        await interaction.response.send_message(
            f"✅ Appointment nominations are now open for **{position}**.\n"
            f"This track does not create a public vote.{close_note}",
            ephemeral=True,
        )

    @app_commands.command(
        name="close_appointment_nominations",
        description="Close nominations for an appointment position and DM nominees privately",
    )
    @app_commands.describe(position="The appointment position to close")
    async def close_appointment_nominations(self, interaction: Interaction, position: str):
        if not interaction.guild:
            await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        settings = get_settings(self.db, guild_id)
        if not settings:
            await interaction.response.send_message("❌ Bot not configured. Ask an admin to run **/setup**.", ephemeral=True)
            return

        if not is_admin(interaction, settings):
            await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
            return

        nominees_channel_id = settings.get("nominees_channel_id")
        if not nominees_channel_id:
            await interaction.response.send_message("❌ Nominees channel not configured. Ask an admin to run **/setup**.", ephemeral=True)
            return

        nominees_channel = interaction.guild.get_channel(int(nominees_channel_id))
        if not nominees_channel or not isinstance(nominees_channel, discord.TextChannel):
            await interaction.response.send_message("❌ Configured nominees channel not found.", ephemeral=True)
            return

        cur = self.db.cursor()
        cur.execute(
            "SELECT status, nominee_message_id, nomination_closes_at FROM appointment_positions WHERE guild_id = ? AND position = ?",
            (guild_id, position),
        )
        row = cur.fetchone()
        if not row:
            await interaction.response.send_message(
                f"❌ No appointment nomination track found for **{position}**.",
                ephemeral=True,
            )
            return

        if row["status"] == "CLOSED":
            await interaction.response.send_message(
                f"ℹ️ Appointment nominations for **{position}** are already closed.",
                ephemeral=True,
            )
            return

        now_utc = datetime.now(timezone.utc)
        cur.execute(
            """
            UPDATE appointment_positions
            SET status = 'CLOSED', closed_by = ?, closed_at = ?
            WHERE guild_id = ? AND position = ?
            """,
            (interaction.user.id, now_utc.isoformat(), guild_id, position),
        )
        self.db.commit()

        cur.execute(
            """
            SELECT user_id, display_name
            FROM appointment_nominations
            WHERE guild_id = ? AND position = ?
            ORDER BY display_name ASC
            """,
            (guild_id, position),
        )
        nominees_rows = cur.fetchall()

        await refresh_appointment_nominees_message(
            bot=self.bot,
            nominees_channel=nominees_channel,
            guild_id=guild_id,
            position=position,
            nominee_message_id=row["nominee_message_id"],
            closes_at_iso_utc=str(row["nomination_closes_at"]) if row["nomination_closes_at"] else None,
            open_status=False,
        )

        dm_embed = discord.Embed(
            title=f"📩 Appointment Nominees (Private) — {position}",
            description="Nominations are closed. Use this shortlist for private selection.",
            color=discord.Color.blurple(),
        )
        dm_embed.add_field(name="Nominees", value=build_private_nominee_lines(nominees_rows), inline=False)

        sent_count, failed_count, invoker_received = await send_nominee_summary_to_admin_role(
            interaction,
            settings,
            dm_embed,
        )

        if sent_count > 0:
            dm_note = f"✅ Sent nominee shortlist to {sent_count} admin role member(s)."
            if failed_count > 0:
                dm_note += f" ({failed_count} failed DM delivery)"
        else:
            dm_note = "⚠️ Could not DM any admin role members (check admin role config and DM settings)."

        if not invoker_received:
            try:
                await interaction.user.send(embed=dm_embed)
                dm_note += " I also DM’d you directly."
            except Exception:
                pass

        await interaction.response.send_message(
            f"✅ Appointment nominations closed for **{position}**.\n{dm_note}",
            ephemeral=True,
        )

    @app_commands.command(
        name="appointment_nominees",
        description="DM yourself the nominee list for an appointment position",
    )
    @app_commands.describe(position="The appointment position to review")
    async def appointment_nominees(self, interaction: Interaction, position: str):
        if not interaction.guild:
            await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
            return

        guild_id = interaction.guild.id
        settings = get_settings(self.db, guild_id)
        if not settings:
            await interaction.response.send_message("❌ Bot not configured. Ask an admin to run **/setup**.", ephemeral=True)
            return

        if not is_admin(interaction, settings):
            await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
            return

        cur = self.db.cursor()
        cur.execute(
            "SELECT status, nomination_closes_at FROM appointment_positions WHERE guild_id = ? AND position = ?",
            (guild_id, position),
        )
        row = cur.fetchone()
        if not row:
            await interaction.response.send_message(
                f"❌ No appointment nomination track found for **{position}**.",
                ephemeral=True,
            )
            return

        cur.execute(
            """
            SELECT user_id, display_name
            FROM appointment_nominations
            WHERE guild_id = ? AND position = ?
            ORDER BY display_name ASC
            """,
            (guild_id, position),
        )
        nominees_rows = cur.fetchall()

        dm_embed = discord.Embed(
            title=f"📩 Appointment Nominees (Private) — {position}",
            color=discord.Color.blurple(),
        )
        dm_embed.add_field(name="Status", value=str(row["status"]), inline=False)
        if row["nomination_closes_at"]:
            dm_embed.add_field(
                name="Configured close time",
                value=utc_iso_to_london_str(str(row["nomination_closes_at"])),
                inline=False,
            )
        dm_embed.add_field(name="Nominees", value=build_private_nominee_lines(nominees_rows), inline=False)

        try:
            await interaction.user.send(embed=dm_embed)
            await interaction.response.send_message("✅ I’ve DM’d you the nominees list.", ephemeral=True)
        except Exception:
            await interaction.response.send_message(
                "⚠️ I couldn’t DM you the nominees list (your DMs may be closed).",
                ephemeral=True,
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(AppointmentNominationsCommand(bot))
