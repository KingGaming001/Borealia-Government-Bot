# config_store.py
# ============================================================
# Configuration Store & Permission Helpers
#
# This module is responsible for:
# - Sorting per-guild configuration (channels, roles)
# - Retrieving configuration for commands to use
# - Centralising permission checks (admin / voter)
#
# This keeps SQL and permission logic OUT of individual
# command files, making the codebase cleaner and safer.
# ============================================================

import discord
import sqlite3

# ------------------------------------------------------------
# Fetch guild configuration
# ------------------------------------------------------------
def get_settings(conn: sqlite3.Connection, guild_id: int) -> dict | None:
    """
    Retrieve the configuration for a guild.

    Returns:
        dict -> Configuration fields (column_name -> value)
        None -> If the guild has not been configured yet
    """
    cursor = conn.cursor()
    cursor.execute(
        "SELECT * FROM guild_settings WHERE guild_id = ?",
        (guild_id,)
    )

    row = cursor.fetchone()

    # If no configuration found yet, return None
    if row is None:
        return None
    
    # Convert sqlite row into a regular dictionary
    return dict(row)

# ------------------------------------------------------------
# Insert or update guild configuration
# ------------------------------------------------------------
def upsert_settings(conn: sqlite3.Connection, guild_id: int, **fields) -> None:
    """
    Insert or update the configuration for a guild.

    This function is used by the /setup command.

    Example:
        upsert_settings(
            conn,
            guild_id,
            elections_channel_id=128,
            voter_role_id_456
        )
    """
    cursor = conn.cursor()

    # Column names to update (e.g. elections_channel_id, voter_role_id)
    columns = ", ".join(fields.keys())

    # SQL placeholders (?, ?, ?, ...)
    placeholders = ", ".join(fields())

    # Used for ON CONFLICT UPDATE clause
    updates = ", ".join([f"{key} = excluded.{key}" for key in fields.keys()])

    cursor.execute(
        f"""
        INSERT INTO guild_settings (guild_id, {columns})
        VALUES (?, {placeholders})
        ON CONFLICT(guild_id) DO UPDATE SET {updates}
        """,
        (guild_id, *fields.values())
    )

    conn.commit()

# ------------------------------------------------------------
# Admin permission check
# ------------------------------------------------------------
def is_admin(interaction: discord.Interaction, settings: dict | None) -> bool:
    """
    Determine whether the user is allowed to run admin commands.

    Permission rules:
    - Discord server administrators are ALWAYS allowed
    - Users with the configured admin_role_id are allowed
    - Everyone else is denied
    """
    # Rule 1: Discord administrator permission
    if interaction.user.guild_permissions.administrator:
        return True

    # Rule 2: Configured admin role
    if settings and settings.get("admin_role_id"):
        admin_role_id = discord.utils.get(
            interaction.user.roles,
            id=int(settings["admin_role_id"])
        )
        return admin_role_id is not None

    # Rule 3: Not an admin
    return False

# ------------------------------------------------------------
# Voter role check
# ------------------------------------------------------------
def has_voter_role(member: discord.Member, settings: dict) -> bool:
    """
    Check whether a guild member has permission to vote.

    This is used by:
    - Election vote dropdowns
    - Any future citizen-only actions
    """
    voter_role_id = settings.get("voter_role_id")

    # No voter role configured
    if not voter_role_id:
        return False
    
    # Check if the member has the voter role
    voter_role = discord.utils.get(
        member.roles,
        id=int(voter_role_id)
    )

    return voter_role is not None