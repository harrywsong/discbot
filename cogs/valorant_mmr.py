import os
import asyncio
import asyncpg
import json
import pytz
from datetime import datetime, timedelta
import urllib.parse
from typing import Optional

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
    def calc_hidden(self, matches: list, puuid: str) -> tuple:
        mmr = 1000
        rd = 350
        vol = 0.06
        enc = 1000
        N = 0
        for m in matches:
            players = m.get("players", {}).get("all_players", [])
            player = next((p for p in players if p.get("puuid") == puuid), None)
            if not player:
                continue
            stats = player.get("stats", {})
            kills = stats.get("kills", 0)
            deaths = stats.get("deaths", 1)
            assists = stats.get("assists", 0)
            win = m.get("teams", {}).get(player["team"].lower(), {}).get("has_won", False)
            perf = kills + assists * 0.7 - deaths * 0.5 + (25 if win else -10)
            mmr += perf
            enc += kills * 1.5 + assists * 0.3
            N += 1
        if N == 0:
            return 1000, 350, 0.06, 1000
        return round(mmr / N), rd, vol, round(enc / N)

    # ── Update a single player’s MMR in DB, then refresh leaderboard ──
    async def update_player_mmrs(self, conn, player: dict, region: str):
        puuid = player["puuid"]
        riot_name = player["riot_name"]
        riot_tag = player["riot_tag"]

        try:
            comp = await self.henrik_get(f"/valorant/v1/mmr/{region}/{riot_name}/{riot_tag}")
            comp_data = comp.get("data", {}) if comp and "data" in comp else {}
            tier = comp_data.get("currenttierpatched", "Iron 1")
            rr = comp_data.get("ranking_in_tier", 0)
            competitive_mmr = self.tier_to_score(tier, rr)

            match_data = await self.henrik_get(f"/valorant/v3/by-puuid/matches/{region}/{puuid}")
            matches = match_data.get("data", []) if match_data and "data" in match_data else []

            hidden_win_mmr, hidden_win_rd, hidden_win_vol, hidden_enc_mmr = self.calc_hidden(matches, puuid)
            visible_mmr = round(competitive_mmr * 0.4 + hidden_win_mmr * 0.6)

            await conn.execute("""
                UPDATE players
                SET
                    competitive_mmr = $1,
                    hidden_win_mmr = $2,
                    hidden_win_rd = $3,
                    hidden_win_vol = $4,
                    hidden_enc_mmr = $5,
                    visible_mmr = $6,
                    last_active = NOW()
                WHERE puuid = $7
            """, competitive_mmr, hidden_win_mmr, hidden_win_rd, hidden_win_vol, hidden_enc_mmr, visible_mmr, puuid)

            await log_to_channel(self.bot, f"✅ 업데이트 완료: {riot_name}#{riot_tag} – 랭크={competitive_mmr}, 히든={hidden_win_mmr}, 최종={visible_mmr}")

            # ── LIVE UPDATE: Refresh the leaderboard immediately ──
            await self.refresh_mmr_leaderboard()

        except Exception as e:
            await log_to_channel(self.bot, f"❌ MMR 업데이트 실패: {riot_name}#{riot_tag}: {e}")

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

            await interaction.followup.send(f"✅ `{riot_name}` 계정이 성공적으로 연동되었습니다!", ephemeral=True)
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
        await interaction.response.defer(ephemeral=True)
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
                            f"• **라운드:** `{rounds}` | **날짜:** {date}\n"
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
    @app_commands.command(
        name="최근내전",
        description="최근 내전 경기 5개를 보여줍니다."
    )
    @app_commands.describe(member="확인할 유저", region_hint="(선택) 지역")
    async def slash_custom_matches(
        self,
        interaction: discord.Interaction,
        region_hint: Optional[str] = "na",
        member: Optional[discord.Member] = None
    ):
        await interaction.response.defer(ephemeral=True)
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
                await log_to_channel(self.bot, f"⚠️ [최근내전] 계정 미연동: {user.display_name} ({user.id})")
                return

            riot_name = row["riot_name"]
            riot_tag = row["riot_tag"]
            puuid = row["puuid"]

            endpoint = f"/valorant/v3/by-puuid/matches/{region_hint}/{puuid}?filter=custom"
            data = await self.henrik_get(endpoint)
            if not data or data.get("status") != 200 or not data.get("data"):
                await interaction.followup.send(
                    "❌ 최근 커스텀 경기 정보를 불러올 수 없습니다.",
                    ephemeral=True
                )
                await log_to_channel(self.bot, f"⚠️ [최근내전] 커스텀 정보 불러오기 실패: {riot_name}#{riot_tag}")
                return

            matches = data["data"][:5]
            if not matches:
                await interaction.followup.send("⚠️ 최근 커스텀 경기가 없습니다.", ephemeral=True)
                await log_to_channel(self.bot, f"⚠️ [최근내전] 커스텀 없음: {riot_name}#{riot_tag}")
                return

            embed = discord.Embed(
                title=f"🎮 {riot_name}#{riot_tag} – 최근 내전 5경기",
                description="최근 커스텀 경기 5개를 보여줍니다",
                color=discord.Color.dark_gold()
            )
            embed.set_footer(text="https://www.instagram.com/dngur.thd/")
            embed.timestamp = discord.utils.utcnow()

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
                            f"• **라운드:** `{rounds}` | **날짜:** {date}\n"
                            f"[🔗 경기 보기](https://tracker.gg/valorant/match/{match_id})"
                        ),
                        inline=False
                    )
                    field_count += 1
                except Exception as e:
                    await log_to_channel(self.bot, f"❌ [최근내전] 커스텀 경기 파싱 오류: {e}")
                    continue

            if not embed.fields:
                await interaction.followup.send("❌ 커스텀 경기 데이터를 찾지 못했습니다.", ephemeral=True)
                await log_to_channel(self.bot, f"⚠️ [최근내전] 커스텀 데이터 없음: {riot_name}#{riot_tag}")
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
                await log_to_channel(self.bot, f"✅ [최근내전] {riot_name}#{riot_tag} – 최근 5커스텀 조회 성공 ({field_count}개 경기)")

        except Exception as e:
            await log_to_channel(self.bot, f"❌ [최근내전] 오류: {user.id} – {e}")
            await interaction.followup.send(f"❌ 오류: {e}", ephemeral=True)

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
        description="서버 모든 유저의 MMR을 순차적으로 업데이트합니다. (1분당 1명, API 제한 방지)"
    )
    @app_commands.check(is_admin)
    async def slash_bulk_update_mmrs(
        self,
        interaction: discord.Interaction,
        region_hint: Optional[str] = "na"
    ):
        await interaction.response.send_message(
            "⏳ 모든 유저의 MMR을 1분에 1명씩 업데이트합니다...", ephemeral=True
        )
        await log_to_channel(self.bot, f"📢 [mmr업데이트] 대량 업데이트 시작 by {interaction.user.display_name} ({interaction.user.id})")
        try:
            async with self.bot.db.acquire() as conn:
                players = await conn.fetch("SELECT * FROM players")
            total = len(players)
            count = 0
            for player in players:
                try:
                    async with self.bot.db.acquire() as conn:
                        await self.update_player_mmrs(conn, player, region_hint)
                    await log_to_channel(self.bot,
                        f"✅ [mmr업데이트] 성공: {player['riot_name']}#{player['riot_tag']} ({count + 1}/{total})"
                    )
                except Exception as e:
                    await log_to_channel(self.bot,
                        f"❌ [mmr업데이트] 실패: {player['riot_name']}#{player['riot_tag']} – {e}"
                    )
                count += 1
                await interaction.followup.send(f"🔄 진행상황: {count}/{total}명 완료.", ephemeral=True)
                await asyncio.sleep(10)

            await interaction.followup.send(
                f"🎉 모든 MMR 업데이트가 완료되었습니다! (총 {count}명)", ephemeral=True
            )
            await log_to_channel(self.bot, f"✅ [mmr업데이트] 대량 업데이트 완료! (총 {count}명)")

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
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.bot.db.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT riot_name, riot_tag, visible_mmr
                    FROM players
                    ORDER BY visible_mmr DESC
                    LIMIT 10
                """)
            if not rows:
                await interaction.followup.send("아직 등록된 유저가 없습니다.", ephemeral=True)
                return

            embed = discord.Embed(
                title=":trophy: 발로란트 MMR 리더보드 (TOP 10)",
                color=discord.Color.gold(),
                timestamp=datetime.utcnow()
            )
            leaderboard = ""
            for i, row in enumerate(rows, 1):
                leaderboard += (
                    f"**{i}.** `{row['riot_name']}#{row['riot_tag']}` – **{row['visible_mmr']}**점\n"
                )
            embed.description = leaderboard
            embed.set_footer(text="최종 MMR(공개+숨김+?) 기준 순위입니다.")
            await interaction.followup.send(embed=embed, ephemeral=False)

        except Exception as e:
            await interaction.followup.send(f"❌ 오류: {e}", ephemeral=True)
            await log_to_channel(self.bot, f"❌ MMR 리더보드 오류: {e}")

    # ── Slash: initial post of leaderboard (admin only) ──
    @app_commands.command(
        name="mmr리더보드_게시",
        description="(관리자 전용) MMR 리더보드를 처음으로 채널에 게시합니다."
    )
    @app_commands.check(is_admin)
    async def slash_initial_mmr_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            await self.initial_post_mmr_leaderboard()
            await interaction.followup.send(
                "✅ MMR 리더보드를 게시했습니다. 이후부터는 자동으로 수정됩니다.",
                ephemeral=True
            )
            await log_to_channel(self.bot, "✅ [mmr리더보드_게시] 초기 리더보드 게시 완료")
        except Exception as e:
            await interaction.followup.send(f"❌ 오류: {e}", ephemeral=True)
            await log_to_channel(self.bot, f"❌ [mmr리더보드_게시] 오류: {e}")

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
        global MMR_LEADERBOARD_MESSAGE_ID
        chan = self.bot.get_channel(int(os.getenv("MMR_CHANNEL_ID", "0")))
        if not chan:
            return

        embed = await self.build_mmr_leaderboard_embed()

        # 새로 추가: ID가 None이면, 처음보내기
        if MMR_LEADERBOARD_MESSAGE_ID is None:
            sent = await chan.send(embed=embed)
            MMR_LEADERBOARD_MESSAGE_ID = sent.id
            print(f"[MMR] Initial leaderboard posted from refresh (ID={MMR_LEADERBOARD_MESSAGE_ID})")
            return

        try:
            # 기존 메시지 편집 시도
            msg = await chan.fetch_message(MMR_LEADERBOARD_MESSAGE_ID)
            await msg.edit(embed=embed)
            print(f"[MMR] Edited existing leaderboard (ID={MMR_LEADERBOARD_MESSAGE_ID})")
        except (discord.NotFound, TypeError, discord.HTTPException):
            # 메시지가 없거나 ID가 잘못되었으면 새로 보낸 뒤 ID를 갱신
            sent = await chan.send(embed=embed)
            MMR_LEADERBOARD_MESSAGE_ID = sent.id
            print(f"[MMR] Leaderboard not found; sent new. New ID = {MMR_LEADERBOARD_MESSAGE_ID}")

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

# ── Cog setup function ──
async def setup(bot: commands.Bot):
    if not hasattr(bot, "db"):
        DATABASE_DSN = os.getenv("DATABASE_URL")
        bot.db = await asyncpg.create_pool(DATABASE_DSN)

    async with bot.db.acquire() as conn:
        await conn.execute(CREATE_PLAYERS_SQL)
        await conn.execute(CREATE_ANALYZED_SQL)
        await log_to_channel(bot, "✅ <MMR> 데이터베이스 테이블 생성 완료")

    cog = ValorantMMRCog(bot)
    await bot.add_cog(cog)
