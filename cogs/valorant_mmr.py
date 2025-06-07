import os
import asyncio
import asyncpg
import json
import pytz
from datetime import datetime, timedelta
import urllib.parse
from typing import Optional
import traceback

import discord
from discord.ext import tasks, commands
from discord import app_commands, Interaction
import aiohttp

from utils.logger import log_to_channel

# If you already have CREATE_PLAYERS_SQL and CREATE_ANALYZED_SQL defined elsewhere,
# just paste them above this class or import them.
CREATE_PLAYERS_SQL = """
 CREATE TABLE IF NOT EXISTS players (
   discord_id     TEXT PRIMARY KEY,
   puuid          TEXT UNIQUE NOT NULL,
   riot_name      TEXT NOT NULL,
   riot_tag       TEXT NOT NULL,
   discord_nick   TEXT,
   competitive_mmr INTEGER NOT NULL DEFAULT 1000,

   hidden_win_mmr NUMERIC NOT NULL DEFAULT 1000,
   hidden_win_rd  NUMERIC NOT NULL DEFAULT 350,
   hidden_win_vol NUMERIC NOT NULL DEFAULT 0.06,

   hidden_enc_mmr NUMERIC NOT NULL DEFAULT 1000,
   hidden_enc_rd  NUMERIC NOT NULL DEFAULT 350,
   hidden_enc_vol NUMERIC NOT NULL DEFAULT 0.06,

   visible_mmr    INTEGER NOT NULL DEFAULT 1000,
   seeded         BOOLEAN NOT NULL DEFAULT FALSE,
   last_active    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
   created_at     TIMESTAMPTZ NOT NULL DEFAULT NOW()
 );

"""

CREATE_ANALYZED_SQL = """
CREATE TABLE IF NOT EXISTS analyzed_matches (
  match_id TEXT PRIMARY KEY
);
"""

CREATE_MATCHES_SQL = """
CREATE TABLE IF NOT EXISTS matches (
  match_id     TEXT PRIMARY KEY,
  map          TEXT NOT NULL,
  mode         TEXT,
  team1_score  INTEGER NOT NULL,
  team2_score  INTEGER NOT NULL,
  round_count  INTEGER NOT NULL,
  winner_team  TEXT NOT NULL,
  game_start   TIMESTAMP NOT NULL
);
"""

MATCH_PLAYERS_SQL = """
CREATE TABLE IF NOT EXISTS match_players (
    match_id       TEXT NOT NULL,
    puuid          TEXT NOT NULL,
    riot_name      TEXT NOT NULL,
    riot_tag       TEXT NOT NULL,
    map            TEXT NOT NULL,
    agent          TEXT NOT NULL,
    kda            TEXT NOT NULL,
    kills          INTEGER NOT NULL,
    deaths         INTEGER NOT NULL,
    assists        INTEGER NOT NULL,
    score          INTEGER NOT NULL,
    adr            NUMERIC NOT NULL,
    hs_pct         NUMERIC NOT NULL,
    kast_pct       TEXT,
    plus_minus     TEXT,
    kd_ratio       NUMERIC,
    dda            TEXT,
    fk             INTEGER,
    fd             INTEGER,
    mk             INTEGER,
    team           TEXT NOT NULL,
    won            BOOLEAN NOT NULL,
    round_count    INTEGER NOT NULL,
    team1_score    INTEGER NOT NULL,
    team2_score    INTEGER NOT NULL,
    tier           TEXT NOT NULL,
    game_start     TIMESTAMP NOT NULL,

    -- Ensure uniqueness of player per match
    PRIMARY KEY (match_id, puuid)
);
"""

# ── Module‐level global to hold the leaderboard message ID ──
MMR_LEADERBOARD_MESSAGE_ID: Optional[int] = None

GhibliIcon = {
    "엠버":    "<:ember:1380270316466733187>",      # Iron
    "프로스트": "<:frost:1380270321629921421>",     # Silver
    "플레어":   "<:flare:1380270323051659314>",      # Gold
    "오로라":   "<:aurora:1380270317569573089>",     # Diamond
    "볼텍스":   "<:vortex:1380270311890751518>",     # Ascendant
    "이터널":   "<:eternal:1380270319792689172>",    # Immortal
    "슈퍼노바": "<:supernova:1380270314432237578>",  # Radiant
}



def is_admin(interaction: Interaction):
    return interaction.user.guild_permissions.administrator

class LeaderboardView(discord.ui.View):
    def __init__(self, cog, page=0, per_page=10):
        super().__init__(timeout=None)  # <- NO TIMEOUT!
        self.cog = cog
        self.page = page
        self.per_page = per_page

    async def update_embed(self, interaction: discord.Interaction):
        embed = await self.cog.build_mmr_leaderboard_embed(page=self.page, per_page=self.per_page)
        # Recreate the view with updated state every time!
        await interaction.response.edit_message(
            embed=embed,
            view=self.__class__(self.cog, page=self.page, per_page=self.per_page)
        )

    @discord.ui.button(label="⏮️ Prev", style=discord.ButtonStyle.secondary, custom_id="prev_page")
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            await self.update_embed(interaction)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Next ⏭️", style=discord.ButtonStyle.secondary, custom_id="next_page")
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        await self.update_embed(interaction)

class ValorantMMRCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # We start the “daily update” loop in cog_load instead of __init__.

    async def cog_load(self):
        # Start the daily task
        self.daily_update_task = asyncio.create_task(self.run_daily_update())

    # ── Periodic task: fallback hourly refresh ──
    @tasks.loop(minutes=60)
    async def periodic_mmr_leaderboard(self):
        await self.bot.wait_until_ready()
        try:
            await self.refresh_mmr_leaderboard()
        except Exception as e:
            await log_to_channel(self.bot, f"❌ MMR 리더보드 업데이트 오류: {e}")

    @commands.Cog.listener()
    async def on_ready(self):
        if not getattr(self, "_synced", False):
            if not getattr(self.bot, "_is_testing", False):
                await self.bot.tree.sync()
            print("✅ 슬래시 명령어 동기화 완료 (global)")
            self._synced = True

            print("[MMR] Running leaderboard refresh in on_ready")
            await self.refresh_mmr_leaderboard()

        if not self.periodic_mmr_leaderboard.is_running():
            self.periodic_mmr_leaderboard.start()

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if before.nick != after.nick:
            async with self.bot.db.acquire() as conn:
                await conn.execute(
                    "UPDATE players SET discord_nick = $1 WHERE discord_id = $2",
                    after.nick or after.name,
                    str(after.id)
                )
            await log_to_channel(self.bot, f"🔄 닉네임 변경 감지: {after.display_name} ({after.id}) – DB 업데이트 완료")
            await self.refresh_mmr_leaderboard()

    # ── Fetch from Henrik API ──
    async def henrik_get(self, endpoint: str) -> Optional[dict]:
        base = "https://api.henrikdev.xyz"
        headers = {"Authorization": os.getenv("HENRIK_API_KEY", "")}
        async with aiohttp.ClientSession() as session:
            async with session.get(base + endpoint, headers=headers) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    await log_to_channel(self.bot, f"⚠️ [Henrik] 요청 실패: {resp.status} {endpoint}")
                    return None

    # ── Convert Riot tier+RR → numeric score (no big “300-point” jumps) ──
    def tier_to_score(self, tier: str, rr: int) -> int:
        TIERS = [
            "Iron 1","Iron 2","Iron 3","Bronze 1","Bronze 2","Bronze 3",
            "Silver 1","Silver 2","Silver 3","Gold 1","Gold 2","Gold 3",
            "Platinum 1","Platinum 2","Platinum 3","Diamond 1","Diamond 2","Diamond 3",
            "Ascendant 1","Ascendant 2","Ascendant 3","Immortal 1","Immortal 2","Immortal 3",
            "Radiant"
        ]
        # change multiplier from 300 → 100 so there are no 300-point “holes”
        base = TIERS.index(tier) * 100 if tier in TIERS else 0
        return base + rr


    @staticmethod
    def get_ghibli_tier_label(visible_mmr: int, rank: int = None) -> str:
        main_tiers = [
            ("엠버", 0),  # Iron (0–950)
            ("프로스트", 951),  # Silver (951–1150)
            ("플레어", 1151),  # Gold (1151–1350)
            ("오로라", 1351),  # Platinum (1351–1550)
            ("볼텍스", 1551),  # Diamond (1551–1700)
            ("이터널", 1701),  # Immortal (1701–1950)
            ("슈퍼노바", 1951),  # Radiant (1951+, but only assign to top 10)
        ]
        # 1) Find the highest tier whose base is ≤ visible_mmr
        tier_name, tier_base = main_tiers[0]
        for name, lb in main_tiers:
            if visible_mmr >= lb:
                tier_name, tier_base = name, lb
            else:
                break

        # Restrict 슈퍼노바 to top 10 ONLY
        if tier_name == "슈퍼노바" and (rank is None or rank > 10):
            tier_name = "이터널"
            tier_base = 1301

        # 2) If it's 슈퍼노바, just return without suffix
        if tier_name == "슈퍼노바":
            return "슈퍼노바"

        # 3) Otherwise compute offset within that tier
        offset = visible_mmr - tier_base  # 0..299

        # 4) Assign sub‐rank: 1 = lowest, 2 = mid, 3 = highest
        if offset <= 99:
            sub = "1"
        elif offset <= 199:
            sub = "2"
        else:
            sub = "3"

        return f"{tier_name} {sub}"

    # ── Calculate hidden MMR based on match list ──
    def calc_hidden(self, matches: list, puuid: str) -> dict:
        if not matches:
            return {
                "hidden_perf": 1000,
                "hidden_enc": 1000,
                "hidden_util": 1000,
                "hidden_eco": 1000,
                "clutch": 1000
            }

        perf, enc, util, eco, clutch = [], [], [], [], []

        for match in matches:
            players = match.get("players", {}).get("all_players", [])
            meta = match.get("metadata", {})
            player = next((p for p in players if p.get("puuid") == puuid), None)
            if not player:
                continue

            stats = player.get("stats", {})
            econ = player.get("economy", {})
            ability = player.get("ability_casts", {})

            rounds = meta.get("rounds_played", 24)
            if not isinstance(rounds, (int, float)) or rounds == 0:
                rounds = 1

            kills = stats.get("kills", 0)
            deaths = stats.get("deaths", 1)
            assists = stats.get("assists", 0)
            score = stats.get("score", 0)
            damage = stats.get("damage_made", 0)
            if not isinstance(damage, (int, float)):
                damage = 0
            received = stats.get("damage_received", 0)
            if not isinstance(received, (int, float)):
                received = 0
            headshots = stats.get("headshots", 0)
            bodyshots = stats.get("bodyshots", 0)
            legshots = stats.get("legshots", 0)
            shots_total = headshots + bodyshots + legshots
            if not isinstance(shots_total, (int, float)) or shots_total == 0:
                shots_total = 1

            # --- Performance
            try:
                adr = damage / rounds
            except Exception:
                adr = 0
            try:
                hs_pct = headshots / shots_total * 100 if shots_total else 0
            except Exception:
                hs_pct = 0
            try:
                kd_ratio = kills / deaths if deaths > 0 else kills
            except Exception:
                kd_ratio = 0
            kda = (kills + 0.7 * assists - 0.5 * deaths)
            try:
                perf.append(kda + (adr * 0.05) + (score / rounds) + (hs_pct * 0.2))
            except Exception:
                perf.append(0)

            # --- Encounter
            fk = stats.get("first_kills", 0)
            fd = stats.get("first_deaths", 0)
            try:
                enc_rating = (fk * 2) - fd
            except Exception:
                enc_rating = 0
            enc.append(enc_rating)

            # --- Utility
            try:
                ability_score = (
                        ability.get("c", 0) +
                        ability.get("q", 0) +
                        ability.get("e", 0) * 1.5 +
                        ability.get("x", 0) * 2
                )
                util.append(ability_score / rounds)
            except Exception:
                util.append(0)

            # --- Economy
            spent = econ.get("spent", 0)
            remaining = econ.get("remaining", 0)
            try:
                eco_eff = (score / spent) * 100 if spent else 0
            except Exception:
                eco_eff = 0
            try:
                eco.append(eco_eff + (remaining if isinstance(remaining, (int, float)) else 0) * 0.001)
            except Exception:
                eco.append(0)

            # --- Clutch
            try:
                clutch_score = stats.get("clutch_score", 0) if "clutch_score" in stats else (fk + kills) / rounds
            except Exception:
                clutch_score = 0
            clutch.append(clutch_score)

        def normalize(arr):
            if not arr:
                return 1000
            avg = sum(arr) / len(arr)
            return round(1000 + (avg - 15) * 10)

        return {
            "hidden_perf": normalize(perf),
            "hidden_enc": normalize(enc),
            "hidden_util": normalize(util),
            "hidden_eco": normalize(eco),
            "clutch": normalize(clutch)
        }

    def calc_hidden_glicko(
            self,
            matches: list,
            puuid: str,
            prev_mmr: float = 1000,
            prev_rd: float = 350,
            prev_vol: float = 0.06,
            tau: float = 0.5
    ) -> dict:
        import math

        # 1) Clamp prev_mmr so that it's never absurdly large
        #    (choose a ceiling that makes sense, e.g. 2000)
        safe_prev_mmr = max(min(prev_mmr, 2000), 0)
        mmr = safe_prev_mmr
        rd = prev_rd
        vol = prev_vol

        # Collect win/loss outcomes
        results = []
        for match in matches:
            players = match.get("players", {}).get("all_players", [])
            player = next((p for p in players if p.get("puuid") == puuid), None)
            if not player:
                continue
            team = player.get("team", "").lower()
            won = match.get("teams", {}).get(team, {}).get("has_won", False)
            results.append((1000, 1 if won else 0))  # Assume opponent rating = 1000

        if not results:
            return {"mmr": mmr, "rd": rd, "vol": vol}

        # Glicko‐style update
        for opp_rating, outcome in results:
            # 2) Compute g and raw E
            g = 1 / math.sqrt(1 + 3 * (math.log(10) / 400) ** 2 * rd ** 2 / math.pi ** 2)

            raw_exp = -g * (mmr - opp_rating) / 400
            x = 10 ** raw_exp

            # 3) Clamp E so it never becomes exactly 0.0 or 1.0
            E = 1 / (1 + x)
            if E <= 0.0:
                E = 1e-9
            elif E >= 1.0:
                E = 1.0 - 1e-9

            # 4) Now compute d2 with the safe E
            denom = (math.log(10) / 400) ** 2 * g ** 2 * E * (1 - E)
            d2 = 1 / denom

            # 5) Update MMR and RD
            mmr_delta = (math.log(10) / 400) / ((1 / rd ** 2) + (1 / d2)) * g * (outcome - E)
            mmr += mmr_delta
            rd = math.sqrt(1 / ((1 / rd ** 2) + (1 / d2)))

            # 6) Adjust volatility
            if abs(outcome - E) > 0.5:
                vol = min(0.1, vol + 0.01)
            else:
                vol = max(0.05, vol - 0.005)

        return {
            "mmr": round(mmr, 2),
            "rd": round(rd, 2),
            "vol": round(vol, 4)
        }

    def calc_encounter_glicko(
            self,
            matches: list,
            puuid: str,
            prev_enc_mmr: float = 1000,
            prev_enc_rd:  float = 350,
            prev_enc_vol: float = 0.06,
            tau:         float = 0.5
    ) -> dict:
        """
        Treat each match’s enc_rating = (first_kills*2 - first_deaths) as a “binary outcome” vs. a default 1000‐rated opponent.
        If enc_rating > 0 → count it as a “win” (1); else → “loss” (0).
        This yields a Glicko‐style update for encounter.
        """

        import math

        # 1) Clamp previous enc‐MMR for safety
        safe_prev = max(min(prev_enc_mmr, 2000), 0)
        enc_mmr = safe_prev
        enc_rd  = prev_enc_rd
        enc_vol = prev_enc_vol

        # 2) Build a list of (opponent_rating, outcome) for each match
        results = []
        for match in matches:
            # Find the player’s entry in this match
            all_players = match.get("players", {}).get("all_players", [])
            me = next((p for p in all_players if p.get("puuid") == puuid), None)
            if not me:
                continue

            # Compute raw enc_rating
            stats = me.get("stats", {})
            fk = stats.get("first_kills", 0)
            fd = stats.get("first_deaths", 0)
            try:
                enc_val = (fk * 2) - fd
            except Exception:
                enc_val = 0

            # If enc_val > 0, treat as a “win” (1); otherwise loss (0).
            outcome = 1 if enc_val > 0 else 0
            # We assume the “opponent rating” is always 1000.
            results.append((1000, outcome))

        # If no results, return prior values unchanged
        if not results:
            return {"enc_mmr": enc_mmr, "enc_rd": enc_rd, "enc_vol": enc_vol}

        # 3) Perform Glicko‐style update for each (opp_rating, outcome)
        for (opp_rating, outcome) in results:
            # Step A: compute g(phi) and expected score E
            g = 1 / math.sqrt(1 + 3 * (math.log(10) / 400)**2 * enc_rd**2 / math.pi**2)
            raw_exp = -g * (enc_mmr - opp_rating) / 400
            x = 10 ** raw_exp
            E = 1 / (1 + x)
            # Clamp E so it’s never exactly 0 or 1
            E = max(min(E, 1 - 1e-9), 1e-9)

            # Step B: compute d^2
            denom = (math.log(10) / 400)**2 * g**2 * E * (1 - E)
            d2 = 1 / denom if denom != 0 else float('inf')

            # Step C: update MMR and RD
            # ΔMMR = (ln(10)/400) / (1/rd^2 + 1/d2) * g * (outcome - E)
            mmr_delta = (math.log(10) / 400) / ((1 / enc_rd**2) + (1 / d2)) * g * (outcome - E)
            enc_mmr += mmr_delta
            enc_rd = math.sqrt(1 / ((1 / enc_rd**2) + (1 / d2)))

            # Step D: update volatility
            if abs(outcome - E) > 0.5:
                enc_vol = min(0.1, enc_vol + 0.01)
            else:
                enc_vol = max(0.05, enc_vol - 0.005)

        # Round values for storage
        return {
            "enc_mmr": round(enc_mmr, 2),
            "enc_rd":  round(enc_rd,  2),
            "enc_vol": round(enc_vol, 4)
        }


    async def process_and_store_match(self, match_id: str, region: str = "na"):
        from utils.henrik import henrik_get

        endpoint = f"/valorant/v3/matches/{region}/{match_id}"
        data = await henrik_get(endpoint)
        if not data or data.get("status") != 200:
            raise ValueError("Henrik API에서 경기를 찾을 수 없습니다.")

        match = data["data"]
        meta = match["metadata"]
        map_name = meta.get("map", "?")
        start_time = datetime.utcfromtimestamp(meta.get("game_start", 0))

        for player in match.get("players", {}).get("all_players", []):
            puuid = player.get("puuid")
            if not puuid:
                continue

            riot_name = player.get("name", "Unknown")
            riot_tag = player.get("tag", "NA")
            stats = player.get("stats", {})
            kda = f"{stats.get('kills', 0)}/{stats.get('deaths', 0)}/{stats.get('assists', 0)}"
            score = stats.get("score", 0)
            headshots = stats.get("headshots", 0)
            bodyshots = stats.get("bodyshots", 0)
            legshots = stats.get("legshots", 0)
            total = headshots + bodyshots + legshots
            hs_pct = (headshots / total) * 100 if total > 0 else 0
            adr = player.get("damage_made", 0) // max(meta.get("rounds_played", 1), 1)
            agent = player.get("character", "?")
            team = player.get("team", None)
            won = match.get("teams", {}).get(team.lower(), {}).get("has_won", None) if team else None
            round_count = meta.get("rounds_played", None)
            tier = player.get("currenttier_patched", None)

            async with self.bot.db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO match_players (match_id, puuid, riot_name, riot_tag, map, agent, kda, score, adr,
                                               hs_pct, team, won, round_count, tier, game_start)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
                    """,
                    match_id,
                    puuid,
                    riot_name,
                    riot_tag,
                    map_name,
                    agent,
                    kda,
                    score,
                    adr,
                    hs_pct,
                    team,
                    won,
                    round_count,
                    tier,
                    start_time
                )

    async def update_player_mmrs(self, conn, player: asyncpg.Record, region_hint: str = "na"):
        puuid = player["puuid"]

        # ── 1) Fetch up to 20 recent competitive matches from Henrik ──
        recent_url = (
            f"https://api.henrikdev.xyz/valorant/v3/by-puuid/matches/{region_hint}/{puuid}"
            f"?filter=competitive&size=20"
        )
        async with aiohttp.ClientSession() as session:
            async with session.get(
                recent_url,
                headers={"Authorization": os.getenv("HENRIK_API_KEY")}
            ) as resp:
                if resp.status != 200:
                    raise Exception(f"HENRIK API failure (status {resp.status})")
                data = await resp.json()

        all_matches = data.get("data", [])
        competitive_only = [
            m for m in all_matches
            if m.get("metadata", {}).get("mode", "").lower() == "competitive"
        ]
        matches = competitive_only[:5]  # most recent 5 competitive matches

        # ── (NEW) Update players.last_active = latest competitive game_start ──
        if matches:
            latest_unix = matches[0]["metadata"].get("game_start")
            if isinstance(latest_unix, (int, float)):
                latest_dt = datetime.utcfromtimestamp(latest_unix)
                await conn.execute(
                    "UPDATE players SET last_active = $1 WHERE puuid = $2",
                    latest_dt,
                    puuid
                )
        # ────────────────────────────────────────────────────────────────────────────

        # ── 2) Fetch recent custom matches from DB ──
        rows = await conn.fetch(
            "SELECT * FROM match_players WHERE puuid = $1 ORDER BY game_start DESC LIMIT 5",
            puuid
        )
        custom_matches = [dict(r) for r in rows]

        # ── 3) Combine matches for competitive MMR calculation ──
        all_matches_for_mmr = []
        for m in matches:
            player_data = next((p for p in m["players"]["all_players"] if p.get("puuid") == puuid), None)
            if not player_data:
                continue

            stats = player_data.get("stats", {})
            team = player_data.get("team", "").lower()
            teams = m.get("teams", {})
            won = teams.get(team, {}).get("has_won", False)
            all_matches_for_mmr.append({
                "score": stats.get("score", 0),
                "kills": stats.get("kills", 0),
                "deaths": stats.get("deaths", 0),
                "assists": stats.get("assists", 0),
                "won": won,
                "custom": False
            })

        for cm in custom_matches:
            all_matches_for_mmr.append({
                "score": cm["score"],
                "kills": cm["kills"],
                "deaths": cm["deaths"],
                "assists": cm["assists"],
                "won": cm["won"],
                "custom": True
            })

        # ── 4) Calculate and write competitive_mmr ──
        mmr = player["competitive_mmr"]
        for match_rec in all_matches_for_mmr:
            delta = 15 if match_rec["won"] else -10
            delta += (match_rec["kills"] - match_rec["deaths"]) * 0.5
            mmr += round(delta)

        await conn.execute(
            "UPDATE players SET competitive_mmr = $1 WHERE puuid = $2",
            int(mmr), puuid
        )

        # ── 5) Compute hidden‐MMR and hidden_encounter etc. ──
        # Build combined_matches list for Glicko updates
        combined_matches = []
        for m in matches:
            player_data = next((p for p in m["players"]["all_players"] if p.get("puuid") == puuid), None)
            if player_data:
                combined_matches.append(m)

        for cm in custom_matches:
            combined_matches.append({
                "metadata": {
                    "map": cm["map"],
                    "rounds_played": cm["round_count"],
                    "game_start_patched": cm["game_start"].strftime("%Y-%m-%d %H:%M"),
                    "game_start": int(cm["game_start"].timestamp())
                },
                "players": {
                    "all_players": [{
                        "puuid": puuid,
                        "team": cm["team"],
                        "character": cm["agent"],
                        "stats": {
                            "kills": cm["kills"],
                            "deaths": cm["deaths"],
                            "assists": cm["assists"],
                            "score": cm["score"],
                            "damage_made": float(cm["adr"]) * max(cm["round_count"], 1),
                            "damage_received": 0,
                            "headshots": int(cm["hs_pct"] * (cm["kills"] + cm["assists"] + 1) // 100),
                            "bodyshots": 0,
                            "legshots": 0,
                            "first_kills": cm.get("fk", 0),
                            "first_deaths": cm.get("fd", 0),
                            "clutch_score": cm.get("mk", 0)
                        },
                        "economy": {
                            "spent": 0,
                            "remaining": 0
                        },
                        "ability_casts": {
                            "c": 0,
                            "q": 0,
                            "e": 0,
                            "x": 0
                        }
                    }]
                },
                "teams": {
                    cm["team"].lower(): {"has_won": cm["won"]}
                }
            })

        if combined_matches:
            # 5.1) Glicko update for hidden_win
            new_hidden_win = self.calc_hidden_glicko(
                matches=combined_matches,
                puuid=puuid,
                prev_mmr=float(player.get("hidden_win_mmr", 1000)),
                prev_rd=float(player.get("hidden_win_rd", 350)),
                prev_vol=float(player.get("hidden_win_vol", 0.06))
            )

            # 5.2) Raw hidden performance metrics (including raw hidden_enc)
            hidden = self.calc_hidden(combined_matches, puuid)

            # 5.3) Glicko update for hidden_encounter
            enc_glicko = self.calc_encounter_glicko(
                matches=combined_matches,
                puuid=puuid,
                prev_enc_mmr=float(player.get("hidden_enc_mmr", 1000)),
                prev_enc_rd=float(player.get("hidden_enc_rd", 350)),
                prev_enc_vol=float(player.get("hidden_enc_vol", 0.06))
            )

            # ── 5.4) Gather Riot-rank info via new tier_to_score ──
            riot_score = 1000  # fallback
            latest_ranked = matches[0] if matches else None
            if latest_ranked:
                rd_players = latest_ranked["players"]["all_players"]
                rd_data = next((p for p in rd_players if p.get("puuid") == puuid), None)
                if rd_data:
                    tier = rd_data.get("currenttier_patched")
                    rr = rd_data.get("ranking_in_tier", 0)
                    if tier:
                        riot_score = self.tier_to_score(tier, rr)

            # ── 5.5) Compute weighted performance score ──
            weights = {
                "hidden_perf": 0.4,
                "hidden_enc": 0.15,
                "hidden_util": 0.15,
                "hidden_eco": 0.15,
                "clutch": 0.15
            }
            perf_score = sum(hidden[k] * weights[k] for k in weights)

            # ── 5.6) Compute Visible MMR, using the fine‐grained riot_score ──
            tier_bounds = [
                ("엠버", 800, 900),
                ("프로스트", 901, 1000),
                ("플레어", 1001, 1100),
                ("오로라", 1101, 1200),
                ("볼텍스", 1201, 1300),
                ("이터널", 1301, 1400),
                ("슈퍼노바", 1401, float("inf"))
            ]

            # 1) Find which tier riot_score falls into
            base_tier_name, tier_min, tier_max = tier_bounds[0]
            for name, low, high in tier_bounds:
                if low <= riot_score <= high:
                    base_tier_name, tier_min, tier_max = name, low, high
                    break

            if tier_max == float("inf"):
                # Radiant region (슈퍼노바): allow visible_mmr to go above or BELOW riot_score!
                base = int(perf_score * 0.5 + riot_score * 0.5)
                offset = int((perf_score % 50) * 0.4)
                enc_contrib = int((enc_glicko["enc_mmr"] - 1000) * 0.2)
                visible_mmr = base + offset + enc_contrib  # <- NO clamping to riot_score!
            else:
                interval_size = tier_max - tier_min + 1
                riot_ratio = (riot_score - tier_min) / interval_size

                if base_tier_name in ["엠버", "프로스트"]:
                    perf_scale = 0.05 + riot_ratio * 0.05
                elif base_tier_name in ["플레어", "오로라"]:
                    perf_scale = 0.10 + riot_ratio * 0.10
                elif base_tier_name in ["볼텍스", "이터널"]:
                    perf_scale = 0.20 + riot_ratio * 0.10
                else:
                    perf_scale = 0.30 + riot_ratio * 0.20

                riot_contrib = tier_min + int(interval_size * riot_ratio * 0.7)
                perf_contrib = int((perf_score - 1000) * perf_scale)
                enc_contrib = int((enc_glicko["enc_mmr"] - 1000) * 0.2)

                visible_mmr = riot_contrib + perf_contrib + enc_contrib
                visible_mmr = max(visible_mmr, tier_min + 5)
                if base_tier_name != "슈퍼노바":
                    visible_mmr = min(visible_mmr, tier_max - 5)

            # ── 5.7) Store everything ──
            await conn.execute(
                """
                UPDATE players
                SET visible_mmr    = $1,
                    hidden_win_mmr = $2,
                    hidden_win_rd  = $3,
                    hidden_win_vol = $4,
                    hidden_enc_mmr = $5,
                    hidden_enc_rd  = $6,
                    hidden_enc_vol = $7
                WHERE puuid = $8
                """,
                visible_mmr,
                new_hidden_win["mmr"],
                new_hidden_win["rd"],
                new_hidden_win["vol"],
                enc_glicko["enc_mmr"],
                enc_glicko["enc_rd"],
                enc_glicko["enc_vol"],
                puuid
            )

    async def get_leaderboard_message_id(self):
        async with self.bot.db.acquire() as conn:
            message_id = await conn.fetchval("SELECT value FROM bot_config WHERE key = 'mmr_leaderboard'")
            return int(message_id) if message_id else None

    async def set_leaderboard_message_id(self, message_id: int):
        async with self.bot.db.acquire() as conn:
            await conn.execute("""
                               INSERT INTO bot_config (key, value)
                               VALUES ('mmr_leaderboard', $1) ON CONFLICT (key) DO
                               UPDATE SET value = EXCLUDED.value
                               """, str(message_id))

    # ── Slash: link account → insert into DB, then live refresh ──
    @app_commands.command(name="연동", description="발로란트 계정을 디스코드랑 연동합니다.")
    @app_commands.describe(riot_name="라이엇 ID (예: 안녕하세요#겨울밤)")
    async def slash_link_account(self, interaction: discord.Interaction, riot_name: str):
        await interaction.response.defer(ephemeral=True)
        try:
            if "#" not in riot_name:
                await interaction.followup.send(
                    "❌ 라이엇 ID에는 반드시 '#'이 포함되어야 합니다.", ephemeral=True
                )
                return

            name, tag = riot_name.split("#", 1)
            endpoint = f"/valorant/v2/account/{urllib.parse.quote(name)}/{urllib.parse.quote(tag)}"
            acc_data = await self.henrik_get(endpoint)

            if not acc_data or "data" not in acc_data:
                await interaction.followup.send(
                    "❌ 해당 라이엇 계정을 찾을 수 없습니다.", ephemeral=True
                )
                await log_to_channel(self.bot, f"❌ 계정 연동 실패: {riot_name} (Not Found)")
                return

            puuid = acc_data["data"]["puuid"]

            # 1) players 테이블에 INSERT or UPDATE
            async with self.bot.db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO players (discord_id, puuid, riot_name, riot_tag, seeded,
                                         hidden_win_mmr, hidden_win_rd, hidden_win_vol,
                                         hidden_enc_mmr, visible_mmr, last_active, created_at)
                    VALUES ($1, $2, $3, $4, TRUE,
                            1000, 350, 0.06,
                            1000, 1000,
                            NOW(), NOW()) ON CONFLICT (discord_id) DO
                    UPDATE SET
                        puuid = EXCLUDED.puuid,
                        riot_name = EXCLUDED.riot_name,
                        riot_tag = EXCLUDED.riot_tag,
                        seeded = TRUE,
                        last_active = NOW();
                    """,
                    str(interaction.user.id),
                    puuid,
                    name,
                    tag
                )

            # 2) 연동 성공 알림
            await interaction.followup.send(f"✅ `{riot_name}` 계정이 성공적으로 연동되었습니다!", ephemeral=True)
            await log_to_channel(
                self.bot,
                f"✅ 계정 연동 성공: {riot_name} (PUUID: {puuid}, Discord: {interaction.user.id})"
            )

            # ──────────────────────────────────────────────────────────────
            # 3) 연동 직후 곧바로 MMR 계산 및 저장
            async with self.bot.db.acquire() as conn:
                # a) 방금 INSERT/UPDATE된 레코드를 다시 조회
                player_row = await conn.fetchrow(
                    "SELECT * FROM players WHERE puuid = $1", puuid
                )
                if player_row:
                    try:
                        # b) 타임아웃 없이 즉시 실행 (혹은 필요 시 asyncio.wait_for 로 감싸도 됨)
                        await self.update_player_mmrs(conn, player_row, "na")
                        await log_to_channel(
                            self.bot,
                            f"🔄 [연동후MMR] {riot_name}#{tag} MMR 계산 및 저장 완료"
                        )
                    except Exception as e:
                        await log_to_channel(
                            self.bot,
                            f"❌ [연동후MMR] {riot_name}#{tag} 계산 중 오류: {e}"
                        )
            # ──────────────────────────────────────────────────────────────

            # 4) 연동된 사용자가 추가됐으므로 리더보드 즉시 갱신
            await self.refresh_mmr_leaderboard()

        except Exception as e:
            await interaction.followup.send(f"❌ 예기치 못한 오류: {e}", ephemeral=True)
            await log_to_channel(self.bot, f"❌ 계정 연동 오류: {riot_name} – {e}")

    # ── Slash: show own rank (no DB change) ──
    @app_commands.command(
        name="티어",
        description="본인의 발로란트 경쟁 랭크, RR 점수, 서버 내 MMR 순위와 자체 티어를 보여줍니다."
    )
    @app_commands.describe(member="확인할 유저", region_hint="(선택) 지역 (na/eu/kr/ap/br/latam)")
    async def slash_rank(
            self,
            interaction: discord.Interaction,
            region_hint: Optional[str] = "na",
            member: Optional[discord.Member] = None
    ):
        await interaction.response.defer(ephemeral=True)
        user = member or interaction.user

        try:
            # 1) DB에서 해당 유저 정보(riot_name, riot_tag, visible_mmr) 가져오기
            async with self.bot.db.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT riot_name, riot_tag, visible_mmr FROM players WHERE discord_id = $1",
                    str(user.id)
                )
            if not row:
                await interaction.followup.send(
                    "❌ 라이엇 계정이 연동되어 있지 않습니다. `/연동` 명령어를 먼저 사용해 주세요.",
                    ephemeral=True
                )
                await log_to_channel(self.bot, f"⚠️ [티어] 계정 미연동: {user.display_name} ({user.id})")
                return

            riot_name = row["riot_name"]
            riot_tag = row["riot_tag"]
            visible_mmr = row["visible_mmr"]

            # 2) Riot API로 현재 실제 경쟁 티어 + RR 가져오기
            endpoint = f"/valorant/v1/mmr/{region_hint}/{riot_name}/{riot_tag}"
            data = await self.henrik_get(endpoint)

            if not data or "data" not in data:
                await interaction.followup.send(
                    "❌ 티어 정보를 불러올 수 없습니다. 라이엇 ID를 다시 확인해 주세요.",
                    ephemeral=True
                )
                await log_to_channel(self.bot, f"⚠️ [티어] 티어 정보 불러오기 실패: {riot_name}#{riot_tag}")
                return

            mmr_data = data["data"]
            current_tier = f"{mmr_data['currenttierpatched']} ({mmr_data['ranking_in_tier']} RR)"

            # 3) 서버 내 MMR 순위 계산
            async with self.bot.db.acquire() as conn:
                higher_count = await conn.fetchval(
                    "SELECT COUNT(*) FROM players WHERE visible_mmr > $1", visible_mmr
                )
                total_count = await conn.fetchval("SELECT COUNT(*) FROM players")
            rank = higher_count + 1

            # 4) 자체 티어(visible_mmr 기반) 산출
            custom_tier = self.get_ghibli_tier_label(visible_mmr)

            # 5) Embed 구성
            embed = discord.Embed(
                title=f"{riot_name}#{riot_tag} – 티어 정보",
                color=0xFF4655,
                timestamp=datetime.utcnow()
            )
            # Riot API 티어
            embed.add_field(name="● Riot 티어 (RR)", value=current_tier, inline=False)
            # 서버 내 MMR 순위
            embed.add_field(
                name="● 서버 내 MMR 순위",
                value=f"{rank}위 / {total_count}명",
                inline=False
            )
            # 자체 티어 (visible_mmr 기반)
            embed.add_field(
                name="● 자체 티어",
                value=f"{custom_tier} ({visible_mmr} MMR)",
                inline=False
            )
            if mmr_data.get("images", {}).get("small"):
                embed.set_thumbnail(url=mmr_data["images"]["small"])

            # 6) 응답 및 로그
            await interaction.followup.send(embed=embed, ephemeral=True)
            await log_to_channel(
                self.bot,
                f"✅ [티어] {riot_name}#{riot_tag} – Riot: {current_tier}, 서버순위: {rank}/{total_count}, 커스텀티어: {custom_tier}"
            )

        except Exception as e:
            await log_to_channel(self.bot, f"❌ [티어] 오류: {user.id} – {e}")
            await interaction.followup.send(f"❌ 오류: {e}", ephemeral=True)

    # ── Slash: show recent competitive matches (no DB change) ──
    @app_commands.command(
        name="최근경쟁",
        description="최근 경쟁전 5경기를 보여줍니다."
    )
    @app_commands.describe(member="확인할 유저", region_hint="(선택) 지역")
    async def slash_recent_matches(
            self,
            interaction: discord.Interaction,
            region_hint: Optional[str] = "na",
            member: Optional[discord.Member] = None
    ):
        await interaction.response.defer()
        user = member or interaction.user

        try:
            # 1) DB에서 해당 유저의 puuid 가져오기
            async with self.bot.db.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT riot_name, riot_tag, puuid FROM players WHERE discord_id = $1",
                    str(user.id)
                )
            if not row:
                await interaction.followup.send(
                    "❌ 라이엇 계정이 연동되어 있지 않습니다. `/연동` 명령어를 먼저 사용해 주세요.",
                    ephemeral=True
                )
                return

            riot_name = row["riot_name"]
            riot_tag = row["riot_tag"]
            puuid = row["puuid"]

            # 2) Henrik API: Competitive 필터를 반드시 붙여서 호출
            #     → pass only the path (no “https://…”!)
            endpoint = f"/valorant/v3/by-puuid/matches/{region_hint}/{puuid}?filter=competitive"
            match_data = await self.henrik_get(endpoint)
            if not match_data or match_data.get("status") != 200 or not match_data.get("data"):
                await interaction.followup.send(
                    "❌ 최근 경쟁전 정보를 불러올 수 없습니다.",
                    ephemeral=True
                )
                return

            # 3) 받은 매치 중 Competitive 모드만 최대 5개 추출
            all_matches = match_data["data"]
            competitive_only = []
            for m in all_matches:
                meta = m.get("metadata", {})
                if meta.get("mode", "").lower() == "competitive":
                    competitive_only.append(m)
                if len(competitive_only) >= 5:
                    break

            if not competitive_only:
                await interaction.followup.send(
                    "⚠️ 최근 5개의 경기 중 Competitive 모드를 찾지 못했습니다.",
                    ephemeral=True
                )
                return

            # 4) Embed 구성
            embed = discord.Embed(
                title=f"📊 {riot_name}#{riot_tag} – 최근 경쟁전 5경기",
                description="최근 Competitive 모드 경기 5개를 보여줍니다.",
                color=discord.Color.brand_red()
            )
            embed.set_footer(text="https://www.instagram.com/dngur.thd/")
            embed.timestamp = datetime.utcnow()

            # ─ 썸네일 세팅(첫 번째 경기에서 가능하다면)
            first_match = competitive_only[0]
            first_players = first_match.get("players", {}).get("all_players", [])
            pdata = next((p for p in first_players if p.get("puuid") == puuid), None)
            if pdata:
                thumb_url = pdata.get("assets", {}).get("card", {}).get("small")
                if thumb_url:
                    embed.set_thumbnail(url=thumb_url)

            # 5) 필드 추가
            field_count = 0
            for match in competitive_only:
                try:
                    meta = match.get("metadata", {})
                    players = match.get("players", {}).get("all_players", [])
                    player_data = next((p for p in players if p.get("puuid") == puuid), None)
                    if not player_data:
                        continue

                    # ── inside your for match loop ──
                    stats = player_data.get("stats", {})
                    kills = stats.get("kills", 0)
                    deaths = stats.get("deaths", 0)
                    assists = stats.get("assists", 0)

                    # restore this so {score} still exists
                    score = stats.get("score", 0)

                    headshots = stats.get("headshots", 0)
                    bodyshots = stats.get("bodyshots", 0)
                    legshots = stats.get("legshots", 0)

                    damage = player_data.get("damage_made", 0)
                    rounds = meta.get("rounds_played", 1) or 1

                    adr = round(damage / rounds)
                    acs = round(score / rounds)  # use score here
                    total_shots = headshots + bodyshots + legshots
                    hs_pct = (headshots / total_shots) * 100 if total_shots else 0

                    damage = stats.get("damage_made", 0)
                    rounds_played = meta.get("rounds_played", 1) or 1
                    adr = damage // rounds_played

                    team = player_data.get("team", "").lower()
                    won = match.get("teams", {}).get(team, {}).get("has_won", False)
                    result = "승리" if won else "패배"

                    match_id = meta.get("matchid", "")
                    map_name = meta.get("map", "알 수 없음")
                    mode_name = meta.get("mode", "알 수 없음")
                    rounds_num = meta.get("rounds_played", "?")
                    tier_name = player_data.get("currenttier_patched", "?")
                    agent_name = player_data.get("character", "?")
                    date_str = meta.get("game_start_patched", "알 수 없음")

                    embed.add_field(
                        name=f"🗺 {map_name} • {agent_name} • {mode_name} • {result}",
                        value=(
                            f"• **KDA:** `{kills}/{deaths}/{assists}` | **헤드샷률:** `{hs_pct:.1f}%`\n"
                            f"• **ACS:** `{acs}` | **점수:** `{score}` | **티어:** `{tier_name}`\n"
                            f"• **라운드:** `{rounds_num}`\n"
                            f"• **날짜:** {date_str}\n"
                            f"[🔗 경기 보기](https://tracker.gg/valorant/match/{match_id})"
                        ),
                        inline=False
                    )
                    field_count += 1
                except Exception as e:
                    await log_to_channel(self.bot, f"❌ [최근경쟁] 경기 파싱 오류: {e}")
                    continue

            if field_count == 0:
                await interaction.followup.send(
                    "❌ Competitive 경기 데이터를 찾지 못했습니다.",
                    ephemeral=True
                )
                await log_to_channel(self.bot, f"⚠️ [최근경쟁] Competitive 경기 데이터 없음: {riot_name}#{riot_tag}")
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
                await log_to_channel(
                    self.bot,
                    f"✅ [최근경쟁] {riot_name}#{riot_tag} – 최근 {field_count}개 경기 조회 성공"
                )

        except Exception as e:
            await log_to_channel(self.bot, f"❌ [최근경쟁] 오류: {user.id} – {e}")
            await interaction.followup.send(f"❌ 오류: {e}", ephemeral=True)

    # ── Slash: show recent custom matches (no DB change) ──
    @app_commands.command(name="최근내전", description="최근 커스텀 경기 5개를 보여줍니다.")
    @app_commands.describe(member="확인할 유저")
    async def slash_recent_custom_games(self, interaction: Interaction, member: Optional[discord.Member] = None):
        await interaction.response.defer()
        user = member or interaction.user

        try:
            async with self.bot.db.acquire() as conn:
                row = await conn.fetchrow("SELECT riot_name, riot_tag, puuid FROM players WHERE discord_id = $1",
                                          str(user.id))

            if not row:
                await interaction.followup.send("❌ 먼저 `/연동` 명령어로 계정을 연동해 주세요.", ephemeral=True)
                return

            riot_name = row["riot_name"]
            riot_tag = row["riot_tag"]
            puuid = row["puuid"]

            async with self.bot.db.acquire() as conn:
                records = await conn.fetch(
                    """
                    SELECT map,
                           agent,
                           kda,
                           score,
                           hs_pct,
                           adr,
                           team,
                           won,
                           round_count,
                           tier,
                           game_start,
                           match_id
                    FROM match_players
                    WHERE puuid = $1
                    ORDER BY game_start DESC LIMIT 5
                    """,
                    puuid
                )

            if not records:
                await interaction.followup.send("⚠️ 최근 커스텀 경기 기록이 없습니다.", ephemeral=True)
                return

            embed = discord.Embed(
                title=f"🎮 {riot_name}#{riot_tag} – 최근 커스텀 5경기",
                description="최근 기록된 내전 경기들을 보여줍니다.",
                color=discord.Color.gold()
            )
            embed.set_footer(text="powered by 겨울봇")
            embed.timestamp = discord.utils.utcnow()

            for rec in records:
                result = "승리" if rec["won"] else "패배"
                adr = round(rec["adr"]) if rec["adr"] is not None else 0
                date_str = rec["game_start"].strftime("%A, %B %d, %Y %I:%M %p")

                embed.add_field(
                    name=f"🗺 {rec['map']} • {rec['agent']} • {result}",
                    value=(
                        f"• **KDA:** {rec['kda']} | **헤드샷률:** {rec['hs_pct']:.1f}%\n"
                        f"• **ADR:** {adr} | **점수:** {rec['score']} | **티어:** {rec['tier']}\n"
                        f"• **라운드:** {rec['round_count']}\n"
                        f"• **날짜:** {date_str}\n"
                        f"[🔗 경기 보기](https://tracker.gg/valorant/match/{rec['match_id']})"
                    ),
                inline=False
                )

            await interaction.followup.send(embed=embed, ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ 오류 발생: {e}", ephemeral=True)
            await log_to_channel(self.bot, f"❌ [최근내전] 실패: {traceback.format_exc()}")

    # ── Slash: bulk update all MMRs one by one ──
    @app_commands.command(
        name="mmr업데이트",
        description="본인 또는 지정한 유저의 MMR을 즉시 업데이트합니다. (생략 시 DB의 모든 유저를 5초 간격으로 순차 갱신)"
    )
    @app_commands.describe(
        member="MMR을 업데이트할 유저 (생략 시 DB의 모든 유저)",
        region_hint="(선택) 지역 (na/eu/kr/ap/br/latam)"
    )
    @app_commands.check(is_admin)
    async def slash_update_single_mmr(
        self,
        interaction: discord.Interaction,
        member: Optional[discord.Member] = None,
        region_hint: Optional[str] = "na"
    ):
        # 1) defer to prepare an ephemeral progress message
        await interaction.response.defer(ephemeral=True)

        # 2) If a specific member is provided, update only that one immediately
        if member:
            async with self.bot.db.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM players WHERE discord_id = $1",
                    str(member.id)
                )
            if not row:
                await interaction.followup.send(
                    "❌ 해당 유저는 라이엇 계정이 연동되어 있지 않습니다.", ephemeral=True
                )
                return

            puuid = row["puuid"]
            riot_name = row["riot_name"]
            riot_tag = row["riot_tag"]

            try:
                async with self.bot.db.acquire() as conn:
                    await asyncio.wait_for(
                        self.update_player_mmrs(conn, row, region_hint),
                        timeout=20
                    )
                await log_to_channel(
                    self.bot,
                    f"✅ [mmr업데이트] 성공: {riot_name}#{riot_tag} (요청자: {interaction.user.display_name})"
                )
                await interaction.followup.send(
                    f"✅ `{riot_name}#{riot_tag}`님의 MMR 업데이트가 완료되었습니다.", ephemeral=True
                )
            except asyncio.TimeoutError:
                await log_to_channel(
                    self.bot,
                    f"⚠️ [mmr업데이트] 타임아웃: {riot_name}#{riot_tag} (요청자: {interaction.user.display_name})"
                )
                await interaction.followup.send(
                    f"⚠️ `{riot_name}#{riot_tag}`님의 MMR 계산이 시간 초과로 중단되었습니다.", ephemeral=True
                )
            except Exception as e:
                await log_to_channel(
                    self.bot,
                    f"❌ [mmr업데이트] 실패: {riot_name}#{riot_tag} – {e}"
                )
                await interaction.followup.send(
                    f"❌ `{riot_name}#{riot_tag}`님의 MMR 업데이트 중 오류가 발생했습니다: {e}", ephemeral=True
                )

            # After single-user update, refresh leaderboard
            try:
                await self.refresh_mmr_leaderboard()
            except:
                pass

            return

        # 3) No member specified → update all players one by one, 5 seconds apart
        async with self.bot.db.acquire() as conn:
            all_players = await conn.fetch("SELECT * FROM players")
        total = len(all_players)
        if total == 0:
            await interaction.followup.send(
                "⚠️ 데이터베이스에 등록된 플레이어가 없습니다.", ephemeral=True
            )
            return

        # 3-1) Send initial ephemeral progress message
        progress_message = await interaction.followup.send(
            f"⏳ MMR 업데이트를 시작합니다… (0/{total})", ephemeral=True
        )

        succeeded = 0
        # 3-2) Iterate over all players, updating one every 5 seconds
        for idx, player in enumerate(all_players, start=1):
            riot_name = player["riot_name"]
            riot_tag = player["riot_tag"]

            try:
                async with self.bot.db.acquire() as conn:
                    await self.update_player_mmrs(conn, player, region_hint)
                succeeded += 1
                await log_to_channel(
                    self.bot,
                    f"📋 ✅ [mmr업데이트] 성공: {riot_name}#{riot_tag} ({idx}/{total})"
                )
            except Exception as e:
                await log_to_channel(
                    self.bot,
                    f"📋 ❌ [mmr업데이트] 실패: {riot_name}#{riot_tag} ({idx}/{total}) – {e}"
                )

            # 3-3) Edit the progress message
            await progress_message.edit(
                content=f"⏳ MMR 업데이트 진행 중… ({idx}/{total})"
            )

            # 3-4) Wait 5 seconds before the next player (skip after the last one)
            if idx < total:
                await asyncio.sleep(2)

        # 4) After all updates are done, finalize the ephemeral message
        await progress_message.edit(
            content=f"✅ MMR 업데이트가 완료되었습니다! (성공 {succeeded}/{total}명)"
        )

        # 5) Finally, refresh the leaderboard one more time
        try:
            await self.refresh_mmr_leaderboard()
        except:
            pass


    @app_commands.command(
        name="mmr디버그",
        description="MMR 계산의 모든 내부 수치를 자세히 확인합니다."
    )
    @app_commands.check(is_admin)
    @app_commands.describe(member="유저 선택 (생략 시 본인)")
    async def slash_mmr_debug(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        user = member or interaction.user

        try:
            # 1) DB에서 유저 정보 가져오기
            async with self.bot.db.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT puuid,
                           riot_name,
                           riot_tag,
                           competitive_mmr,
                           hidden_win_mmr,
                           hidden_win_rd,
                           hidden_win_vol,
                           hidden_enc_mmr,
                           hidden_enc_rd,
                           hidden_enc_vol,
                           visible_mmr
                    FROM players
                    WHERE discord_id = $1
                    """,
                    str(user.id)
                )

            if not row:
                await interaction.followup.send(
                    "❌ This user is not linked. Use `/연동` first.", ephemeral=True
                )
                return

            puuid     = row["puuid"]
            riot_name = row["riot_name"]
            riot_tag  = row["riot_tag"]

            # 2) Henrik API: 최대 20개(match list) 가져오기
            henrik_url = (
                f"https://api.henrikdev.xyz/valorant/v3/by-puuid/matches/na/{puuid}"
                f"?filter=competitive&size=20"
            )
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    henrik_url,
                    headers={"Authorization": os.getenv("HENRIK_API_KEY", "")}
                ) as resp:
                    if resp.status != 200:
                        await interaction.followup.send(
                            f"❌ Henrik API 요청 실패: {resp.status}", ephemeral=True
                        )
                        return
                    data = await resp.json()

            all_matches = data.get("data", [])

            # 2.1) “Competitive” 모드만 골라냅니다
            competitive_matches = [
                m for m in all_matches
                if m.get("metadata", {}).get("mode", "").lower() == "competitive"
            ]

            # 2.2) 최근 5개 Competitive 경기만 사용
            henrik_matches = competitive_matches[:5]

            if not henrik_matches:
                await interaction.followup.send(
                    "❌ 최근 Competitive 경기를 찾을 수 없습니다.", ephemeral=True
                )
                return

            # 3) DB에서 마지막 5개 커스텀 경기 가져오기
            async with self.bot.db.acquire() as conn:
                custom_rows = await conn.fetch(
                    "SELECT * FROM match_players WHERE puuid = $1 ORDER BY game_start DESC LIMIT 5",
                    puuid
                )
            custom_matches = [dict(r) for r in custom_rows]

            # 4) “Competitive” + “Custom” 합치기 (hidden 계산용)
            combined_matches = henrik_matches.copy()
            for cm in custom_matches:
                combined_matches.append({
                    "metadata": {
                        "map": cm["map"],
                        "game_start_patched": cm["game_start"].strftime("%Y-%m-%d %H:%M")
                    },
                    "players": {
                        "all_players": [{
                            "puuid": puuid,
                            "stats": {
                                "kills": cm["kills"],
                                "deaths": cm["deaths"],
                                "assists": cm["assists"],
                                "damage_made": float(cm["adr"]) * max(cm["round_count"], 1),
                                "headshots": int(cm["hs_pct"] * (cm["kills"] + cm["assists"] + 1) // 100),
                                "bodyshots": 0,
                                "legshots": 0
                            },
                            "team": cm["team"],
                            "character": cm["agent"],
                            "currenttier_patched": cm["tier"],
                            "ranking_in_tier": 50  # dummy RR if needed
                        }]
                    }
                })

            # 5) Hidden 계산
            hidden_stats = self.calc_hidden(combined_matches, puuid)
            glicko_stats = self.calc_hidden_glicko(
                combined_matches,
                puuid,
                prev_mmr=float(row["hidden_win_mmr"]),
                prev_rd=float(row["hidden_win_rd"]),
                prev_vol=float(row["hidden_win_vol"])
            )

            # 6) Riot RR 정보 가져오기
            mmr_endpoint = f"/valorant/v1/mmr/na/{riot_name}/{riot_tag}"
            mmr_data = await self.henrik_get(mmr_endpoint)
            if not mmr_data or "data" not in mmr_data:
                riot_tier  = "Unrated"
                riot_rr    = 0
                riot_score = 0
            else:
                riot_info  = mmr_data["data"]
                riot_tier  = riot_info.get("currenttierpatched", "Unrated")
                riot_rr    = riot_info.get("ranking_in_tier", 0)
                riot_score = self.tier_to_score(riot_tier, riot_rr)

            # 7) Visible MMR 계산 (Riot 70% + Perf 30%)
            weights    = {
                "hidden_perf": 0.4,
                "hidden_enc":  0.15,
                "hidden_util": 0.15,
                "hidden_eco":  0.15,
                "clutch":      0.15
            }
            perf_score = sum(hidden_stats[k] * weights[k] for k in weights)
            visible = riot_score * 0.7 + perf_score * 0.3

            # 8) 디버그 메시지 구성
            lines = []
            lines.append(f"📊 **MMR Debug for `{riot_name}#{riot_tag}`**")
            lines.append(
                f"• Riot Rank: **{riot_tier}** ({riot_rr} RR) → `riot_score = {riot_score}`"
            )
            lines.append(f"• Weighted Performance Score (30% of final):")
            for k, w in weights.items():
                lines.append(f"   - `{k}` = {hidden_stats[k]} × {w} → `{int(hidden_stats[k] * w)}`")
            lines.append(f"   → Total Perf Score = `{int(perf_score)}`")
            lines.append("")
            lines.append("🏁 Final Visible MMR = Riot(70%) + Performance(30%)")
            lines.append(f"   = {riot_score} × 0.7 + {int(perf_score)} × 0.3 = **{visible}**")
            lines.append("")
            lines.append("🔐 **Hidden Glicko (Win/Loss Based)**")
            lines.append(f"• hidden_win_mmr = `{glicko_stats['mmr']}`")
            lines.append(f"• hidden_win_rd  = `{glicko_stats['rd']}`")
            lines.append(f"• hidden_win_vol = `{glicko_stats['vol']}`")
            lines.append("")
            lines.append("🎯 **Stat Breakdown (Last 5 Competitive Matches)**")
            for i, match in enumerate(henrik_matches):
                meta         = match.get("metadata", {})
                map_name     = meta.get("map", "?")
                date         = meta.get("game_start_patched", "?")
                players_list = match.get("players", {}).get("all_players", [])
                pdata        = next((p for p in players_list if p.get("puuid") == puuid), None)
                if not pdata:
                    continue

                stats         = pdata.get("stats", {})
                kills         = stats.get("kills", 0)
                deaths        = stats.get("deaths", 1)
                assists       = stats.get("assists", 0)
                damage        = stats.get("damage_made", 0)
                rounds_played = meta.get("rounds_played", 1) or 1
                adr           = damage // rounds_played

                hs            = stats.get("headshots", 0)
                bs            = stats.get("bodyshots", 0)
                ls            = stats.get("legshots", 0)
                total         = hs + bs + ls
                hs_pct        = (hs / total * 100) if total > 0 else 0

                lines.append(
                    f"• Match {i+1}: `{map_name}` ({date}) → "
                    f"KDA: {kills}/{deaths}/{assists}, ADR: {adr}, HS%: {hs_pct:.1f}"
                )

            await interaction.followup.send("```" + "\n".join(lines) + "```", ephemeral=True)

        except Exception as e:
            await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

    # ── Slash: manually view TOP 10 leaderboard (ephemeral) ──
    @app_commands.command(
        name="mmr리더보드",
        description="서버 내 최종 MMR 랭킹 TOP 10을 보여줍니다."
    )
    @app_commands.check(is_admin)
    async def slash_mmr_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        view = LeaderboardView(self, page=0, per_page=10)
        embed = await self.build_mmr_leaderboard_embed(page=0, per_page=10)
        await interaction.followup.send(embed=embed, view=view, ephemeral=False)

    @app_commands.command(name="내전추가", description="Tracker.gg 링크에서 최근 커스텀 경기를 수동 저장합니다.")
    @app_commands.describe(link="Tracker.gg 매치 링크")
    async def slash_add_custom_game(self, interaction: discord.Interaction, link: str):
        await interaction.response.defer(ephemeral=False)  # ⬅️ Make response public

        # Get invoking user's Riot ID
        async with self.bot.db.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT riot_name, riot_tag FROM players WHERE discord_id = $1",
                str(interaction.user.id)
            )

        if not row:
            await interaction.followup.send("❌ 먼저 `/연동` 명령어로 계정을 등록해 주세요.", ephemeral=True)
            return

        riot_id = f"{row['riot_name']}#{row['riot_tag']}"

        try:
            # Run Puppeteer scraper with Riot ID as argument
            proc = await asyncio.create_subprocess_exec(
                "node", "puppeteer/scrape_tracker.js", link, riot_id,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            try:
                data = json.loads(stdout)
            except Exception as e:
                stdout_str = stdout.decode(errors="ignore")
                if len(stdout_str) > 1900:
                    stdout_str = stdout_str[:1900] + "\n... (중략)"
                await interaction.followup.send(
                    f"❌ JSON 파싱 오류: {e}\n\nstdout:\n```{stdout_str}```",
                    ephemeral=True
                )
                return

            if stderr:
                print(f"[내전추가 stderr] {stderr.decode(errors='ignore').strip()}")

            players = data.get("players", [])
            if not players:
                await interaction.followup.send("❌ 플레이어 데이터를 찾을 수 없습니다.", ephemeral=True)
                return

            # Get all linked players
            async with self.bot.db.acquire() as conn:
                rows = await conn.fetch("SELECT puuid, riot_name, riot_tag FROM players")

            linked_players = {
                f"{r['riot_name']}#{r['riot_tag']}": r['puuid']
                for r in rows
            }

            valid_players = []
            for player in players:
                player_name = player["name"].strip()
                if player_name.count('#') != 1:
                    await log_to_channel(self.bot, f"⚠️ Invalid Riot ID format: {player_name}")
                    continue
                if player_name in linked_players:
                    player["puuid"] = linked_players[player_name]
                    valid_players.append(player)
                else:
                    await log_to_channel(self.bot, f"📋 Unregistered player: {player_name}")

            match_id = link.split("/")[-1]
            map_name = data.get("map", "Unknown")
            if map_name == "Unknown":
                map_name = data.get("mapText", "Unknown")

            round_count = data.get("round_count", 0)
            won_team = "Red" if data.get("won") else "Blue"
            team1_score = data.get("team1_score", 0)
            team2_score = data.get("team2_score", 0)
            game_start = datetime.utcnow()

            async with self.bot.db.acquire() as conn:
                for player in valid_players:
                    riot_name, riot_tag = player["name"].split("#", 1)
                    await conn.execute("""
                                       INSERT INTO match_players (match_id, puuid, riot_name, riot_tag, map, agent, kda,
                                                                  kills, deaths, assists, score, adr, hs_pct, kast_pct,
                                                                  plus_minus, kd_ratio, dda, fk, fd, mk, team, won,
                                                                  round_count, tier, game_start, team1_score,
                                                                  team2_score)
                                       VALUES ($1, $2, $3, $4, $5, $6, $7,
                                               $8, $9, $10, $11, $12, $13, $14,
                                               $15, $16, $17, $18, $19, $20, $21, $22,
                                               $23, $24, $25, $26, $27) ON CONFLICT (match_id, puuid) DO NOTHING
                                       """,
                                       match_id,
                                       player["puuid"],
                                       riot_name,
                                       riot_tag,
                                       map_name,
                                       player["agent"],
                                       f"{player['kills']}/{player['deaths']}/{player['assists']}",
                                       player["kills"],
                                       player["deaths"],
                                       player["assists"],
                                       player["score"],
                                       player["adr"],
                                       player["hs_pct"],
                                       player["kast_pct"],
                                       player["plus_minus"],
                                       player["kd_ratio"],
                                       player["dda"],
                                       player["fk"],
                                       player["fd"],
                                       player["mk"],
                                       player["team"],
                                       player["team"] == won_team,
                                       round_count,
                                       player["tier"],
                                       game_start,
                                       team1_score,
                                       team2_score
                                       )

            await interaction.followup.send(
                f"✅ {map_name} 맵 내전이 저장되었습니다. 플레이어 수: {len(valid_players)}명\n"
                f"🔹 {won_team} 팀 승리 ({team1_score}-{team2_score})",
                ephemeral=False
            )

        except Exception as e:
            await interaction.followup.send(f"❌ 오류 발생: {str(e)}", ephemeral=True)
            await log_to_channel(self.bot, f"❌ [내전추가] 실패: {traceback.format_exc()}")


    # ── Send the embed once and store its message ID ──
    async def initial_post_mmr_leaderboard(self):
        chan = self.bot.get_channel(int(os.getenv("MMR_CHANNEL_ID", "0")))
        if not chan:
            await log_to_channel(self.bot, f"⚠️ [MMR] Invalid MMR_CHANNEL_ID: {os.getenv('MMR_CHANNEL_ID')}")
            return

        embed = await self.build_mmr_leaderboard_embed()
        view = LeaderboardView(self, page=0, per_page=10)
        msg = await chan.send(embed=embed, view=view)

        global MMR_LEADERBOARD_MESSAGE_ID
        MMR_LEADERBOARD_MESSAGE_ID = msg.id
        print(f"[MMR] Initial leaderboard posted. Message ID = {MMR_LEADERBOARD_MESSAGE_ID}")

    # ── Build the TOP 10 embed ──
    async def build_mmr_leaderboard_embed(self, page: int = 0, per_page: int = 10) -> discord.Embed:
        # Calculate correct offset
        offset = page * per_page

        # Get total number of eligible players for navigation info (optional)
        async with self.bot.db.acquire() as conn:
            total_count = await conn.fetchval("""
                                              SELECT COUNT(*)
                                              FROM players
                                              WHERE last_active >= NOW() - INTERVAL '7 days'
                                              """)
            rows = await conn.fetch("""
                                    SELECT discord_id, riot_name, riot_tag, visible_mmr
                                    FROM players
                                    WHERE last_active >= NOW() - INTERVAL '7 days'
                                    ORDER BY visible_mmr DESC
                                    OFFSET $1 LIMIT $2
                                    """, offset, per_page)

        embed = discord.Embed(
            title=":trophy: Valorant MMR Leaderboard",
            color=discord.Color.gold(),
            timestamp=datetime.utcnow()
        )

        if not rows:
            embed.description = "최근 7일 이내에 활동한 플레이어가 없습니다."
        else:
            lines = []
            # Global rank is offset + index+1
            for i, row in enumerate(rows, start=offset + 1):
                discord_id = int(row["discord_id"])
                mention = f"<@{discord_id}>"
                riot_name = row["riot_name"]
                riot_tag = row["riot_tag"]
                mmr = row["visible_mmr"]

                tracker_url = (
                    f"https://tracker.gg/valorant/profile/riot/"
                    f"{urllib.parse.quote(riot_name)}%23{urllib.parse.quote(riot_tag)}/overview"
                )

                # Pass global rank to the tier label for supernova restriction!
                tier_label = self.get_ghibli_tier_label(mmr, rank=i)
                main_tier = tier_label.split(" ", 1)[0]
                icon_ref = GhibliIcon.get(main_tier, "")

                line = (
                    f"**{i}.** {mention} — "
                    f"[{riot_name}#{riot_tag}]({tracker_url})  \n"
                    f"{icon_ref} `{tier_label}` ({mmr} MMR)"
                )
                lines.append(line)

            embed.description = "\n".join(lines)

            # Pagination info (optional, can be removed)
            max_page = (total_count - 1) // per_page
            embed.set_footer(text=f"Page {page + 1}/{max_page + 1}  |  지난 7일간 최소 1경기 기록한 플레이어만 노출됩니다.")

        return embed

    # ── Edit the existing leaderboard message or send a new one if not found ──
    async def refresh_mmr_leaderboard(self, page=0, per_page=10):
        chan = self.bot.get_channel(int(os.getenv("MMR_CHANNEL_ID", "0")))
        if not chan:
            await log_to_channel(self.bot, f"⚠️ [MMR] Invalid MMR_CHANNEL_ID: {os.getenv('MMR_CHANNEL_ID')}")
            return

        embed = await self.build_mmr_leaderboard_embed(page=page, per_page=per_page)
        view = LeaderboardView(self, page=page, per_page=per_page)

        message_id = await self.get_leaderboard_message_id()
        msg = None

        if message_id:
            try:
                msg = await chan.fetch_message(message_id)
                await msg.edit(embed=embed, view=view)
            except discord.NotFound:
                msg = None
            except Exception as e:
                await log_to_channel(self.bot, f"⚠️ [MMR] Failed to edit leaderboard message: {e}")
                msg = None

        if not msg:
            new_msg = await chan.send(embed=embed, view=view)
            await self.set_leaderboard_message_id(new_msg.id)

    # ── Daily loop: update every player in DB once per day ──
    async def run_daily_update(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            # 0) Wait until next midnight ET
            now = datetime.now(pytz.timezone("America/Toronto"))
            tomorrow = (now + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            wait_seconds = (tomorrow - now).total_seconds()
            await log_to_channel(
                self.bot,
                f"⏰ 다음 MMR 업데이트까지 {wait_seconds:.1f}초 "
                f"({tomorrow.strftime('%Y-%m-%d %H:%M')} 동부 시간) 대기"
            )
            await asyncio.sleep(wait_seconds)

            try:
                # 1) Log start
                timestamp = datetime.now(pytz.timezone("America/Toronto")).strftime(
                    "%Y-%m-%d %H:%M"
                )
                await log_to_channel(
                    self.bot,
                    f"⏬ [SCHEDULER] 일일 MMR 업데이트 실행 중: {timestamp}"
                )

                # 2) Bulk update every player's MMR
                async with self.bot.db.acquire() as conn:
                    players = await conn.fetch("SELECT * FROM players")

                count = 0
                for player in players:
                    try:
                        async with self.bot.db.acquire() as conn:
                            await self.update_player_mmrs(conn, player, "na")
                        await log_to_channel(
                            self.bot,
                            f"✅ [SCHEDULER] 업데이트 완료: "
                            f"{player['riot_name']}#{player['riot_tag']}"
                        )
                    except Exception as e:
                        await log_to_channel(
                            self.bot,
                            f"❌ [SCHEDULER] 업데이트 실패: "
                            f"{player['riot_name']}#{player['riot_tag']}: {e}"
                        )
                    count += 1
                    await asyncio.sleep(10)  # throttle between updates

                await log_to_channel(
                    self.bot,
                    f"✅ [SCHEDULER] 일일 MMR 업데이트 완료. 총: {count}명"
                )

                # 3) Refresh the leaderboard embed
                try:
                    await self.refresh_mmr_leaderboard()
                    await log_to_channel(
                        self.bot,
                        "🔄 [SCHEDULER] 리더보드 embed 갱신 완료"
                    )
                except Exception as e:
                    await log_to_channel(
                        self.bot,
                        f"❌ [SCHEDULER] 리더보드 갱신 실패: {e}"
                    )

                # 4) Riot ID 변경 감지 (throttled, 429-safe)
                async with self.bot.db.acquire() as conn:
                    for player in players:
                        old_name = player["riot_name"]
                        old_tag  = player["riot_tag"]
                        puuid    = player["puuid"]

                        # throttle so we stay under rate limits
                        await asyncio.sleep(4)

                        # 4.1) Try name#tag lookup
                        data = await self.henrik_get(
                            f"/valorant/v2/account/{old_name}/{old_tag}"
                        )
                        if data is None:
                            # if we hit a 429 or error, skip the PUUID lookup
                            continue

                        # throttle again before second call
                        await asyncio.sleep(4)

                        # 4.2) Confirm via PUUID lookup
                        puuid_data = await self.henrik_get(
                            f"/valorant/v2/account/by-puuid/{puuid}"
                        )
                        if puuid_data is None or "data" not in puuid_data:
                            continue

                        new_name = puuid_data["data"]["name"]
                        new_tag  = puuid_data["data"]["tag"]
                        if new_name != old_name or new_tag != old_tag:
                            # update DB and refresh leaderboard on change
                            await conn.execute(
                                """
                                UPDATE players
                                SET riot_name = $1, riot_tag = $2
                                WHERE puuid = $3
                                """,
                                new_name, new_tag, puuid
                            )
                            await log_to_channel(
                                self.bot,
                                f"🔄 Riot ID 변경 감지: "
                                f"{old_name}#{old_tag} → {new_name}#{new_tag}"
                            )
                            await self.refresh_mmr_leaderboard()

            except Exception as e:
                await log_to_channel(
                    self.bot,
                    f"❌ [SCHEDULER] 일일 MMR 업데이트 실패: {e}"
                )

#setup
async def setup(bot: commands.Bot):
    if not hasattr(bot, "db"):
        DATABASE_DSN = os.getenv("DATABASE_URL")
        bot.db = await asyncpg.create_pool(DATABASE_DSN)

    async with bot.db.acquire() as conn:
        await conn.execute(CREATE_PLAYERS_SQL)
        await conn.execute(CREATE_ANALYZED_SQL)
        await conn.execute(MATCH_PLAYERS_SQL)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS bot_config (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        await log_to_channel(bot, "✅ <MMR> 데이터베이스 테이블 생성 완료")

    cog = ValorantMMRCog(bot)
    await bot.add_cog(cog)
