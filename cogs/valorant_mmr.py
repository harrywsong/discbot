import os
import asyncio
import asyncpg
import json
import pytz
from datetime import timedelta

from datetime import datetime
import urllib.parse
from typing import Optional
from utils import config

import discord
from discord.ext import tasks
from discord.ext import commands
from discord import app_commands
from utils.logger import log_to_channel

import aiohttp

from discord import Interaction

def is_admin(interaction: Interaction):
    return interaction.user.guild_permissions.administrator

HENRIK_API_KEY = os.getenv("HENRIK_API_KEY")
MMR_CHANNEL_ID = int(os.getenv("MMR_CHANNEL_ID", "0"))  # fallback to 0 if not set


REGION_SHARD = {
    'na': 'na',
    'eu': 'eu',
    'kr': 'kr',
    'ap': 'ap',
    'br': 'br',
    'latam': 'latam',
}

CREATE_PLAYERS_SQL = """
CREATE TABLE IF NOT EXISTS players (
  discord_id     TEXT PRIMARY KEY,
  puuid          TEXT UNIQUE NOT NULL,
  riot_name      TEXT NOT NULL,
  riot_tag       TEXT NOT NULL,
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

# ----------------- Main Cog -----------------
class ValorantMMRCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Don't start the loop here!
        # Instead, start in on_ready

    async def cog_load(self):
        # This is the new hook called after cog is loaded!
        self.daily_update_task = asyncio.create_task(self.run_daily_update())

    @tasks.loop(minutes=60)
    async def periodic_mmr_leaderboard(self):
        await self.bot.wait_until_ready()  # Ensure the bot is ready
        try:
            await self.post_mmr_leaderboard()
        except Exception as e:
            await log_to_channel(self.bot, f"MMR 리더보드 자동 게시 오류: {e}")



    async def run_daily_update(self):
        await self.bot.wait_until_ready()
        while not self.bot.is_closed():
            now = datetime.now(pytz.timezone("America/New_York"))
            tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            wait_seconds = (tomorrow - now).total_seconds()
            await log_to_channel(self.bot, f"[SCHEDULER] Next MMR update in {wait_seconds:.1f} seconds at {tomorrow}")

            await asyncio.sleep(wait_seconds)
            try:
                await log_to_channel(self.bot, f"[SCHEDULER] Running daily MMR update at {datetime.now()}")
                async with self.bot.db.acquire() as conn:
                    players = await conn.fetch("SELECT * FROM players")
                total = len(players)
                count = 0
                for player in players:
                    try:
                        async with self.bot.db.acquire() as conn:
                            await self.update_player_mmrs(conn, player, "na")
                        await log_to_channel(self.bot, f"[SCHEDULER] Updated MMR for {player['riot_name']}#{player['riot_tag']}")
                    except Exception as e:
                        await log_to_channel(self.bot, f"[SCHEDULER] Failed for {player['riot_name']}#{player['riot_tag']}: {e}")
                    count += 1
                    await asyncio.sleep(10)
                await log_to_channel(self.bot, f"[SCHEDULER] ✅ Daily MMR update done. Total: {count}")
            except Exception as e:
                await log_to_channel(self.bot, f"[SCHEDULER] ❌ Daily MMR update failed: {e}")

    async def henrik_get(self, endpoint: str) -> Optional[dict]:
        base = "https://api.henrikdev.xyz"
        headers = {"Authorization": HENRIK_API_KEY}
        async with aiohttp.ClientSession() as session:
            async with session.get(base + endpoint, headers=headers) as resp:
                await log_to_channel(self.bot, f"[Henrik] {resp.status} {endpoint}")
                if resp.status == 200:
                    return await resp.json()
                else:
                    await log_to_channel(self.bot, f"[Henrik] Request failed: {resp.status} {endpoint}")
                return None

    # ---------- Helper: Tier → Score ----------
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

    # ---------- Helper: Calculate Hidden MMR ----------
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

    # ---------- Helper: Update MMR for One Player ----------
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
                UPDATE players SET
                    competitive_mmr = $1,
                    hidden_win_mmr = $2,
                    hidden_win_rd = $3,
                    hidden_win_vol = $4,
                    hidden_enc_mmr = $5,
                    visible_mmr = $6,
                    last_active = NOW()
                WHERE puuid = $7
            """, competitive_mmr, hidden_win_mmr, hidden_win_rd, hidden_win_vol, hidden_enc_mmr, visible_mmr, puuid)

            await log_to_channel(self.bot, f"업데이트 완료: {riot_name}#{riot_tag}: 랭크={competitive_mmr}, 히든={hidden_win_mmr}, 최종={visible_mmr}")
        except Exception as e:
            await log_to_channel(self.bot, f"MMR 업데이트 실패: {riot_name}#{riot_tag}: {e}")

    # ------------------- Slash Commands -------------------

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
                await log_to_channel(self.bot, f"계정 연동 실패: {riot_name} (not found)")
                return

            puuid = acc_data["data"]["puuid"]
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
                        last_active = NOW()
                    """,
                    str(interaction.user.id),
                    puuid,
                    name,
                    tag
                )

            await interaction.followup.send(
                f"✅ `{riot_name}` 계정이 성공적으로 연동되었습니다!",
                ephemeral=True
            )
            await log_to_channel(self.bot, f"계정 연동 성공: {riot_name}#{tag} (PUUID: {puuid}, Discord: {interaction.user.id})")

        except Exception as e:
            await interaction.followup.send(f"❌ 예기치 못한 오류: {str(e)}", ephemeral=True)
            await log_to_channel(self.bot,
                                 f"계정 연동 오류: {riot_name} - {e}")

    @app_commands.command(name="티어", description="본인의 발로란트 경쟁 랭크와 RR점수를 보여줍니다.")
    @app_commands.describe(member="확인할 유저", region_hint="(선택) 지역 (na/eu/kr/ap/br/latam)")
    async def slash_rank(self, interaction: discord.Interaction,
                         region_hint: Optional[str] = "na",
                         member: Optional[discord.Member] = None):
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
                    "❌ 라이엇 계정이 연동되어 있지 않습니다. `/계정연동` 명령어를 먼저 사용해 주세요.", ephemeral=True
                )
                await log_to_channel(self.bot, f"[티어] 계정 미연동: {user.id} ({user.display_name})")
                return

            riot_name = row["riot_name"]
            riot_tag = row["riot_tag"]
            endpoint = f"/valorant/v1/mmr/{region_hint}/{riot_name}/{riot_tag}"
            data = await self.henrik_get(endpoint)

            if not data or "data" not in data:
                await interaction.followup.send(
                    "❌ 티어 정보를 불러올 수 없습니다. 라이엇 ID를 다시 확인해 주세요.", ephemeral=True
                )
                await log_to_channel(self.bot,
                                     f"[티어] 티어 정보 불러오기 실패: {riot_name}#{riot_tag}")
                return

            mmr = data["data"]
            current = f"{mmr['currenttierpatched']} ({mmr['ranking_in_tier']} RR)"
            embed = discord.Embed(
                title=f"{riot_name}#{riot_tag} – 현재 티어",
                color=0xFF4655,
                timestamp=datetime.utcnow()
            )
            embed.add_field(name="현재 티어", value=current, inline=False)
            if mmr.get("images", {}).get("small"):
                embed.set_thumbnail(url=mmr["images"]["small"])
            await interaction.followup.send(embed=embed, ephemeral=True)
            await log_to_channel(self.bot, f"[티어] {riot_name}#{riot_tag} - {current} ({user.id})")
        except Exception as e:
            await log_to_channel(self.bot, f"[티어] 오류: {user.id} - {e}")
            await interaction.followup.send(f"❌ 오류: {e}", ephemeral=True)

    @app_commands.command(name="최근경쟁", description="최근 경쟁전 5경기를 보여줍니다.")
    @app_commands.describe(member="확인할 유저", region_hint="(선택) 지역")
    async def slash_recent_matches(self, interaction: discord.Interaction,
                                   region_hint: Optional[str] = "na",
                                   member: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        user = member or interaction.user
        try:
            async with self.bot.db.acquire() as conn:
                row = await conn.fetchrow("SELECT riot_name, riot_tag, puuid FROM players WHERE discord_id = $1",
                                          str(user.id))
            if not row:
                await interaction.followup.send("❌ 라이엇 계정이 연동되어 있지 않습니다. `/계정연동` 명령어를 먼저 사용해 주세요.", ephemeral=True)
                await log_to_channel(self.bot, f"[최근경쟁] 계정 미연동: {user.id} ({user.display_name})")
                return

            riot_name = row["riot_name"]
            riot_tag = row["riot_tag"]
            puuid = row["puuid"]

            match_endpoint = f"/valorant/v3/by-puuid/matches/{region_hint}/{puuid}"
            match_data = await self.henrik_get(match_endpoint)
            if not match_data or match_data.get("status") != 200 or not match_data.get("data"):
                await interaction.followup.send("❌ 최근 경기 정보를 불러올 수 없습니다.", ephemeral=True)
                await log_to_channel(self.bot, f"[최근경쟁] 경기 정보 불러오기 실패: {riot_name}#{riot_tag}")
                return

            matches = match_data["data"][:5]
            embed = discord.Embed(
                title=f"📊 {riot_name}#{riot_tag} – 최근 경쟁전 5경기",
                description="최근 내전 경기 5개를 보여줍니다",
                color=discord.Color.brand_red()
            )
            embed.set_footer(text="https://www.instagram.com/dngur.thd/")
            embed.timestamp = datetime.utcnow()

            first_match = matches[0]
            players = first_match.get("players", {}).get("all_players", [])
            player_data = next((p for p in players if p.get("puuid") == puuid), None)
            if player_data:
                card_icon = player_data.get("assets", {}).get("card", {}).get("small")
                if card_icon:
                    embed.set_thumbnail(url=card_icon)

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
                    adr = player_data.get("damage_made", 0) // meta.get("rounds_played", 1)

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
                    await log_to_channel(self.bot, f"[최근경쟁] 경기 파싱 오류: {e}")
                    continue

            if not embed.fields:
                await interaction.followup.send("❌ 경기 데이터를 찾지 못했습니다.", ephemeral=True)
                await log_to_channel(self.bot, f"[최근경쟁] 경기 데이터 없음: {riot_name}#{riot_tag}")
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
                await log_to_channel(self.bot, f"[최근경쟁] {riot_name}#{riot_tag} - 최근 5경기 조회 ({user.id}) 성공 ({field_count}개 경기)")
        except Exception as e:
            await log_to_channel(self.bot, f"[최근경쟁] 오류: {user.id} - {e}")
            await interaction.followup.send(f"❌ 오류: {e}", ephemeral=True)

    @app_commands.command(name="최근내전", description="최근 내전 경기 5개를 보여줍니다.")
    @app_commands.describe(member="확인할 유저", region_hint="(선택) 지역")
    async def slash_custom_matches(self, interaction: discord.Interaction,
                                   region_hint: Optional[str] = "na",
                                   member: Optional[discord.Member] = None):
        await interaction.response.defer(ephemeral=True)
        user = member or interaction.user
        try:
            async with self.bot.db.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT riot_name, riot_tag, puuid FROM players WHERE discord_id = $1",
                    str(user.id))
            if not row:
                await interaction.followup.send("❌ 라이엇 계정이 연동되어 있지 않습니다. `/계정연동` 명령어를 먼저 사용해 주세요.", ephemeral=True)
                await log_to_channel(self.bot, f"[최근내전] 계정 미연동: {user.id} ({user.display_name})")
                return

            riot_name = row["riot_name"]
            riot_tag = row["riot_tag"]
            puuid = row["puuid"]

            endpoint = f"/valorant/v3/by-puuid/matches/{region_hint}/{puuid}?filter=custom"
            data = await self.henrik_get(endpoint)
            if not data or data.get("status") != 200 or not data.get("data"):
                await interaction.followup.send("❌ 최근 커스텀 경기 정보를 불러올 수 없습니다.", ephemeral=True)
                await log_to_channel(self.bot, f"[최근내전] 커스텀 정보 불러오기 실패: {riot_name}#{riot_tag}")
                return

            matches = data["data"][:5]
            if not matches:
                await interaction.followup.send("⚠️ 최근 커스텀 경기가 없습니다.", ephemeral=True)
                await log_to_channel(self.bot, f"[최근내전] 커스텀 없음: {riot_name}#{riot_tag}")
                return

            embed = discord.Embed(
                title=f"🎮 {riot_name}#{riot_tag} – 최근 내전 5경기",
                description="최근 내전 경기 5개를 보여줍니다",
                color=discord.Color.dark_gold()
            )
            embed.set_footer(text="https://www.instagram.com/dngur.thd/")
            embed.timestamp = discord.utils.utcnow()

            first_match = matches[0]
            players = first_match.get("players", {}).get("all_players", [])
            player_data = next((p for p in players if p.get("puuid") == puuid), None)
            if player_data:
                card_icon = player_data.get("assets", {}).get("card", {}).get("small")
                if card_icon:
                    embed.set_thumbnail(url=card_icon)

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
                    adr = player_data.get("damage_made", 0) // meta.get("rounds_played", 1)

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
                    await log_to_channel(self.bot, f"[최근내전] 커스텀 경기 파싱 오류: {e}")
                    continue

            if not embed.fields:
                await interaction.followup.send("❌ 커스텀 경기 데이터를 찾지 못했습니다.", ephemeral=True)
                await log_to_channel(self.bot, f"[최근내전] 커스텀 데이터 없음: {riot_name}#{riot_tag}")
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
                await log_to_channel(self.bot, f"[최근내전] {riot_name}#{riot_tag} - 최근 5커스텀 조회 ({user.id}) 성공 ({field_count}개 경기)")
        except Exception as e:
            await log_to_channel(self.bot, f"[최근내전] 오류: {user.id} - {e}")
            await interaction.followup.send(f"❌ 오류: {e}", ephemeral=True)

    @app_commands.command(name="mmr", description="본인의 최종 MMR을 보여줍니다.")
    @app_commands.describe(member="확인할 유저")
    @app_commands.check(is_admin)
    async def slash_mmr(self, interaction: discord.Interaction, member: Optional[discord.Member] = None):
        try:
            await interaction.response.defer(ephemeral=True)
            user = member or interaction.user
            async with self.bot.db.acquire() as conn:
                row = await conn.fetchrow("SELECT * FROM players WHERE discord_id = $1", str(user.id))
            if not row:
                await interaction.followup.send(
                    "❌ 라이엇 계정이 연동되어 있지 않습니다. `/계정연동` 명령어를 먼저 사용해 주세요.",
                    ephemeral=True
                )
                await log_to_channel(self.bot, f"MMR 요청 실패: {user.id} - 라이엇 계정 미연동")
                return

            embed = discord.Embed(
                title=f"{row['riot_name']}#{row['riot_tag']} – MMR 상세 정보",
                color=discord.Color.blurple()
            )
            embed.add_field(name="공개 MMR (랭크)", value=row['competitive_mmr'], inline=False)
            embed.add_field(name="숨김 MMR (봇 계산)", value=row['hidden_win_mmr'], inline=False)
            embed.add_field(name="최종(합산) MMR", value=row['visible_mmr'], inline=False)
            await interaction.followup.send(embed=embed, ephemeral=True)
            await log_to_channel(self.bot, f"MMR 조회: {row['riot_name']}#{row['riot_tag']} (Discord: {user.id})")
        except Exception as e:
            await interaction.followup.send(f"❌ 오류: {e}", ephemeral=True)
            await log_to_channel(self.bot, f"MMR 조회 오류: {interaction.user.id} - {e}")

    @app_commands.command(
        name="mmr업데이트",
        description="서버 모든 유저의 MMR을 순차적으로 업데이트합니다. (1분당 1명, API 제한 방지)"
    )
    @app_commands.check(is_admin)
    async def slash_bulk_update_mmrs(self, interaction: discord.Interaction, region_hint: Optional[str] = "na"):
        await interaction.response.send_message(
            "⏳ 모든 유저의 MMR을 1분에 1명씩 업데이트합니다...", ephemeral=True
        )
        await log_to_channel(self.bot, f"[mmr업데이트] 대량 업데이트 시작 by {interaction.user.id} ({interaction.user.display_name})")
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
                        f"[mmr업데이트] 성공: {player['riot_name']}#{player['riot_tag']} ({count + 1}/{total})"
                    )
                except Exception as e:
                    await log_to_channel(self.bot,
                        f"[mmr업데이트] 실패: {player['riot_name']}#{player['riot_tag']} - {e}"
                    )
                count += 1
                await interaction.followup.send(f"진행상황: {count}/{total}명 완료.", ephemeral=True)
                await asyncio.sleep(10)
            await interaction.followup.send(
                f"✅ 모든 MMR 업데이트가 완료되었습니다! (총 {count}명)", ephemeral=True
            )
            await log_to_channel(self.bot, f"[mmr업데이트] 대량 업데이트 완료! (총 {count}명)")
        except Exception as e:
            await interaction.followup.send(f"❌ 오류: {e}", ephemeral=True)
            await log_to_channel(self.bot, f"[mmr업데이트] 전체 오류: {e}")

    # ---------- Optional: On Ready Sync ----------
    @commands.Cog.listener()
    async def on_ready(self):
        if not getattr(self, "_synced", False):
            await self.bot.tree.sync()
            print("✅ 슬래시 명령어 동기화 완료 (global)")
            self._synced = True

        # Start the periodic leaderboard if not started
        if not self.periodic_mmr_leaderboard.is_running():
            self.periodic_mmr_leaderboard.start()

    @app_commands.command(
        name="mmr리더보드",
        description="서버 내 최종 MMR 랭킹 TOP 10을 보여줍니다."
    )
    @app_commands.check(is_admin)
    async def slash_mmr_leaderboard(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            async with self.bot.db.acquire() as conn:
                rows = await conn.fetch("""
                                        SELECT riot_name, riot_tag, visible_mmr
                                        FROM players
                                        ORDER BY visible_mmr DESC LIMIT 10
                                        """)
            if not rows:
                await interaction.followup.send("아직 등록된 유저가 없습니다.", ephemeral=True)
                return

            embed = discord.Embed(
                title=":trophy: 발로란트 MMR 리더보드 (TOP 10)",
                color=discord.Color.gold(),
                timestamp=datetime.utcnow()
            )
            leaderboard = ""
            for i, row in enumerate(rows, 1):
                leaderboard += (
                    f"**{i}.** `{row['riot_name']}#{row['riot_tag']}` - **{row['visible_mmr']}**점\n"
                )
            embed.description = leaderboard
            embed.set_footer(text="최종 MMR(공개+숨김+?) 기준 순위입니다.")

            await interaction.followup.send(embed=embed, ephemeral=False)
        except Exception as e:
            await interaction.followup.send(f"❌ 오류: {e}", ephemeral=True)
            await log_to_channel(self.bot, f"MMR 리더보드 오류: {e}")

    async def post_mmr_leaderboard(self):
        global MMR_LEADERBOARD_MESSAGE_ID
        print(f"Using channel ID: {MMR_CHANNEL_ID}")
        chan = self.bot.get_channel(MMR_CHANNEL_ID)
        if not chan:
            await log_to_channel(self.bot, f"[MMR] Invalid MMR_CHANNEL_ID: {MMR_CHANNEL_ID}")
            print(f"[MMR] Invalid MMR_CHANNEL_ID: {MMR_CHANNEL_ID}")
            return

        # Debug: Print channel info
        print(f"[MMR] Got channel: {chan} (type: {type(chan)})")

        # Clear all previous messages in the channel
        try:
            await chan.purge(limit=100)
            print(f"[MMR] Purged messages in {chan.name}")
        except Exception as e:
            await log_to_channel(self.bot, f"❌ MMR 채널 비우기 실패: {e}")
            print(f"❌ MMR 채널 비우기 실패: {e}")

        # Create the embed leaderboard
        embed = await self.build_mmr_leaderboard_embed()
        msg = await chan.send(embed=embed)
        print(f"[MMR] Sent leaderboard embed to {chan.name}")
        MMR_LEADERBOARD_MESSAGE_ID = msg.id

    async def build_mmr_leaderboard_embed(self) -> discord.Embed:
        async with self.bot.db.acquire() as conn:
            rows = await conn.fetch("""
                                    SELECT discord_id, riot_name, riot_tag, visible_mmr
                                    FROM players
                                    ORDER BY visible_mmr DESC LIMIT 10
                                    """)
        embed = discord.Embed(
            title=":trophy: 발로란트 MMR 리더보드 (TOP 10)",
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
                # Include Riot name for clarity
                leaderboard += (
                    f"**{i}.** {mention} (`{row['riot_name']}#{row['riot_tag']}`) - **{row['visible_mmr']}**점\n"
                )
            embed.description = leaderboard
        embed.set_footer(text="최종 MMR(공개+숨김+?) 기준 순위입니다.")
        return embed

    async def refresh_mmr_leaderboard(self):
        global MMR_LEADERBOARD_MESSAGE_ID
        chan = self.bot.get_channel(MMR_CHANNEL_ID)
        if not chan:
            return
        embed = await self.build_mmr_leaderboard_embed()
        try:
            msg = await chan.fetch_message(MMR_LEADERBOARD_MESSAGE_ID)
            await msg.edit(embed=embed)
        except discord.NotFound:
            sent = await chan.send(embed=embed)
            MMR_LEADERBOARD_MESSAGE_ID = sent.id


# ------------- Setup Function -------------
async def setup(bot: commands.Bot):
    if not hasattr(bot, "db"):
        DATABASE_DSN = os.getenv("DATABASE_URL")
        bot.db = await asyncpg.create_pool(DATABASE_DSN)

    async with bot.db.acquire() as conn:
        await conn.execute(CREATE_PLAYERS_SQL)
        await conn.execute(CREATE_ANALYZED_SQL)
        await log_to_channel(bot, "✅ ValorantMMRCog: 데이터베이스 테이블 생성 완료.")

    cog = ValorantMMRCog(bot)
    await bot.add_cog(cog)