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
    guild_id INTEGER PRIMARY KEY,

    nominees_channel_id INTEGER,
    elections_channel_id INTEGER,
    laws_channel_id INTEGER,
    bank_transactions_channel_id INTEGER,
    log_channel_id INTEGER,

    voter_role_id INTEGER,
    admin_role_id INTEGER,
    associate_parliamentarian_role_id INTEGER,
    king_role_id INTEGER
)
""")

    # ------------------------------------------------------------
    # 2) Elections
    # Tracks whether a specific election is open or closed.
    # One row per (guild_id, position).
    # ------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS elections (
            guild_id                INTEGER NOT NULL,
            position                TEXT NOT NULL,
            
            status                  TEXT NOT NULL,
            start_at                TEXT NOT NULL,
            
            nominee_message_id      INTEGER,
            vote_message_id         INTEGER,
                
            created_by              INTEGER,
            created_at              TEXT,
            
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
            candidate_id  INTEGER NOT NULL,
                
            PRIMARY KEY (guild_id, position, voter_id)
        )
    """)

    # ------------------------------------------------------------
    # 4b) Appointment Nominations (no public vote)
    # Used for PM/leadership appointments where nominations are
    # collected but there is no voting phase.
    # ------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS appointment_positions (
            guild_id            INTEGER NOT NULL,
            position            TEXT NOT NULL,
            status              TEXT NOT NULL,          -- OPEN | CLOSED
            nominee_message_id  INTEGER,
            opened_by           INTEGER,
            opened_at           TEXT,
            nomination_closes_at TEXT,
            closed_by           INTEGER,
            closed_at           TEXT,

            PRIMARY KEY (guild_id, position)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS appointment_nominations (
            guild_id      INTEGER NOT NULL,
            position      TEXT NOT NULL,
            user_id       INTEGER NOT NULL,
            display_name  TEXT NOT NULL,

            PRIMARY KEY (guild_id, position, user_id)
        )
    """)

    # ------------------------------------------------------------
    # 5) Motions (Parliament Votes)
    # Motions are things like:
    # - Acts of Parliament
    # - Resolutions
    # - Confidence Votes
    # 
    # Each motion has a lifecycle:
    # DRAFT -> VOTING -> CLOSED
    # ------------------------------------------------------------
    cur.execute("""
                CREATE TABLE IF NOT EXISTS motions (
                motion_id               INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id                INTEGER NOT NULL,
                
                kind                    TEXT NOT NULL,          -- "act", "resolution", etc.
                title                   TEXT NOT NULL,
                text                    TEXT NOT NULL,

                created_by              INTEGER NOT NULL,
                created_at              TEXT NOT NULL,

                status                  TEXT NOT NULL,          -- DRAFT | VOTING | CLOSED
                opens_at                TEXT,
                closes_at               TEXT,
                
                public_votes            INTEGER DEFAULT 1,      -- 1 = roll-call visible

                -- Final outcome and royal assent workflow
                final_result            TEXT,                   -- PASSED | FAILED | TIED
                royal_assent_status     TEXT,                   -- PENDING | APPROVED | REJECTED
                royal_assented_by       INTEGER,
                royal_assented_at       TEXT,
                assent_channel_id       INTEGER,
                assent_message_id       INTEGER,
                target_act_id           INTEGER,
                
                -- Where the public roll-call message is posted
                message_channel_id      INTEGER,
                message_id              INTEGER
                )
            """)

    # ------------------------------------------------------------
    # 7) Acts Registry
    # Stores enacted acts and their repeal status.
    # ------------------------------------------------------------
    cur.execute("""
                CREATE TABLE IF NOT EXISTS acts (
                act_id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id                 INTEGER NOT NULL,

                source_motion_id         INTEGER,
                title                    TEXT NOT NULL,
                text                     TEXT NOT NULL,

                enacted_by_user_id       INTEGER,
                enacted_at               TEXT NOT NULL,

                status                   TEXT NOT NULL DEFAULT 'ENACTED', -- ENACTED | REPEALED
                repealed_by_motion_id    INTEGER,
                repealed_by_user_id      INTEGER,
                repealed_at              TEXT
                )
            """)
    
    # ------------------------------------------------------------
    # 6) Motion Votes
    # Stores votes for each motion.
    # One row per (guild_id, motion_id, user_id) to enforce:
    #   "one vote per user per motion"
    # ------------------------------------------------------------
    cur.execute("""
                CREATE TABLE IF NOT EXISTS motion_votes (
                vote_id        INTEGER PRIMARY KEY AUTOINCREMENT,

                guild_id       INTEGER NOT NULL,
                motion_id      INTEGER NOT NULL,
                user_id        INTEGER NOT NULL,

                choice         TEXT NOT NULL,       -- "yes", "no", "abstain"

                UNIQUE (guild_id, motion_id, user_id)
            )
            """)  

    # ------------------------------------------------------------
    # 9b) Weekly financial reports (dedupe + audit)
    # One row per (guild, report week id).
    # ------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS financial_reports (
            guild_id         INTEGER NOT NULL,
            report_id        TEXT NOT NULL,
            report_start_at  TEXT NOT NULL,
            report_end_at    TEXT NOT NULL,
            message_id       INTEGER,
            generated_by     INTEGER,
            mode             TEXT NOT NULL,   -- AUTO | MANUAL
            generated_at     TEXT NOT NULL,

            PRIMARY KEY (guild_id, report_id)
        )
    """)
    
    # ------------------------------------------------------------
    # 8) Migration: add Parliament fields to guild_settings (if missing)
    # SQLite cannot "ADD COLUMN IF NOT EXISTS", so we try and ignore errors.
    # ------------------------------------------------------------
    try:
        cur.execute("ALTER TABLE guild_settings ADD COLUMN parliament_channel_id INTEGER")
    except sqlite3.OperationalError:
        pass

    try: 
        cur.execute("ALTER TABLE guild_settings ADD COLUMN parliament_role_id INTEGER")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE guild_settings ADD COLUMN associate_parliamentarian_role_id INTEGER")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE guild_settings ADD COLUMN king_role_id INTEGER")
    except sqlite3.OperationalError:
        pass

    try:
        cur.execute("ALTER TABLE guild_settings ADD COLUMN bank_transactions_channel_id INTEGER")
    except sqlite3.OperationalError:
        pass

    # ------------------------------------------------------------
    # 8b) Migration: add nomination_closes_at for appointment flows
    # ------------------------------------------------------------
    try:
        cur.execute("ALTER TABLE appointment_positions ADD COLUMN nomination_closes_at TEXT")
    except sqlite3.OperationalError:
        pass

    # ------------------------------------------------------------
    # 9) Migration: legacy motion_votes.vote -> motion_votes.choice
    # ------------------------------------------------------------
    try:
        cur.execute("PRAGMA table_info(motion_votes)")
        motion_vote_columns = {row[1] for row in cur.fetchall()}

        if "choice" not in motion_vote_columns and "vote" in motion_vote_columns:
            cur.execute("ALTER TABLE motion_votes ADD COLUMN choice TEXT")
            cur.execute("UPDATE motion_votes SET choice = vote WHERE choice IS NULL")
    except sqlite3.OperationalError:
        pass

    # ------------------------------------------------------------
    # 10) Migration: ensure legacy motions tables have all columns
    # ------------------------------------------------------------
    try:
        cur.execute("PRAGMA table_info(motions)")
        motions_columns = {row[1] for row in cur.fetchall()}

        missing_motion_columns = [
            ("created_by", "INTEGER"),
            ("created_at", "TEXT"),
            ("status", "TEXT DEFAULT 'DRAFT'"),
            ("opens_at", "TEXT"),
            ("closes_at", "TEXT"),
            ("public_votes", "INTEGER DEFAULT 1"),
            ("final_result", "TEXT"),
            ("royal_assent_status", "TEXT"),
            ("royal_assented_by", "INTEGER"),
            ("royal_assented_at", "TEXT"),
            ("assent_channel_id", "INTEGER"),
            ("assent_message_id", "INTEGER"),
            ("target_act_id", "INTEGER"),
            ("message_channel_id", "INTEGER"),
            ("message_id", "INTEGER"),
        ]

        for column_name, column_type in missing_motion_columns:
            if column_name not in motions_columns:
                cur.execute(f"ALTER TABLE motions ADD COLUMN {column_name} {column_type}")
    except sqlite3.OperationalError:
        pass

    # ------------------------------------------------------------
    # 11) Migration: ensure legacy acts tables have all columns
    # ------------------------------------------------------------
    try:
        cur.execute("PRAGMA table_info(acts)")
        acts_columns = {row[1] for row in cur.fetchall()}

        missing_act_columns = [
            ("source_motion_id", "INTEGER"),
            ("title", "TEXT"),
            ("text", "TEXT"),
            ("enacted_by_user_id", "INTEGER"),
            ("enacted_at", "TEXT"),
            ("status", "TEXT DEFAULT 'ENACTED'"),
            ("repealed_by_motion_id", "INTEGER"),
            ("repealed_by_user_id", "INTEGER"),
            ("repealed_at", "TEXT"),
        ]

        for column_name, column_type in missing_act_columns:
            if column_name not in acts_columns:
                cur.execute(f"ALTER TABLE acts ADD COLUMN {column_name} {column_type}")
    except sqlite3.OperationalError:
        pass

    # ------------------------------------------------------------
    # 12) Extended slowmode configuration
    # Supports custom slowmode durations beyond Discord native 6h cap.
    # ------------------------------------------------------------
    cur.execute("""
        CREATE TABLE IF NOT EXISTS extended_slowmode_channels (
            guild_id       INTEGER NOT NULL,
            channel_id     INTEGER NOT NULL,
            delay_seconds  INTEGER NOT NULL,
            enabled_by     INTEGER,
            enabled_at     TEXT,
            PRIMARY KEY (guild_id, channel_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS extended_slowmode_activity (
            guild_id        INTEGER NOT NULL,
            channel_id      INTEGER NOT NULL,
            user_id         INTEGER NOT NULL,
            last_message_at TEXT NOT NULL,
            PRIMARY KEY (guild_id, channel_id, user_id)
        )
    """)

    # Save table creation tables
    conn.commit()