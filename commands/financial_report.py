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

# REMOVED: strict line anchors (^ and $) that blocked matches with emojis or spaces
TYPE_RE = re.compile(r"Type:\s*(Deposit|Withdrawal)", re.IGNORECASE | re.MULTILINE)
# UPDATED: Now stops at the number and ignores trailing text like "500g"
AMOUNT_RE = re.compile(r"Amount:\s*([\d,.]+)", re.IGNORECASE | re.MULTILINE)
STATUS_RE = re.compile(r"Status:\s*(Pending|Completed|Rejected)", re.IGNORECASE | re.MULTILINE)
BALANCE_RE = re.compile(r"Balance\s*After:\s*([\d,.]+)", re.IGNORECASE | re.MULTILINE)


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


def _report_id_from_start(start_local: datetime, period: str = "weekly") -> str:
    if period == "weekly":
        iso = start_local.isocalendar()
        return f"{iso.year}-W{iso.week:02d}"
    if period == "monthly":
        return f"{start_local.year}-{start_local.month:02d}"
    if period == "yearly":
        return f"{start_local.year}"
    raise ValueError(f"Unsupported period: {period}")


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


def _last_completed_month_bounds(month_offset: int = 0) -> tuple[datetime, datetime]:
    # Monthly report boundaries:
    #   - month_offset=0: last completed month (e.g., if now is 2026-03-23, returns 2026-02-01 -> 2026-03-01)
    #   - month_offset=1: one month earlier (2026-01-01 -> 2026-02-01), etc.
    # This is used for /financial_report period=monthly and monthly auto-scheduling on day 1.
    # It is inclusive start, exclusive end (UTC), and uses Europe/London timezone alignment.
    now_local = datetime.now(LONDON_TZ)
    this_month_start = now_local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    target_month_number = this_month_start.month - (month_offset + 1)
    target_year = this_month_start.year

    while target_month_number <= 0:
        target_month_number += 12
        target_year -= 1

    start_local = datetime(
        target_year,
        target_month_number,
        1,
        tzinfo=LONDON_TZ,
    )

    next_month_number = this_month_start.month - month_offset
    next_year = this_month_start.year

    while next_month_number <= 0:
        next_month_number += 12
        next_year -= 1

    end_local = datetime(
        next_year,
        next_month_number,
        1,
        tzinfo=LONDON_TZ,
    )

    return start_local, end_local


def _last_completed_year_bounds(year_offset: int = 0) -> tuple[datetime, datetime]:
    # Yearly report boundaries:
    #   - year_offset=0: last completed year (e.g., on 2026-03-23 it returns 2025-01-01 -> 2026-01-01)
    #   - year_offset=1: previous completed year (2024-01-01 -> 2025-01-01), etc.
    # This is used for /financial_report period=yearly and yearly auto-scheduling on Jan 1.
    # It is inclusive start, exclusive end (UTC), and uses Europe/London timezone alignment.
    now_local = datetime.now(LONDON_TZ)
    this_year_start = now_local.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
    start_year = this_year_start.year - (year_offset + 1)

    start_local = datetime(start_year, 1, 1, tzinfo=LONDON_TZ)
    end_local = datetime(start_year + 1, 1, 1, tzinfo=LONDON_TZ)

    return start_local, end_local


def _shift_month(start_local: datetime, delta: int) -> datetime:
    year = start_local.year + (start_local.month - 1 + delta) // 12
    month = ((start_local.month - 1 + delta) % 12) + 1
    return start_local.replace(year=year, month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


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
        # Start scheduler loops for each report cadence (weekly, monthly, yearly)
        if not self.weekly_report_loop.is_running():
            self.weekly_report_loop.start()
        if not self.monthly_report_loop.is_running():
            self.monthly_report_loop.start()
        if not self.yearly_report_loop.is_running():
            self.yearly_report_loop.start()

    async def cog_unload(self):
        if self.weekly_report_loop.is_running():
            self.weekly_report_loop.cancel()
        if self.monthly_report_loop.is_running():
            self.monthly_report_loop.cancel()

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
        # Guardrail: Accepts "Nation Bank", "National Bank", etc.
        text = _message_text(message).lower()
        if "nation" not in text or "bank" not in text:
            return None

        type_match = TYPE_RE.search(text)
        amount_match = AMOUNT_RE.search(text)
        status_match = STATUS_RE.search(text)
        balance_match = BALANCE_RE.search(text)

        if not type_match or not amount_match or not status_match:
            return None

        # Clean the amount (now handles "500g" correctly)
        amount_value = _parse_amount(amount_match.group(1))
        if amount_value is None:
            return None

        balance_value = _parse_amount(balance_match.group(1)) if balance_match else None

        txn_type = type_match.group(1).strip().lower()
        status = status_match.group(1).strip().lower()
        is_nortco_metro = "nortco metro" in text

        return {
            "type": txn_type,
            "amount": abs(amount_value),
            "status": status,
            "balance_after": balance_value,
            "created_at": message.created_at,
            "message_id": message.id,
            "is_nortco_metro": is_nortco_metro,
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
        nortco_total = sum(
            t["amount"] for t in completed_transactions
            if t["type"] == "deposit" and t.get("is_nortco_metro")
        )

        nortco_daily_by_weekday = {i: 0.0 for i in range(7)}
        for t in completed_transactions:
            if t["type"] == "deposit" and t.get("is_nortco_metro"):
                dt_local = t["created_at"].astimezone(LONDON_TZ)
                nortco_daily_by_weekday[dt_local.weekday()] += t["amount"]

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
            "nortco_total": nortco_total,
            "nortco_daily_by_weekday": nortco_daily_by_weekday,
            "nortco_allocation": {
                "national_bank": nortco_total * 0.70,
                "toronto_town_bank": nortco_total * 0.20,
                "staff_wages": nortco_total * 0.10,
            },
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
        current_period: dict,
        previous_period: dict,
        previous_close: float | None,
        opening_balance: float | None,
        period: str = "weekly",
        is_preview: bool = False,
    ) -> discord.Embed:
        current_close = current_period["closing_balance"]
        net_value_change = None
        if opening_balance is not None and current_close is not None:
            net_value_change = current_close - opening_balance

        expected_closing_balance = None
        discrepancy = None
        if opening_balance is not None:
            expected_closing_balance = opening_balance + current_period["net_flow"]
        if expected_closing_balance is not None and current_close is not None:
            discrepancy = current_close - expected_closing_balance

        wow_delta = None
        if previous_close is not None and current_close is not None:
            wow_delta = current_close - previous_close

        if previous_close not in (None, 0) and wow_delta is not None:
            wow_pct_text = f"{(wow_delta / abs(previous_close)) * 100:+.2f}%"
        else:
            wow_pct_text = "N/A"

        def _pct_change(curr: float, prev: float | None) -> str:
            if prev is None or prev == 0:
                if curr == 0:
                    return "⚪ 0.00%"
                return "🟢 +∞%" if curr > 0 else "🔴 -∞%"
            delta = curr - prev
            return f"{'🟢' if delta >= 0 else '🔴'} {delta / abs(prev) * 100:+.2f}%"

        title_text = "🏦 Weekly Financial Report" if period == "weekly" else "🏦 Monthly Financial Report" if period == "monthly" else "🏦 Yearly Financial Report"
        comparison_label = "Week-over-Week" if period == "weekly" else "Month-over-Month" if period == "monthly" else "Year-over-Year"

        embed = discord.Embed(
            title=title_text,
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
                f"• Completed Deposits: **{current_period['deposit_count']}**\n"
                f"• Completed Withdrawals: **{current_period['withdrawal_count']}**\n"
                f"• Rejected/Pending (excluded): **{current_period['pending_or_rejected']}**"
            ),
            inline=False,
        )

        embed.add_field(
            name="Money Flow",
            value=(
                f"• Total Deposited: **{_fmt_money(current_period['total_deposited'], signed=True)}**\n"
                f"• Total Withdrawn: **-{_fmt_money(current_period['total_withdrawn'])}**\n"
                f"• Net Flow: **{_fmt_money(current_period['net_flow'], signed=True)}**"
            ),
            inline=False,
        )

        nortco_total = current_period.get("nortco_total", 0.0) or 0.0
        if nortco_total > 0:
            alloc = current_period.get("nortco_allocation", {})
            embed.add_field(
                name="Nortco Metro Take Allocation",
                value=(
                    f"• Nortco Metro Deposits: **{_fmt_money(nortco_total, signed=True)}**\n"
                    f"• National Bank (70%): **{_fmt_money(alloc.get('national_bank'))}**\n"
                    f"• Toronto Town Bank (20%): **{_fmt_money(alloc.get('toronto_town_bank'))}**\n"
                    f"• Staff Wages (10%): **{_fmt_money(alloc.get('staff_wages'))}**"
                ),
                inline=False,
            )

            if period == "weekly":
                current_daily = current_period.get("nortco_daily_by_weekday", {})
                previous_daily = previous_period.get("nortco_daily_by_weekday", {})
                days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
                daily_lines = []
                for i, day_name in enumerate(days):
                    day_current = current_daily.get(i, 0.0)
                    day_previous = previous_daily.get(i, 0.0)
                    pct_change = _pct_change(day_current, day_previous)
                    daily_lines.append(
                        f"• {day_name}: **{_fmt_money(day_current)}** ({pct_change})"
                    )

                embed.add_field(
                    name="Nortco Metro Daily Breakdown",
                    value="\n".join(daily_lines),
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
            name="Discrepancy",
            value=f"• Result: **{_fmt_money(discrepancy, signed=True)}**",
            inline=False,
        )

        embed.add_field(
            name=comparison_label,
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
        period: str = "weekly",
        is_preview: bool = False,
    ) -> tuple[str, discord.Embed]:
        report_id = _report_id_from_start(start_local, period=period)

        current_period = await self._collect_week_data(channel, start_local, end_local)

        if period == "weekly":
            previous_start = start_local - timedelta(days=7)
        elif period == "monthly":
            previous_start = _shift_month(start_local, -1)
        elif period == "yearly":
            previous_start = datetime(
                start_local.year - 1,
                1,
                1,
                tzinfo=LONDON_TZ,
            )
        else:
            raise ValueError(f"Unsupported period: {period}")

        previous_end = start_local
        previous_period = await self._collect_week_data(channel, previous_start, previous_end)
        previous_close = previous_period["closing_balance"]

        opening_balance = previous_close
        if opening_balance is None:
            opening_balance = await self._latest_balance_before(channel, start_local)

        embed = self._build_embed(
            report_id=report_id,
            start_local=start_local,
            end_local=end_local,
            current_period=current_period,
            previous_period=previous_period,
            previous_close=previous_close,
            opening_balance=opening_balance,
            period=period,
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
        period: str = "weekly",
    ) -> tuple[bool, str | None]:
        report_id = _report_id_from_start(start_local, period=period)

        if not force and self._report_exists(guild.id, report_id):
            return False, None

        _, embed = await self._generate_embed_for_period(channel, start_local, end_local, period=period)

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
        # Run hourly but only execute report generation at local Monday 01:00.
        # This avoids brittle long-sleep scheduling and handles restarts cleanly.
        now_local = datetime.now(LONDON_TZ)

        if now_local.weekday() != 0:
            return
        if now_local.hour != 1:
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
                    period="weekly",
                )
            except Exception as exc:
                print(f"❌ Weekly financial report failed for guild {guild.id}: {exc!r}")
                traceback.print_exception(type(exc), exc, exc.__traceback__)

    @tasks.loop(hours=1)
    async def monthly_report_loop(self):
        # Monthly auto-report scheduler:
        # - runs once per hour
        # - only processes when clock is 01:00 on day 1 (Europe/London)
        # - generates report covering the previous month (e.g., Mar 1 triggers Feb report)
        # - avoids clock drift and restart issues by re-evaluating every hour
        now_local = datetime.now(LONDON_TZ)

        if now_local.day != 1:
            return
        if now_local.hour != 1:
            return

        start_local, end_local = _last_completed_month_bounds(month_offset=0)

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
                    period="monthly",
                )
            except Exception as exc:
                print(f"❌ Monthly financial report failed for guild {guild.id}: {exc!r}")
                traceback.print_exception(type(exc), exc, exc.__traceback__)

    @tasks.loop(hours=1)
    async def yearly_report_loop(self):
        # Yearly auto-report scheduler:
        # - runs once per hour
        # - only processes at Jan 1 01:00 (Europe/London)
        # - generates report for the previous year (e.g., 2027-01-01 triggers 2026 report)
        # - avoids missing an execution if bot restarts around the boundary time
        now_local = datetime.now(LONDON_TZ)

        if now_local.month != 1 or now_local.day != 1:
            return
        if now_local.hour != 1:
            return

        start_local, end_local = _last_completed_year_bounds(year_offset=0)

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
                    period="yearly",
                )
            except Exception as exc:
                print(f"❌ Yearly financial report failed for guild {guild.id}: {exc!r}")
                traceback.print_exception(type(exc), exc, exc.__traceback__)

    @weekly_report_loop.before_loop
    async def before_weekly_report_loop(self):
        await self.bot.wait_until_ready()

    @monthly_report_loop.before_loop
    async def before_monthly_report_loop(self):
        await self.bot.wait_until_ready()

    @yearly_report_loop.before_loop
    async def before_yearly_report_loop(self):
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="financial_report",
        description="Generate a nation bank financial report"
    )
    @app_commands.describe(
        period="weekly or monthly report",
        period_offset="0 = last completed period, 1 = previous period, etc.",
        force="Re-generate even if that report already exists",
        preview="Generate privately without posting or saving report record",
    )
    @app_commands.choices(
        period=[
            app_commands.Choice(name="weekly", value="weekly"),
            app_commands.Choice(name="monthly", value="monthly"),
            app_commands.Choice(name="yearly", value="yearly"),
        ]
    )
    async def financial_report(
        self,
        interaction: discord.Interaction,
        period: app_commands.Choice[str],
        period_offset: app_commands.Range[int, 0, 12] = 0,
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

        chosen_period = period.value if isinstance(period, app_commands.Choice) else period
        offset = int(period_offset)

        if chosen_period == "monthly":
            start_local, end_local = _last_completed_month_bounds(month_offset=offset)
        elif chosen_period == "yearly":
            start_local, end_local = _last_completed_year_bounds(year_offset=offset)
        else:
            start_local, end_local = _last_completed_week_bounds(week_offset=offset)

        report_id = _report_id_from_start(start_local, period=chosen_period)

        if preview:
            _, embed = await self._generate_embed_for_period(channel, start_local, end_local, period=chosen_period, is_preview=True)
            await interaction.followup.send(
                content=f"🧪 Preview for {chosen_period} report **{report_id}** (not posted).",
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
            period=chosen_period,
        )

        if not posted and not force:
            await interaction.followup.send(
                f"ℹ️ Report **{report_id}** already exists. Use `force=True` to re-generate it.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"✅ Generated {chosen_period} financial report **{report_id}** in {channel.mention}."
            + (f"\n{jump_url}" if jump_url else ""),
            ephemeral=True,
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(FinancialReportCommand(bot))
