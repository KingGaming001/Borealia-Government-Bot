# main.py
# ============================================================
# Borealia Government Bot
#
# This is the main entry point for the bot.
# Responsibilities:
# - Load environment variables (.env)
# - Create and configure the Discord bot
# - Initialise the database
# - Load all command modules
# - Sync slash commands
# - Start the bot
# ============================================================

import discord
from discord.ext import commands
import os
from pathlib import Path

# ------------------------------------------------------------
# Load environment variables
#
# This allows us to keep the Discord bot token out of GitHub
# by storing it in a .env file.
# ------------------------------------------------------------
from dotenv import load_dotenv
load_dotenv()

# ------------------------------------------------------------
# Local imports
# ------------------------------------------------------------
import config
from db import get_db, init_db

# ------------------------------------------------------------
# Read token from environment
#
# DISCORD_TOKEN defined in .env file
# ------------------------------------------------------------
TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("❌ DISCORD_TOKEN not found. Check your .env file.")

# ------------------------------------------------------------
# Discord intents
#
# members=True is required for role checks
# ------------------------------------------------------------
intents = discord.Intents.default()
intents.members = True

# ------------------------------------------------------------
# Create bot instance
# ------------------------------------------------------------
bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

# ------------------------------------------------------------
# Database setup
#
# One shared DB connection attached to the bot instance
# ------------------------------------------------------------
bot.db = get_db()
init_db(bot.db)

# ------------------------------------------------------------
# On ready event
# ------------------------------------------------------------
@bot.event
async def on_ready():
    print("========================================")
    print(f"✅ Logged in as: {bot.user}")
    print(f"🆔 Bot ID: {bot.user.id}")
    print("========================================")

    # --------------------------------------------------------
    # Sync slash commands
    #
    # GLOBAL SYNC:
    #   First sync may take up to ~1 hour to appear
    #
    # For FAST TESTING, uncomment the guild sync below.
    # --------------------------------------------------------

    # FAST GUILD-ONLY SYNC (optional)
    # guild = discord.Object(id=config.GUILD_ID)
    # await bot.tree.sync(guild=guild)

    # --- GLOBAL SYNC ---
    await bot.tree.sync()

    print("🔁 Slash commands synced.")
    print("🏛️ Borealia Government Bot is online.")

# ------------------------------------------------------------
# Load command modules automatically
# ------------------------------------------------------------
COMMANDS_DIR = "./commands"

for filename in os.listdir(COMMANDS_DIR):
        if filename.endswith(".py") and not filename.startswith("__"):
            module_name = filename[:-3]
            try:
                bot.load_extension(f"commands.{module_name}")
                print(f"📦 Loaded command module: {module_name}")
            except Exception as e:
                print(f"❌ Failed to load module {module_name}: {e}")
# ------------------------------------------------------------
# Start the bot
# ------------------------------------------------------------
if __name__ == "__main__":
    print("🚀 Starting Borealia Government Bot...")
    bot.run(TOKEN)