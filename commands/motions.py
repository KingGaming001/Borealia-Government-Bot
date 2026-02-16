# commands/motions.py
# ------------------------------------------------------------
# Parliament Motions (Acts, Resolutions, etc.)
#
# Features:
# - Parliament-only voting (role required)
# - Public roll-call results (who votes Yes/No/Abstain)
# - Locked votes (cannot change once cast)
# - Simple majority: Yes > No => PASSED; No > Yes => FAILED; tie => TIED
#
# Commands:
# /motion_create
# /motion_open
# /motion_vote
# /motion_close
# /motion_results
# ------------------------------------------------------------

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

import discord
from discord import app_commands
from discord.ext import commands, tasks

from config_store import get_settings, is_admin, has_king_role, has_parliament_role, has_voter_role

def iso_now() -> str:
    """UTC timestamp string for the database"""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_iso_utc(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def format_discord_time(value: str | None, relative: bool=False) -> str | None:
    dt = parse_iso_utc(value)
    if not dt:
        return None
    style = "R" if relative else "F"
    return discord.utils.format_dt(dt, style=style)


def format_utc_text(value: str | None) -> str | None:
    dt = parse_iso_utc(value)
    if not dt:
        return None
    return dt.strftime("%Y-%m-%d %H:%M UTC")


def motion_status_emoji(status: str) -> str:
    return {
        "DRAFT": "📝",
        "VOTING": "🗳️",
        "CLOSED": "📜",
    }.get(status, "📄")


def motion_embed_color(status: str, result: str | None=None) -> discord.Color:
    if status == "DRAFT":
        return discord.Color.blurple()
    if status == "VOTING":
        return discord.Color.blue()
    if status == "CLOSED":
        if result == "PASSED":
            return discord.Color.green()
        if result == "FAILED":
            return discord.Color.red()
        return discord.Color.gold()
    return discord.Color.light_grey()


def motion_value(motion, key: str):
    try:
        return motion[key]
    except (KeyError, IndexError, TypeError):
        return None


def motion_effective_result(motion, tally: dict) -> str:
    final_result = motion_value(motion, "final_result")
    return str(final_result) if final_result else tally["result"]


def assent_status_text(assent_status: str | None) -> str:
    return {
        "PENDING": "👑 Pending Royal Assent",
        "APPROVED": "👑 Royal Assent Granted",
        "REJECTED": "👑 Royal Assent Rejected",
    }.get(str(assent_status) if assent_status else "", "-")


def motion_kind(motion) -> str:
    raw = motion_value(motion, "kind")
    return str(raw).strip().lower() if raw else ""


def proposer_mention(motion) -> str:
    created_by = motion_value(motion, "created_by")
    if not created_by:
        return "Unknown"
    return f"<@{int(created_by)}>"


def format_voter_list(guild: discord.Guild, user_ids: list[int], limit: int=25) -> str:
    """
    Turns a list of user IDs into mentions, capped for readability.
    """
    if not user_ids:
        return "-"
    
    shown = []
    for uid in user_ids[:limit]:
        member = guild.get_member(uid)
        shown.append(member.mention if member else f"<@{uid}>")

    extra = len(user_ids) - len(shown)
    if extra > 0:
        shown.append(f"+ {extra} more")

    return", ".join(shown)


def get_motion_vote_columns(db) -> set[str]:
    cur = db.cursor()
    cur.execute("PRAGMA table_info(motion_votes)")
    return {row[1] for row in cur.fetchall()}


def tally_motion(db, guild_id: int, motion_id: int) -> dict:
    """
    Read all votes and compute:
    - yes/no/abstain voter lists
    - result based on simple majority (yes vs no)
    """
    c = db.cursor()
    vote_columns = get_motion_vote_columns(db)

    if "choice" in vote_columns and "vote" in vote_columns:
        vote_expr = "COALESCE(choice, vote)"
    elif "choice" in vote_columns:
        vote_expr = "choice"
    elif "vote" in vote_columns:
        vote_expr = "vote"
    else:
        raise RuntimeError("motion_votes table has no vote column")

    c.execute(
        f"""
        SELECT {vote_expr} AS choice, user_id
        FROM motion_votes
        WHERE guild_id = ? AND motion_id = ?
        ORDER BY vote_id ASC
        """,
        (guild_id, motion_id)
    )
    rows = c.fetchall()

    yes = [r["user_id"] for r in rows if r["choice"] == "yes"]
    no = [r["user_id"] for r in rows if r["choice"] == "no"]
    abstain = [r["user_id"] for r in rows if r["choice"] == "abstain"]

    if len(yes) > len(no):
        result = "PASSED"
    elif len(no) > len(yes):
        result = "FAILED"
    else:
        result = "TIED"

    return {"yes": yes, "no": no, "abstain": abstain, "result": result}


def build_motion_rollcall_embed(
    motion_id: int,
    motion,
    tally: dict,
    guild: discord.Guild,
    repeal_motion_summary: str | None = None,
    repeal_original_proposer: str | None = None,
) -> discord.Embed:
    status = str(motion["status"])
    effective_result = motion_effective_result(motion, tally)
    result_text = effective_result if status == "CLOSED" else "Pending"

    embed = discord.Embed(
        title=f"{motion_status_emoji(status)} Motion #{motion_id} — {motion['title']}",
        description=motion["text"][:3800],
        color=motion_embed_color(status, effective_result),
    )
    embed.add_field(name="Kind", value=motion["kind"], inline=True)
    embed.add_field(name="Status", value=f"{motion_status_emoji(status)} {status}", inline=True)
    embed.add_field(name="Result", value=result_text, inline=True)
    embed.add_field(name="Proposed by", value=proposer_mention(motion), inline=True)

    target_act_id = motion_value(motion, "target_act_id")
    if target_act_id:
        embed.add_field(name="Target Act", value=f"Act #{target_act_id}", inline=True)
    if repeal_motion_summary:
        embed.add_field(name="Motion", value=repeal_motion_summary, inline=False)
    if repeal_original_proposer:
        embed.add_field(name="Original Motion Proposed by", value=repeal_original_proposer, inline=False)

    embed.add_field(name=f"✅ Yes ({len(tally['yes'])})", value=format_voter_list(guild, tally["yes"]), inline=False)
    embed.add_field(name=f"❌ No ({len(tally['no'])})", value=format_voter_list(guild, tally["no"]), inline=False)
    embed.add_field(name=f"⚪ Abstain ({len(tally['abstain'])})", value=format_voter_list(guild, tally["abstain"]), inline=False)

    if status == "CLOSED" and motion_value(motion, "royal_assent_status"):
        embed.add_field(
            name="Royal Assent",
            value=assent_status_text(motion_value(motion, "royal_assent_status")),
            inline=False,
        )

    closes_at_absolute = format_discord_time(motion["closes_at"], relative=False)
    closes_at_relative = format_discord_time(motion["closes_at"], relative=True)
    closes_at_text = format_utc_text(motion["closes_at"])

    if status == "VOTING" and closes_at_absolute and closes_at_relative:
        embed.add_field(name="Closes", value=f"{closes_at_relative} ({closes_at_absolute})", inline=False)
        embed.set_footer(text=f"Closes at {closes_at_text}" if closes_at_text else "Voting in progress")
    elif closes_at_absolute:
        embed.add_field(name="Closed", value=closes_at_absolute, inline=False)
        embed.set_footer(text=f"Closed at {closes_at_text}" if closes_at_text else "Voting closed")
    elif motion["closes_at"]:
        embed.set_footer(text=f"Closes at: {motion['closes_at']}")

    return embed


def build_motion_draft_embed(motion_id: int, motion, author: discord.abc.User | discord.Member) -> discord.Embed:
    embed = discord.Embed(
        title=f"📝 Motion Draft #{motion_id} — {motion['title']}",
        description=motion["text"][:3800],
        color=motion_embed_color("DRAFT"),
    )
    embed.add_field(name="Kind", value=motion["kind"], inline=True)
    embed.add_field(name="Status", value="📝 DRAFT", inline=True)
    embed.add_field(name="Proposed by", value=author.mention, inline=True)
    embed.set_footer(text="Awaiting admin action: /motion_open")
    return embed


def get_repeal_motion_summary(db, guild_id: int, motion) -> str | None:
    target_act_id = motion_value(motion, "target_act_id")
    if not target_act_id:
        return None

    cur = db.cursor()
    cur.execute(
        """
        SELECT act_id, source_motion_id, title
        FROM acts
        WHERE guild_id = ? AND act_id = ?
        """,
        (guild_id, int(target_act_id)),
    )
    act = cur.fetchone()
    if not act:
        return f"Unknown (Act #{int(target_act_id)})"

    source_motion_id = act["source_motion_id"]
    title = str(act["title"]) if act["title"] else "Untitled"
    if source_motion_id:
        return f"#{int(source_motion_id)} — {title}"
    return f"Act #{int(target_act_id)} — {title}"


def get_repeal_original_proposer(db, guild_id: int, motion) -> str | None:
    target_act_id = motion_value(motion, "target_act_id")
    if not target_act_id:
        return None

    cur = db.cursor()
    cur.execute(
        """
        SELECT a.enacted_by_user_id, m.created_by AS source_created_by
        FROM acts a
        LEFT JOIN motions m
          ON m.guild_id = a.guild_id
         AND m.motion_id = a.source_motion_id
        WHERE a.guild_id = ? AND a.act_id = ?
        """,
        (guild_id, int(target_act_id)),
    )
    row = cur.fetchone()
    if not row:
        return None

    proposer_id = row["source_created_by"] if row["source_created_by"] else row["enacted_by_user_id"]
    if not proposer_id:
        return "Unknown"
    return f"<@{int(proposer_id)}>"

async def update_rollcall_message(bot: commands.Bot, guild: discord.Guild, motion_id: int) -> None:
    """
    Rebuild the public roll-call message embed and edit it in-place.
    """
    db = bot.db
    cur = db.cursor()

    cur.execute(
        "SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?",
        (guild.id, motion_id)
    )
    motion = cur.fetchone()
    if not motion:
        return
    
    # If no message saved, nothing to update
    if not motion["message_channel_id"] or not motion["message_id"]:
        return
    
    channel = guild.get_channel(int(motion["message_channel_id"]))
    if not isinstance(channel, discord.TextChannel):
        return
    
    try:
        msg = await channel.fetch_message(int(motion["message_id"]))
    except discord.NotFound:
        return
    
    t = tally_motion(db, guild.id, motion_id)

    repeal_motion_summary = get_repeal_motion_summary(db, guild.id, motion) if motion_kind(motion) == "repeal" else None
    repeal_original_proposer = get_repeal_original_proposer(db, guild.id, motion) if motion_kind(motion) == "repeal" else None
    embed = build_motion_rollcall_embed(
        motion_id,
        motion,
        t,
        guild,
        repeal_motion_summary=repeal_motion_summary,
        repeal_original_proposer=repeal_original_proposer,
    )

    await msg.edit(
        embed=embed,
        view=MotionVoteView(bot, motion_id) if motion["status"] == "VOTING" else None,
    )


def build_result_embed(
    motion_id: int,
    motion,
    tally: dict,
    repeal_motion_summary: str | None = None,
    repeal_original_proposer: str | None = None,
) -> discord.Embed:
    result = motion_effective_result(motion, tally)
    color = (
        discord.Color.green()
        if result == "PASSED"
        else discord.Color.red()
        if result == "FAILED"
        else discord.Color.gold()
    )
    embed = discord.Embed(
        title=f"📜 Motion #{motion_id} Concluded — {result}",
        description=motion["title"][:3800],
        color=color,
    )
    embed.add_field(name="✅ Yes", value=str(len(tally["yes"])), inline=True)
    embed.add_field(name="❌ No", value=str(len(tally["no"])), inline=True)
    embed.add_field(name="⚪ Abstain", value=str(len(tally["abstain"])), inline=True)
    embed.add_field(name="Proposed by", value=proposer_mention(motion), inline=False)

    if repeal_motion_summary:
        embed.add_field(name="Motion", value=repeal_motion_summary, inline=False)
    if repeal_original_proposer:
        embed.add_field(name="Original Motion Proposed by", value=repeal_original_proposer, inline=False)

    if result == "PASSED" and motion_value(motion, "royal_assent_status") == "PENDING":
        embed.add_field(
            name="Royal Assent",
            value="👑 Awaiting approval from the configured King role.",
            inline=False,
        )

    if motion_value(motion, "royal_assent_status") in {"APPROVED", "REJECTED"}:
        embed.add_field(
            name="Royal Assent",
            value=assent_status_text(motion_value(motion, "royal_assent_status")),
            inline=False,
        )

    return embed


def build_assent_request_embed(motion_id: int, motion) -> discord.Embed:
    embed = discord.Embed(
        title=f"👑 Royal Assent Required — Motion #{motion_id}",
        description=(
            f"**{motion['title']}** has passed Parliament and now requires Royal Assent.\n"
            "Only members with the configured King role may approve or reject."
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(name="Current Result", value="PASSED (Pending Royal Assent)", inline=False)
    embed.add_field(name="Proposed by", value=proposer_mention(motion), inline=False)
    return embed


def build_assent_decision_embed(motion_id: int, motion, decision: str, decider: discord.Member | discord.User) -> discord.Embed:
    approved = decision == "APPROVED"
    embed = discord.Embed(
        title=f"👑 Royal Assent {'Approved' if approved else 'Rejected'} — Motion #{motion_id}",
        description=f"**{motion['title']}**",
        color=discord.Color.green() if approved else discord.Color.red(),
    )
    embed.add_field(
        name="Decision",
        value="Approved (motion remains PASSED)." if approved else "Rejected (motion is marked FAILED).",
        inline=False,
    )
    embed.add_field(name="Proposed by", value=proposer_mention(motion), inline=False)
    embed.add_field(name="By", value=decider.mention, inline=False)
    return embed


def build_law_embed(motion_id: int, motion, assenter: discord.Member | discord.User) -> discord.Embed:
    embed = discord.Embed(
        title=f"📘 Enacted Law — Motion #{motion_id}: {motion['title']}",
        description=motion["text"][:3800],
        color=discord.Color.green(),
    )
    embed.add_field(name="Kind", value=motion["kind"], inline=True)
    embed.add_field(name="Source", value="Parliament Motion", inline=True)
    embed.add_field(name="Proposed by", value=proposer_mention(motion), inline=True)
    embed.add_field(name="Royal Assent", value=f"Approved by {assenter.mention}", inline=False)
    return embed


def build_repeal_embed(
    motion_id: int,
    motion,
    target_act_id: int,
    assenter: discord.Member | discord.User,
    repeal_motion_summary: str | None = None,
    repeal_original_proposer: str | None = None,
) -> discord.Embed:
    embed = discord.Embed(
        title=f"🧾 Act Repealed — Act #{target_act_id}",
        description=(
            f"The repeal motion **#{motion_id} — {motion['title']}** received Royal Assent.\n"
            f"Act #{target_act_id} is now marked as repealed."
        ),
        color=discord.Color.orange(),
    )
    if repeal_motion_summary:
        embed.add_field(name="Motion", value=repeal_motion_summary, inline=False)
    if repeal_original_proposer:
        embed.add_field(name="Original Motion Proposed by", value=repeal_original_proposer, inline=False)
    embed.add_field(name="Proposed by", value=proposer_mention(motion), inline=False)
    embed.add_field(name="Royal Assent", value=f"Approved by {assenter.mention}", inline=False)
    return embed

class MotionVoteSelect(discord.ui.Select):
    def __init__(self, bot: commands.Bot, motion_id: int):
        self.bot = bot
        self.motion_id = motion_id

        super().__init__(
            placeholder="Select your vote…",
            custom_id=f"motion_vote_select:{motion_id}",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Yes", value="yes", emoji="✅"),
                discord.SelectOption(label="No", value="no", emoji="❌"),
                discord.SelectOption(label="Abstain", value="abstain", emoji="⚪"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        await self.cast(interaction, str(self.values[0]))

    async def cast(self, interaction: discord.Interaction, choice: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Server-only.", ephemeral=True)
            
        settings = get_settings(self.bot.db, interaction.guild.id)
        if not settings:
            return await interaction.response.send_message("❌ Server not configured. Run /setup first.", ephemeral=True)
        
        # Parliament-only voting rule
        if not has_parliament_role(interaction.user, settings):
            return await interaction.response.send_message("❌ Only Parliament may vote on motions.", ephemeral=True)
        
        cur = self.bot.db.cursor()

        # Motion must be open for voting
        cur.execute(
            "SELECT status, closes_at FROM motions WHERE guild_id = ? AND motion_id = ?",
            (interaction.guild.id, self.motion_id)
        )
        motion = cur.fetchone()
        if not motion or motion["status"] != "VOTING":
            return await interaction.response.send_message("❌ Voting is not open for this motion.", ephemeral=True)

        closes_at = parse_iso_utc(motion["closes_at"])
        if closes_at and datetime.now(timezone.utc) >= closes_at:
            return await interaction.response.send_message("❌ Voting has already closed for this motion.", ephemeral=True)
        
        # Locked voting: insert once, fail if already exists
        try:
            vote_columns = get_motion_vote_columns(self.bot.db)
            params = (interaction.guild.id, self.motion_id, interaction.user.id, choice)

            if "choice" in vote_columns and "vote" in vote_columns:
                cur.execute(
                    """
                    INSERT INTO motion_votes (guild_id, motion_id, user_id, choice, vote)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (interaction.guild.id, self.motion_id, interaction.user.id, choice, choice)
                )
            elif "choice" in vote_columns:
                cur.execute(
                    """
                    INSERT INTO motion_votes (guild_id, motion_id, user_id, choice)
                    VALUES (?, ?, ?, ?)
                    """,
                    params
                )
            elif "vote" in vote_columns:
                cur.execute(
                    """
                    INSERT INTO motion_votes (guild_id, motion_id, user_id, vote)
                    VALUES (?, ?, ?, ?)
                    """,
                    params
                )
            else:
                raise RuntimeError("motion_votes table has no vote column")

            self.bot.db.commit()
        except sqlite3.IntegrityError as exc:
            message = str(exc).lower()
            if "unique constraint failed" in message:
                return await interaction.response.send_message("🔒 Your vote is already recorded and locked.", ephemeral=True)
            return await interaction.response.send_message("❌ Could not record vote due to a database constraint.", ephemeral=True)
        except (sqlite3.OperationalError, RuntimeError):
            return await interaction.response.send_message("❌ Could not record vote due to a database schema mismatch.", ephemeral=True)
        except sqlite3.DatabaseError:
            return await interaction.response.send_message("❌ Could not record vote due to a database error.", ephemeral=True)
        except Exception:
            return await interaction.response.send_message("🔒 Your vote is already recorded and locked.", ephemeral=True)
        
        # Update the public roll-call message
        await update_rollcall_message(self.bot, interaction.guild, self.motion_id)

        await interaction.response.send_message("✅ Vote recorded.", ephemeral=True)


class MotionVoteView(discord.ui.View):
    """Dropdown shown to Parliament members when they run /motion_vote."""

    def __init__(self, bot: commands.Bot, motion_id: int):
        super().__init__(timeout=None)
        self.add_item(MotionVoteSelect(bot, motion_id))


class RoyalAssentButton(discord.ui.Button):
    def __init__(self, cog: "Motions", motion_id: int, action: str):
        label = "Approve" if action == "approve" else "Reject"
        style = discord.ButtonStyle.success if action == "approve" else discord.ButtonStyle.danger
        emoji = "✅" if action == "approve" else "❌"
        super().__init__(
            label=label,
            style=style,
            emoji=emoji,
            custom_id=f"motion_assent:{action}:{motion_id}",
        )
        self.cog = cog
        self.motion_id = motion_id
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        await self.cog.handle_royal_assent(interaction, self.motion_id, self.action)


class RoyalAssentView(discord.ui.View):
    def __init__(self, cog: "Motions", motion_id: int):
        super().__init__(timeout=None)
        self.add_item(RoyalAssentButton(cog, motion_id, "approve"))
        self.add_item(RoyalAssentButton(cog, motion_id, "reject"))

class Motions(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        if not self.motion_scheduler.is_running():
            self.motion_scheduler.start()

    async def cog_unload(self):
        if self.motion_scheduler.is_running():
            self.motion_scheduler.cancel()

    @tasks.loop(seconds=30)
    async def motion_scheduler(self):
        now_utc = datetime.now(timezone.utc)
        cur = self.bot.db.cursor()
        cur.execute(
            """
            SELECT guild_id, motion_id, closes_at
            FROM motions
            WHERE status = 'VOTING' AND closes_at IS NOT NULL
            """
        )
        rows = cur.fetchall()

        for row in rows:
            closes_at = parse_iso_utc(row["closes_at"])
            if not closes_at or closes_at > now_utc:
                continue

            guild = self.bot.get_guild(int(row["guild_id"]))
            if not guild:
                continue

            await self.close_motion_and_publish_result(guild, int(row["motion_id"]))

    @motion_scheduler.before_loop
    async def before_motion_scheduler(self):
        await self.bot.wait_until_ready()
        await self.restore_open_motion_vote_views()
        await self.restore_pending_assent_views()

    async def restore_open_motion_vote_views(self):
        cur = self.bot.db.cursor()
        cur.execute(
            """
            SELECT guild_id, motion_id, message_channel_id, message_id
            FROM motions
            WHERE status = 'VOTING'
              AND message_channel_id IS NOT NULL
              AND message_id IS NOT NULL
            """
        )
        rows = cur.fetchall()

        for row in rows:
            guild = self.bot.get_guild(int(row["guild_id"]))
            if not guild:
                continue

            channel = guild.get_channel(int(row["message_channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                continue

            try:
                message = await channel.fetch_message(int(row["message_id"]))
                await message.edit(view=MotionVoteView(self.bot, int(row["motion_id"])))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue

    async def restore_pending_assent_views(self):
        cur = self.bot.db.cursor()
        cur.execute(
            """
            SELECT guild_id, motion_id, assent_channel_id, assent_message_id
            FROM motions
            WHERE status = 'CLOSED'
              AND royal_assent_status = 'PENDING'
              AND assent_channel_id IS NOT NULL
              AND assent_message_id IS NOT NULL
            """
        )
        rows = cur.fetchall()

        for row in rows:
            guild = self.bot.get_guild(int(row["guild_id"]))
            if not guild:
                continue

            channel = guild.get_channel(int(row["assent_channel_id"]))
            if not isinstance(channel, discord.TextChannel):
                continue

            try:
                message = await channel.fetch_message(int(row["assent_message_id"]))
                await message.edit(view=RoyalAssentView(self, int(row["motion_id"])))
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                continue

    async def post_royal_assent_request(self, guild: discord.Guild, motion) -> None:
        motion_id = int(motion["motion_id"])
        channel = None
        if motion["message_channel_id"]:
            channel = guild.get_channel(int(motion["message_channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return

        embed = build_assent_request_embed(motion_id, motion)
        view = RoyalAssentView(self, motion_id)

        assent_message = None
        if motion_value(motion, "assent_channel_id") and motion_value(motion, "assent_message_id"):
            saved_channel = guild.get_channel(int(motion["assent_channel_id"]))
            if isinstance(saved_channel, discord.TextChannel):
                try:
                    existing = await saved_channel.fetch_message(int(motion["assent_message_id"]))
                    await existing.edit(embed=embed, view=view)
                    assent_message = existing
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    assent_message = None

        if assent_message is None:
            assent_message = await channel.send(embed=embed, view=view)

        cur = self.bot.db.cursor()
        cur.execute(
            """
            UPDATE motions
            SET assent_channel_id = ?, assent_message_id = ?
            WHERE guild_id = ? AND motion_id = ?
            """,
            (channel.id, assent_message.id, guild.id, motion_id),
        )
        self.bot.db.commit()

    async def update_assent_message(self, guild: discord.Guild, motion, decision: str, decider: discord.Member) -> None:
        if not motion_value(motion, "assent_channel_id") or not motion_value(motion, "assent_message_id"):
            return
        channel = guild.get_channel(int(motion["assent_channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            message = await channel.fetch_message(int(motion["assent_message_id"]))
            await message.edit(embed=build_assent_decision_embed(int(motion["motion_id"]), motion, decision, decider), view=None)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return

    async def get_act(self, guild_id: int, act_id: int):
        cur = self.bot.db.cursor()
        cur.execute(
            """
            SELECT *
            FROM acts
            WHERE guild_id = ? AND act_id = ?
            """,
            (guild_id, act_id),
        )
        return cur.fetchone()

    async def register_enacted_act(self, guild_id: int, motion, enacted_by_user_id: int) -> int:
        cur = self.bot.db.cursor()

        source_motion_id = int(motion["motion_id"])
        cur.execute(
            """
            SELECT act_id
            FROM acts
            WHERE guild_id = ? AND source_motion_id = ?
            """,
            (guild_id, source_motion_id),
        )
        existing = cur.fetchone()
        if existing:
            return int(existing["act_id"])

        cur.execute(
            """
            INSERT INTO acts (guild_id, source_motion_id, title, text, enacted_by_user_id, enacted_at, status)
            VALUES (?, ?, ?, ?, ?, ?, 'ENACTED')
            """,
            (guild_id, source_motion_id, motion["title"], motion["text"], enacted_by_user_id, iso_now()),
        )
        self.bot.db.commit()
        return int(cur.lastrowid)

    async def apply_repeal_to_act(self, guild_id: int, target_act_id: int, repeal_motion_id: int, assenter_user_id: int) -> bool:
        cur = self.bot.db.cursor()
        cur.execute(
            """
            UPDATE acts
            SET status = 'REPEALED',
                repealed_by_motion_id = ?,
                repealed_by_user_id = ?,
                repealed_at = ?
            WHERE guild_id = ?
              AND act_id = ?
              AND status = 'ENACTED'
            """,
            (repeal_motion_id, assenter_user_id, iso_now(), guild_id, target_act_id),
        )
        self.bot.db.commit()
        return cur.rowcount > 0

    async def publish_law_from_motion(self, guild: discord.Guild, motion, decider: discord.Member) -> bool:
        settings = get_settings(self.bot.db, guild.id)
        if not settings or not settings.get("laws_channel_id"):
            return False
        channel = guild.get_channel(int(settings["laws_channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return False
        await channel.send(embed=build_law_embed(int(motion["motion_id"]), motion, decider))
        return True

    async def publish_repeal_from_motion(self, guild: discord.Guild, motion, target_act_id: int, decider: discord.Member) -> bool:
        settings = get_settings(self.bot.db, guild.id)
        if not settings or not settings.get("laws_channel_id"):
            return False
        channel = guild.get_channel(int(settings["laws_channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return False
        repeal_motion_summary = get_repeal_motion_summary(self.bot.db, guild.id, motion)
        repeal_original_proposer = get_repeal_original_proposer(self.bot.db, guild.id, motion)
        await channel.send(
            embed=build_repeal_embed(
                int(motion["motion_id"]),
                motion,
                target_act_id,
                decider,
                repeal_motion_summary=repeal_motion_summary,
                repeal_original_proposer=repeal_original_proposer,
            )
        )
        return True

    async def handle_royal_assent(self, interaction: discord.Interaction, motion_id: int, action: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Server-only.", ephemeral=True)

        settings = get_settings(self.bot.db, interaction.guild.id)
        if not has_king_role(interaction.user, settings):
            return await interaction.response.send_message(
                "❌ Only members with the configured King role can grant or deny Royal Assent.",
                ephemeral=True,
            )

        cur = self.bot.db.cursor()
        cur.execute(
            "SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?",
            (interaction.guild.id, motion_id),
        )
        motion = cur.fetchone()
        if not motion:
            return await interaction.response.send_message("❌ Motion not found.", ephemeral=True)
        if str(motion["status"]) != "CLOSED":
            return await interaction.response.send_message("❌ Motion is not closed yet.", ephemeral=True)
        if str(motion["royal_assent_status"] or "") != "PENDING":
            return await interaction.response.send_message("ℹ️ Royal Assent has already been finalized for this motion.", ephemeral=True)

        decision = "APPROVED" if action == "approve" else "REJECTED"
        final_result = "PASSED" if decision == "APPROVED" else "FAILED"

        cur.execute(
            """
            UPDATE motions
            SET royal_assent_status = ?,
                royal_assented_by = ?,
                royal_assented_at = ?,
                final_result = ?
            WHERE guild_id = ? AND motion_id = ? AND royal_assent_status = 'PENDING'
            """,
            (decision, interaction.user.id, iso_now(), final_result, interaction.guild.id, motion_id),
        )
        if cur.rowcount == 0:
            self.bot.db.commit()
            return await interaction.response.send_message("ℹ️ Royal Assent has already been finalized for this motion.", ephemeral=True)

        self.bot.db.commit()

        cur.execute(
            "SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?",
            (interaction.guild.id, motion_id),
        )
        updated_motion = cur.fetchone()
        if not updated_motion:
            return await interaction.response.send_message("❌ Motion state could not be loaded.", ephemeral=True)

        await self.update_assent_message(interaction.guild, updated_motion, decision, interaction.user)
        await update_rollcall_message(self.bot, interaction.guild, motion_id)

        if decision == "APPROVED":
            kind = motion_kind(updated_motion)
            if kind == "repeal":
                target_act_id = motion_value(updated_motion, "target_act_id")
                if not target_act_id:
                    return await interaction.response.send_message(
                        f"✅ Royal Assent approved for motion #{motion_id}, but no target act was set.",
                        ephemeral=True,
                    )

                repealed = await self.apply_repeal_to_act(
                    interaction.guild.id,
                    int(target_act_id),
                    int(updated_motion["motion_id"]),
                    interaction.user.id,
                )

                repeal_posted = await self.publish_repeal_from_motion(
                    interaction.guild,
                    updated_motion,
                    int(target_act_id),
                    interaction.user,
                )

                if repealed and repeal_posted:
                    return await interaction.response.send_message(
                        f"✅ Royal Assent approved for motion #{motion_id}. Act #{int(target_act_id)} has been repealed and posted in laws.",
                        ephemeral=True,
                    )
                if repealed:
                    return await interaction.response.send_message(
                        f"✅ Royal Assent approved for motion #{motion_id}. Act #{int(target_act_id)} has been repealed.",
                        ephemeral=True,
                    )
                return await interaction.response.send_message(
                    f"✅ Royal Assent approved for motion #{motion_id}, but Act #{int(target_act_id)} was already repealed or not found.",
                    ephemeral=True,
                )

            law_posted = await self.publish_law_from_motion(interaction.guild, updated_motion, interaction.user)
            if kind == "act":
                act_id = await self.register_enacted_act(interaction.guild.id, updated_motion, interaction.user.id)
                if law_posted:
                    return await interaction.response.send_message(
                        f"✅ Royal Assent approved for motion #{motion_id}. Enacted as Act #{act_id} and posted in the laws channel.",
                        ephemeral=True,
                    )
                return await interaction.response.send_message(
                    f"✅ Royal Assent approved for motion #{motion_id}. Enacted as Act #{act_id}, but no valid laws channel is configured.",
                    ephemeral=True,
                )

            if law_posted:
                return await interaction.response.send_message(
                    f"✅ Royal Assent approved for motion #{motion_id}. The motion has been posted in the laws channel.",
                    ephemeral=True,
                )
            return await interaction.response.send_message(
                f"✅ Royal Assent approved for motion #{motion_id}, but no valid laws channel is configured.",
                ephemeral=True,
            )

        return await interaction.response.send_message(
            f"✅ Royal Assent rejected for motion #{motion_id}. The motion is now marked as FAILED.",
            ephemeral=True,
        )

    async def close_motion_and_publish_result(self, guild: discord.Guild, motion_id: int):
        cur = self.bot.db.cursor()
        cur.execute(
            "SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?",
            (guild.id, motion_id)
        )
        motion = cur.fetchone()
        if not motion or motion["status"] != "VOTING":
            return None

        tally = tally_motion(self.bot.db, guild.id, motion_id)
        final_result = tally["result"]
        assent_status = "PENDING" if final_result == "PASSED" else None

        cur.execute(
            """
            UPDATE motions
            SET status = 'CLOSED',
                final_result = ?,
                royal_assent_status = ?,
                royal_assented_by = NULL,
                royal_assented_at = NULL
            WHERE guild_id = ? AND motion_id = ? AND status = 'VOTING'
            """,
            (final_result, assent_status, guild.id, motion_id)
        )
        if cur.rowcount == 0:
            return None
        self.bot.db.commit()

        cur.execute(
            "SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?",
            (guild.id, motion_id)
        )
        closed_motion = cur.fetchone()
        if not closed_motion:
            return None

        await update_rollcall_message(self.bot, guild, motion_id)

        if closed_motion["message_channel_id"]:
            channel = guild.get_channel(int(closed_motion["message_channel_id"]))
            if isinstance(channel, discord.TextChannel):
                repeal_motion_summary = get_repeal_motion_summary(self.bot.db, guild.id, closed_motion) if motion_kind(closed_motion) == "repeal" else None
                repeal_original_proposer = get_repeal_original_proposer(self.bot.db, guild.id, closed_motion) if motion_kind(closed_motion) == "repeal" else None
                await channel.send(
                    embed=build_result_embed(
                        motion_id,
                        closed_motion,
                        tally,
                        repeal_motion_summary=repeal_motion_summary,
                        repeal_original_proposer=repeal_original_proposer,
                    )
                )

        if str(closed_motion["royal_assent_status"] or "") == "PENDING":
            await self.post_royal_assent_request(guild, closed_motion)

        return {"motion": closed_motion, "tally": tally}
    
    # ----------------------------
    # /motion_create
    # ----------------------------
    @app_commands.command(name="motion_create", description="Create a Parliament motion (draft).")
    @app_commands.guild_only()
    @app_commands.describe(kind="act/resolution/confidence/etc", title="Short title", text="Full text")
    async def motion_create(self, interaction: discord.Interaction, kind: str, title: str, text: str):
        settings = get_settings(self.bot.db, interaction.guild.id)
        is_voter = isinstance(interaction.user, discord.Member) and has_voter_role(interaction.user, settings)
        if not (is_admin(interaction, settings) or is_voter):
            return await interaction.response.send_message("❌ Only admins or users with the voter role can create drafts.", ephemeral=True)

        cur = self.bot.db.cursor()
        cur.execute(
            """
            INSERT INTO motions (guild_id, kind, title, text, created_by, created_at, status, opens_at, closes_at, public_votes, target_act_id)
            VALUES (?, ?, ?, ?, ?, ?, 'DRAFT', NULL, NULL, 1, NULL)
            """,
            (interaction.guild.id, kind, title, text, interaction.user.id, iso_now())
        )
        self.bot.db.commit()

        motion_id = cur.lastrowid

        cur.execute(
            "SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?",
            (interaction.guild.id, motion_id)
        )
        motion = cur.fetchone()

        posted_preview = False
        if settings and settings.get("parliament_channel_id"):
            channel = interaction.guild.get_channel(int(settings["parliament_channel_id"]))
            if isinstance(channel, discord.TextChannel) and motion:
                draft_embed = build_motion_draft_embed(motion_id, motion, interaction.user)
                draft_msg = await channel.send(embed=draft_embed)
                cur.execute(
                    """
                    UPDATE motions
                    SET message_channel_id = ?, message_id = ?
                    WHERE guild_id = ? AND motion_id = ?
                    """,
                    (channel.id, draft_msg.id, interaction.guild.id, motion_id)
                )
                self.bot.db.commit()
                posted_preview = True

        await interaction.response.send_message(
            (
                f"✅ Motion #{motion_id} created as **DRAFT**"
                + (" and posted in the Parliament channel." if posted_preview else ".")
                + f"\nUse `/motion_open {motion_id}` to start voting."
            ),
            ephemeral=True
        )

    # ----------------------------
    # /motion_repeal
    # ----------------------------
    @app_commands.command(name="motion_repeal", description="Create a repeal motion for an enacted act.")
    @app_commands.guild_only()
    @app_commands.describe(act_id="The enacted act number to repeal", reason="Reason for repeal")
    async def motion_repeal(self, interaction: discord.Interaction, act_id: int, reason: str):
        settings = get_settings(self.bot.db, interaction.guild.id)
        is_voter = isinstance(interaction.user, discord.Member) and has_voter_role(interaction.user, settings)
        if not (is_admin(interaction, settings) or is_voter):
            return await interaction.response.send_message("❌ Only admins or users with the voter role can create repeal motions.", ephemeral=True)

        act = await self.get_act(interaction.guild.id, act_id)
        if not act:
            return await interaction.response.send_message(f"❌ Act #{act_id} was not found.", ephemeral=True)
        if str(act["status"]) != "ENACTED":
            return await interaction.response.send_message(f"❌ Act #{act_id} is already repealed.", ephemeral=True)

        repeal_title = f"Repeal Act #{act_id} — {act['title']}"
        repeal_text = (
            f"This motion proposes to repeal **Act #{act_id}: {act['title']}**.\n\n"
            f"Reason:\n{reason}\n\n"
            f"Original Act Text:\n{str(act['text'])[:2400]}"
        )

        cur = self.bot.db.cursor()
        cur.execute(
            """
            INSERT INTO motions (guild_id, kind, title, text, created_by, created_at, status, opens_at, closes_at, public_votes, target_act_id)
            VALUES (?, 'repeal', ?, ?, ?, ?, 'DRAFT', NULL, NULL, 1, ?)
            """,
            (interaction.guild.id, repeal_title, repeal_text, interaction.user.id, iso_now(), act_id),
        )
        self.bot.db.commit()

        motion_id = cur.lastrowid

        cur.execute(
            "SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?",
            (interaction.guild.id, motion_id),
        )
        motion = cur.fetchone()

        posted_preview = False
        if settings and settings.get("parliament_channel_id"):
            channel = interaction.guild.get_channel(int(settings["parliament_channel_id"]))
            if isinstance(channel, discord.TextChannel) and motion:
                draft_embed = build_motion_draft_embed(motion_id, motion, interaction.user)
                draft_msg = await channel.send(embed=draft_embed)
                cur.execute(
                    """
                    UPDATE motions
                    SET message_channel_id = ?, message_id = ?
                    WHERE guild_id = ? AND motion_id = ?
                    """,
                    (channel.id, draft_msg.id, interaction.guild.id, motion_id),
                )
                self.bot.db.commit()
                posted_preview = True

        await interaction.response.send_message(
            (
                f"✅ Repeal motion #{motion_id} created for Act #{act_id} as **DRAFT**"
                + (" and posted in the Parliament channel." if posted_preview else ".")
                + f"\nUse `/motion_open {motion_id}` to start voting."
            ),
            ephemeral=True,
        )

    # ----------------------------
    # /motion_open
    # ----------------------------
    @app_commands.command(name="motion_open", description="Open voting on a motion and post the roll-call.")
    @app_commands.guild_only()
    @app_commands.describe(motion_id="The motion number")
    async def motion_open(self, interaction: discord.Interaction, motion_id: int):
        settings = get_settings(self.bot.db, interaction.guild.id)
        if not is_admin(interaction, settings):
            return await interaction.response.send_message("❌ Admins only.", ephemeral=True)

        if not settings or not settings.get("parliament_channel_id"):
            return await interaction.response.send_message(
                "❌ Parliament channel not set. Run `/setup` and set `parliament_channel`.",
                ephemeral=True
            )

        cur = self.bot.db.cursor()

        # Must exist and be draft
        cur.execute(
            "SELECT status FROM motions WHERE guild_id = ? AND motion_id = ?",
            (interaction.guild.id, motion_id)
        )
        row = cur.fetchone()
        if not row:
            return await interaction.response.send_message("❌ Motion not found.", ephemeral=True)
        if row["status"] != "DRAFT":
            return await interaction.response.send_message("❌ Motion is not in DRAFT state.", ephemeral=True)

        opens_at = iso_now()
        closes_at = (datetime.now(timezone.utc) + timedelta(hours=24)).replace(microsecond=0).isoformat()

        cur.execute(
            """
            UPDATE motions
            SET status = 'VOTING', opens_at = ?, closes_at = ?
            WHERE guild_id = ? AND motion_id = ?
            """,
            (opens_at, closes_at, interaction.guild.id, motion_id)
        )
        self.bot.db.commit()

        # Post or update the public roll-call embed
        channel = interaction.guild.get_channel(int(settings["parliament_channel_id"]))
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Configured parliament channel is invalid.", ephemeral=True)

        cur.execute(
            "SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?",
            (interaction.guild.id, motion_id)
        )
        motion = cur.fetchone()

        empty_tally = {"yes": [], "no": [], "abstain": [], "result": "TIED"}
        repeal_motion_summary = get_repeal_motion_summary(self.bot.db, interaction.guild.id, motion) if motion_kind(motion) == "repeal" else None
        repeal_original_proposer = get_repeal_original_proposer(self.bot.db, interaction.guild.id, motion) if motion_kind(motion) == "repeal" else None
        embed = build_motion_rollcall_embed(
            motion_id,
            motion,
            empty_tally,
            interaction.guild,
            repeal_motion_summary=repeal_motion_summary,
            repeal_original_proposer=repeal_original_proposer,
        )

        msg = None
        if motion["message_channel_id"] and motion["message_id"] and int(motion["message_channel_id"]) == channel.id:
            try:
                existing = await channel.fetch_message(int(motion["message_id"]))
                await existing.edit(embed=embed, view=MotionVoteView(self.bot, motion_id))
                msg = existing
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                msg = None

        if msg is None:
            msg = await channel.send(embed=embed, view=MotionVoteView(self.bot, motion_id))

        # Save message reference so we can update it later
        cur.execute(
            """
            UPDATE motions
            SET message_channel_id = ?, message_id = ?
            WHERE guild_id = ? AND motion_id = ?
            """,
            (channel.id, msg.id, interaction.guild.id, motion_id)
        )
        self.bot.db.commit()

        # Fill in initial tallies
        await update_rollcall_message(self.bot, interaction.guild, motion_id)

        await interaction.response.send_message(
            f"✅ Voting opened for motion #{motion_id}. It will close automatically in 24 hours.",
            ephemeral=True
        )

    # ----------------------------
    # /motion_vote
    # ----------------------------
    @app_commands.command(name="motion_vote", description="Vote on a Parliament motion (Parliament only).")
    @app_commands.guild_only()
    @app_commands.describe(motion_id="The motion number")
    async def motion_vote(self, interaction: discord.Interaction, motion_id: int):
        # Voting UI is ephemeral so only the user sees the dropdown,
        # but the roll-call message is public.
        view = MotionVoteView(self.bot, motion_id)
        await interaction.response.send_message(
            f"Cast your vote on motion #{motion_id}:",
            view=view,
            ephemeral=True
        )

    # ----------------------------
    # /motion_close
    # ----------------------------
    @app_commands.command(name="motion_close", description="Close voting on a motion and publish final result.")
    @app_commands.guild_only()
    @app_commands.describe(motion_id="The motion number")
    async def motion_close(self, interaction: discord.Interaction, motion_id: int):
        settings = get_settings(self.bot.db, interaction.guild.id)
        if not is_admin(interaction, settings):
            return await interaction.response.send_message("❌ Admins only.", ephemeral=True)

        cur = self.bot.db.cursor()
        cur.execute(
            "SELECT status FROM motions WHERE guild_id = ? AND motion_id = ?",
            (interaction.guild.id, motion_id)
        )
        row = cur.fetchone()
        if not row:
            return await interaction.response.send_message("❌ Motion not found.", ephemeral=True)
        if row["status"] != "VOTING":
            return await interaction.response.send_message("❌ Motion is not currently open for voting.", ephemeral=True)

        result = await self.close_motion_and_publish_result(interaction.guild, motion_id)
        if not result:
            return await interaction.response.send_message("❌ Motion could not be closed.", ephemeral=True)

        t = result["tally"]
        await interaction.response.send_message(
            f"✅ Motion #{motion_id} closed. Result: **{t['result']}** "
            f"(Yes {len(t['yes'])} / No {len(t['no'])} / Abstain {len(t['abstain'])}).",
            ephemeral=True
        )

    # ----------------------------
    # /motion_results
    # ----------------------------
    @app_commands.command(name="motion_results", description="Show current or final motion results.")
    @app_commands.guild_only()
    @app_commands.describe(motion_id="The motion number")
    async def motion_results(self, interaction: discord.Interaction, motion_id: int):
        cur = self.bot.db.cursor()
        cur.execute(
            "SELECT * FROM motions WHERE guild_id = ? AND motion_id = ?",
            (interaction.guild.id, motion_id)
        )
        motion = cur.fetchone()
        if not motion:
            return await interaction.response.send_message("❌ Motion not found.", ephemeral=True)

        t = tally_motion(self.bot.db, interaction.guild.id, motion_id)

        repeal_motion_summary = get_repeal_motion_summary(self.bot.db, interaction.guild.id, motion) if motion_kind(motion) == "repeal" else None
        repeal_original_proposer = get_repeal_original_proposer(self.bot.db, interaction.guild.id, motion) if motion_kind(motion) == "repeal" else None
        embed = build_motion_rollcall_embed(
            motion_id,
            motion,
            t,
            interaction.guild,
            repeal_motion_summary=repeal_motion_summary,
            repeal_original_proposer=repeal_original_proposer,
        )

        await interaction.response.send_message(embed=embed)


async def setup(bot: commands.Bot):
    """Called by main.py's extension loader."""
    await bot.add_cog(Motions(bot))