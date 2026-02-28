# main.py
# ============================================================
# Borealia Government Bot
#
# Responsibilities:
# - Load environment variables (.env)
# - Create bot + intents
# - Initialise database
# - Load all command modules
# - Sync slash commands (single scope to avoid duplicates)
# - Run election scheduler:
#     SCHEDULED -> VOTING when start_at is reached
#     Posts voting dropdown in elections channel
#
# Updates included:
# 1) Prevent duplicate slash commands (/status appearing multiple times)
# 2) Lock votes:
#    - Once a voter votes, they cannot change their vote
# ============================================================

from __future__ import annotations

import os
import hashlib
import traceback
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

import config
from db import get_db, init_db
from config_store import get_settings, has_voter_role, is_admin

LONDON_TZ = ZoneInfo("Europe/London")

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("❌ DISCORD_TOKEN not found. Check your .env file.")

# ------------------------------------------------------------
# Intents
# members=True is required for role checks
# ------------------------------------------------------------
intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ------------------------------------------------------------
# Database setup
# ------------------------------------------------------------
bot.db = get_db()
init_db(bot.db)

# ============================================================
# Voting UI (created ONLY when an election enters VOTING)
# ============================================================

class VoteSelect(discord.ui.Select):
    def __init__(
        self,
        bot: commands.Bot,
        guild_id: int,
        position: str,
        candidates: list[dict],
        settings: dict
    ):
        self.bot = bot
        self.guild_id = guild_id
        self.position = position
        self.candidates = candidates
        self.settings = settings
        position_hash = hashlib.sha1(position.encode("utf-8")).hexdigest()[:12]
        custom_id = f"vote:{guild_id}:{position_hash}"

        options = []
        for c in candidates:
            options.append(
                discord.SelectOption(
                    label=c["display_name"],
                    description=f"Vote for {c['display_name']}",
                    value=str(c["user_id"])  # candidate user_id
                )
            )

        super().__init__(
            placeholder="Select a candidate to vote for…",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=custom_id,
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            if not interaction.guild or not isinstance(interaction.user, discord.Member):
                await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)
                return

            if not has_voter_role(interaction.user, self.settings):
                await interaction.response.send_message(
                    "❌ You do not have the voter role required to vote in this election.",
                    ephemeral=True
                )
                return

            candidate_id = int(self.values[0])

            cur = self.bot.db.cursor()
            cur.execute(
                "SELECT status FROM elections WHERE guild_id = ? AND position = ?",
                (self.guild_id, self.position)
            )
            row = cur.fetchone()
            if not row or row["status"] != "VOTING":
                await interaction.response.send_message(
                    "❌ This election is not currently open for voting.",
                    ephemeral=True
                )
                return

            cur.execute(
                "SELECT candidate_id FROM votes WHERE guild_id = ? AND position = ? AND voter_id = ?",
                (self.guild_id, self.position, interaction.user.id)
            )
            existing = cur.fetchone()
            if existing:
                await interaction.response.send_message(
                    "❌ Your vote is already recorded and cannot be changed.",
                    ephemeral=True
                )
                return

            cur.execute(
                """
                INSERT INTO votes (guild_id, position, voter_id, candidate_id)
                VALUES (?, ?, ?, ?)
                """,
                (self.guild_id, self.position, interaction.user.id, candidate_id)
            )
            self.bot.db.commit()

            chosen = next((c for c in self.candidates if int(c["user_id"]) == candidate_id), None)
            chosen_name = chosen["display_name"] if chosen else "that candidate"

            await interaction.response.send_message(
                f"✅ Your vote for **{chosen_name}** has been recorded. (Votes are private.)",
                ephemeral=True
            )
        except Exception as exc:
            print(f"❌ Vote dropdown error ({self.guild_id}/{self.position}): {exc!r}")
            traceback.print_exception(type(exc), exc, exc.__traceback__)
            message = "❌ Could not record your vote due to a temporary error. Please try again."
            try:
                if interaction.response.is_done():
                    await interaction.followup.send(message, ephemeral=True)
                else:
                    await interaction.response.send_message(message, ephemeral=True)
            except Exception:
                pass

class VoteView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        guild_id: int,
        position: str,
        candidates: list[dict],
        settings: dict
    ):
        super().__init__(timeout=None)
        self.add_item(VoteSelect(bot, guild_id, position, candidates, settings))


def restore_active_election_views() -> int:
    """
    Re-attach persistent vote dropdown views to active election messages.
    Needed so dropdowns continue working after reconnect/restart.
    """
    cur = bot.db.cursor()
    cur.execute(
        """
        SELECT guild_id, position, vote_message_id
        FROM elections
        WHERE status = 'VOTING'
          AND vote_message_id IS NOT NULL
        """
    )
    active = cur.fetchall()

    restored = 0
    for row in active:
        guild_id = int(row["guild_id"])
        position = str(row["position"])
        vote_message_id = int(row["vote_message_id"])

        settings = get_settings(bot.db, guild_id)
        if not settings:
            continue

        cur2 = bot.db.cursor()
        cur2.execute(
            """
            SELECT user_id, display_name
            FROM nominations
            WHERE guild_id = ? AND position = ?
            ORDER BY display_name ASC
            """,
            (guild_id, position)
        )
        nominees = cur2.fetchall()
        candidates = [{"user_id": int(n["user_id"]), "display_name": str(n["display_name"])} for n in nominees]
        if not candidates:
            continue

        view = VoteView(bot, guild_id, position, candidates, settings)
        bot.add_view(view, message_id=vote_message_id)
        restored += 1

    return restored

# ============================================================
# Election scheduler (SCHEDULED -> VOTING)
# ============================================================

@tasks.loop(seconds=30)
async def election_scheduler():
    """
    Every 30 seconds:
    - Find elections that are SCHEDULED and start_at <= now (UTC)
    - Move them to VOTING
    - Post voting embed + dropdown in elections channel
    - Store vote_message_id
    """
    now_utc = datetime.now(timezone.utc)

    cur = bot.db.cursor()
    cur.execute(
        """
        SELECT guild_id, position, start_at
        FROM elections
        WHERE status = 'SCHEDULED'
        """
    )
    scheduled = cur.fetchall()
    if not scheduled:
        return

    for e in scheduled:
        guild_id = int(e["guild_id"])
        position = str(e["position"])
        start_at_raw = str(e["start_at"])

        # Parse ISO string (should include timezone, e.g. +00:00)
        try:
            start_at = datetime.fromisoformat(start_at_raw)
            if start_at.tzinfo is None:
                start_at = start_at.replace(tzinfo=timezone.utc)
            start_at_utc = start_at.astimezone(timezone.utc)
        except Exception:
            print(f"⚠️ Could not parse start_at for {position}: {start_at_raw}")
            continue

        if start_at_utc > now_utc:
            continue

        # Get guild + settings
        guild = bot.get_guild(guild_id)
        if not guild:
            continue

        settings = get_settings(bot.db, guild_id)
        if not settings:
            continue

        elections_channel_id = settings.get("elections_channel_id")
        if not elections_channel_id:
            continue

        elections_channel = guild.get_channel(int(elections_channel_id))
        if not elections_channel or not isinstance(elections_channel, discord.TextChannel):
            continue

        # Pull nominees (candidates)
        cur2 = bot.db.cursor()
        cur2.execute(
            """
            SELECT user_id, display_name
            FROM nominations
            WHERE guild_id = ? AND position = ?
            ORDER BY display_name ASC
            """,
            (guild_id, position)
        )
        nominees = cur2.fetchall()

        candidates = [{"user_id": int(n["user_id"]), "display_name": str(n["display_name"])} for n in nominees]

        embed = discord.Embed(
            title=f"🗳️ Voting Now Open — {position}",
            description="Use the dropdown below to vote. Votes are private and final.",
            color=discord.Color.green()
        )

        if candidates:
            for c in candidates:
                embed.add_field(name=c["display_name"], value=f"<@{c['user_id']}>", inline=False)
        else:
            embed.add_field(
                name="No candidates nominated",
                value="No nominees were recorded before voting began.",
                inline=False
            )

        view = VoteView(bot, guild_id, position, candidates, settings) if candidates else None

        # Post voting message
        sent = await elections_channel.send(embed=embed, view=view)

        # Update election to VOTING + store vote_message_id
        cur3 = bot.db.cursor()
        cur3.execute(
            """
            UPDATE elections
            SET status = 'VOTING',
                vote_message_id = ?
            WHERE guild_id = ? AND position = ?
            """,
            (sent.id, guild_id, position)
        )
        bot.db.commit()

        print(f"✅ Election started: {guild.name} | {position} | message_id={sent.id}")

# ============================================================
# Helper function to close an election and DM results
# ============================================================

async def auto_close_election(guild_id: int, position: str, admin_user: discord.User | None = None):
    """
    Closes an election and sends results to the admin via DM.
    Called by the auto-closer task or manually.
    """
    cur = bot.db.cursor()
    
    # Fetch election details
    cur.execute(
        """
        SELECT status, start_at, nominee_message_id, vote_message_id
        FROM elections
        WHERE guild_id = ? AND position = ?
        """,
        (guild_id, position)
    )
    election = cur.fetchone()
    
    if not election:
        return None
    
    current_status = str(election["status"])
    if current_status == "CLOSED":
        return None  # Already closed
    
    # Get guild and settings
    guild = bot.get_guild(guild_id)
    if not guild:
        return None
    
    settings = get_settings(bot.db, guild_id)
    if not settings:
        return None
    
    # Collect nominees
    cur.execute(
        """
        SELECT user_id, display_name
        FROM nominations
        WHERE guild_id = ? AND position = ?
        ORDER BY display_name ASC
        """,
        (guild_id, position)
    )
    nominees_rows = cur.fetchall()
    nominees = [{"user_id": int(r["user_id"]), "display_name": str(r["display_name"])} for r in nominees_rows]
    name_by_id = {n["user_id"]: n["display_name"] for n in nominees}
    
    # Collect votes
    cur.execute(
        """
        SELECT candidate_id, COUNT(*) as votes
        FROM votes
        WHERE guild_id = ? AND position = ?
        GROUP BY candidate_id
        ORDER BY votes DESC
        """,
        (guild_id, position)
    )
    vote_rows = cur.fetchall()
    
    # Calculate total votes and vote share percentages
    total_votes = 0
    for r in vote_rows:
        total_votes += int(r["votes"])
    
    results_lines = []
    winner_id = None
    winner_votes = 0
    
    for r in vote_rows:
        cid = int(r["candidate_id"])
        v = int(r["votes"])
        display = name_by_id.get(cid, f"Unknown Candidate ({cid})")
        vote_share = (v / total_votes * 100) if total_votes > 0 else 0
        results_lines.append(f"• **{display}** — {v} vote(s) ({vote_share:.1f}%)")
        
        if winner_id is None:
            winner_id = cid
            winner_votes = v
    
    if not results_lines:
        results_lines.append("• *(No votes were recorded.)*")
    
    # Calculate voter turnout
    voter_role_id = settings.get("voter_role_id")
    num_eligible_voters = 0
    if voter_role_id:
        try:
            voter_role = guild.get_role(int(voter_role_id))
            if voter_role:
                num_eligible_voters = len(voter_role.members)
        except Exception:
            pass
    
    cur.execute(
        """
        SELECT COUNT(DISTINCT voter_id) as voter_count
        FROM votes
        WHERE guild_id = ? AND position = ?
        """,
        (guild_id, position)
    )
    voter_count_row = cur.fetchone()
    num_voters = int(voter_count_row["voter_count"]) if voter_count_row else 0
    
    turnout_pct = (num_voters / num_eligible_voters * 100) if num_eligible_voters > 0 else 0
    turnout_str = f"{num_voters}/{num_eligible_voters} ({turnout_pct:.1f}%)" if num_eligible_voters > 0 else f"{num_voters} (eligible voters unknown)"
    
    # Check for tie
    is_tie = False
    if vote_rows and len(vote_rows) > 1:
        top = int(vote_rows[0]["votes"])
        second = int(vote_rows[1]["votes"])
        if second == top:
            is_tie = True
    
    # Mark election as CLOSED
    cur.execute(
        """
        UPDATE elections
        SET status = 'CLOSED'
        WHERE guild_id = ? AND position = ?
        """,
        (guild_id, position)
    )
    bot.db.commit()
    
    # Disable voting UI in elections channel
    elections_channel_id = settings.get("elections_channel_id")
    vote_message_id = election["vote_message_id"]
    
    if elections_channel_id and vote_message_id:
        elections_channel = guild.get_channel(int(elections_channel_id))
        if elections_channel and isinstance(elections_channel, discord.TextChannel):
            try:
                msg = await elections_channel.fetch_message(int(vote_message_id))
                closed_embed = discord.Embed(
                    title=f"🗳️ Election Closed — {position}",
                    description="This election has been closed (automatically after 24 hours).",
                    color=discord.Color.red()
                )
                await msg.edit(embed=closed_embed, view=None)
            except Exception:
                pass
    
    # Update nominees message
    nominee_message_id = election["nominee_message_id"]
    nominees_channel_id = settings.get("nominees_channel_id")
    
    if nominees_channel_id and nominee_message_id:
        nominees_channel = guild.get_channel(int(nominees_channel_id))
        if nominees_channel and isinstance(nominees_channel, discord.TextChannel):
            try:
                nmsg = await nominees_channel.fetch_message(int(nominee_message_id))
                embed = discord.Embed(
                    title=f"📝 Nominations Closed — {position}",
                    description="This election has been closed.",
                    color=discord.Color.dark_grey()
                )
                if nominees:
                    for n in nominees:
                        embed.add_field(name=n["display_name"], value=f"<@{n['user_id']}>", inline=False)
                else:
                    embed.add_field(name="No nominees", value="No nominees were recorded.", inline=False)
                
                await nmsg.edit(embed=embed)
            except Exception:
                pass
    
    # Build and send DM to admin
    start_at_iso = str(election["start_at"]) if election["start_at"] else None
    
    def utc_iso_to_london_str(iso_utc: str) -> str:
        dt = datetime.fromisoformat(iso_utc)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        local = dt.astimezone(LONDON_TZ)
        return local.strftime("%d %b %Y, %H:%M") + " (Europe/London)"
    
    dm_embed = discord.Embed(
        title=f"📩 Election Results (Auto-Closed) — {position}",
        color=discord.Color.blurple()
    )
    dm_embed.add_field(name="Guild", value=guild.name, inline=False)
    dm_embed.add_field(name="Status when closed", value=current_status, inline=False)
    if start_at_iso:
        dm_embed.add_field(name="Voting started", value=utc_iso_to_london_str(start_at_iso), inline=False)
    dm_embed.add_field(name="Voter Turnout", value=turnout_str, inline=False)
    dm_embed.add_field(name="Total votes recorded", value=str(total_votes), inline=False)
    dm_embed.add_field(name="Results", value="\n".join(results_lines), inline=False)
    
    if winner_id is None:
        dm_embed.add_field(name="Winner", value="No winner (no votes).", inline=False)
    else:
        winner_name = name_by_id.get(winner_id, f"Unknown Candidate ({winner_id})")
        if is_tie:
            dm_embed.add_field(
                name="Winner",
                value=f"⚠️ Tie detected at {winner_votes} vote(s). Top candidate: **{winner_name}** (tie-break required).",
                inline=False
            )
        else:
            dm_embed.add_field(
                name="Winner",
                value=f"🏆 **{winner_name}** with {winner_votes} vote(s).",
                inline=False
            )
    
    # Send DM to admin_user or find them by settings
    if not admin_user:
        # Try to get admin from guild (if they're still there)
        admin_id = settings.get("admin_role_id")
        if admin_id:
            admin_role = guild.get_role(int(admin_id))
            if admin_role and admin_role.members:
                admin_user = admin_role.members[0]
    
    if admin_user:
        try:
            await admin_user.send(embed=dm_embed)
            print(f"✅ Auto-closed election results sent to {admin_user}: {guild.name} | {position}")
        except Exception as e:
            print(f"⚠️ Could not DM admin for auto-closed election: {e}")
    else:
        print(f"⚠️ Could not find admin to DM for auto-closed election: {guild.name} | {position}")
    
    return True

# ============================================================
# Auto-close elections after 24 hours
# ============================================================

@tasks.loop(seconds=30)
async def election_auto_closer():
    """
    Every 30 seconds:
    - Find elections in VOTING status
    - Check if they've been open for 24 hours (start_at + 24h <= now)
    - Auto-close them and DM the admin results
    """
    now_utc = datetime.now(timezone.utc)
    
    cur = bot.db.cursor()
    cur.execute(
        """
        SELECT guild_id, position, start_at
        FROM elections
        WHERE status = 'VOTING'
        """
    )
    voting = cur.fetchall()
    
    if not voting:
        return
    
    for e in voting:
        guild_id = int(e["guild_id"])
        position = str(e["position"])
        start_at_raw = str(e["start_at"])
        
        try:
            start_at = datetime.fromisoformat(start_at_raw)
            if start_at.tzinfo is None:
                start_at = start_at.replace(tzinfo=timezone.utc)
            start_at_utc = start_at.astimezone(timezone.utc)
        except Exception:
            continue
        
        # Check if 24 hours have passed
        if start_at_utc + timedelta(hours=24) <= now_utc:
            guild = bot.get_guild(guild_id)
            if guild:
                settings = get_settings(bot.db, guild_id)
                if settings:
                    # Find admin user if possible
                    admin_user = None
                    try:
                        # Try to get from guild owner
                        admin_user = guild.owner
                    except Exception:
                        pass
                    
                    await auto_close_election(guild_id, position, admin_user)
                    print(f"⏰ Auto-closed election after 24 hours: {guild.name} | {position}")

@election_auto_closer.before_loop
async def before_election_auto_closer():
    await bot.wait_until_ready()

@election_scheduler.before_loop
async def before_election_scheduler():
    await bot.wait_until_ready()

# ============================================================
# Load extensions + sync slash commands
# ============================================================

@bot.event
async def setup_hook():
    base_dir = Path(__file__).resolve().parent
    commands_dir = base_dir / "commands"

    # Load all command modules
    for file in commands_dir.glob("*.py"):
        if file.name.startswith("__"):
            continue
        module_name = file.stem
        await bot.load_extension(f"commands.{module_name}")
        print(f"📦 Loaded command module: {module_name}")

    # Use a single command scope (global) to avoid duplicate entries in guilds.
    # If a test guild had previously been used, clear its guild-specific command set.
    if getattr(config, "TEST_GUILD_ID", None):
        guild = discord.Object(id=config.TEST_GUILD_ID)
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"🧹 Cleared guild-specific commands in TEST guild {config.TEST_GUILD_ID}")

    synced = await bot.tree.sync()
    print(f"🌐 Synced {len(synced)} slash commands globally")


@bot.tree.command(
    name="election_repost_vote",
    description="Admin: repost the voting dropdown for an active election without clearing votes"
)
@app_commands.describe(position="Election position (optional if only one election is currently in VOTING)")
async def election_repost_vote(interaction: discord.Interaction, position: str | None = None):
    if not interaction.guild:
        await interaction.response.send_message("❌ This command can only be used in a server.", ephemeral=True)
        return

    settings = get_settings(bot.db, interaction.guild.id)
    if not settings:
        await interaction.response.send_message("❌ Bot not configured. Run /setup first.", ephemeral=True)
        return

    if not is_admin(interaction, settings):
        await interaction.response.send_message("❌ You do not have permission to use this command.", ephemeral=True)
        return

    elections_channel_id = settings.get("elections_channel_id")
    if not elections_channel_id:
        await interaction.response.send_message("❌ Elections channel is not configured.", ephemeral=True)
        return

    elections_channel = interaction.guild.get_channel(int(elections_channel_id))
    if not elections_channel or not isinstance(elections_channel, discord.TextChannel):
        await interaction.response.send_message("❌ Configured elections channel is invalid.", ephemeral=True)
        return

    cur = bot.db.cursor()
    if position:
        cur.execute(
            """
            SELECT guild_id, position, status, vote_message_id
            FROM elections
            WHERE guild_id = ? AND status = 'VOTING' AND position = ?
            """,
            (interaction.guild.id, position)
        )
        election = cur.fetchone()
        if not election:
            await interaction.response.send_message(
                f"❌ No active VOTING election found for **{position}**.",
                ephemeral=True
            )
            return
    else:
        cur.execute(
            """
            SELECT guild_id, position, status, vote_message_id
            FROM elections
            WHERE guild_id = ? AND status = 'VOTING'
            ORDER BY position ASC
            """,
            (interaction.guild.id,)
        )
        rows = cur.fetchall()
        if not rows:
            await interaction.response.send_message("❌ No active VOTING elections found.", ephemeral=True)
            return
        if len(rows) > 1:
            names = ", ".join(str(r["position"]) for r in rows)
            await interaction.response.send_message(
                f"❌ Multiple active elections found: {names}. Please pass the **position**.",
                ephemeral=True
            )
            return
        election = rows[0]

    guild_id = int(election["guild_id"])
    election_position = str(election["position"])
    old_vote_message_id = election["vote_message_id"]

    cur.execute(
        """
        SELECT user_id, display_name
        FROM nominations
        WHERE guild_id = ? AND position = ?
        ORDER BY display_name ASC
        """,
        (guild_id, election_position)
    )
    nominees = cur.fetchall()
    candidates = [{"user_id": int(n["user_id"]), "display_name": str(n["display_name"])} for n in nominees]

    if not candidates:
        await interaction.response.send_message(
            "❌ Cannot repost voting dropdown because no nominees were found.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title=f"🗳️ Voting Now Open — {election_position}",
        description="Use the dropdown below to vote. Votes are private and final.",
        color=discord.Color.green()
    )
    for c in candidates:
        embed.add_field(name=c["display_name"], value=f"<@{c['user_id']}>", inline=False)

    view = VoteView(bot, guild_id, election_position, candidates, settings)
    sent = await elections_channel.send(embed=embed, view=view)
    bot.add_view(view, message_id=sent.id)

    cur.execute(
        """
        UPDATE elections
        SET vote_message_id = ?
        WHERE guild_id = ? AND position = ?
        """,
        (sent.id, guild_id, election_position)
    )
    bot.db.commit()

    if old_vote_message_id:
        try:
            old_msg = await elections_channel.fetch_message(int(old_vote_message_id))
            await old_msg.edit(view=None)
        except Exception:
            pass

    await interaction.response.send_message(
        f"✅ Reposted voting message for **{election_position}**: {sent.jump_url}\n"
        "Existing votes were preserved.",
        ephemeral=True
    )

@bot.event
async def on_ready():
    print("========================================")
    print(f"✅ Logged in as: {bot.user}")
    print(f"🆔 Bot ID: {bot.user.id}")
    print("========================================")
    print("🏛️ Borealia Government Bot is online.")

    if not getattr(bot, "_election_views_restored", False):
        restored_count = restore_active_election_views()
        bot._election_views_restored = True
        print(f"🔁 Restored {restored_count} active election dropdown view(s).")

    if not election_scheduler.is_running():
        election_scheduler.start()
        print("⏱️ Election scheduler started (checks every 30s).")

    if not election_auto_closer.is_running():
        election_auto_closer.start()
        print("⏰ Election auto-closer started (checks every 30s).")


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    root_error = getattr(error, "original", error)
    print("❌ Slash command error:", repr(root_error))
    traceback.print_exception(type(root_error), root_error, root_error.__traceback__)

    message = "❌ This command failed due to a temporary error. Please try again."
    try:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
    except Exception:
        pass

if __name__ == "__main__":
    print("🚀 Starting Borealia Government Bot...")
    bot.run(TOKEN)