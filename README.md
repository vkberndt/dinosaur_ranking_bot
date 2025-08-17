# 🦖 DinoRateBot DEMO BRANCH

## 🚀 Run Locally
- Copy env.example → .env
- Fill in your Discord token, Google Sheets credentials, etc.
- Install dependencies and start the bot:
pip install -r requirements.txt && python bot.py



## ✨ Overview
DinoRateBot lets your Discord community rate dinosaurs via persistent dropdowns and view auto-updating results. The bot survives restarts and redeploys, reviving archived threads automatically. It stores message and thread IDs in Google Sheets and reattaches persistent views on startup.

## 🛠️ Commands
| Command | Description | 
| /rate | Posts an embed with three dropdowns (Complexity, Sociability, Survivability). | 
| /results | Posts a results embed (global or per-dino) and updates it on a scheduled cadence. | 



## 🔒 Permissions
- View Channels
- Read Message History
- Send Messages
- Send Messages in Threads
- Manage Threads (to unarchive/unlock)

## 💾 Persistence
- Each dropdown selection is linked to a stable dino_id.
- Metadata tab in Google Sheets stores:
  - Thread ID
  - dino_id
  - rate_message_id
  - results_message_id
- On startup, the bot fetches messages by ID and re-attaches persistent views.

## 🏗️ Architecture
- Discord.py for slash commands, Views, and background tasks
- Google Sheets for data storage:
  - Votes: raw user ratings
  - Compiled: aggregated scores (read-only)
  - Metadata: durable state (Thread ID, dino_id, rate_message_id, results_message_id)
Startup Revival
- Load metadata safely, skipping rows missing Thread ID or dino_id.
- Log a summary:
- Loaded metadata for X dinos: dino_id1, dino_id2, …
- Unarchive/unlock threads if needed.
- Fetch rate_message_id and results_message_id, attach views, and seed the results updater.
Updater Loop
- Regenerates all embeds on a 45-minute cadence.
- Uses TTL caching for Sheets reads to avoid quota spikes.
- Retries with exponential backoff on 429/5xx errors.

## ⚙️ Setup
- Create a Google Sheet with three tabs:
  - Votes
  - Compiled
  - Metadata
- Set Metadata headers:
Thread ID | dino_id | rate_message_id | results_message_id
- Provide a credentials.json Google Service Account with access to the sheet.

## 🧩 Environment Variables
```bash
DISCORD_TOKEN=your_discord_bot_token
GUILD_ID=your_guild_id
SHEET_NAME=your_google_sheet_name
GOOGLE_SHEETS_CREDENTIALS_PATH=credentials.json
```



## 📝 Key Implementation Details
- Persistent Views
Legacy posts are patched via message.edit(view=RateView(...)) so their components become persistent.
  - Safe Metadata Load
  - Skip rows missing Thread ID or dino_id.
  - Print a summary:
  - Loaded metadata for X dinos: dino_id1, dino_id2, …

- Revival Process
  - Unarchive/unlock thread channels by Thread ID.
  - Re-attach views to rate_message_id and results_message_id.
  - Track and log failures per dino_id.
- Sheets Resilience
  - Cache reads of the Compiled tab with a TTL.
  - Retry calls with exponential backoff on rate limits or server errors.
  - Optionally upsert new rate_message_id and results_message_id when posting commands to keep Metadata current.

    ---
  
## 📄 LicenseMIT – Fork, adapt, and have fun. If you improve the revival flow or add a database backend, I’d love a PR or a note.
