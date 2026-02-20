# Borealia Government Bot

A Discord bot for running role-based government workflows in a server:

- Election scheduling and nominations
- Private ballot voting via dropdowns
- Election close + private results to admins
- Parliament motion tracking with roll-call voting

Built with `discord.py` and SQLite.

## Features

- **Per-server configuration** via `/setup`
- **Election lifecycle**
	- Schedule elections in advance (`SCHEDULED`)
	- Nominations open before vote start
	- Automatic transition to `VOTING` at scheduled time
	- Voter role enforcement + locked votes (no vote changes)
- **Motion lifecycle**
	- Draft, open, vote, close
	- 24-hour fixed voting window once opened
	- Parliament-role-only voting
	- Public Yes/No/Abstain roll-call
	- Automatic conclusion message with final result
	- If a motion passes, Royal Assent buttons appear (Approve/Reject) for the configured King role
	- Approved motions are automatically posted into the laws channel
	- Enacted acts are tracked and can be repealed by a dedicated repeal motion vote

## Tech Stack

- Python 3.11+
- `discord.py>=2.3.0`
- `python-dotenv>=1.0.0`
- `tzdata` (timezone support, especially on Windows)
- SQLite database at `data/borealia.db`

## Project Structure

- `main.py` — bot startup, extension loading, election scheduler
- `db.py` — SQLite schema + migrations
- `config.py` — env/config values
- `config_store.py` — guild settings + permission helpers
- `commands/` — slash command modules

## Setup

### 1) Clone and enter project

```powershell
cd "C:\Users\YOURUSER\Documents\Borealia Government Bot"
```

### 2) Create virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 3) Install dependencies

```powershell
pip install -r requirements.txt
```

### 4) Create `.env`

Create a `.env` file in the project root:

```env
DISCORD_TOKEN=your_bot_token_here
# Optional: faster slash command sync to one guild while developing
TEST_GUILD_ID=123456789012345678
```

`TEST_GUILD_ID` is optional. Leave it unset for global sync.

### 5) Run the bot

```powershell
python main.py
```

## First-Time Server Configuration

Run `/setup` (server admins or configured admin role only):

- `nominees_channel`
- `elections_channel`
- `laws_channel`
- `voter_role`
- `admin_role`
- Optional: `log_channel`
- Optional: `parliament_channel`
- Optional: `parliament_role`
- Optional: `king_role`

Use `/status` to inspect saved configuration.

## Command Reference

### Configuration

#### `/setup`
Configure channels and roles for the current server.

**Admin only** (Discord Administrator or configured admin role).

#### `/status`
Show current bot configuration for the server.

**Admin only**.

---

### Elections

#### `/open_election`
Schedule an election for a position.

Parameters:

- `position` — office name (example: `Prime Minister`)
- `start_time` — Europe/London time in `YYYY-MM-DD HH:MM`
- `clear_nominees` (optional, default `false`) — also clear nominees for a fresh cycle

Behavior:

- Sets election status to `SCHEDULED`
- Clears prior votes for that position
- Opens nominations immediately
- Voting message is posted automatically at start time by the scheduler

#### `/nominate`
Nominate yourself for an available scheduled election.

Parameter:

- `name` — ballot display name

Behavior:

- Shows a dropdown of elections currently accepting nominations
- Upserts your nomination for selected position
- Updates/creates nominees embed in nominees channel

#### `/close_election`
Close an election early (or close a scheduled one before start).

Parameter:

- `position` — office to close

Behavior:

- Sets status to `CLOSED`
- Disables voting UI message (if present)
- Updates nominees message to closed state (if present)
- Sends private results to command user by DM

**Admin only**.

---

### Parliament Motions

#### `/motion_create`
Create a draft motion.

Parameters:

- `kind` — type (act, resolution, confidence, etc.)
- `title` — short title
- `text` — full motion text

State after creation: `DRAFT`.

**Admin only**.

#### `/motion_open`
Open voting on a draft motion and post roll-call message.

Parameters:

- `motion_id`

State change: `DRAFT` → `VOTING`.

Behavior:

- Motion voting always lasts **24 hours** from open time
- Motion is closed automatically after 24 hours
- A new result message is posted when the motion concludes

**Admin only**.

#### `/motion_vote`
Cast a motion vote (Parliament only).

Parameter:

- `motion_id`

Behavior:

- Ephemeral dropdown: Yes / No / Abstain
- One vote per user per motion (locked after first vote)
- Public roll-call message updates after each vote

#### `/motion_close`
Close a motion and publish final tally in roll-call message.

Parameter:

- `motion_id`

State change: `VOTING` → `CLOSED`.

**Admin only**.

#### `/motion_results`
Show current or final motion tallies.

Parameter:

- `motion_id`

#### `/motion_repeal`
Create a repeal motion targeting an enacted act.

Parameters:

- `act_id` — enacted act number
- `reason` — explanation for repeal

Behavior:

- Creates a `repeal` motion in `DRAFT` targeting that act
- Uses the normal motion flow (`/motion_open` → vote → close)
- If passed and given Royal Assent, the target act is marked repealed

### Motion Flow Example

1. Admin creates draft: `/motion_create kind:act title:"Budget Act" text:"..."`
2. Admin opens voting: `/motion_open motion_id:12`
3. Parliament members vote with `/motion_vote motion_id:12` and select **Yes / No / Abstain** from dropdown
4. After 24 hours, the bot automatically closes the motion and posts a new message with the final result
5. If result is **PASSED**, members with the configured King role can click **Approve** or **Reject** for Royal Assent
6. **Approve** posts the motion into the laws channel; **Reject** marks the motion as **FAILED**
7. Repeal motions (`/motion_repeal`) can repeal enacted acts after passing + Royal Assent

---

## Permissions Model

- **Discord Administrator** always has admin access to bot admin commands.
- **Configured admin role** (from `/setup`) also has admin access.
- **Configured voter role** can vote in elections.
- **Configured parliament role** can vote on motions.
- **Configured king role** can grant or reject Royal Assent on passed motions.

## Database

Tables are created automatically at startup in `db.py`:

- `guild_settings`
- `elections`
- `nominations`
- `votes`
- `motions`
- `motion_votes`
- `acts`

Database file path: `data/borealia.db`.

## Notes

- Time parsing for `/open_election` is interpreted as **Europe/London**.
- If timezone data is unavailable on Windows, `tzdata` dependency provides it.
- The election scheduler in `main.py` checks every 30 seconds for elections that should enter voting.
- Motion close checks run every 30 seconds and automatically conclude expired motion votes.

## Development Tips

- For faster slash command iteration, set `TEST_GUILD_ID` in `.env`.
- If slash commands look stale, restart the bot and allow command sync to complete.
- Use `/status` after `/setup` to verify channels and roles are saved correctly.
