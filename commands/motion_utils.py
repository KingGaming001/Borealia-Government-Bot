from __future__ import annotations

from datetime import datetime, timezone

import discord


def iso_now() -> str:
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


def format_discord_time(value: str | None, relative: bool = False) -> str | None:
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


def motion_embed_color(status: str, result: str | None = None) -> discord.Color:
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


def format_voter_list(guild: discord.Guild, user_ids: list[int], limit: int = 25) -> str:
    if not user_ids:
        return "-"

    shown = []
    for uid in user_ids[:limit]:
        member = guild.get_member(uid)
        shown.append(member.mention if member else f"<@{uid}>")

    extra = len(user_ids) - len(shown)
    if extra > 0:
        shown.append(f"+ {extra} more")

    return ", ".join(shown)


def get_motion_vote_columns(db) -> set[str]:
    cur = db.cursor()
    cur.execute("PRAGMA table_info(motion_votes)")
    return {row[1] for row in cur.fetchall()}


def tally_motion(db, guild_id: int, motion_id: int) -> dict:
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
        (guild_id, motion_id),
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
