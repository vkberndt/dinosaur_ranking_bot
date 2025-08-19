import os
import discord
from discord import app_commands
from discord.ext import commands, tasks
from oauth2client.service_account import ServiceAccountCredentials
import gspread
from datetime import datetime, timezone, time
from dotenv import load_dotenv

# =============================================================================
# 1) Load environment variables from .env
# =============================================================================
load_dotenv()

DISCORD_TOKEN   = os.getenv("DISCORD_TOKEN")
LOG_CHANNEL_ID  = int(os.getenv("LOG_CHANNEL_ID", 0))
SHEET_ID        = os.getenv("SHEET_ID")
GOOGLE_CREDS    = os.getenv("GOOGLE_SHEETS_CREDENTIALS_PATH", "credentials.json")

if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in .env")
if not LOG_CHANNEL_ID:
    raise RuntimeError("Missing LOG_CHANNEL_ID in .env")
if not SHEET_ID:
    raise RuntimeError("Missing SHEET_ID in .env")

# =============================================================================
# 2) Authenticate with Google Sheets
# =============================================================================
scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]
creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_CREDS, scope)
client = gspread.authorize(creds)
entries_sheet = client.open_by_key(SHEET_ID).worksheet("Entries")

# =============================================================================
# 3) Bot Configuration & Intents
# =============================================================================
SPECIES = [
    "Barsboldia", "Eotriceratops", "Parasaurolophus", "Tyrannosaurus",
    "Allosaurus", "Amargasaurus", "Daspletosaurus", "Hatzegopteryx",
    "Iguanodon", "Pachyrhinosaurus", "Yutyrannus", "Achillobator",
    "Albertaceratops", "Alioramus", "Anodontosaurus", "Concavenator",
    "Tenontosaurus", "Tropeognathus", "Camptosaurus", "Compsognathus",
    "Psittacosaurus", "Rhamphorhynchus", "Struthiomimus", "Apatosaurus", "Latenivenatrix", "Lambeosaurus",
    "Stegosaurus", "Gigantoraptor", "Giganotosaurus", "Deinonychus", "Kentrosaurus", "Ceratosaurus", "Giant Salamander", "Megalania", "Torvosaurus", "Spinosaurus"  
]

TIERS = ["Bronze", "Silver", "Gold", "Diamond"]

TIER_IMAGES = {
    "Bronze":  "https://cdn.discordapp.com/emojis/1382035517994176522.webp?size=96",
    "Silver":  "https://cdn.discordapp.com/emojis/1382035523522265088.webp?size=96",
    "Gold":    "https://cdn.discordapp.com/emojis/1382035515393441802.webp?size=96",
    "Diamond": "https://cdn.discordapp.com/emojis/1382035520732921976.webp?size=96"
}

EMBED_COLORS = {
    "Bronze":  0xCD7F32,
    "Silver":  0xC0C0C0,
    "Gold":    0xFFD700,
    "Diamond": 0x00FFFF
}

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# =============================================================================
# Autocomplete helper for species (avoids 25-choice limit)
# =============================================================================
async def species_autocomplete(
    interaction: discord.Interaction,
    current: str
) -> list[app_commands.Choice[str]]:
    return [
        app_commands.Choice(name=s, value=s)
        for s in SPECIES
        if current.lower() in s.lower()
    ][:25]

# =============================================================================
# 4) /milestone Slash Command
# =============================================================================
@bot.tree.command(
    name="milestone",
    description="Submit your species, tier, and character-sheet link"
)
@app_commands.describe(
    species="Type to autocomplete your species",
    tier="Select your milestone tier",
    sheet_url="Link to your character sheet"
)
@app_commands.autocomplete(species=species_autocomplete)
@app_commands.choices(
    tier=[app_commands.Choice(name=t, value=t) for t in TIERS]
)
async def milestone(
    interaction: discord.Interaction,
    species: str,
    tier: str,
    sheet_url: str
):
    await interaction.response.defer(ephemeral=True)

    now       = datetime.now(timezone.utc)
    timestamp = now.isoformat()
    row = [
        timestamp,
        str(interaction.user.id),
        str(interaction.user),
        species,
        tier,
        sheet_url
    ]
    try:
        entries_sheet.append_row(row, value_input_option="RAW")
    except Exception as e:
        return await interaction.followup.send(
            f"❌ Logging failed: {e}", ephemeral=True
        )

    log_ch = bot.get_channel(LOG_CHANNEL_ID)
    if not log_ch:
        return await interaction.followup.send(
            "❌ Log channel not found. Check your LOG_CHANNEL_ID.", ephemeral=True
        )

    embed = discord.Embed(
        title=f"{interaction.user.display_name} earned a {tier} milestone!",
        color=EMBED_COLORS[tier],
        timestamp=now
    )
    embed.add_field(name="Species",             value=species,   inline=False)
    embed.add_field(name="Tier",                value=tier,      inline=False)
    embed.add_field(name="Character Sheet URL", value=sheet_url, inline=False)
    embed.set_image(url=TIER_IMAGES[tier])

    await log_ch.send(embed=embed)
    await interaction.followup.send("✅ Milestone logged and announced!", ephemeral=True)

# =============================================================================
# 5) /leaderboard Slash Command
# =============================================================================
@bot.tree.command(
    name="leaderboard",
    description="Show the current top 5 players with tier breakdown"
)
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)

    pt_sheet = client.open_by_key(SHEET_ID).worksheet("PlayerTotals")
    records  = pt_sheet.get_all_records()
    top5     = sorted(records, key=lambda r: r.get("Total", 0), reverse=True)[:5]

    now   = datetime.now(timezone.utc)
    embed = discord.Embed(
        title="🏆 Leaderboard: Top 5 Players",
        color=0x00FF00,
        timestamp=now
    )

    guild = interaction.guild
    for i, rec in enumerate(top5, start=1):
        raw_id  = rec.get("DiscordID")
        total   = rec.get("Total",   0)
        bronze  = rec.get("Bronze",  0)
        silver  = rec.get("Silver",  0)
        gold    = rec.get("Gold",    0)
        diamond = rec.get("Diamond", 0)

        try:
            member = guild.get_member(int(raw_id)) or await guild.fetch_member(int(raw_id))
            name   = member.display_name
        except:
            name = f"User {raw_id}"

        value = (
            f"Milestones: {total}\n"
            f"- Bronze: {bronze}\n"
            f"- Silver: {silver}\n"
            f"- Gold: {gold}\n"
            f"- Diamond: {diamond}"
        )
        embed.add_field(name=f"{i}. {name}", value=value, inline=False)

    await interaction.followup.send(embed=embed, ephemeral=True)

# =============================================================================
# 6) /remove_milestone Slash Command
# =============================================================================
@bot.tree.command(
    name="remove_milestone",
    description="Remove your last logged milestone for a given species and tier"
)
@app_commands.describe(
    species="Type to autocomplete the species to remove",
    tier="Select the tier of the milestone to remove"
)
@app_commands.autocomplete(species=species_autocomplete)
@app_commands.choices(
    tier=[app_commands.Choice(name=t, value=t) for t in TIERS]
)
async def remove_milestone(
    interaction: discord.Interaction,
    species: str,
    tier: str
):
    await interaction.response.defer(ephemeral=True)

    # We know headers: Timestamp, DiscordID, DiscordName, Species, Tier, SheetURL
    all_rows = entries_sheet.get_all_values()
    matched_rows = [
        idx
        for idx, row in enumerate(all_rows[1:], start=2)
        if row[1] == str(interaction.user.id)   # DiscordID in col B
        and row[3] == species                   # Species in col D
        and row[4] == tier                      # Tier in col E
    ]

    if not matched_rows:
        return await interaction.followup.send(
            f"❌ No matching milestone found for {species} ({tier}).",
            ephemeral=True
        )

    # delete the most recent
    target = matched_rows[-1]

    sheet_id = entries_sheet._properties["sheetId"]
    requests = [{
        "deleteDimension": {
            "range": {
                "sheetId": sheet_id,
                "dimension": "ROWS",
                "startIndex": target-1,
                "endIndex": target
            }
        }
    }]

    try:
        client.open_by_key(SHEET_ID).batch_update({"requests": requests})
    except Exception as e:
        return await interaction.followup.send(
            f"❌ Failed to remove entry: {e}", ephemeral=True
        )

    await interaction.followup.send(
        f"✅ Removed your milestone entry for {species} ({tier}).",
        ephemeral=True
    )

# =============================================================================
# 7) /my_stats Slash Command
# =============================================================================
@bot.tree.command(
    name="my_stats",
    description="See your personal milestone stats"
)
async def my_stats(interaction: discord.Interaction):
    rows = entries_sheet.get_all_records()
    stats = {}
    uid   = str(interaction.user.id)
    for rec in rows:
        if str(rec.get("DiscordID")) != uid:
            continue
        sp, tier = rec.get("Species"), rec.get("Tier")
        stats.setdefault(sp, {t: 0 for t in TIERS})
        stats[sp][tier] += 1

    if not stats:
        return await interaction.response.send_message(
            "You have no milestones logged.", ephemeral=True
        )

    now   = datetime.now(timezone.utc)
    embed = discord.Embed(
        title=f"{interaction.user.display_name}'s Milestone Stats",
        color=0x00FF00,
        timestamp=now
    )

    for sp in sorted(stats):
        counts = stats[sp]
        lines  = [f"- {t}: {counts[t]}" for t in TIERS]
        embed.add_field(name=sp, value="\n".join(lines), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)

# =============================================================================
# 8) Daily Leaderboard Task
# =============================================================================
@tasks.loop(time=time(hour=0, minute=0, tzinfo=timezone.utc))
async def daily_leaderboard():
    pt_sheet = client.open_by_key(SHEET_ID).worksheet("PlayerTotals")
    records  = pt_sheet.get_all_records()
    top5     = sorted(records, key=lambda r: r.get("Total", 0), reverse=True)[:5]

    now   = datetime.now(timezone.utc)
    embed = discord.Embed(
        title="🏆 Daily Leaderboard: Top 5 Players",
        color=0x00FF00,
        timestamp=now
    )

    ch = bot.get_channel(LOG_CHANNEL_ID)
    if not ch:
        return
    guild = ch.guild

    for i, rec in enumerate(top5, start=1):
        raw_id  = rec.get("DiscordID")
        total   = rec.get("Total",   0)
        bronze  = rec.get("Bronze",  0)
        silver  = rec.get("Silver",  0)
        gold    = rec.get("Gold",    0)
        diamond = rec.get("Diamond", 0)

        try:
            member = guild.get_member(int(raw_id)) or await guild.fetch_member(int(raw_id))
            name   = member.display_name
        except:
            name = f"User {raw_id}"

        value = (
            f"Milestones: {total}\n"
            f"- Bronze: {bronze}\n"
            f"- Silver: {silver}\n"
            f"- Gold: {gold}\n"
            f"- Diamond: {diamond}"
        )
        embed.add_field(name=f"{i}. {name}", value=value, inline=False)

    await ch.send(embed=embed)

@daily_leaderboard.before_loop
async def before_leaderboard():
    await bot.wait_until_ready()

# =============================================================================
# 9) Bot Events & Startup
# =============================================================================
@bot.event
async def on_ready():
    print(f"✔️ Logged in as {bot.user}")
    try:
        await bot.tree.sync()
        print("🔄 Slash commands synced.")
    except Exception as e:
        print("❌ Command sync failed:", e)

    if not daily_leaderboard.is_running():
        daily_leaderboard.start()
        print("⏰ Daily leaderboard task started.")

bot.run(DISCORD_TOKEN)