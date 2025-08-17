import os
import time
import asyncio
from dotenv import load_dotenv
import discord
from discord import app_commands
from discord.ext import commands, tasks
import gspread
from gspread.exceptions import APIError
import json
from oauth2client.service_account import ServiceAccountCredentials
from typing import Optional, Union
import logging

# ───────────────────────────────────────────────────────────────────────────────
# 1) Load .env & config
# ───────────────────────────────────────────────────────────────────────────────
load_dotenv()
TOKEN      = os.getenv("DISCORD_TOKEN")
GUILD_ID   = int(os.getenv("GUILD_ID", 0))
SHEET_NAME = os.getenv("SHEET_NAME")

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN environment variable not set")
if not SHEET_NAME:
    raise RuntimeError("SHEET_NAME environment variable not set")

# ───────────────────────────────────────────────────────────────────────────────
# 2) Google Sheets setup + header enforcement
# ───────────────────────────────────────────────────────────────────────────────
scope       = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
if creds_json:
    keyfile_dict = json.loads(creds_json)
    creds = ServiceAccountCredentials.from_json_keyfile_dict(
        keyfile_dict, scope
    )
else:
    creds = ServiceAccountCredentials.from_json_keyfile_name(
        "credentials.json", scope
    )

gc          = gspread.authorize(creds)
spreadsheet = gc.open(SHEET_NAME)

HEADER_ROW = ["dino_id", "user_id", "Complexity", "Sociability", "Survivability"]
try:
    votes_sheet = spreadsheet.worksheet("Votes")
    if votes_sheet.row_values(1) != HEADER_ROW:
        votes_sheet.delete_rows(1)
        votes_sheet.insert_row(HEADER_ROW, index=1)
except gspread.exceptions.WorksheetNotFound:
    votes_sheet = spreadsheet.add_worksheet(title="Votes", rows="1000", cols="5")
    votes_sheet.append_row(HEADER_ROW)

# ───────────────────────────────────────────────────────────────────────────────
# 3) Metadata sheet setup (persist thread & message IDs)
# ───────────────────────────────────────────────────────────────────────────────
posts_metadata: dict[str, dict[str, int]] = {}

def load_metadata():
    """Locate 'Metadata' sheet, parse only valid rows, and print each loaded dino_id."""
    try:
        ws = spreadsheet.worksheet("Metadata")
        print("📊 Found existing 'Metadata' sheet")

        rows = ws.get_all_records()
        loaded_ids = []

        for row in rows:
            # Thread ID → ensure it’s a numeric string
            thread_val = row.get("Thread ID")
            thread_str = str(thread_val).strip()
            if not thread_str.isdigit():
                continue
            thread_id = int(thread_str)

            # dino_id must be non-empty
            dino_id = str(row.get("dino_id") or "").strip()
            if not dino_id:
                continue

            # Rate message ID (optional)
            rate_str = str(row.get("rate_message_id") or "").strip()
            rate_id  = int(rate_str) if rate_str.isdigit() else 0

            # Results message ID (optional)
            results_str = str(row.get("results_message_id") or "").strip()
            results_id  = int(results_str) if results_str.isdigit() else 0

            posts_metadata[dino_id] = {
                "thread_id": thread_id,
                "rate_msg_id": rate_id,
                "results_msg_id": results_id,
            }
            loaded_ids.append(dino_id)

        if loaded_ids:
            print(f"📊 Loaded metadata for {len(loaded_ids)} dinos: {', '.join(loaded_ids)}")
        else:
            print("📊 'Metadata' sheet is present but empty or all rows invalid.")

    except gspread.exceptions.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title="Metadata", rows="1000", cols="4")
        ws.append_row([
            "Thread ID", "dino_id", "rate_message_id", "results_message_id"
        ])
        print("📊 Created new 'Metadata' sheet with headers.")

    return ws

metadata_ws = load_metadata()

# ───────────────────────────────────────────────────────────────────────────────
# 4) Caching & rate-limit helpers for compiled sheet reads
# ───────────────────────────────────────────────────────────────────────────────
sheet_lock = asyncio.Lock()
RESULTS_CACHE = None
LAST_RESULTS_FETCH = 0
CACHE_TTL = 45 * 60

async def fetch_compiled_records():
    global RESULTS_CACHE, LAST_RESULTS_FETCH
    now = time.time()
    if RESULTS_CACHE and (now - LAST_RESULTS_FETCH) < CACHE_TTL:
        return RESULTS_CACHE
    async with sheet_lock:
        now = time.time()
        if RESULTS_CACHE and (now - LAST_RESULTS_FETCH) < CACHE_TTL:
            return RESULTS_CACHE
        try:
            comp_sheet = spreadsheet.worksheet("Compiled")
            recs = comp_sheet.get_all_records()
            RESULTS_CACHE = recs
            LAST_RESULTS_FETCH = time.time()
            return recs
        except APIError as e:
            if e.response.status_code == 429 and RESULTS_CACHE:
                return RESULTS_CACHE
            raise

# ───────────────────────────────────────────────────────────────────────────────
# 5) Vote logic + retry wrapper
# ───────────────────────────────────────────────────────────────────────────────
def update_vote(dino_id: str, user_id: str, category: str, rating: int):
    records = votes_sheet.get_all_records()
    col_map = {"Complexity": 3, "Sociability": 4, "Survivability": 5}
    row_idx = next((i for i,r in enumerate(records, start=2)
                    if r["dino_id"]==dino_id and str(r["user_id"])==user_id), None)
    if row_idx:
        votes_sheet.update_cell(row_idx, col_map[category], str(rating))
    else:
        votes_sheet.append_row([dino_id, user_id, "", "", ""])
        new_row = len(votes_sheet.get_all_records())+1
        votes_sheet.update_cell(new_row, col_map[category], str(rating))

async def safe_update_vote(dino_id: str, user_id: str, category: str, rating: int):
    backoff = 1
    for attempt in range(3):
        try:
            await asyncio.to_thread(update_vote, dino_id, user_id, category, rating)
            return
        except APIError as e:
            code = e.response.status_code
            if code in (429,500,503) and attempt<2:
                await asyncio.sleep(backoff)
                backoff*=2
                continue
            raise

# ───────────────────────────────────────────────────────────────────────────────
# 6) Bot & intents setup
# ───────────────────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
logger = logging.getLogger("anthranks")

class AnthranksBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="!", intents=intents)
        self.target_guild = discord.Object(id=GUILD_ID) if GUILD_ID else None
        self.results_messages: dict[str, discord.Message] = {}

    async def setup_hook(self):
        if self.target_guild:
            self.tree.copy_global_to(guild=self.target_guild)
            await self.tree.sync(guild=self.target_guild)
            print(f"✅ Synced slash commands to guild {GUILD_ID}")
        else:
            await self.tree.sync()
            print("✅ Synced global slash commands")

bot = AnthranksBot()

# ───────────────────────────────────────────────────────────────────────────────
# 7) Rating UI & retry
# ───────────────────────────────────────────────────────────────────────────────
class RatingSelect(discord.ui.Select):
    def __init__(self, dino_id: str, category: str):
        opts = [discord.SelectOption(label=str(i), value=str(i)) for i in range(1, 6)]
        # give each dropdown a stable custom_id so it's persistent
        cid = f"rate_select:{dino_id}:{category}"
        super().__init__(
            placeholder=category,
            min_values=1,
            max_values=1,
            options=opts,
            custom_id=cid
        )
        self.dino_id = dino_id
        self.category = category

    async def callback(self, interaction: discord.Interaction):
        rating = int(self.values[0])
        await interaction.response.defer(ephemeral=True)
        try:
            await safe_update_vote(
                self.dino_id,
                str(interaction.user.id),
                self.category,
                rating
            )
        except APIError as e:
            if e.response.status_code == 429:
                await interaction.followup.send(
                    "⚠️ Sheets quota reached; try in a minute.",
                    ephemeral=True
                )
            else:
                logger.exception("Sheets APIError in rating callback")
                await interaction.followup.send(
                    "⚠️ Could not record rating, try later.",
                    ephemeral=True
                )
            return

        await interaction.followup.send(
            f"You rated **{self.dino_id}** • {self.category} • {rating}",
            ephemeral=True
        )


class RateView(discord.ui.View):
    def __init__(self, dino_id: str):
        # must be timeout=None for persistence
        super().__init__(timeout=None)
        for c in ["Complexity", "Sociability", "Survivability"]:
            self.add_item(RatingSelect(dino_id, c))

@bot.tree.command(
    name="rate",
    description="Rate a dinosaur on three categories",
    guild=bot.target_guild
)
@app_commands.describe(dino="The dinosaur ID or name you want to rate")
async def slash_rate(interaction: discord.Interaction, dino: str):
    embed = discord.Embed(
        title=f"Rate {dino}",
        description="Use the dropdown below to rate (1–5)."
    )

    embed.add_field(
        name="Complexity",
        value="1 = very simple, 5 = very complex",
        inline=False
    )
    embed.add_field(
        name="Sociability",
        value="1 = mostly solitary, 5 = highly social",
        inline=False
    )
    embed.add_field(
        name="Survivability",
        value="1 = low survivability, 5 = very resilient",
        inline=False
    )

    await interaction.response.send_message(embed=embed, view=RateView(dino))
    msg = await interaction.original_response()
    save_metadata(interaction.channel, dino, msg, None)

# ───────────────────────────────────────────────────────────────────────────────
# 8) Stars & results embed
# ───────────────────────────────────────────────────────────────────────────────
FULL_STAR  = "<:Full:1401549320523878552>"
HALF_STAR  = "<:Half:1401549345052164196>"
EMPTY_STAR = "<:Empty:1401549305688621147>"

def star_display(r: float) -> str:
    f = int(r)
    h = 1 if (r - f) >= 0.5 else 0
    e = 5 - f - h
    return FULL_STAR * f + HALF_STAR * h + EMPTY_STAR * e

async def generate_results_embed(dino_filter: Optional[str] = None) -> discord.Embed:
    recs = await fetch_compiled_records()
    if dino_filter:
        recs = [r for r in recs if r["dino_id"] == dino_filter]
        if not recs:
            raise ValueError(f"Dinosaur ID '{dino_filter}' not found")
    title = f"{dino_filter} Rating" if dino_filter else "🦖 Dinosaur Ratings"
    emb = discord.Embed(title=title, description="Auto-updates every 45 minutes.")
    for r in recs:
        emb.add_field(
            name=r["dino_id"],
            value=(
                f"Complexity:    {star_display(float(r['Complexity']))}\n"
                f"Sociability:   {star_display(float(r['Sociability']))}\n"
                f"Survivability: {star_display(float(r['Survivability']))}"
            ),
            inline=False
        )
    return emb

# ───────────────────────────────────────────────────────────────────────────────
# 9) Updater loop
# ───────────────────────────────────────────────────────────────────────────────
@tasks.loop(minutes=45)
async def results_updater():
    for key, target in list(bot.results_messages.items()):
        try:
            emb = await generate_results_embed(None if key == "__all__" else key)
            await target.edit(embed=emb)
            print(f"🔄 Updated '{key}' embed in channel {target.channel.id}, message {target.id}")
        except Exception as e:
            print(f"❌ Failed to update '{key}': {e}")
            bot.results_messages.pop(key, None)

# ───────────────────────────────────────────────────────────────────────────────
# 10) /results command
# ───────────────────────────────────────────────────────────────────────────────
@bot.tree.command(
    name="results",
    description="Post or update the compiled dinosaur ratings",
    guild=bot.target_guild
)
@app_commands.describe(
    thread_id="Thread ID (digits only)",
    channel="Text/forum channel",
    dino="Optional dino_id"
)
async def slash_results(
    interaction: discord.Interaction,
    thread_id: Optional[str] = None,
    channel: Optional[Union[discord.TextChannel, discord.ForumChannel]] = None,
    dino: Optional[str] = None
):
    # choose destination
    if thread_id:
        try:
            snow = int(thread_id)
        except:
            return await interaction.response.send_message(
                "❌ `thread_id` digits only", ephemeral=True
            )
        dest = bot.get_channel(snow) or await bot.fetch_channel(snow)
        if not isinstance(dest, discord.Thread):
            return await interaction.response.send_message(
                "❌ Not a thread or inaccessible", ephemeral=True
            )
    else:
        dest = channel or interaction.channel

    # build embed
    try:
        emb = await generate_results_embed(dino)
    except ValueError as ve:
        return await interaction.response.send_message(str(ve), ephemeral=True)

    # send or update
    if isinstance(dest, discord.ForumChannel):
        msg = await dest.send(embed=emb)
        thread_obj = msg.thread
        target = thread_obj.get_partial_message(msg.id)
    else:
        thread_obj = dest
        target = await dest.send(embed=emb)

    key = "__all__" if not dino else dino
    bot.results_messages[key] = target
    if not results_updater.is_running():
        results_updater.start()

    # persist results_message_id
    existing = posts_metadata.get(key)
    if existing and existing["thread_id"] == thread_obj.id:
        cell = metadata_ws.find(key, in_column=2)
        metadata_ws.update_cell(cell.row, 4, str(target.id))
        posts_metadata[key]["results_msg_id"] = target.id
        print(f"📊 Updated metadata for results_message_id of '{key}'")
    else:
        save_metadata(thread_obj, key, None, target)

    await interaction.response.send_message(
        f"✅ Posted ratings for **{key}** in {dest.mention}.", ephemeral=True
    )

# ───────────────────────────────────────────────────────────────────────────────
# 11) Helper to revive archived channel if necessary
# ───────────────────────────────────────────────────────────────────────────────

async def ensure_thread_open(channel: discord.abc.GuildChannel, dino_id: str, ctx: str):
    """Unarchive/unlock a thread if needed before we edit messages."""
    if isinstance(channel, discord.Thread):
        # Refetch to avoid stale archived/locked flags
        try:
            channel = await bot.fetch_channel(channel.id)
        except Exception:
            pass

        if getattr(channel, "archived", False) or getattr(channel, "locked", False):
            try:
                await channel.edit(archived=False, locked=False)
                print(f"🧵 Unarchived thread {channel.id} for '{dino_id}' ({ctx})")
            except discord.Forbidden:
                print(f"🚫 Missing Manage Threads to unarchive {channel.id} for '{dino_id}' ({ctx})")
                return None
            except discord.HTTPException as e:
                print(f"❌ Failed to unarchive {channel.id} for '{dino_id}' ({ctx}): {e}")
                return None
    return channel
# ───────────────────────────────────────────────────────────────────────────────
# 12) on_ready: revive views & seed updater
# ───────────────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"🚀 Logged in as {bot.user} (ID: {bot.user.id})")

    revived_dropdowns = 0
    revived_embeds    = 0

    failed_rate_ids  = []
    failed_embed_ids = []

    # 1) Revive rate dropdowns
    for dino_id, meta in posts_metadata.items():
        thread_id   = meta.get("thread_id")
        rate_msg_id = meta.get("rate_msg_id")
        if not thread_id or not rate_msg_id:
            continue

        # fetch channel
        try:
            channel = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
        except discord.NotFound:
            print(f"🔸 Channel {thread_id} for '{dino_id}' not found")
            failed_rate_ids.append(dino_id)
            continue
        except discord.Forbidden:
            print(f"🔸 No access to channel {thread_id} for '{dino_id}'")
            failed_rate_ids.append(dino_id)
            continue
        except Exception as e:
            print(f"🔸 Error fetching channel {thread_id} for '{dino_id}': {e}")
            failed_rate_ids.append(dino_id)
            continue

        # Ensure thread is open before editing
        channel = await ensure_thread_open(channel, dino_id, ctx="rate")
        if channel is None:
            failed_rate_ids.append(dino_id)
            continue

        # fetch message
        try:
            msg = await channel.fetch_message(rate_msg_id)
        except discord.NotFound:
            print(f"🔸 Rate message {rate_msg_id} for '{dino_id}' not found")
            failed_rate_ids.append(dino_id)
            continue
        except Exception as e:
            print(f"🔸 Error fetching rate message {rate_msg_id} for '{dino_id}': {e}")
            failed_rate_ids.append(dino_id)
            continue

        # edit view
        try:
            view = RateView(dino_id)
            await msg.edit(view=view)
            bot.add_view(view, message_id=rate_msg_id)
            revived_dropdowns += 1
        except Exception as e:
            print(f"🔸 Could not patch rate view for '{dino_id}' ({rate_msg_id}): {e}")
            failed_rate_ids.append(dino_id)

    # 2) Revive result embeds
    for dino_id, meta in posts_metadata.items():
        thread_id      = meta.get("thread_id")
        results_msg_id = meta.get("results_msg_id")
        if not thread_id or not results_msg_id:
            continue

        try:
            channel = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
        except Exception:
            failed_embed_ids.append(dino_id)
            continue

        # Ensure thread is open before fetching/updating
        channel = await ensure_thread_open(channel, dino_id, ctx="results")
        if channel is None:
            failed_embed_ids.append(dino_id)
            continue

        try:
            msg = await channel.fetch_message(results_msg_id)
        except Exception:
            failed_embed_ids.append(dino_id)
            continue

        key = "__all__" if dino_id == "__all__" else dino_id
        bot.results_messages[key] = msg
        revived_embeds += 1

    # 3) Start updater
    if bot.results_messages and not results_updater.is_running():
        results_updater.start()

    # 4) Summary
    print(f"✅ Patched & revived {revived_dropdowns} rate dropdowns.")
    print(f"✅ Revived {revived_embeds} result embeds.")
    
    if failed_rate_ids:
        print(f"⚠️ Failed to revive rate dropdowns for: {', '.join(failed_rate_ids)}")
    if failed_embed_ids:
        print(f"⚠️ Failed to revive embeds for:        {', '.join(failed_embed_ids)}")

bot.run(TOKEN)