# main.py
# ============================================================
# Borealia Government Bot
#
# Responsibilities:
# - Load environment variables (.env)
# - Create bot + intents
# - Initialise database
# - Load all command modules
# - Sync slash commands to your test guild (fast)
# - Run election scheduler:
#     SCHEDULED -> VOTING when start_at is reached
#     Posts voting dropdown in elections channel
#
# Updates included:
# 1) Prevent duplicate slash commands (/status appearing multiple times):
#    - Clear guild commands + sync (no copy_global_to)
# 2) Lock votes:
#    - Once a voter votes, they cannot change their vote
# ============================================================

from __future__ import annotations

import os
from pathlib import Path
from datetime import datetime, timezone

import discord
from discord.ext import commands, tasks
from dotenv import load_dotenv

import config
from db import get_db, init_db
from config_store import get_settings, has_voter_role

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
            options=options
        )

    async def callback(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("❌ This can only be used in a server.", ephemeral=True)
            return

        # Role check
        if not has_voter_role(interaction.user, self.settings):
            await interaction.response.send_message(
                "❌ You do not have the voter role required to vote in this election.",
                ephemeral=True
            )
            return

        candidate_id = int(self.values[0])

        # Confirm election is still VOTING
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

        # --------------------------------------------------------
        # LOCKED VOTING:
        # If the voter has already voted, do not allow changes.
        # --------------------------------------------------------
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

        # Insert vote (no upsert/update)
        cur.execute(
            """
            INSERT INTO votes (guild_id, position, voter_id, candidate_id)
            VALUES (?, ?, ?, ?)
            """,
            (self.guild_id, self.position, interaction.user.id, candidate_id)
        )
        self.bot.db.commit()

        # Find candidate display name for confirmation
        chosen = next((c for c in self.candidates if int(c["user_id"]) == candidate_id), None)
        chosen_name = chosen["display_name"] if chosen else "that candidate"

        await interaction.response.send_message(
            f"✅ Your vote for **{chosen_name}** has been recorded. (Votes are private.)",
            ephemeral=True
        )


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

    guild = discord.Object(id=config.GUILD_ID)

    # 1) Clear guild commands on Discord (prevents duplicates)
    bot.tree.clear_commands(guild=guild)

    # 2) Copy your global commands into the guild scope (so guild sync sees them)
    bot.tree.copy_global_to(guild=guild)

    # 3) Sync once
    synced = await bot.tree.sync(guild=guild)
    print(f"🔁 Synced {len(synced)} slash commands to guild {config.GUILD_ID}.")



@bot.event
async def on_ready():
    print("========================================")
    print(f"✅ Logged in as: {bot.user}")
    print(f"🆔 Bot ID: {bot.user.id}")
    print("========================================")
    print("🏛️ Borealia Government Bot is online.")

    if not election_scheduler.is_running():
        election_scheduler.start()
        print("⏱️ Election scheduler started (checks every 30s).")


if __name__ == "__main__":
    print("🚀 Starting Borealia Government Bot...")
    bot.run(TOKEN)
