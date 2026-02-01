# config.py
import os
from dotenv import load_dotenv

# Discord bot token (loaded from .env file)
TOKEN = os.getenv("DISCORD_TOKEN")

# Optional: set this ONLY for fast dev sync in one server.
# In production (multi-server), leave it unset so commands sync globally.
TEST_GUILD_ID = int(os.getenv("TEST_GUILD_ID", "0")) or None

# Database path
DATABASE_PATH = "data/borealia.db"
