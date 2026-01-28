# main.py
# ============================================================
# Borealia Government Bot
# ============================================================

import discord
from discord.ext import commands
import os
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

import config
from db import get_db, init_db

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("❌ DISCORD_TOKEN not found. Check your .env file.")

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

bot.db = get_db()
init_db(bot.db)

# ------------------------------------------------------------
# IMPORTANT: In discord.py 2.x, extensions must be loaded with await
# and should be loaded BEFORE syncing slash commands.
# ------------------------------------------------------------
@bot.event
@bot.event
async def setup_hook():
    base_dir = Path(__file__).resolve().parent
    commands_dir = base_dir / "commands"

    for file in commands_dir.glob("*.py"):
        if file.name.startswith("__"):
            continue
        module_name = file.stem
        await bot.load_extension(f"commands.{module_name}")
        print(f"📦 Loaded command module: {module_name}")

    guild = discord.Object(id=config.GUILD_ID)

    bot.tree.copy_global_to(guild=guild)

    synced = await bot.tree.sync(guild=guild)
    print(f"🔁 Synced {len(synced)} slash commands to guild {config.GUILD_ID}.")



@bot.event
async def on_ready():
    print("========================================")
    print(f"✅ Logged in as: {bot.user}")
    print(f"🆔 Bot ID: {bot.user.id}")
    print("TREE COMMANDS:", [c.name for c in bot.tree.get_commands()])
    
    print("GUILDS BOT IS IN:")
    for g in bot.guilds:
        print("-", g.name, g.id)

    print("========================================")
    print("🏛️ Borealia Government Bot is online.")

if __name__ == "__main__":
    print("🚀 Starting Borealia Government Bot...")
    bot.run(TOKEN)
