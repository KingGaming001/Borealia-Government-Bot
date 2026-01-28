Great — here’s a **clean, ready-to-commit `README.md`** tailored to your Borealia Government bot.
You can copy-paste this directly into a file called `README.md` in your repo.

---

```markdown
# 🏛️ Borealia Government Bot

A modular Discord bot designed to manage **government functions on EarthPol**, starting with a secure, private **election system** for the Nation of **Borealia**.

The bot is built to be clean, extensible, and suitable for long-term governance features such as elections, by-elections, and (in future) proposed laws.

---

## ✨ Features

### 🗳️ Elections
- Slash-command based (no message reading required)
- Self-nominations via `/nominate`
- Private voting via dropdown menus
- One vote per user per position
- Admin-only election control
- Private DM results when elections close
- Supports by-elections and re-runs

### ⚙️ Server Configuration
- `/setup` to configure channels and roles
- `/status` to view current configuration
- Per-server persistent configuration

### 🧼 Architecture
- One command per file
- Centralised configuration & permissions
- SQLite database (no external services required)
- Safe for GitHub (no secrets committed)

---

## 📁 Project Structure

```

.
├── main.py                 # Bot entry point
├── db.py                   # Database setup (SQLite)
├── config_store.py         # Guild config + permission helpers
├── requirements.txt
├── .env                    # Bot token (NOT committed)
├── commands/
│   ├── **init**.py
│   ├── setup.py
│   ├── status.py
│   ├── nominate.py
│   ├── open_election.py
│   └── close_election.py
└── data/
└── borealia.db         # SQLite database (auto-created)

````

---

## 🧾 Requirements

- Python **3.11+**
- Discord bot token
- Permissions to add bots to a server

Python dependencies:
```bash
pip install -r requirements.txt
````

---

## 🔐 Environment Setup

Create a `.env` file in the project root:

```
DISCORD_TOKEN=YOUR_BOT_TOKEN_HERE
```

⚠️ **Never commit `.env` to GitHub**

---

## 🤖 Bot Setup (Discord)

1. Create an application in the Discord Developer Portal
2. Add a bot
3. Enable **Server Members Intent**
4. Invite the bot using:

   * Scopes:

     * `bot`
     * `applications.commands`
   * Permissions:

     * Send Messages
     * Embed Links
     * Read Message History

---

## ▶️ Running the Bot

Activate your virtual environment (Windows PowerShell):

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Run the bot:

```bash
python main.py
```

On first run, the bot will:

* Create the `data/` folder
* Create the SQLite database
* Create all required tables

---

## 🛠️ First-Time Configuration (In Discord)

### 1️⃣ Configure the server

```
/setup
```

Set:

* Nominees channel
* Elections channel
* Proposed laws channel
* Voter role
* Admin role
* (Optional) log channel

### 2️⃣ Verify setup

```
/status
```

---

## 🗳️ Election Flow

### Open an election

```
/open_election position:Prime Minister clear_nominations:true
```

### Nominate yourself

```
/nominate position:Prime Minister name:Your Name
```

### Vote

* Use the dropdown menu under the election panel
* Votes are private

### Close election

```
/close_election position:Prime Minister
```

➡ Results are sent **privately via DM** to the admin who closed the election.

---

## 🗄️ Database

* Uses **SQLite**
* Stored locally in `data/borealia.db`
* No external database required
* Easy to back up (copy the file)

---

## 🔮 Planned / Future Features

* Proposed laws & legislative voting
* Automatic by-elections on resignation
* Audit logs & transparency tools
* Role assignment for elected officials
* Additional EarthPol governance modules

---

## 🛡️ Security & Privacy

* Votes are never posted publicly
* No vote counts during active elections
* Results only visible to authorised admins
* Bot token stored securely via `.env`

---

## 📜 License

MIT License

---

## 👑 Maintained By

**Borealia Government**
EarthPol Nation – Borealia

```