import os, json
from dotenv import load_dotenv

load_dotenv()

def get_int(key, default=None):
    v = os.getenv(key)
    return int(v) if v and v.isdigit() else default

def get_json(key, default=None):
    v = os.getenv(key)
    try:
        return json.loads(v) if v else default
    except json.JSONDecodeError:
        return default

# ── Secrets & Database ──────────────────────────────
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL  = os.getenv("DATABASE_URL")

# ── Core Channel & Role IDs ─────────────────────────
LOG_CHANNEL_ID      = get_int("LOG_CHANNEL_ID")
HELP_CHANNEL_ID     = get_int("HELP_CHANNEL_ID")
TICKET_CATEGORY_ID  = get_int("TICKET_CATEGORY_ID")
SUPPORT_ROLE_ID     = get_int("SUPPORT_ROLE_ID")
HISTORY_CHANNEL_ID  = get_int("HISTORY_CHANNEL_ID")
WELCOME_CHANNEL_ID  = get_int("WELCOME_CHANNEL_ID")
LEAVE_CHANNEL_ID    = get_int("LEAVE_CHANNEL_ID")
ANNOUNCEMENTS_CHANNEL_ID = int(os.getenv("ANNOUNCEMENTS_CHANNEL_ID", "0"))

# ── Reaction‐Role Channels & Messages ──────────────
RULES_CHANNEL_ID        = get_int("RULES_CHANNEL_ID")
RULES_MESSAGE_ID        = get_int("RULES_MESSAGE_ID")

ROLE_ASSIGN_CHANNEL_ID  = get_int("ROLE_ASSIGN_CHANNEL_ID")
ROLE_ASSIGN_MESSAGE_ID  = get_int("ROLE_ASSIGN_MESSAGE_ID")

COLOR_ASSIGN_CHANNEL_ID = get_int("COLOR_ASSIGN_CHANNEL_ID")
COLOR_ASSIGN_MESSAGE_ID = get_int("COLOR_ASSIGN_MESSAGE_ID")

TIER_ASSIGN_CHANNEL_ID  = get_int("TIER_ASSIGN_CHANNEL_ID")
TIER_ASSIGN_MESSAGE_ID  = get_int("TIER_ASSIGN_MESSAGE_ID")

GAME_ROLE_CHANNEL_ID    = get_int("GAME_ROLE_CHANNEL_ID")
GAME_ROLE_MESSAGE_ID    = get_int("GAME_ROLE_MESSAGE_ID")

# ── Reaction → Roles maps (hard‑coded) ───────────────
REACTION_TO_ACCEPT_RULES = {
    "✅": [
        1059223354101481512,
        1366087688263827477,
        1366088275470844044,
        1366084112791765192,
        1367056177476665384,
    ]
}

REACTION_TO_ROLES = {
    "🇪": [1264505832914030593],
    "🇼": [1264505828128591944],
    "🇨": [1264505829869092864],
}

REACTION_TO_COLOR_ROLES = {
    "🔴": [1366768302617006132],
    "🟠": [1366768753731178516],
    "🟡": [1366744677449203883],
    "🟢": [1366768860077887538],
    "🔵": [1366768898363494460],
    "🟣": [1366769616394780702],
    "🟤": [1366768956106342463],
    "⚫": [1366769228476055553],
    "⚪": [1366769328682307667],
}

REACTION_TO_TIERS = {
    "<:valorantiron:1367050325457899590>":      [1367056457543188520],
    "<:valorantbronze:1367050339987095563>":    [1367056446092738561],
    "<:valorantsilver:1367050333083402280>":    [1367056435669635072],
    "<:valorantgold:1367050331242106951>":      [1367056422495584349],
    "<:valorantplatinum:1367055859435175986>":  [1367056400710242405],
    "<:valorantdiamond:1367055861351972905>":   [1367056373963296768],
    "<:valorantascendant:1367050328976920606>": [1367056342732242944],
    "<:valorantimmortal:1367050346874011668>":  [1367056231792902204],
    "<:valorantradiant:1367055860479692822>":   [1367056117280149525],
}

REACTION_TO_GAMES = {
    "<:valorant:1367050356852396106>": [1209013681753563156],
    "<:lol:1367065409240698942>":      [1209014051317743626],
    "<:tft:1367065410419298326>":      [1333664246608957461],
    "<:steam:1367065407726288896>":    [1209013974931345478],
}

# ── XP & Level‑Up Channels & Messages ──────────────
XP_CHANNEL_ID           = get_int("XP_CHANNEL_ID")
LEVELUP_CHANNEL_ID      = get_int("LEVELUP_CHANNEL_ID")
LEADERBOARD_MESSAGE_ID  = get_int("LEADERBOARD_MESSAGE_ID")
DAILY_XP_MESSAGE_ID     = get_int("DAILY_XP_MESSAGE_ID")
LEADERBOARD_MESSAGE_ID  = None
DAILY_XP_MESSAGE_ID     = None

# ── Custom‑Game Settings ─────────────────────────────
CUSTOM_GAME_VOICE_CHANNEL_ID  = get_int("CUSTOM_GAME_VOICE_CHANNEL_ID")
CUSTOM_GAME_ROLE_ID           = get_int("CUSTOM_GAME_ROLE_ID")
CUSTOM_GAME_ADMIN_ROLE_IDS    = get_json("CUSTOM_GAME_ADMIN_ROLE_IDS", [])

# ── Casino Settings ──────────────────────────────────
SLOTS_CHANNEL_ID      = int(os.getenv("SLOTS_CHANNEL_ID"))
BLACKJACK_CHANNEL_ID  = int(os.getenv("BLACKJACK_CHANNEL_ID"))
CRASH_CHANNEL_ID      = int(os.getenv("CRASH_CHANNEL_ID"))
COINFLIP_CHANNEL_ID   = int(os.getenv("COINFLIP_CHANNEL_ID"))

# Dice Duel
DICE_DUEL_CHANNEL_ID  = int(os.getenv("DICE_DUEL_CHANNEL_ID"))
DICE_DUEL_CATEGORY_ID = int(os.getenv("DICE_DUEL_CATEGORY_ID"))

# Daily Coins
DAILY_COINS_CHANNEL_ID = get_int("DAILY_COINS_CHANNEL_ID")
DAILY_COINS_AMOUNT          = int(os.getenv("DAILY_COINS_AMOUNT", "100"))

# dynamic message IDs (initialized None)
DAILY_COINS_MESSAGE_ID      = None
COIN_LEADERBOARD_MESSAGE_ID = None

# 익명 게시판에 올릴 공개 채널
ANON_BOARD_CHANNEL_ID = int(os.getenv("ANON_BOARD_CHANNEL_ID", 0))

# 실제 작성자를 기록할 운영진 전용 로그 채널
ANON_LOG_CHANNEL_ID   = int(os.getenv("ANON_LOG_CHANNEL_ID", 0))

ADMIN_USER_ID = int(os.getenv("ADMIN_USER_ID", 0))

SHOP_CHANNEL_ID = int(os.getenv("SHOP_CHANNEL_ID", 0))

BASE_ROLE = int(os.getenv("BASE_ROLE", 0))

RPC_CHANNEL_ID = int(os.getenv("RPC_CHANNEL_ID", 0))

STORE_ROLE_ID = int(os.getenv("STORE_ROLE_ID", 0))

CRASH_NOTIFY_USER_ID = int(os.getenv("CRASH_NOTIFY_USER_ID", 0))

ROULETTE_CHANNEL_ID = int(os.getenv("ROULETTE_CHANNEL_ID", 0))