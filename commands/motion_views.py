from __future__ import annotations

# Interactive UI components for Parliament motions:
# - vote dropdown used in roll-call messages
# - royal assent buttons used after a motion passes Parliament

import sqlite3
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, cast

import discord
from discord.ext import commands

from config_store import get_settings, has_parliament_role
from commands.motion_utils import get_motion_vote_columns, parse_iso_utc


class MotionVoteSelect(discord.ui.Select):
    def __init__(
        self,
        bot: commands.Bot,
        motion_id: int,
        on_vote_recorded: Callable[[discord.Guild, int], Awaitable[None]],
    ):
        self.bot = bot
        self.db: sqlite3.Connection = cast(Any, bot).db
        self.motion_id = motion_id
        self.on_vote_recorded = on_vote_recorded

        super().__init__(
            placeholder="Select your vote…",
            custom_id=f"motion_vote_select:{motion_id}",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(label="Yes", value="yes", emoji="✅"),
                discord.SelectOption(label="No", value="no", emoji="❌"),
                discord.SelectOption(label="Abstain", value="abstain", emoji="⚪"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        await self.cast(interaction, str(self.values[0]))

    async def cast(self, interaction: discord.Interaction, choice: str):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            return await interaction.response.send_message("❌ Server-only.", ephemeral=True)

        settings = get_settings(self.db, interaction.guild.id)
        if not settings:
            return await interaction.response.send_message("❌ Server not configured. Run /setup first.", ephemeral=True)

        if not has_parliament_role(interaction.user, settings):
            return await interaction.response.send_message("❌ Only Parliament may vote on motions.", ephemeral=True)

        cur = self.db.cursor()
        cur.execute(
            "SELECT status, closes_at FROM motions WHERE guild_id = ? AND motion_id = ?",
            (interaction.guild.id, self.motion_id),
        )
        motion = cur.fetchone()
        if not motion or motion["status"] != "VOTING":
            return await interaction.response.send_message("❌ Voting is not open for this motion.", ephemeral=True)

        closes_at = parse_iso_utc(motion["closes_at"])
        if closes_at and datetime.now(timezone.utc) >= closes_at:
            return await interaction.response.send_message("❌ Voting has already closed for this motion.", ephemeral=True)

        try:
            vote_columns = get_motion_vote_columns(self.db)
            params = (interaction.guild.id, self.motion_id, interaction.user.id, choice)

            # Insert path supports both legacy and migrated schemas.
            if "choice" in vote_columns and "vote" in vote_columns:
                cur.execute(
                    """
                    INSERT INTO motion_votes (guild_id, motion_id, user_id, choice, vote)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (interaction.guild.id, self.motion_id, interaction.user.id, choice, choice),
                )
            elif "choice" in vote_columns:
                cur.execute(
                    """
                    INSERT INTO motion_votes (guild_id, motion_id, user_id, choice)
                    VALUES (?, ?, ?, ?)
                    """,
                    params,
                )
            elif "vote" in vote_columns:
                cur.execute(
                    """
                    INSERT INTO motion_votes (guild_id, motion_id, user_id, vote)
                    VALUES (?, ?, ?, ?)
                    """,
                    params,
                )
            else:
                raise RuntimeError("motion_votes table has no vote column")

            self.db.commit()
        except sqlite3.IntegrityError as exc:
            message = str(exc).lower()
            if "unique constraint failed" in message:
                return await interaction.response.send_message("🔒 Your vote is already recorded and locked.", ephemeral=True)
            return await interaction.response.send_message("❌ Could not record vote due to a database constraint.", ephemeral=True)
        except (sqlite3.OperationalError, RuntimeError):
            return await interaction.response.send_message("❌ Could not record vote due to a database schema mismatch.", ephemeral=True)
        except sqlite3.DatabaseError:
            return await interaction.response.send_message("❌ Could not record vote due to a database error.", ephemeral=True)
        except Exception:
            return await interaction.response.send_message("🔒 Your vote is already recorded and locked.", ephemeral=True)

        await self.on_vote_recorded(interaction.guild, self.motion_id)
        # Acknowledge after callback so users only see success if tally refresh ran.
        await interaction.response.send_message("✅ Vote recorded.", ephemeral=True)


class MotionVoteView(discord.ui.View):
    def __init__(
        self,
        bot: commands.Bot,
        motion_id: int,
        on_vote_recorded: Callable[[discord.Guild, int], Awaitable[None]],
    ):
        super().__init__(timeout=None)
        self.add_item(MotionVoteSelect(bot, motion_id, on_vote_recorded))


class RoyalAssentButton(discord.ui.Button):
    def __init__(
        self,
        assent_handler: Callable[[discord.Interaction, int, str], Awaitable[object]],
        motion_id: int,
        action: str,
    ):
        label = "Approve" if action == "approve" else "Reject"
        style = discord.ButtonStyle.success if action == "approve" else discord.ButtonStyle.danger
        emoji = "✅" if action == "approve" else "❌"
        super().__init__(
            label=label,
            style=style,
            emoji=emoji,
            custom_id=f"motion_assent:{action}:{motion_id}",
        )
        self.assent_handler = assent_handler
        self.motion_id = motion_id
        self.action = action

    async def callback(self, interaction: discord.Interaction):
        await self.assent_handler(interaction, self.motion_id, self.action)


class RoyalAssentView(discord.ui.View):
    def __init__(
        self,
        assent_handler: Callable[[discord.Interaction, int, str], Awaitable[object]],
        motion_id: int,
    ):
        super().__init__(timeout=None)
        self.add_item(RoyalAssentButton(assent_handler, motion_id, "approve"))
        self.add_item(RoyalAssentButton(assent_handler, motion_id, "reject"))
