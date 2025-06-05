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
  discord_nick   TEXT,                -- 새로 추가한 컬럼
  competitive_mmr INTEGER NOT NULL DEFAULT 1000,
  hidden_win_mmr NUMERIC   NOT NULL DEFAULT 1000,
  hidden_win_rd  NUMERIC   NOT NULL DEFAULT 350,
  hidden_win_vol NUMERIC   NOT NULL DEFAULT 0.06,
  hidden_enc_mmr NUMERIC   NOT NULL DEFAULT 1000,
  visible_mmr    INTEGER   NOT NULL DEFAULT 1000,
  seeded         BOOLEAN   NOT NULL DEFAULT FALSE,
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

def is_admin(interaction: Interaction):
    return interaction.user.guild_permissions.administrator

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

    # ── Convert Riot tier+RR → numeric score ──
    def tier_to_score(self, tier: str, rr: int) -> int:
        TIERS = [
            "Iron 1","Iron 2","Iron 3","Bronze 1","Bronze 2","Bronze 3",
            "Silver 1","Silver 2","Silver 3","Gold 1","Gold 2","Gold 3",
            "Platinum 1","Platinum 2","Platinum 3","Diamond 1","Diamond 2","Diamond 3",
            "Ascendant 1","Ascendant 2","Ascendant 3","Immortal 1","Immortal 2","Immortal 3",
            "Radiant"
        ]
        base = TIERS.index(tier) * 300 if tier in TIERS else 0
        return base + rr

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

        q = math.log(10) / 400
        mmr = prev_mmr
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
            results.append((1000, 1 if won else 0))  # Assume opponent rating is 1000

        if not results:
            return {"mmr": mmr, "rd": rd, "vol": vol}

        # Glicko-style update
        for opp_rating, outcome in results:
            g = 1 / math.sqrt(1 + 3 * q ** 2 * rd ** 2 / math.pi ** 2)
            E = 1 / (1 + 10 ** (-g * (mmr - opp_rating) / 400))
            d2 = 1 / (q ** 2 * g ** 2 * E * (1 - E))
            mmr_delta = q / ((1 / rd ** 2) + (1 / d2)) * g * (outcome - E)
            mmr += mmr_delta
            rd = math.sqrt(((1 / rd ** 2) + (1 / d2)) ** -1)
            if abs(outcome - E) > 0.5:
                vol = min(0.1, vol + 0.01)
            else:
                vol = max(0.05, vol - 0.005)

        return {
            "mmr": round(mmr, 2),
            "rd": round(rd, 2),
            "vol": round(vol, 4)
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

        # 1. Fetch latest competitive matches from Henrik API
        recent_url = f"https://api.henrikdev.xyz/valorant/v3/by-puuid/matches/{region_hint}/{puuid}?filter=competitive"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                    recent_url,
                    headers={"Authorization": os.getenv("HENRIK_API_KEY")}
            ) as resp:
                if resp.status != 200:
                    raise Exception(f"HENRIK API 실패 (status {resp.status})")
                data = await resp.json()

        matches = data.get("data", [])[:5]  # Limit to 5 most recent

        # 2. Fetch recent custom matches from DB
        rows = await conn.fetch(
            "SELECT * FROM match_players WHERE puuid = $1 ORDER BY game_start DESC LIMIT 5",
            puuid
        )
        custom_matches = [dict(r) for r in rows]

        # 3. Combine matches for competitive MMR calculation
        all_matches = []
        for m in matches:
            players = m.get("players", {}).get("all_players", [])
            player_data = next((p for p in players if p.get("puuid") == puuid), None)
            if not player_data:
                continue
            stats = player_data.get("stats", {})
            team = player_data.get("team", "").lower()
            teams = m.get("teams", {})
            won = teams.get(team, {}).get("has_won", False)
            all_matches.append({
                "score": stats.get("score", 0),
                "kills": stats.get("kills", 0),
                "deaths": stats.get("deaths", 0),
                "assists": stats.get("assists", 0),
                "won": won,
                "custom": False
            })

        for m in custom_matches:
            all_matches.append({
                "score": m.get("score", 0),
                "kills": m.get("kills", 0),
                "deaths": m.get("deaths", 0),
                "assists": m.get("assists", 0),
                "won": m.get("won", False),
                "custom": True
            })

        # 4. Calculate competitive MMR
        mmr = player["competitive_mmr"]
        for match in all_matches:
            delta = 0
            if match["won"]:
                delta += 15
            else:
                delta -= 10
            delta += (match["kills"] - match["deaths"]) * 0.5
            mmr += round(delta)

        await conn.execute(
            "UPDATE players SET competitive_mmr = $1 WHERE puuid = $2",
            int(mmr), puuid
        )

        # 5. Calculate advanced visible and hidden mmrs (with new hidden logic)
        henrik_matches = []
        for m in matches:
            players = m.get("players", {}).get("all_players", [])
            player_data = next((p for p in players if p.get("puuid") == puuid), None)
            if player_data:
                henrik_matches.append(m)

        if henrik_matches:
            # --- Glicko-based hidden_win stats update ---
            new_hidden_win = self.calc_hidden_glicko(
                matches=henrik_matches,
                puuid=puuid,
                prev_mmr=float(player.get("hidden_win_mmr", 1000)),
                prev_rd=float(player.get("hidden_win_rd", 350)),
                prev_vol=float(player.get("hidden_win_vol", 0.06))
            )

            # --- Other hidden stats ---
            hidden = self.calc_hidden(henrik_matches, puuid)

            weights = {
                "hidden_perf": 0.4,
                "hidden_enc": 0.15,
                "hidden_util": 0.15,
                "hidden_eco": 0.15,
                "clutch": 0.15
            }
            riot_score = 1000  # fallback/default
            latest = henrik_matches[0]
            players = latest.get("players", {}).get("all_players", [])
            player_data = next((p for p in players if p.get("puuid") == puuid), None)
            if player_data:
                currenttier = player_data.get("currenttier_patched")
                rr = player_data.get("ranking_in_tier", 0)
                if currenttier:
                    riot_score = self.tier_to_score(currenttier, rr)
            perf_score = sum(hidden[k] * weights[k] for k in weights)
            visible = int(riot_score * 0.7 + perf_score * 0.3)

            await conn.execute(
                """
                UPDATE players
                SET visible_mmr    = $1,
                    hidden_win_mmr = $2,
                    hidden_win_rd  = $3,
                    hidden_win_vol = $4,
                    hidden_enc_mmr = $5
                WHERE puuid = $6
                """,
                int(visible),
                new_hidden_win["mmr"],
                new_hidden_win["rd"],
                new_hidden_win["vol"],
                hidden["hidden_enc"],
                puuid
            )
        else:
            await conn.execute(
                "UPDATE players SET visible_mmr = $1 WHERE puuid = $2",
                1000, puuid
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
                await interaction.followup.send("❌ 라이엇 ID에는 반드시 '#'이 포함되어야 합니다.", ephemeral=True)
                return

            name, tag = riot_name.split("#", 1)
            endpoint = f"/valorant/v2/account/{urllib.parse.quote(name)}/{urllib.parse.quote(tag)}"
            acc_data = await self.henrik_get(endpoint)

            if not acc_data or "data" not in acc_data:
                await interaction.followup.send("❌ 해당 라이엇 계정을 찾을 수 없습니다.", ephemeral=True)
                await log_to_channel(self.bot, f"❌ 계정 연동 실패: {riot_name} (Not Found)")
                return

            puuid = acc_data["data"]["puuid"]
            async with self.bot.db.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO players (
                        discord_id, puuid, riot_name, riot_tag, seeded,
                        hidden_win_mmr, hidden_win_rd, hidden_win_vol,
                        hidden_enc_mmr, visible_mmr, last_active, created_at
                    ) VALUES ($1, $2, $3, $4, TRUE,
                              1000, 350, 0.06,
                              1000, 1000,
                              NOW(), NOW())
                    ON CONFLICT (discord_id) DO UPDATE SET
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

            await interaction.followup.send(f"✅ `{riot_name}` 계정이 성공적으로 연동되었습니다!")
            await log_to_channel(self.bot, f"✅ 계정 연동 성공: {riot_name}#{tag} (PUUID: {puuid}, Discord: {interaction.user.id})")

            # ── LIVE UPDATE: Someone new joined → refresh leaderboard ──
            await self.refresh_mmr_leaderboard()

        except Exception as e:
            await interaction.followup.send(f"❌ 예기치 못한 오류: {e}", ephemeral=True)
            await log_to_channel(self.bot, f"❌ 계정 연동 오류: {riot_name} – {e}")

    # ── Slash: show own rank (no DB change) ──
    @app_commands.command(
        name="티어",
        description="본인의 발로란트 경쟁 랭크와 RR점수를 보여줍니다."
    )
    @app_commands.describe(member="확인할 유저", region_hint="(선택) 지역 (na/eu/kr/ap/br/latam)")
    async def slash_rank(
        self,
        interaction: discord.Interaction,
        region_hint: Optional[str] = "na",
        member: Optional[discord.Member] = None
    ):
        await interaction.response.defer()
        user = member or interaction.user
        try:
            async with self.bot.db.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT riot_name, riot_tag FROM players WHERE discord_id = $1",
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
            endpoint = f"/valorant/v1/mmr/{region_hint}/{riot_name}/{riot_tag}"
            data = await self.henrik_get(endpoint)

            if not data or "data" not in data:
                await interaction.followup.send(
                    "❌ 티어 정보를 불러올 수 없습니다. 라이엇 ID를 다시 확인해 주세요.",
                    ephemeral=True
                )
                await log_to_channel(self.bot, f"⚠️ [티어] 티어 정보 불러오기 실패: {riot_name}#{riot_tag}")
                return

            mmr = data["data"]
            current = f"{mmr['currenttierpatched']} ({mmr['ranking_in_tier']} RR)"
            embed = discord.Embed(
                title=f"{riot_name}#{riot_tag} – 현재 티어",
                color=0xFF4655,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="현재 티어", value=current, inline=False)
            if mmr.get("images", {}).get("small"):
                embed.set_thumbnail(url=mmr["images"]["small"])
            await interaction.followup.send(embed=embed, ephemeral=True)
            await log_to_channel(self.bot, f"✅ [티어] {riot_name}#{riot_tag} – {current} ({user.id})")

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
                await log_to_channel(self.bot, f"⚠️ [최근경쟁] 계정 미연동: {user.display_name} ({user.id})")
                return

            riot_name = row["riot_name"]
            riot_tag = row["riot_tag"]
            puuid = row["puuid"]

            match_endpoint = f"/valorant/v3/by-puuid/matches/{region_hint}/{puuid}"
            match_data = await self.henrik_get(match_endpoint)
            if not match_data or match_data.get("status") != 200 or not match_data.get("data"):
                await interaction.followup.send(
                    "❌ 최근 경기 정보를 불러올 수 없습니다.",
                    ephemeral=True
                )
                await log_to_channel(self.bot, f"⚠️ [최근경쟁] 경기 정보 불러오기 실패: {riot_name}#{riot_tag}")
                return

            matches = match_data["data"][:5]
            embed = discord.Embed(
                title=f"📊 {riot_name}#{riot_tag} – 최근 경쟁전 5경기",
                description="최근 경쟁 경기 5개를 보여줍니다",
                color=discord.Color.brand_red()
            )
            embed.set_footer(text="https://www.instagram.com/dngur.thd/")
            embed.timestamp = datetime.utcnow()

            first_match = matches[0]
            players = first_match.get("players", {}).get("all_players", [])
            player_data = next((p for p in players if p.get("puuid") == puuid), None)
            if player_data and player_data.get("assets", {}).get("card", {}).get("small"):
                embed.set_thumbnail(url=player_data["assets"]["card"]["small"])

            field_count = 0
            for match in matches:
                try:
                    meta = match.get("metadata", {})
                    players = match.get("players", {}).get("all_players", [])
                    player_data = next((p for p in players if p.get("puuid") == puuid), None)
                    if not player_data:
                        continue

                    stats = player_data["stats"]
                    kills = stats.get("kills", 0)
                    deaths = stats.get("deaths", 0)
                    assists = stats.get("assists", 0)
                    score = stats.get("score", 0)
                    headshots = stats.get("headshots", 0)
                    bodyshots = stats.get("bodyshots", 0)
                    legshots = stats.get("legshots", 0)
                    total_shots = headshots + bodyshots + legshots
                    hs_pct = (headshots / total_shots) * 100 if total_shots > 0 else 0
                    adr = player_data.get("damage_made", 0) // max(meta.get("rounds_played", 1), 1)

                    team = player_data["team"].lower()
                    won = match.get("teams", {}).get(team, {}).get("has_won", False)
                    result = "승리" if won else "패배"

                    match_id = meta.get("matchid", "")
                    map_name = meta.get("map", "알 수 없음")
                    mode = meta.get("mode", "알 수 없음")
                    rounds = meta.get("rounds_played", "?")
                    tier = player_data.get("currenttier_patched", "?")
                    agent = player_data.get("character", "?")
                    date = meta.get("game_start_patched", "알 수 없음")

                    embed.add_field(
                        name=f"🗺 {map_name} • {agent} • {mode} • {result}",
                        value=(
                            f"• **KDA:** `{kills}/{deaths}/{assists}` | **헤드샷률:** `{hs_pct:.1f}%`\n"
                            f"• **ADR:** `{adr}` | **점수:** `{score}` | **티어:** `{tier}`\n"
                            f"• **라운드:** `{rounds}`\n"
                            f"• **날짜:** {date}\n"
                            f"[🔗 경기 보기](https://tracker.gg/valorant/match/{match_id})"
                        ),
                        inline=False
                    )
                    field_count += 1
                except Exception as e:
                    await log_to_channel(self.bot, f"❌ [최근경쟁] 경기 파싱 오류: {e}")
                    continue

            if not embed.fields:
                await interaction.followup.send("❌ 경기 데이터를 찾지 못했습니다.", ephemeral=True)
                await log_to_channel(self.bot, f"⚠️ [최근경쟁] 경기 데이터 없음: {riot_name}#{riot_tag}")
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
                await log_to_channel(self.bot, f"✅ [최근경쟁] {riot_name}#{riot_tag} – 최근 5경기 조회 성공 ({field_count}개 경기)")

        except Exception as e:
            await log_to_channel(self.bot, f"❌ [최근경쟁] 오류: {user.id} – {e}")
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

    # ── Slash: show single player’s MMR details ──
    @app_commands.command(
        name="mmr",
        description="본인의 최종 MMR을 보여줍니다."
    )
    @app_commands.describe(member="확인할 유저")
    @app_commands.check(is_admin)
    async def slash_mmr(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        try:
            await interaction.response.defer(ephemeral=True)
            user = member or interaction.user
            async with self.bot.db.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT * FROM players WHERE discord_id = $1",
                    str(user.id)
                )
            if not row:
                await interaction.followup.send(
                    "❌ 라이엇 계정이 연동되어 있지 않습니다. `/연동` 명령어를 먼저 사용해 주세요.",
                    ephemeral=True
                )
                await log_to_channel(self.bot, f"⚠️ MMR 요청 실패: 계정 미연동 – {user.display_name} ({user.id})")
                return

            embed = discord.Embed(
                title=f"{row['riot_name']}#{row['riot_tag']} – MMR 상세 정보",
                color=discord.Color.blurple()
            )
            embed.add_field(name="공개 MMR (랭크)", value=row['competitive_mmr'], inline=False)
            embed.add_field(name="숨김 MMR (봇 계산)", value=row['hidden_win_mmr'], inline=False)
            embed.add_field(name="최종(합산) MMR", value=row['visible_mmr'], inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
            await log_to_channel(self.bot, f"✅ MMR 조회: {row['riot_name']}#{row['riot_tag']} (Discord: {user.id})")

        except Exception as e:
            await interaction.followup.send(f"❌ 오류: {e}", ephemeral=True)
            await log_to_channel(self.bot, f"❌ MMR 조회 오류: {interaction.user.display_name} ({interaction.user.id}) – {e}")

    # ── Slash: bulk update all MMRs one by one ──
    @app_commands.command(
        name="mmr업데이트",
        description="서버 모든 유저의 MMR을 순차적으로 업데이트합니다. (10초당 1명, API 제한 방지)"
    )
    @app_commands.check(is_admin)
    async def slash_bulk_update_mmrs(
            self,
            interaction: discord.Interaction,
            region_hint: Optional[str] = "na"
    ):
        await interaction.response.defer(ephemeral=True)

        await log_to_channel(self.bot,
                             f"📢 [mmr업데이트] 대량 업데이트 시작 by {interaction.user.display_name} ({interaction.user.id})")

        try:
            async with self.bot.db.acquire() as conn:
                players = await conn.fetch("SELECT * FROM players")
            total = len(players)
            count = 0

            # ⬇️ Send the initial progress message
            # Initial progress message (sent once)
            progress_msg = await interaction.followup.send(
                f"🔄 진행상황: 0/{total}명 완료.", ephemeral=True
            )

            for player in players:
                try:
                    async with self.bot.db.acquire() as conn:
                        await self.update_player_mmrs(conn, player, region_hint)
                    await log_to_channel(
                        self.bot,
                        f"✅ [mmr업데이트] 성공: {player['riot_name']}#{player['riot_tag']} ({count + 1}/{total})"
                    )
                except Exception as e:
                    await log_to_channel(
                        self.bot,
                        f"❌ [mmr업데이트] 실패: {player['riot_name']}#{player['riot_tag']} – {e}"
                    )

                count += 1

                # ✅ Edit the existing progress message
                await progress_msg.edit(content=f"🔄 진행상황: {count}/{total}명 완료.")
                await asyncio.sleep(10)

            await progress_msg.edit(content=f"✅ 모든 MMR 업데이트가 완료되었습니다! (총 {count}명)")
            await log_to_channel(self.bot, f"✅ [mmr업데이트] 대량 업데이트 완료! (총 {count}명)")

            await self.refresh_mmr_leaderboard()

        except Exception as e:
            await interaction.followup.send(f"❌ 오류: {e}", ephemeral=True)
            await log_to_channel(self.bot, f"❌ [mmr업데이트] 전체 오류: {e}")

    # ── Slash: manually view TOP 10 leaderboard (ephemeral) ──
    @app_commands.command(
        name="mmr리더보드",
        description="서버 내 최종 MMR 랭킹 TOP 10을 보여줍니다."
    )
    @app_commands.check(is_admin)
    async def slash_mmr_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer()
        try:
            embed = await self.build_mmr_leaderboard_embed()
            await interaction.followup.send(embed=embed, ephemeral=False)
        except Exception as e:
            await interaction.followup.send(f"❌ 오류: {e}", ephemeral=True)
            await log_to_channel(self.bot, f"❌ MMR 리더보드 오류: {e}")

    @app_commands.command(name="내전추가", description="Tracker.gg 링크에서 최근 커스텀 경기를 수동 저장합니다.")
    @app_commands.describe(link="Tracker.gg 매치 링크")
    @app_commands.check(lambda i: i.user.guild_permissions.administrator)
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
        msg = await chan.send(embed=embed)

        global MMR_LEADERBOARD_MESSAGE_ID
        MMR_LEADERBOARD_MESSAGE_ID = msg.id
        print(f"[MMR] Initial leaderboard posted. Message ID = {MMR_LEADERBOARD_MESSAGE_ID}")

    # ── Build the TOP 10 embed ──
    async def build_mmr_leaderboard_embed(self) -> discord.Embed:
        async with self.bot.db.acquire() as conn:
            rows = await conn.fetch("""
                SELECT discord_id, riot_name, riot_tag, visible_mmr
                FROM players
                ORDER BY visible_mmr DESC
                LIMIT 10
            """)

        embed = discord.Embed(
            title=":trophy: 발로란트 MMR 리더보드 (TOP 10)",
            color=discord.Color.gold(),
            timestamp=datetime.utcnow()
        )

        if not rows:
            embed.description = "아직 등록된 유저가 없습니다."
        else:
            leaderboard = ""
            for i, row in enumerate(rows, 1):
                user_id = int(row['discord_id'])
                mention = f"<@{user_id}>"
                leaderboard += (
                    f"**{i}.** {mention} (`{row['riot_name']}#{row['riot_tag']}`) – **{row['visible_mmr']}**점\n"
                )
            embed.description = leaderboard

        embed.set_footer(text="최종 MMR(공개+숨김+?) 기준 순위입니다.")
        return embed

    # ── Edit the existing leaderboard message or send a new one if not found ──
    async def refresh_mmr_leaderboard(self):
        chan = self.bot.get_channel(int(os.getenv("MMR_CHANNEL_ID", "0")))
        if not chan:
            await log_to_channel(self.bot, f"⚠️ [MMR] Invalid MMR_CHANNEL_ID: {os.getenv('MMR_CHANNEL_ID')}")
            return

        embed = await self.build_mmr_leaderboard_embed()

        try:
            # Always clear previous leaderboard messages!
            await chan.purge(limit=100)
        except Exception as e:
            await log_to_channel(self.bot, f"⚠️ [MMR] Channel purge failed: {e}")

        # Now post the new leaderboard and save the ID
        msg = await chan.send(embed=embed)
        await self.set_leaderboard_message_id(msg.id)
        print(f"[MMR] Leaderboard posted and saved in channel {chan.id} (message {msg.id})")

    # ── Daily loop: update every player in DB once per day ──
    async def run_daily_update(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            now = datetime.now(pytz.timezone("America/Toronto"))
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            wait_seconds = (tomorrow - now).total_seconds()
            await log_to_channel(self.bot, f"⏰ 다음 MMR 업데이트까지 {wait_seconds:.1f}초 ({tomorrow.strftime('%Y-%m-%d %H:%M')} 동부 시간) 대기")
            await asyncio.sleep(wait_seconds)

            try:
                timestamp = datetime.now(pytz.timezone("America/Toronto")).strftime("%Y-%m-%d %H:%M")
                await log_to_channel(self.bot, f"⏬ [SCHEDULER] 일일 MMR 업데이트 실행 중: {timestamp}")

                async with self.bot.db.acquire() as conn:
                    players = await conn.fetch("SELECT * FROM players")

                total = len(players)
                count = 0

                # ── 1) 모든 플레이어 MMR 업데이트 ──
                for player in players:
                    try:
                        async with self.bot.db.acquire() as conn:
                            await self.update_player_mmrs(conn, player, "na")
                        await log_to_channel(self.bot, f"✅ [SCHEDULER] 업데이트 완료: {player['riot_name']}#{player['riot_tag']}")
                    except Exception as e:
                        await log_to_channel(self.bot, f"❌ [SCHEDULER] 업데이트 실패: {player['riot_name']}#{player['riot_tag']}: {e}")
                    count += 1
                    await asyncio.sleep(10)  # throttle

                await log_to_channel(self.bot, f"✅ [SCHEDULER] 일일 MMR 업데이트 완료. 총: {count}명")

                # ── 2) Riot ID 변경 감지 ──
                async with self.bot.db.acquire() as conn:
                    for player in players:
                        old_name = player["riot_name"]
                        old_tag  = player["riot_tag"]
                        puuid    = player["puuid"]

                        # 2.1) name#tag 로 조회해 보기
                        data = await self.henrik_get(f"/valorant/v2/account/{old_name}/{old_tag}")
                        if not data or "data" not in data:
                            # 2.2) 실패했으면 puuid 로 lookup 해서 새로운 name/tag 획득
                            puuid_lookup = await self.henrik_get(f"/valorant/v2/account/by-puuid/{puuid}")
                            if puuid_lookup and "data" in puuid_lookup:
                                new_name = puuid_lookup["data"]["name"]
                                new_tag  = puuid_lookup["data"]["tag"]
                                if new_name != old_name or new_tag != old_tag:
                                    # Riot ID가 바뀐 걸로 판정 → DB에 업데이트
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
                                        f"🔄 Riot ID 변경 감지: {old_name}#{old_tag} → {new_name}#{new_tag}"
                                    )
                                    # 변경이 생겼으니 새 리더보드 푸시
                                    await self.refresh_mmr_leaderboard()

            except Exception as e:
                await log_to_channel(self.bot, f"❌ [SCHEDULER] 일일 MMR 업데이트 실패: {e}")

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
