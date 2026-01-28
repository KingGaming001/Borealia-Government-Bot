# db.py
# ============================================================
# Database Helpers (SQLite)
#
# This module is responsible for:
# 1) Creating/opening the SQLite database connection
# 2) Creating all required tables (if they don't exist)
# 
# We keep this separate so:
# - main.py stays clean
# - command modules don't repeat table creation logic
# 
# The database file path is defined in config.py
#   DATABASE_PATH = "data/borealia.db"
# ============================================================

import os
import sqlite3
import config

def get_db() -> sqlite3.Connection:
    """
    Create (or open) the SQLite database file and return a connection.

    Notes:
    - We ensure the 'data/' directory exists first.
    - row_factory is set so rows behave like dictionaries.
        row['column_name']
    """
    # Ensure the folder for the DB exists (e.g. data/)
    os.makedirs(os.path.dirname(config.DATABASE_PATH), exist_ok=True)

    # Open a connection to the SQLite database file
    conn = sqlite3.connect(config.DATABASE_PATH)

    # Make SQLite return rows as dict-like objects for nicer code
    conn.row_factory = sqlite3.Row

    return conn

def init_db(conn: sqlite3.Connection) -> None:
    """
    Create all required tables for the bot if they do not exist.

    Tables:
    - guild_settings: per-server configuration saved via /setup
    - elections: tracks whether an election is open/closed per position
    - nominations: stores who is nominated for which position
    - votes: stores each user's vote (one vote per user per position)
    """

    cur = conn.cursor()

    # ------------------------------------------------------------
    # 1) Guild settings
    # Stores configuration by /setup for each server.
    # ------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS guild_settings (
            guild_id              INTEGER PRIMARY KEY,
            
            -- Channels
            elections_channel_id  INTEGER,
            logs_channel_id       INTEGER,
            
            -- Roles
            admin_role_id         INTEGER,
            voter_role_id         INTEGER
        )
    """)

    # ------------------------------------------------------------
    # 2) Elections
    # Tracks whether a specific election is open or closed.
    # One row per (guild_id, position).
    # ------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS elections (
            guild_id      INTEGER NOT NULL,
            position      TEXT NOT NULL,
            is_closed     INTEGER DEFAULT 0,
                
            PRIMARY KEY (guild_id, position)
        )
    """)

    # ------------------------------------------------------------
    # 3) Nominations
    # Stores nominees for each election position.
    # One row per (guild_id, position, user_id) to prevent duplicates.
    cur.execute("""
        CREATE TABLE IF NOT EXISTS nominations (
            guild_id      INTEGER NOT NULL,
            position      TEXT NOT NULL,
            user_id       INTEGER NOT NULL,
            display_name          TEXT NOT NULL,
                
            PRIMARY KEY (guild_id, position, user_id)
        )
    """)

    # ------------------------------------------------------------
    # 4) Votes
    # Stores votes for each election position.
    # One row per (guild_id, position, voter_id) to enforce:
    #   "one vote per user per position
    # ------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS votes (
            guild_id      INTEGER NOT NULL,
            position      TEXT NOT NULL,
            voter_id      INTEGER NOT NULL,
                
            PRIMARY KEY (guild_id, position, voter_id)
        )
    """)

    # Save table creation tables
    conn.commit()