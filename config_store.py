# config_store.py
# ============================================================
# Stores and retrieves per-guild configuration from SQLite.
#
# - guild_settings table holds channel IDs + role IDs.
# - /setup uses upsert_settings() to save config.
# - commands use get_settings() to read config.
# - is_admin() / has_voter_role() help with permissions.
# ============================================================

import sqlite3
import discord


def get_settings(conn: sqlite3.Connection, guild_id: int) -> dict | None:
    """
    Returns the guild settings as a dict, or None if not configured.
    """
    cur = conn.cursor()
    cur.execute("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
    row = cur.fetchone()
    return dict(row) if row else None


def upsert_settings(
    conn: sqlite3.Connection,
    guild_id: int,
    nominees_channel_id: int | None = None,
    elections_channel_id: int | None = None,
    laws_channel_id: int | None = None,
    log_channel_id: int | None = None,
    voter_role_id: int | None = None,
    admin_role_id: int | None = None,
    parliament_channel_id: int | None = None,
    parliament_role_id: int | None = None,
) -> None:
    """
    Insert or update guild settings.

    We only write the columns that were provided (not None),
    so you can update config incrementally if you want.
    """
    # Build a dict of fields we actually want to write
    fields: dict[str, int] = {}

    if nominees_channel_id is not None:
        fields["nominees_channel_id"] = nominees_channel_id
    if elections_channel_id is not None:
        fields["elections_channel_id"] = elections_channel_id
    if laws_channel_id is not None:
        fields["laws_channel_id"] = laws_channel_id
    if log_channel_id is not None:
        fields["log_channel_id"] = log_channel_id
    if voter_role_id is not None:
        fields["voter_role_id"] = voter_role_id
    if admin_role_id is not None:
        fields["admin_role_id"] = admin_role_id
    if parliament_channel_id is not None:
        fields["parliament_channel_id"] = parliament_channel_id
    if parliament_role_id is not None:
        fields["parliament_role_id"] = parliament_role_id

    # Nothing to update
    if not fields:
        return

    # Ensure the row exists first (so UPDATE always works)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO guild_settings (guild_id) VALUES (?)", (guild_id,))

    # Build UPDATE statement dynamically
    set_clause = ", ".join([f"{k} = ?" for k in fields.keys()])
    values = list(fields.values()) + [guild_id]

    cur.execute(f"UPDATE guild_settings SET {set_clause} WHERE guild_id = ?", values)
    conn.commit()


def has_voter_role(member: discord.Member, settings: dict | None) -> bool:
    """
    True if member has the configured voter role.
    """
    if not settings:
        return False
    voter_role_id = settings.get("voter_role_id")
    if not voter_role_id:
        return False
    return any(r.id == int(voter_role_id) for r in member.roles)


def has_parliament_role(member: discord.Member, settings: dict | None) -> bool:
    """
    True if member has the configured parliament role.
    """
    if not settings:
        return False
    parliament_role_id = settings.get("parliament_role_id")
    if not parliament_role_id:
        return False
    return any(r.id == int(parliament_role_id) for r in member.roles)


def is_admin(interaction: discord.Interaction, settings: dict | None) -> bool:
    """
    True if the user is a Discord admin OR has the configured admin role.
    """
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return False

    # Discord administrators always allowed
    if interaction.user.guild_permissions.administrator:
        return True

    # If admin role configured, allow that too
    if not settings:
        return False
    admin_role_id = settings.get("admin_role_id")
    if not admin_role_id:
        return False

    return any(r.id == int(admin_role_id) for r in interaction.user.roles)
