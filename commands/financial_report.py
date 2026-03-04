from __future__ import annotations

# Weekly financial reporting pipeline:
# - parses Nation Bank transaction messages from a configured channel
# - computes weekly totals/balances in Europe/London week boundaries
# - supports scheduled auto-post and manual on-demand generation

import re
import traceback
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any, cast
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config_store import get_settings, is_admin

LONDON_TZ = ZoneInfo("Europe/London")

TYPE_RE = re.compile(r"^\s*Type:\s*(Deposit|Withdrawal)\s*$", re.IGNORECASE | re.MULTILINE)
AMOUNT_RE = re.compile(r"^\s*Amount:\s*([^\n\r]+)$", re.IGNORECASE | re.MULTILINE)
STATUS_RE = re.compile(r"^\s*Status:\s*(Pending|Completed|Rejected)\s*$", re.IGNORECASE | re.MULTILINE)
BALANCE_RE = re.compile(r"^\s*Balance\s*After:\s*([^\n\r]+)$", re.IGNORECASE | re.MULTILINE)


def _parse_amount(raw: str) -> float | None:
    cleaned = re.sub(r"[^0-9.\-]", "", raw or "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _message_text(message: discord.Message) -> str:
    # The transaction bot sometimes stores data in embed fields rather than
    # plain content, so we normalize all text sources into one searchable blob.
    parts: list[str] = []
    if message.content:
        parts.append(message.content)

    for embed in message.embeds:
        if embed.title:
            parts.append(embed.title)
        if embed.description:
            parts.append(embed.description)
        for field in embed.fields:
            parts.append(f"{field.name}: {field.value}")

    return "\n".join(parts)


def _report_id_from_start(start_local: datetime) -> str:
    iso = start_local.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _last_completed_week_bounds(week_offset: int = 0) -> tuple[datetime, datetime]:
    # Week window is Monday 00:00 -> next Monday 00:00 in London time.
    # week_offset=0 means "last completed week".
    now_local = datetime.now(LONDON_TZ)
    this_week_start = (now_local - timedelta(days=now_local.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    start_local = this_week_start - timedelta(weeks=week_offset + 1)
    end_local = start_local + timedelta(days=7)
    return start_local, end_local


def _fmt_money(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "N/A"
    if signed:
        return f"{value:+,.2f}"
    return f"{value:,.2f}"


def _trend_indicator(delta: float | None) -> str:
    if delta is None:
        return "⚪ →"
    if delta > 0:
        return "🟢 ↑"
    if delta < 0:
        return "🔴 ↓"
    return "⚪ →"


class FinancialReportCommand(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db: sqlite3.Connection = cast(Any, bot).db
        if not self.weekly_report_loop.is_running():
            self.weekly_report_loop.start()

    async def cog_unload(self):
        if self.weekly_report_loop.is_running():
            self.weekly_report_loop.cancel()

    def _report_exists(self, guild_id: int, report_id: str) -> bool:
        cur = self.db.cursor()
        cur.execute(
            """
            SELECT 1
            FROM financial_reports
            WHERE guild_id = ? AND report_id = ?
            """,
            (guild_id, report_id),
        )
        return cur.fetchone() is not None

    def _record_report(
        self,
        guild_id: int,
        report_id: str,
        start_local: datetime,
        end_local: datetime,
        message_id: int,
        generated_by: int | None,
        mode: str,
    ) -> None:
        cur = self.db.cursor()
        cur.execute(
            """
            INSERT INTO financial_reports (
                guild_id,
                report_id,
                report_start_at,
                report_end_at,
                message_id,
                generated_by,
                mode,
                generated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, report_id) DO UPDATE SET
                report_start_at = excluded.report_start_at,
                report_end_at = excluded.report_end_at,
                message_id = excluded.message_id,
                generated_by = excluded.generated_by,
                mode = excluded.mode,
                generated_at = excluded.generated_at
            """,
            (
                guild_id,
                report_id,
                start_local.isoformat(),
                end_local.isoformat(),
                message_id,
                generated_by,
                mode,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        self.db.commit()

    def _parse_transaction(self, message: discord.Message) -> dict | None:
        # Guardrail so unrelated channel chatter does not pollute the report.
        text = _message_text(message)
        if "nation bank transaction" not in text.lower():
            return None

        type_match = TYPE_RE.search(text)
        amount_match = AMOUNT_RE.search(text)
        status_match = STATUS_RE.search(text)
        balance_match = BALANCE_RE.search(text)

        if not type_match or not amount_match or not status_match:
            return None

        amount_value = _parse_amount(amount_match.group(1))
        if amount_value is None:
            return None

        balance_value = _parse_amount(balance_match.group(1)) if balance_match else None

        txn_type = type_match.group(1).strip().lower()
        status = status_match.group(1).strip().lower()

        return {
            "type": txn_type,
            "amount": abs(amount_value),
            "status": status,
            "balance_after": balance_value,
            "created_at": message.created_at,
            "message_id": message.id,
        }

    async def _collect_week_data(
        self,
        channel: discord.TextChannel,
        start_local: datetime,
        end_local: datetime,
    ) -> dict:
        start_utc = start_local.astimezone(timezone.utc)
        end_utc = end_local.astimezone(timezone.utc)

        parsed_total = 0
        completed_transactions: list[dict] = []
        pending_or_rejected = 0

        async for message in channel.history(after=start_utc, before=end_utc, oldest_first=True, limit=None):
            txn = self._parse_transaction(message)
            if not txn:
                continue

            parsed_total += 1
            if txn["status"] != "completed":
                pending_or_rejected += 1
                continue

            completed_transactions.append(txn)

        total_deposited = sum(t["amount"] for t in completed_transactions if t["type"] == "deposit")
        total_withdrawn = sum(t["amount"] for t in completed_transactions if t["type"] == "withdrawal")

        closing_balance = None
        for txn in reversed(completed_transactions):
            if txn["balance_after"] is not None:
                closing_balance = txn["balance_after"]
                break

        return {
            "parsed_total": parsed_total,
            "completed_count": len(completed_transactions),
            "pending_or_rejected": pending_or_rejected,
            "deposit_count": sum(1 for t in completed_transactions if t["type"] == "deposit"),
            "withdrawal_count": sum(1 for t in completed_transactions if t["type"] == "withdrawal"),
            "total_deposited": total_deposited,
            "total_withdrawn": total_withdrawn,
            "net_flow": total_deposited - total_withdrawn,
            "closing_balance": closing_balance,
        }

    async def _latest_balance_before(
        self,
        channel: discord.TextChannel,
        start_local: datetime,
    ) -> float | None:
        start_utc = start_local.astimezone(timezone.utc)
        async for message in channel.history(before=start_utc, oldest_first=False, limit=None):
            txn = self._parse_transaction(message)
            if not txn:
                continue
            if txn["status"] == "completed" and txn["balance_after"] is not None:
                return txn["balance_after"]
        return None

    def _build_embed(
        self,
        report_id: str,
        start_local: datetime,
        end_local: datetime,
        current_week: dict,
        previous_close: float | None,
        opening_balance: float | None,
        is_preview: bool = False,
    ) -> discord.Embed:
        current_close = current_week["closing_balance"]
        net_value_change = None
        if opening_balance is not None and current_close is not None:
            net_value_change = current_close - opening_balance

        wow_delta = None
        if previous_close is not None and current_close is not None:
            wow_delta = current_close - previous_close

        if previous_close not in (None, 0) and wow_delta is not None:
            wow_pct_text = f"{(wow_delta / abs(previous_close)) * 100:+.2f}%"
        else:
            wow_pct_text = "N/A"

        embed = discord.Embed(
            title="🏦 Weekly Financial Report",
            description=(
                f"**Period:** {start_local.strftime('%Y-%m-%d %H:%M %Z')} → "
                f"{(end_local - timedelta(seconds=1)).strftime('%Y-%m-%d %H:%M %Z')}"
            ),
            color=discord.Color.green() if (wow_delta or 0) > 0 else discord.Color.red() if (wow_delta or 0) < 0 else discord.Color.light_grey(),
            timestamp=datetime.now(timezone.utc),
        )

        embed.add_field(
            name="Transactions",
            value=(
                f"• Completed Deposits: **{current_week['deposit_count']}**\n"
                f"• Completed Withdrawals: **{current_week['withdrawal_count']}**\n"
                f"• Rejected/Pending (excluded): **{current_week['pending_or_rejected']}**"
            ),
            inline=False,
        )

        embed.add_field(
            name="Money Flow",
            value=(
                f"• Total Deposited: **{_fmt_money(current_week['total_deposited'], signed=True)}**\n"
                f"• Total Withdrawn: **-{_fmt_money(current_week['total_withdrawn'])}**\n"
                f"• Net Flow: **{_fmt_money(current_week['net_flow'], signed=True)}**"
            ),
            inline=False,
        )

        embed.add_field(
            name="Balance Snapshot",
            value=(
                f"• Opening Balance: **{_fmt_money(opening_balance)}**\n"
                f"• Closing Balance: **{_fmt_money(current_close)}**\n"
                f"• Net Value Change: **{_fmt_money(net_value_change, signed=True)}**"
            ),
            inline=False,
        )

        embed.add_field(
            name="Week-over-Week",
            value=(
                f"• Previous Closing Balance: **{_fmt_money(previous_close)}**\n"
                f"• Current Closing Balance: **{_fmt_money(current_close)}**\n"
                f"• WoW Change: **{_fmt_money(wow_delta, signed=True)}** ({_trend_indicator(wow_delta)} {wow_pct_text})"
            ),
            inline=False,
        )

        footer_text = f"Report ID: {report_id} • Data source: Nation Bank transactions"
        if is_preview:
            footer_text = f"{footer_text} • PREVIEW"
        embed.set_footer(text=footer_text)
        return embed

    async def _generate_embed_for_period(
        self,
        channel: discord.TextChannel,
        start_local: datetime,
        end_local: datetime,
        is_preview: bool = False,
    ) -> tuple[str, discord.Embed]:
        report_id = _report_id_from_start(start_local)

        current_week = await self._collect_week_data(channel, start_local, end_local)

        previous_start = start_local - timedelta(days=7)
        previous_end = start_local
        previous_week = await self._collect_week_data(channel, previous_start, previous_end)
        previous_close = previous_week["closing_balance"]

        opening_balance = previous_close
        if opening_balance is None:
            opening_balance = await self._latest_balance_before(channel, start_local)

        embed = self._build_embed(
            report_id=report_id,
            start_local=start_local,
            end_local=end_local,
            current_week=current_week,
            previous_close=previous_close,
            opening_balance=opening_balance,
            is_preview=is_preview,
        )

        return report_id, embed

    async def _generate_and_post(
        self,
        guild: discord.Guild,
        channel: discord.TextChannel,
        start_local: datetime,
        end_local: datetime,
        mode: str,
        generated_by: int | None,
        force: bool,
    ) -> tuple[bool, str | None]:
        report_id = _report_id_from_start(start_local)

        if not force and self._report_exists(guild.id, report_id):
            return False, None

        _, embed = await self._generate_embed_for_period(channel, start_local, end_local)

        sent = await channel.send(embed=embed)

        self._record_report(
            guild_id=guild.id,
            report_id=report_id,
            start_local=start_local,
            end_local=end_local,
            message_id=sent.id,
            generated_by=generated_by,
            mode=mode,
        )

        return True, sent.jump_url

    @tasks.loop(hours=1)
    async def weekly_report_loop(self):
        # Run hourly but only execute report generation at local Monday 00:00.
        # This avoids brittle long-sleep scheduling and handles restarts cleanly.
        now_local = datetime.now(LONDON_TZ)

        if now_local.weekday() != 0:
            return
        if now_local.hour != 0:
            return

        start_local, end_local = _last_completed_week_bounds(week_offset=0)

        for guild in self.bot.guilds:
            try:
                settings = get_settings(self.db, guild.id)
                if not settings:
                    continue

                bank_channel_id = settings.get("bank_transactions_channel_id")
                if not bank_channel_id:
                    continue

                channel = guild.get_channel(int(bank_channel_id))
                if not isinstance(channel, discord.TextChannel):
                    continue

                await self._generate_and_post(
                    guild=guild,
                    channel=channel,
                    start_local=start_local,
                    end_local=end_local,
                    mode="AUTO",
                    generated_by=None,
                    force=False,
                )
            except Exception as exc:
                print(f"❌ Weekly financial report failed for guild {guild.id}: {exc!r}")
                traceback.print_exception(type(exc), exc, exc.__traceback__)

    @weekly_report_loop.before_loop
    async def before_weekly_report_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="financial_report",
        description="Generate a weekly nation bank financial report"
    )
    @app_commands.describe(
        week_offset="0 = last completed week, 1 = week before, etc.",
        force="Re-generate even if that week's report already exists",
        preview="Generate privately without posting or saving report record",
    )
    async def financial_report(
        self,
        interaction: discord.Interaction,
        week_offset: app_commands.Range[int, 0, 12] = 0,
        force: bool = False,
        preview: bool = False,
    ):
        if not interaction.guild:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.",
                ephemeral=True,
            )
            return

        settings = get_settings(self.db, interaction.guild.id)
        if not settings:
            await interaction.response.send_message(
                "❌ Bot is not configured. Run /setup first.",
                ephemeral=True,
            )
            return

        if not is_admin(interaction, settings):
            await interaction.response.send_message(
                "❌ You do not have permission to use this command.",
                ephemeral=True,
            )
            return

        bank_channel_id = settings.get("bank_transactions_channel_id")
        if not bank_channel_id:
            await interaction.response.send_message(
                "❌ Bank transactions channel is not configured. Run /setup again.",
                ephemeral=True,
            )
            return

        channel = interaction.guild.get_channel(int(bank_channel_id))
        if not isinstance(channel, discord.TextChannel):
            await interaction.response.send_message(
                "❌ Configured bank transactions channel is invalid.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        start_local, end_local = _last_completed_week_bounds(week_offset=int(week_offset))
        report_id = _report_id_from_start(start_local)

        if preview:
            _, embed = await self._generate_embed_for_period(channel, start_local, end_local, is_preview=True)
            await interaction.followup.send(
                content=f"🧪 Preview for weekly report **{report_id}** (not posted).",
                embed=embed,
                ephemeral=True,
            )
            return

        posted, jump_url = await self._generate_and_post(
            guild=interaction.guild,
            channel=channel,
            start_local=start_local,
            end_local=end_local,
            mode="MANUAL",
            generated_by=interaction.user.id,
            force=force,
        )

        if not posted and not force:
            await interaction.followup.send(
                f"ℹ️ Report **{report_id}** already exists. Use `force=True` to re-generate it.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Generated weekly financial report **{report_id}** in {channel.mention}."
            + (f"\n{jump_url}" if jump_url else ""),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(FinancialReportCommand(bot))
