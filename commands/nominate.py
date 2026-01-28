# commands/nominate.py
# ------------------------------------------------------------
# /nominate
# Allows a user to nominate themselves for a given position.
# 
# What it does:
# 1) Checks the bots has been configured via /setup
# 2) Ensures the election exists (creates it if needed)
# 3) Blocks nomination if the election is closed
# 4) Stores the nominee in the database (prevents duplicates)
# 5) Posts an "Election Panel" embed + vote dropdown in the
#    configured elections channel
#
# Notes:
# - This version POSTS a new panel each time someone nominates.
#   (Simple + clean. Later we can store message_id and edit
#    the same panel instead.)
# - Voting is private: voters get ephermeral confirmations.
# ------------------------------------------------------------

import discord
from discord import app_commands, Interaction
from discord.ext import commands
import sqlite3

from config_store import get_settings, has_voter_role

# ------------------------------------------------------------
# Database Helpers
# ------------------------------------------------------------
def ensure_election_row(conn: sqlite3.Connection, guild_id: int, position: str) -> None:
    """
    Ensure an election row exists for this (guild, position).
    If it does not exist, create it as OPEN (isclosed=0).
    """
    cursor = conn.cursor()
    cursor.execute(
        "INSERT OR IGNORE INTO elections (guild_id, position, is_closed) VALUES (?, ?, 0)",
        (guild_id, position)
    )
    conn.commit()

def election_is_closed(conn: sqlite3.Connection, guild_id: int, position: str) -> bool:
    """
    Returns TRUE if the election exists and is marked closed.
    If it doesn't exist yet, it is treated as not closed.
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT is_closed FROM elections WHERE guild_id = ? AND position = ?",
        (guild_id, position)
    )
    row = cursor.fetchone()
    return bool(row and int(row["is_closed"]) == 1)

def add_nomination(conn: sqlite3.Connection, guild_id: int, position: str, user_id: int, display_name: str) -> bool:
    """
    Insert a nominee into nominations table.
    Returns FALSE if the nominee is already nominated (duplicate).
    """
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO nominations (guild_id, position, user_id, display_name) VALUES (?, ?, ?, ?)",
            (guild_id, position, user_id, display_name)
        )
        conn.commit()
        return True
    except sqlite3.IntegrityError:
        # Primary key (guild_id, position, user_id) already exists
        return False
    
def get_nominees(conn: sqlite3.Connection, guild_id: int, position: str) -> list[sqlite3.Row]:
    """
    Return all nominees for this position in this guild.
    """
    cur = conn.cursor()
    cur.execute(
        """
        SELECT user_id, display_name
        FROM nominations
        WHERE guild_id = ? AND position = ?
        ORDER BY display_name COLLATE NOCASE
        """,
        (guild_id, position)
    )
    return cur.fetchall()

def record_vote(conn: sqlite3.Connection, guild_id: int, position: str, voter_id: int, candidate_id: int) -> str:
    """
    Store a vote for (position) by (voter_id).
    Returns:
      - "ok"      -> vote recorded
      - "already" -> voter has already voted for this position
      - "closed" -> election closed
    """
    if election_is_closed(conn,guild_id, position):
        return "closed"
    
    cur = conn.cursor()
    try:
        cur.execute(
            "INSERT INTO votes (guild_id, position, voter_id, candidate_id) VALUES (?, ?, ?, ?)",
            (guild_id, position, voter_id, candidate_id)
        )
        conn.commit()
        return "ok"
    except sqlite3.IntegrityError:
        # Primary key (guild_id, position, voter_id) already exists
        return "already"
    
# ------------------------------------------------------------
# Discord UI Components (dropdown voting)
# ------------------------------------------------------------
class VoteDropdown(discord.ui.Select):
    """
    The dropdown component under the election panel embed.
    A user selects a nominee to cast their vote.
    """

    def __init__(self, bot: commands.Bot, guild_id: int, position: str, nominees: list[sqlite3.Row], settings: dict):
        # Build the dropdown options from the nominee list
        options = [
            discord.SelectOption(label=n["display_name"], value=str(n["user_id"]))
            for n in nominees
        ]

        super().__init__(placeholder="Select your vote", options=options)

        # Store what we need for vote handling
        self.bot = bot
        self.guild_id = guild_id
        self.position = position
        self.settings = settings

    async def callback(self, interaction: discord.Interaction):
        """
        Runs when a user selects an option in the dropdown.
        """
        # Must be used inside a guild
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message(
                "❌ This action can only be performed in a server.",
                ephemeral=True
            )
            return
        
        # Only members wiht the configured voter role can vote
        if not has_voter_role(interaction.user, self.settings):
            await interaction.response.send_message(
                "❌ You do not have permission to vote in this election.",
                ephemeral=True
            )
            return
        
        # Record vote in database
        candidate_id = int(self.values[0])
        result = record_vote(
            self.bot.db,
            self.guild_id,
            self.position,
            interaction.user.id,
            candidate_id
        )

        # Respond privately (ephemeral) so votes remain private
        if result == "closed":
            await interaction.response.send_message(
                f"❌ The election for **{self.position}** is now closed. You cannot vote.",
                ephemeral=True
            )
        elif result == "already":
            await interaction.response.send_message(
                f"❌ You have already voted in the election for **{self.position}**. "
                "You cannot change your vote.",
                ephemeral=True
            )
        else:  # "ok"
            await interaction.response.send_message(
                f"✅ Your vote for **{self.position}** has been recorded. Thank you for voting!",
                ephemeral=True
            )

class VoteView(discord.ui.View):
    """
    View wrapper that holds the VoteDropdown.
    """

    def __init__(self, bot: commands.Bot, guild_id: int, position: str, nominees: list[sqlite3.Row], settings: dict):
        super().__init__(timeout=None) # No timeout
        self.add_item(VoteDropdown(bot, guild_id, position, nominees, settings))

# ------------------------------------------------------------
# /nominate command cog
# ------------------------------------------------------------
class NominateCommand(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="nominate", description="Nominate yourself for a government position")
    @app_commands.describe(
        position="The position you are running for (e.g., Prime Minister)",
        name="Your name as it displays on the ballot"
    )
    async def nominate(self, interaction: Interaction, position: str, name: str):
        # -----------------------------
        # Must be used in a server
        # -----------------------------
        if not interaction.guild:
            await interaction.response.send_message(
                "❌ This command can only be used in a server.",
                ephemeral=True
            )
            return

        guild_id = interaction.guild.id

        # -----------------------------
        # Ensure the bot has been configured
        # -----------------------------
        settings = get_settings(self.bot.db, guild_id)
        if not settings:
            await interaction.response.send_message(
                "❌ The Borealia Government bot is not yet configured for this server. "
                "An administrator can run the /setup command to configure it.",
                ephemeral=True
            )
            return

        elections_channel_id = settings.get("elections_channel_id")
        if not elections_channel_id:
            await interaction.response.send_message(
                "❌ The Borealia Government bot is not properly configured. "
                "The elections channel is missing. An administrator can run the /setup command to fix this.",
                ephemeral=True
            )
            return

        # -----------------------------
        # Ensure election exists; prevent nomination if closed
        # -----------------------------
        ensure_election_row(self.bot.db, guild_id, position)

        if election_is_closed(self.bot.db, guild_id, position):
            await interaction.response.send_message(
                f"❌ The election for **{position}** is now closed. You cannot nominate yourself.",
                ephemeral=True
            )
            return

        # -----------------------------
        # Insert the nomination
        # -----------------------------
        inserted = add_nomination(self.bot.db, guild_id, position, interaction.user.id, name)

        if not inserted:
            await interaction.response.send_message(
                f"❌ You have already nominated yourself for **{position}**.",
                ephemeral=True
            )
            return

        # -----------------------------
        # Build the election panel embed
        # -----------------------------
        nominees = get_nominees(self.bot.db, guild_id, position)

        embed = discord.Embed(
            title=f"🗳️ Election for {position}",
            description="Use the dropdown below to vote. Votes are private.",
            color=discord.Color.purple()
        )

        for n in nominees:
            embed.add_field(
                name=n["display_name"],
                value=f"<@{n['user_id']}>",
                inline=False
            )

        # -----------------------------
        # Post the election panel to the elections channel
        # -----------------------------
        elections_channel = interaction.guild.get_channel(int(elections_channel_id))
        if not elections_channel:
            await interaction.response.send_message(
                "❌ The configured elections channel was not found. "
                "An administrator can run the /setup command to fix this.",
                ephemeral=True
            )
            return

        view = VoteView(self.bot, guild_id, position, nominees, settings)
        await elections_channel.send(embed=embed, view=view)

        # -----------------------------
        # Confirm privately to the nominator
        # -----------------------------
        await interaction.response.send_message(
            f"✅ You have successfully nominated yourself for **{position}**. "
            "An election panel has been posted in the elections channel.",
            ephemeral=True
        )

async def setup(bot: commands.Bot):
    await bot.add_cog(NominateCommand(bot))
