# test_bot.py

import asyncio
import traceback
from datetime import datetime, timedelta, timezone
import os
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, Select
from cogs.tickets import HelpView, CloseTicketView
from cogs.voice import created_channels
from cogs.xp import voice_session_starts, DailyXPView
from cogs.reactions import reaction_mappings  # ← Make sure to import this
import zoneinfo
zoneinfo.ZoneInfo = lambda key: timezone.utc
import cogs.xp
# Replace the ZoneInfo name inside cogs.xp with a dummy that always returns UTC
cogs.xp.ZoneInfo = lambda key: timezone.utc

import logging
logging.getLogger().handlers.clear()
logging.basicConfig(level=logging.ERROR)

from utils import config

from dotenv import load_dotenv
load_dotenv()

# ────────────────────────────────────────────────────────────────────────────────
# 1) FAKE “DATABASE” BACKEND (in‐memory)
# ────────────────────────────────────────────────────────────────────────────────

from contextlib import asynccontextmanager

class FakeConnection:
    async def execute(self, *args, **kwargs):    return None
    async def fetch(self, *args, **kwargs):      return []
    async def fetchrow(self, *args, **kwargs):   return None
    async def fetchval(self, *args, **kwargs):   return None

class FakeDBPool:
    def __init__(self):
        self._coins = {}            # user_id -> balance
        self._xp    = {}            # user_id -> (xp, level)
        self._daily_claim = {}      # user_id -> last_claim (UTC datetime)
        self._daily_coin_claim = {} # user_id -> last_coin_claim
        self._players = {}          # discord_id (str) -> {puuid, riot_name, riot_tag, …}
        self._analyzed = set()      # match_id strings

    @asynccontextmanager
    async def acquire(self):
        conn = FakeConnection()
        try:
            yield conn
        finally:
            pass

    # Also allow direct calls like bot.db.fetchrow(...)
    async def fetchrow(self, query: str, *args):
        # COINS: SELECT balance FROM coins WHERE user_id = $1
        if "SELECT balance FROM coins" in query:
            user_id = args[0]
            bal = self._coins.get(user_id, 0)
            return {"balance": bal}

        # XP: SELECT xp, level FROM xp WHERE user_id = $1
        if "SELECT xp, level FROM xp" in query:
            user_id = args[0]
            xp_val, lvl_val = self._xp.get(user_id, (0, 0))
            return {"xp": xp_val, "level": lvl_val}

        # daily_claim: SELECT last_claim FROM daily_claim WHERE user_id = $1
        if "SELECT last_claim FROM daily_claim" in query:
            user_id = args[0]
            dt = self._daily_claim.get(user_id)
            if dt:
                return {"last_claim": dt}
            return None

        # daily_coin_claim: SELECT last_claim FROM daily_coin_claim WHERE user_id = $1
        if "SELECT last_claim FROM daily_coin_claim" in query:
            user_id = args[0]
            dt = self._daily_coin_claim.get(user_id)
            if dt:
                return {"last_claim": dt}
            return None

        # XP leaderboard: SELECT user_id, xp, level FROM xp ORDER BY level DESC, xp DESC LIMIT 10
        if "FROM xp ORDER BY level DESC" in query:
            rows = sorted(
                [(uid, xp, lvl) for uid, (xp, lvl) in self._xp.items()],
                key=lambda t: (-t[2], -t[1])
            )[:10]
            return [ {"user_id": uid, "xp": xp, "level": lvl} for uid, xp, lvl in rows ]

        # COINS leaderboard: SELECT user_id, balance FROM coins ORDER BY balance DESC LIMIT 10
        if "FROM coins ORDER BY balance DESC" in query:
            rows = sorted(
                [(uid, bal) for uid, bal in self._coins.items()],
                key=lambda t: -t[1]
            )[:10]
            return [ {"user_id": uid, "balance": bal} for uid, bal in rows ]

        # PLAYER lookup: SELECT riot_name, riot_tag, puuid FROM players WHERE discord_id = $1
        if "FROM players WHERE discord_id" in query:
            discord_id = args[0]
            rec = self._players.get(discord_id)
            if rec:
                return {
                    "riot_name": rec["riot_name"],
                    "riot_tag": rec["riot_tag"],
                    "puuid": rec["puuid"]
                }
            return None

        # MMR leaderboard: SELECT discord_id, riot_name, riot_tag, visible_mmr FROM players ORDER BY visible_mmr DESC LIMIT 10
        if "FROM players ORDER BY visible_mmr" in query:
            rows = []
            for rec in self._players.values():
                rows.append({
                    "discord_id": rec["discord_id"],
                    "riot_name": rec["riot_name"],
                    "riot_tag": rec["riot_tag"],
                    "visible_mmr": 1000
                })
            return rows[:10]

        return None

    async def fetch(self, query: str, *args):
        # “fetch” is used for leaderboard queries (lists)
        if "FROM xp ORDER BY level DESC" in query:
            return await self.fetchrow(query, *args)
        if "FROM coins ORDER BY balance DESC" in query:
            return await self.fetchrow(query, *args)
        if "FROM players ORDER BY visible_mmr" in query:
            return await self.fetchrow(query, *args)
        return []

    async def execute(self, query: str, *args):
        # COINS updates/inserts
        if "INSERT INTO coins" in query or "UPDATE coins" in query:
            user_id = args[0]
            delta_or_new = args[1]
            if "UPDATE coins SET balance = GREATEST(balance +" in query:
                old = self._coins.get(user_id, 0)
                new = max(old + delta_or_new, 0)
                self._coins[user_id] = new
            else:
                # INSERT or “set absolute”
                if "ON CONFLICT" in query and "DO UPDATE SET balance = EXCLUDED.balance" in query:
                    self._coins[user_id] = delta_or_new
                else:
                    old = self._coins.get(user_id, 0)
                    self._coins[user_id] = old + delta_or_new
            return

        # XP updates/inserts
        if "INSERT INTO xp" in query or "UPDATE xp" in query:
            user_id, xp_val, lvl_val = args
            self._xp[user_id] = (xp_val, lvl_val)
            return

        # daily_claim INSERT/UPDATE
        if "INSERT INTO daily_claim" in query:
            user_id, dt = args
            self._daily_claim[user_id] = dt
            return

        # daily_coin_claim INSERT/UPDATE
        if "INSERT INTO daily_coin_claim" in query:
            user_id, dt = args
            self._daily_coin_claim[user_id] = dt
            return

        # players INSERT/UPDATE
        if "INSERT INTO players" in query:
            d_id, puuid, name, tag = args[:4]
            self._players[d_id] = {
                "discord_id": d_id,
                "puuid": puuid,
                "riot_name": name,
                "riot_tag": tag
            }
            return

        # analyzed_matches INSERT
        if "INSERT INTO analyzed_matches" in query:
            match_id = args[0]
            self._analyzed.add(match_id)
            return

        return

# ────────────────────────────────────────────────────────────────────────────────
# 2) FAKE “DISCORD” OBJECTS
# ────────────────────────────────────────────────────────────────────────────────

class FakeUser:
    def __init__(self, id: int, name: str, discriminator: str = "0001"):
        self.id = id
        self.name = name
        self.discriminator = discriminator
        self.display_name = name
        self.roles = []
        self.voice = None
        self.avatar = None
        self.guild = None

        class Perms:
            manage_guild = False
        self.guild_permissions = Perms()

    @property
    def mention(self):
        return f"<@{self.id}>"

    @property
    def display_avatar(self):
        class DummyAsset:
            async def read(self_inner):
                return None
            @property
            def url(self_inner):
                return ""
            def with_size(self_inner, *a, **kw):
                return self_inner
            def with_format(self_inner, *a, **kw):
                return self_inner
        return DummyAsset()

    async def send(self, *args, **kwargs):
        return

    async def add_roles(self, *roles, reason=None):
        for role in roles:
            self.roles.append(role)

    # ← ADD THIS:
    async def move_to(self, channel):
        # Simulate moving into `channel`; update voice.channel and channel.members
        self.voice = type("VState", (), {"channel": channel})()
        if self not in channel.members:
            channel.members.append(self)


class FakeGuild:
    def __init__(self):
        self.members = []
        self.roles = []
        self.voice_channels = []
        self.text_channels = []
        self.categories = []
        self.default_role = None

    def get_channel(self, id_):
        for ch in self.text_channels + self.voice_channels:
            if ch.id == id_:
                return ch
        return None

    def get_role(self, id_):
        for r in self.roles:
            if r.id == id_:
                return r
        return None

    async def fetch_member(self, member_id: int):
        for m in self.members:
            if m.id == member_id:
                return m
        return None

    # ← new: create_role
    async def create_role(self, *, name, color=None, colour=None, reason=None):
        # Use whichever was passed: color (preferred) or colour
        role_color = color if color is not None else colour

        # Generate a new ID
        new_id = max((r.id for r in self.roles), default=0) + 1

        # Create a dummy Role object that has id, name, and color/colour
        Role = type(
            "Role",
            (),
            {"id": new_id, "name": name, "colour": role_color, "color": role_color},
        )
        role = Role()
        self.roles.append(role)
        return role


    # ← new: create_voice_channel
    async def create_voice_channel(self, *, name: str, overwrites=None, category=None, reason=None):
        # (we can ignore overwrites/reason internally)
        new_id = max((vc.id for vc in self.voice_channels), default=0) + 1
        vc = FakeVoiceChannel(new_id, name)
        vc.category = category
        self.voice_channels.append(vc)
        if category is not None:
            category.voice_channels.append(vc)
        return vc


class FakeTextChannel:
    def __init__(self, id_: int, name: str = "test-text"):
        self.id = id_
        self.name = name
        self.sent_messages = []

    async def send(self, *args, **kwargs):
        # record what was sent
        self.sent_messages.append((args, kwargs))
        return FakeMessage()

    async def purge(self, limit: int = 50):
        # do nothing
        pass

    async def fetch_message(self, msg_id: int):
        raise discord.NotFound

    async def edit(self, **kwargs):
        pass

class FakeVoiceChannel:
    def __init__(self, id_: int, name: str = "voice-test"):
        self.id = id_
        self.name = name
        self.members = []
        self.category = None  # ← add this so `.category` exists

    async def edit(self, **kwargs):
        self.name = kwargs.get("name", self.name)


class FakeCategory:
    def __init__(self, id_: int, name: str = "cat"):
        self.id = id_
        self.name = name
        self.text_channels = []
        self.voice_channels = []

class FakeMessage:
    def __init__(self):
        self.id = 0
        self.embeds = []
        self.content = ""
        self.file = None
        self.reactions = []

    async def edit(self, **kwargs):
        self.edited = True

    async def reply(self, content=None, embed=None, file=None):
        pass

class FakeInteraction:
    def __init__(self, user: FakeUser, guild: FakeGuild, channel: FakeTextChannel):
        self.user = user
        self.guild = guild
        self.channel = channel
        self.client = None

        self._resp = {"deferred": False, "sent": False, "args": None, "kwargs": None}
        self._followups = []

        # Let “interaction.response.defer/send” map to these methods
        self.response = self
        self.followup = self

    async def defer(self, *, ephemeral=False):
        self._resp["deferred"] = True

    async def send(self, *args, **kwargs):
        self._resp["sent"] = True
        self._resp["args"] = args
        self._resp["kwargs"] = kwargs

    async def followup_send(self, *args, **kwargs):
        self._followups.append((args, kwargs))

    async def send_modal(self, modal):
        self._resp["sent_modal"] = True
        self._resp["modal"] = type(modal).__name__

    def __getattr__(self, name):
        if name == "defer":
            return lambda **kw: self.defer(**kw)
        if name in ("send", "send_message"):
            return lambda *a, **kw: self.send(*a, **kw)
        if name == "followup":
            return type("Fut", (), {"send": self.followup_send})
        raise AttributeError(f"{name} missing")
        if name == "send_modal":
            return lambda modal: self.send_modal(modal)
        raise AttributeError(f"{name} missing")


# ────────────────────────────────────────────────────────────────────────────────
# 3) MAIN: SET UP BOT, REGISTER COGS, AND RUN THROUGH EACH COMMAND/INTERACTION
# ────────────────────────────────────────────────────────────────────────────────

async def main():
    intents = discord.Intents.default()
    intents.members = True

    bot = commands.Bot(
        command_prefix="!",
        intents=intents,
        application_id=123456789012345678
    )

    bot.test_mode = True

    async def dummy_ready():
        pass

    bot.wait_until_ready = dummy_ready

    bot._is_testing = True


    # ────────────────────────────────
    # attach our fake DB here:
    bot.db = FakeDBPool()
    # ────────────────────────────────

    # now inject DummyConn, create FakeGuild, etc.
    bot._connection = type("DummyConn", (), {
        "user": type("DummyUser", (), {"name": "TestBot"})(),
        "application_id": bot.application_id,
        "guilds": [],
        "store_view": lambda self, view, message_id=None: None
    })()

    # … right after creating the Bot and injecting DummyConn:
    bot._views = []
    bot.add_view = lambda view, message_id=None: bot._views.append(view)

    # Create exactly one FakeGuild and immediately register it
    G = FakeGuild()
    bot._connection.guilds.append(G)

    # Now build its text‑voice channels and category
    xp_chan = FakeTextChannel(config.XP_CHANNEL_ID, "xp-channel")
    coins_chan = FakeTextChannel(config.DAILY_COINS_CHANNEL_ID, "coins-channel")
    shop_chan = FakeTextChannel(config.SHOP_CHANNEL_ID, "shop-channel")
    ticket_cat = FakeCategory(config.TICKET_CATEGORY_ID, "ticket-category")
    welcome_chan = FakeTextChannel(config.WELCOME_CHANNEL_ID, "welcome-channel")
    leave_chan = FakeTextChannel(config.LEAVE_CHANNEL_ID, "leave-channel")
    lvlup_chan = FakeTextChannel(config.LEVELUP_CHANNEL_ID, "levelup-channel")
    mmr_chan = FakeTextChannel(int(config.MMR_CHANNEL_ID), "mmr-channel")
    crash_chan = FakeTextChannel(config.CRASH_CHANNEL_ID, "crash-channel")

    G.text_channels.extend([
        xp_chan, coins_chan, shop_chan, welcome_chan, leave_chan, lvlup_chan, mmr_chan, crash_chan
    ])
    G.categories.append(ticket_cat)

    # Create the special “join‑to‑create” voice channel:
    join_vc = FakeVoiceChannel(config.TEMP_VOICE_VIEW_ROLE_ID, "🔊┆임시 음성채널 생성")
    join_vc.category = ticket_cat
    G.voice_channels.append(join_vc)

    # We need a “view role” object in the guild for voice tests:
    G.roles.append(discord.Object(config.TEMP_VOICE_VIEW_ROLE_ID))
    G.default_role = discord.Object(0)

    # Create two FakeUsers:
    alice = FakeUser(1111, "Alice")
    alice.guild = G
    bob = FakeUser(2222, "Bob")
    bob.guild = G
    G.members.extend([alice, bob])

    # Override get_channel/get_guild so cogs can find channels and categories
    bot.get_channel = lambda cid: next(
        (ch for ch in (G.text_channels + G.voice_channels + G.categories) if ch.id == cid),
        None
    )

    bot.get_guild = lambda gid=None: G

    # Make sure bot.loop exists before dispatching “ready”:
    bot.loop = asyncio.get_running_loop()

    # ── Step 6: Load all cogs (using list_all_cog_modules()) ──
    def list_all_cog_modules():
        base = os.path.join(os.path.dirname(__file__), "cogs")
        names = []
        for fn in os.listdir(base):
            if fn.endswith(".py") and not fn.startswith("__"):
                names.append(f"cogs.{fn[:-3]}")
        return names

    for module_name in list_all_cog_modules():
        try:
            mod = __import__(module_name, fromlist=["setup"])
            await getattr(mod, "setup")(bot)
            print(f"[TEST] Loaded {module_name}")
        except Exception as e:
            print(f"[TEST] Could not load {module_name}: {e}")
            traceback.print_exc()

    valorant_cog = bot.get_cog("ValorantMMRCog")
    if valorant_cog:
        valorant_cog.on_ready = lambda *args, **kwargs: None

        async def _noop_run_daily_update():
            return
        valorant_cog.run_daily_update = _noop_run_daily_update

    bot.dispatch("ready")
    await asyncio.sleep(0.1)



    print("\n=== Running individual command tests ===\n")

    # ────────────────────────────────────────────────────────────────────────────
    # 4) XP SYSTEM TESTS (cogs/xp.py)
    # ────────────────────────────────────────────────────────────────────────────
    xp_cog = bot.get_cog("XPSystem")

    print("1) XP: build_leaderboard_embed (empty DB)")
    lb = await xp_cog.build_leaderboard_embed()
    print("   → Embed description:", lb.description)

    # Simulate daily XP button click for Alice
    print("2) XP: Alice presses ‘오늘의 XP 받기’ button")
    interaction = FakeInteraction(alice, G, xp_chan)
    view = DailyXPView(bot)
    # Manually call the Button’s callback
    btn = view.children[0]  # this is the “오늘의 XP 받기” Button
    await btn.callback(interaction)
    print("   → Followups:", interaction._followups)

    print("3) XP: Alice uses /dailyxp slash")
    slash_int = FakeInteraction(alice, G, xp_chan)
    # .dailyxp is an app_commands.Command; call its .callback(...) instead:
    await xp_cog.dailyxp.callback(xp_cog, slash_int)
    print("   → Response args:", slash_int._resp)

    print("4) XP: Alice joins + leaves voice channel → gains voice XP")
    before = type("S", (), {"channel": None})
    after = type("S", (), {"channel": join_vc})
    await xp_cog.on_voice_state_update(alice, before, after)
    # Fake 2 minutes have passed:
    voice_session_starts[alice.id] = datetime.now(timezone.utc) - timedelta(minutes=2)
    # Simulate leave:
    before2 = after
    after2 = type("S2", (), {"channel": None})
    await xp_cog.on_voice_state_update(alice, before2, after2)
    print("   → Alice XP/level now:", bot.db._xp.get(alice.id))

    print("5) XP: Bob tries /xp_modify but lacks perms")
    staff_int = FakeInteraction(bob, G, xp_chan)
    await xp_cog.xp_modify.callback(
        xp_cog,
        staff_int,
        f"<@{alice.id}>",
        app_commands.Choice(name="추가", value="add"),
        50
    )
    print("   → Permission‐denied response:", staff_int._resp)

    bob.guild_permissions = type("P", (), {"manage_guild": True})
    print("6) XP: Bob (admin) does /xp_modify @Alice add 50")
    staff_int2 = FakeInteraction(bob, G, xp_chan)
    await xp_cog.xp_modify.callback(
        xp_cog,  # “self”
        staff_int2,  # interaction
        f"<@{alice.id}>",  # users
        app_commands.Choice(name="추가", value="add"),
        50
    )
    print("   → Alice’s XP after mod:", bot.db._xp.get(alice.id))
    print("   → xp_modify followups:", staff_int2._followups)

    print("7) XP: Alice uses /xp to view her stats")
    view_int = FakeInteraction(alice, G, xp_chan)
    await xp_cog.xp.callback(xp_cog, view_int)
    print("   → XP embed sent:", view_int._resp)

    # ────────────────────────────────────────────────────────────────────────────
    # 5) COINS SYSTEM TESTS (cogs/coins.py)
    # ────────────────────────────────────────────────────────────────────────────
    coins_cog = bot.get_cog("Coins")

    print("\n8) COINS: on_ready builds leaderboard + “오늘의 코인” button")
    await coins_cog.on_ready()
    print("   → Coins channel messages:", coins_chan.sent_messages)

    bot.db._coins[alice.id] = 100
    print("9) COINS: Alice does /coins")
    coin_int = FakeInteraction(alice, G, coins_chan)
    await coins_cog.coins.callback(coins_cog, coin_int)
    print("   → coins response:", coin_int._resp)

    print("10) COINS: Bob does /coin_leaderboard")
    coin_int2 = FakeInteraction(bob, G, coins_chan)
    await coins_cog.coin_leaderboard.callback(coins_cog, coin_int2)
    print("   → leaderboard embed:", coin_int2._resp)

    print("11) COINS: Bob does /coins_modify without perms")
    coins_int3 = FakeInteraction(bob, G, coins_chan)
    await coins_cog.coins_modify.callback(
        coins_cog,
        coins_int3,
        f"<@{alice.id}>",
        app_commands.Choice(name="추가", value="add"),
        50
    )
    print("   → permission error:", coins_int3._resp)

    bob.guild_permissions = type("P", (), {"manage_guild": True})
    print("12) COINS: Bob (admin) does /coins_modify @Alice add 50")
    coins_int4 = FakeInteraction(bob, G, coins_chan)
    await coins_cog.coins_modify.callback(
        coins_cog,
        coins_int4,
        f"<@{alice.id}>",
        app_commands.Choice(name="추가", value="add"),
        50
    )
    print("   → new Alice balance:", bot.db._coins[alice.id],
          " summary:", coins_int4._resp)

    print("13) COINS: Alice does /coins_tip @Bob 30")
    bot.db._coins[alice.id] = 100
    tip_int = FakeInteraction(alice, G, coins_chan)
    await coins_cog.coins_tip.callback(coins_cog, tip_int, bob, 30)
    print("   → Alice bal:", bot.db._coins[alice.id],
          " Bob bal:", bot.db._coins[bob.id])
    print("   → tip response:", tip_int._resp)

    # ────────────────────────────────────────────────────────────────────────────
    # 6) SHOP FEATURES TESTS (cogs/shop_features.py)
    # ────────────────────────────────────────────────────────────────────────────
    shop_cog = bot.get_cog("ShopPersistent")

    print("\n14) SHOP: on_ready posts the shop embed + view")
    await shop_cog.on_ready()
    print("   → Shop channel messages:", shop_chan.sent_messages)

    bot.db._coins[alice.id] = 2000
    print("15) SHOP: Alice clicks CustomRoleButton")
    cr_button = None
    for v in bot._views:
        for child in v.children:
            if isinstance(child, Button) and child.custom_id == "custom_role_btn":
                cr_button = child
    if cr_button:
        shop_int = FakeInteraction(alice, G, shop_chan)
        shop_int.client = bot

        await cr_button.callback(shop_int)
        print("   → coins after purchase:", bot.db._coins[alice.id])
    else:
        print("   → could not locate CustomRoleButton")

    bot.db._coins[alice.id] = 1000
    print("16) SHOP: Alice selects a color from NickColorSelect")
    color_select = None
    for v in bot._views:
        for child in v.children:
            if isinstance(child, Select) and child.custom_id == "nick_color_select":
                color_select = child
    if color_select:
        # pick any valid color from config.REACTION_TO_COLOR_ROLES
        from utils import config as cfg
        choice = next(iter(cfg.REACTION_TO_COLOR_ROLES.keys()))
        # ← instead, set _values, not _raw_values:
        color_select._values = [choice]

        shop_int2 = FakeInteraction(alice, G, shop_chan)
        shop_int2.client = bot
        await color_select.callback(shop_int2)

        print("   → coins after color purchase:", bot.db._coins[alice.id])

    else:
        print("   → could not locate NickColorSelect")

    bot.db._coins[alice.id] = 5000

    # ─── right before “17) SHOP: Alice clicks XPBoosterButton” ───
    # Drop any existing discord.Object() entries, and replace them with one "role" that has a .name
    XPBoosterRole = type("Role", (), {"id": 5555, "name": "XP Booster"})
    G.roles = [XPBoosterRole]

    print("17) SHOP: Alice clicks XPBoosterButton")
    xpboost_btn = None
    for v in bot._views:
        for child in v.children:
            if isinstance(child, Button) and child.custom_id == "xp_booster_btn":
                xpboost_btn = child
    if xpboost_btn:
        shop_int3 = FakeInteraction(alice, G, shop_chan)
        shop_int3.client = bot
        await xpboost_btn.callback(shop_int3)
        print("   → coins after XPBooster purchase:", bot.db._coins[alice.id])
    else:
        print("   → could not locate XPBoosterButton")

    # ────────────────────────────────────────────────────────────────────────────
    # 7) VOICE MANAGER TESTS (cogs/voice.py)
    # ────────────────────────────────────────────────────────────────────────────
    voice_cog = bot.get_cog("VoiceManager")

    print("\n18) VOICE: Simulate periodic_update")
    # Patch G.members statuses:
    class DummyMember:
        def __init__(self, status):
            self.status = status
    G.members = [
        DummyMember(discord.Status.online),
        DummyMember(discord.Status.idle),
        DummyMember(discord.Status.dnd),
        DummyMember(discord.Status.offline)
    ]
    # We need at least three voice channels whose names start with 🟢, 📸, 👥
    vc1 = FakeVoiceChannel(4000, "🟢 oldname")
    vc2 = FakeVoiceChannel(4001, "📸 oldname")
    vc3 = FakeVoiceChannel(4002, "👥 oldname")
    G.voice_channels[:3] = [vc1, vc2, vc3]
    await voice_cog.periodic_update()
    print("   → Voice channels renamed:", [vc.name for vc in G.voice_channels[:3]])

    print("19) VOICE: Simulate on_voice_state_update to create/delete temp channel")
    before_state = type("S", (), {"channel": None})
    after_state  = type("S2", (), {"channel": join_vc})
    await voice_cog.on_voice_state_update(bob, before_state, after_state)
    print("   → created_channels dict:", created_channels)

    # ────────────────────────────────────────────────────────────────────────────
    # 8) WELCOME COG TESTS (cogs/welcome.py)
    # ────────────────────────────────────────────────────────────────────────────
    welcome_cog = bot.get_cog("WelcomeCog")

    print("\n20) WELCOME: Simulate on_member_join for Carol")
    carol = FakeUser(3333, "Carol")
    G.members.append(carol)
    await welcome_cog.on_member_join(carol)
    print("   → Welcome channel messages:", welcome_chan.sent_messages)

    print("21) WELCOME: Simulate on_member_remove for Carol")
    await welcome_cog.on_member_remove(carol)
    print("   → Leave channel messages:", leave_chan.sent_messages)

    # ────────────────────────────────────────────────────────────────────────────
    # 9) TICKET SYSTEM TESTS (cogs/tickets.py)
    # ────────────────────────────────────────────────────────────────────────────
    ticket_cog = bot.get_cog("TicketSystem")

    print("\n22) TICKETS: Simulate /help slash")
    help_int = FakeInteraction(alice, G, xp_chan)
    await ticket_cog.slash_help.callback(ticket_cog, help_int)
    print("   → Help channel messages (last):", xp_chan.sent_messages[-1])

    print("   → Simulate clicking 문의하기 button")
    help_view = HelpView(bot)
    help_button = help_view.children[0]
    ticket_int = FakeInteraction(alice, G, xp_chan)
    await help_button.callback(ticket_int)
    if not ticket_cat.text_channels:
        print("❌ No ticket channel was created in the mock category.")
        return
    fake_new_ticket = ticket_cat.text_channels[-1]
    print("   → Ticket channel created:", fake_new_ticket.name)

    print("   → Simulate pressing 티켓 닫기")
    close_view = CloseTicketView(bot)
    close_button = close_view.children[0]
    fake_new_ticket.name = f"ticket-{alice.id}"
    close_int = FakeInteraction(alice, G, fake_new_ticket)
    await close_button.callback(close_int, close_button)
    print("   → After closing, ticket category channels:", ticket_cat.text_channels)

    # ────────────────────────────────────────────────────────────────────────────
    # 10) ENTRY SYSTEM TESTS (cogs/entry.py)
    # ────────────────────────────────────────────────────────────────────────────
    entry_cog = bot.get_cog("EntryPersistent")

    print("\n23) ENTRY: Simulate on_ready to post entry button")
    await entry_cog.on_ready()
    print("   → Entry channel messages:", xp_chan.sent_messages[-1])

    entry_button = None
    for v in bot._views:
        for child in v.children:
            if isinstance(child, Button) and child.custom_id == "entry_button":
                entry_button = child
    if entry_button:
        entry_int = FakeInteraction(alice, G, xp_chan)
        await entry_button.callback(entry_int)
        print("   → EntryButton callback ran (no crash)")

    # ────────────────────────────────────────────────────────────────────────────
    # 11) REACTION ROLES TESTS (cogs/reactions.py)
    # ────────────────────────────────────────────────────────────────────────────
    react_cog = bot.get_cog("ReactionRoles")
    print("\n24) REACTIONS: Seed and simulate on_raw_reaction_add/remove")
    test_msg_id = 12345
    from utils import config as cfg
    cfg.REACTION_TO_COLOR_ROLES = {"🔴": [4444]}
    reaction_mappings[test_msg_id] = {"🔴": [4444]}

    # pretend guild now has a role with id=4444
    fake_role = discord.Object(4444)
    G.roles.append(fake_role)

    class DummyPayload:
        def __init__(self, user_id, message_id, guild_id, emoji):
            self.user_id = user_id
            self.message_id = message_id
            self.guild_id = guild_id
            self.emoji = emoji

    payload_add = DummyPayload(alice.id, test_msg_id, None, "🔴")
    await react_cog.on_raw_reaction_add(payload_add)
    print("   → Alice roles after add:", alice.roles)

    payload_rem = DummyPayload(alice.id, test_msg_id, None, "🔴")
    await react_cog.on_raw_reaction_remove(payload_rem)
    print("   → Alice roles after remove:", alice.roles)

    # ────────────────────────────────────────────────────────────────────────────
    # 12) CASINO COG TEST (cogs/casino.py)
    # ────────────────────────────────────────────────────────────────────────────
    casino_cog = bot.get_cog("Casino")
    print("\n25) CASINO: Simulate /rps for Alice")
    rps_int = FakeInteraction(alice, G, coins_chan)
    await casino_cog.rps.callback(casino_cog, rps_int)
    print("   → RPS buttons would appear next (no crash).")

    # ────────────────────────────────────────────────────────────────────────────
    # 13) VALORANT MMR COG TESTS (cogs/valorant_mmr.py)
    # ────────────────────────────────────────────────────────────────────────────
    mmr_cog = bot.get_cog("ValorantMMRCog")

    print("\n26) MMR: Simulate /연동 bad format")
    bad_link_int = FakeInteraction(alice, G, mmr_chan)
    await mmr_cog.slash_link_account.callback(mmr_cog, bad_link_int, "NoHashHere")
    print("   → bad format response:", bad_link_int._resp)

    print("\n27) MMR: Simulate /연동 good format (fake API returns None)")
    good_link_int = FakeInteraction(alice, G, mmr_chan)
    await mmr_cog.slash_link_account.callback(mmr_cog, good_link_int, "Test#1234")
    print("   → account‐not‐found response:", good_link_int._resp)

    print("28) MMR: Simulate /티어 without linking")
    tier_int = FakeInteraction(alice, G, mmr_chan)
    await mmr_cog.slash_rank.callback(mmr_cog, tier_int, "na", None)
    print("   → no‐link response:", tier_int._resp)

    print("\n\n=== ALL TESTS COMPLETED ===\n")

if __name__ == "__main__":
    asyncio.run(main())
