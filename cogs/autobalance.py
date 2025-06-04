# cogs/autobalance.py
import os
import re
from typing import Optional, Tuple, List

import discord
from discord import app_commands, Interaction
from discord.ext import commands

import aiohttp
import gspread
from oauth2client.service_account import ServiceAccountCredentials

from utils.logger import log_to_channel  # import logger

TEAM_A_VC_ID = 1333666313066905640
TEAM_B_VC_ID = 1333666377743073372

# ─── Helper: Team Balancing ────────────────────────────────────────────────
def balance_teams(
    ranks: dict[discord.Member, str]
) -> Tuple[List[discord.Member], List[discord.Member]]:
    def rank_value(t: str) -> int:
        # t 예: "Gold 3", "Platinum 2" 등에서 숫자만 추출
        m = re.search(r"(\d+)$", t)
        return int(m.group(1)) if m else 0

    items = sorted(ranks.items(), key=lambda kv: rank_value(kv[1]), reverse=True)
    team_a, team_b = [], []
    sum_a = sum_b = 0
    for member, tier_str in items:
        val = rank_value(tier_str)
        if sum_a <= sum_b:
            team_a.append(member)
            sum_a += val
        else:
            team_b.append(member)
            sum_b += val
    return team_a, team_b

class MoveTeamsView(discord.ui.View):
    def __init__(self, team_a_members, team_b_members):
        super().__init__(timeout=None)
        self.team_a_members = team_a_members
        self.team_b_members = team_b_members

    @discord.ui.button(label="A팀 보이스로 이동", style=discord.ButtonStyle.primary)
    async def move_team_a(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.get_channel(TEAM_A_VC_ID)
        moved = []
        for m in self.team_a_members:
            if hasattr(m, "id"):
                try:
                    await m.move_to(vc)
                    moved.append(m.mention)
                except Exception:
                    pass
        msg = "A팀 이동 완료:\n" + "\n".join(moved) if moved else "이동할 인원이 없습니다."
        await interaction.response.send_message(msg, ephemeral=True)

    @discord.ui.button(label="B팀 보이스로 이동", style=discord.ButtonStyle.secondary)
    async def move_team_b(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.get_channel(TEAM_B_VC_ID)
        moved = []
        for m in self.team_b_members:
            if hasattr(m, "id"):
                try:
                    await m.move_to(vc)
                    moved.append(m.mention)
                except Exception:
                    pass
        msg = "B팀 이동 완료:\n" + "\n".join(moved) if moved else "이동할 인원이 없습니다."
        await interaction.response.send_message(msg, ephemeral=True)


class AutoBalanceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # ── (Optional) Internal helper: fetch tier from your webapp (if used) ──
        self.webapp_url = os.getenv("VALO_WEBAPP_URL")
        if not self.webapp_url:
            raise RuntimeError("VALO_WEBAPP_URL not set in environment")

        # ── Google Sheets setup ──
        creds_path = os.getenv("GOOGLE_CREDS_JSON")
        if not creds_path or not os.path.isfile(creds_path):
            raise RuntimeError(f"Invalid GOOGLE_CREDS_JSON: {creds_path!r}")

        sheet_id = os.getenv("VALO_SHEET_ID")
        if not sheet_id:
            raise RuntimeError("VALO_SHEET_ID not set")

        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scopes)
        self.gc = gspread.authorize(creds)
        try:
            self.ws = self.gc.open_by_key(sheet_id).get_worksheet(0)
        except Exception as e:
            raise RuntimeError(f"Failed to open sheet: {e}")

    def find_riot_info(self, member: discord.Member) -> Optional[Tuple[str, str]]:
        """
        구글 시트에 들어 있는 Riot Name/Tag를 (Discord Member.name) 기반으로 찾음.
        시트에 “Discord 닉네임”이 2열, “Riot Name”이 3열, “Tag”가 4열에 들어 있다고 가정.
        """
        rows = self.ws.get_all_values()
        for row in rows[1:]:
            # row[1]에 Discord 닉네임이 있고, row[2], row[3]에 riot_name, riot_tag가 있다고 가정
            if len(row) >= 2 and row[1] == member.name:
                rn = row[2].strip() if len(row) >= 3 else ""
                rt = row[3].strip() if len(row) >= 4 else ""
                if rn and rt:
                    return rn, rt
                break
        return None

    async def call_get_tier(self, riot_name: str, riot_tag: str) -> str:
        """
        (선택) 자체적으로 운영하는 WebApp에서 티어를 받아올 때 사용하는 예시.
        """
        params = {"riotName": riot_name, "riotTag": riot_tag, "region": "na"}
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(self.webapp_url, params=params) as resp:
                try:
                    data = await resp.json()
                    return data.get("tier", "").strip()
                except:
                    text = await resp.text()
                    return text.strip()

    # ── Slash command “오토밸런스” ─────────────────────────────────────────────
    @app_commands.command(
        name="오토밸런스",
        description="내전 참가자 또는 직접 입력한 명단을 기반으로 팀 밸런싱을 자동으로 해줍니다."
    )
    @app_commands.describe(
        m1="1번 참가자 (@멘션)", m2="2번 참가자", m3="3번 참가자", m4="4번 참가자", m5="5번 참가자",
        m6="6번 참가자", m7="7번 참가자", m8="8번 참가자", m9="9번 참가자", m10="10번 참가자"
    )
    @app_commands.check(lambda inter: inter.user.guild_permissions.administrator)
    async def slash_autobalance(
        self,
        interaction: Interaction,
        m1: Optional[discord.Member] = None,
        m2: Optional[discord.Member] = None,
        m3: Optional[discord.Member] = None,
        m4: Optional[discord.Member] = None,
        m5: Optional[discord.Member] = None,
        m6: Optional[discord.Member] = None,
        m7: Optional[discord.Member] = None,
        m8: Optional[discord.Member] = None,
        m9: Optional[discord.Member] = None,
        m10: Optional[discord.Member] = None
    ):
        await interaction.response.defer(ephemeral=True)

        user_display = f"{interaction.user.display_name} 님"

        # 1) “m1…m10” 슬롯에 멘션이 들어왔는지 확인
        input_members = [m for m in (m1, m2, m3, m4, m5, m6, m7, m8, m9, m10) if m]
        player_infos = []
        riotid_to_member: dict[str, discord.Member] = {}

        # If user provided at least one slot, treat as manual input
        if input_members:
            async with self.bot.db.acquire() as conn:
                for member in input_members:
                    row = await conn.fetchrow(
                        "SELECT visible_mmr, riot_name, riot_tag FROM players WHERE discord_id = $1",
                        str(member.id)
                    )
                    if not row:
                        await interaction.followup.send(
                            f"❌ {member.display_name} 님: DB에서 정보를 찾을 수 없습니다.",
                            ephemeral=True
                        )
                        await log_to_channel(
                            self.bot,
                            f"❌ [오토밸런스] {member.display_name} 님 DB 정보 없음"
                        )
                        return

                    riotid = f"{row['riot_name']}#{row['riot_tag']}"
                    mmr = row["visible_mmr"]
                    player_infos.append({"riotid": riotid, "mmr": mmr})
                    riotid_to_member[riotid] = member

        # 2) If no manual input, fetch participants from active custom game
        else:
            current_custom_game = getattr(self.bot, "current_custom_game", None)
            if not current_custom_game or not hasattr(current_custom_game, "participants"):
                await interaction.followup.send(
                    "❌ 활성화된 내전 참가자를 찾을 수 없습니다.",
                    ephemeral=True
                )
                await log_to_channel(
                    self.bot,
                    f"❌ [오토밸런스] {user_display} 활성 내전 없음"
                )
                return

            participants = current_custom_game.participants
            if len(participants) != 10:
                await interaction.followup.send(
                    "❌ 참가자 10명이 필요합니다.",
                    ephemeral=True
                )
                await log_to_channel(
                    self.bot,
                    f"❌ [오토밸런스] {user_display} 참가자 부족"
                )
                return

            async with self.bot.db.acquire() as conn:
                for user in participants:
                    # FakeUser 판별: Member 객체가 아니면 MMR=1으로 간주
                    if hasattr(user, "display_name") and not hasattr(user, "id"):
                        player_infos.append({"riotid": user.display_name, "mmr": 1})
                        continue

                    # 실제 Discord.Member 객체 → DB에서 MMR 조회
                    row = await conn.fetchrow(
                        "SELECT visible_mmr, riot_name, riot_tag FROM players WHERE discord_id = $1",
                        str(user.id)
                    )
                    if not row:
                        await interaction.followup.send(
                            f"❌ {user.display_name} 님: DB에서 정보를 찾을 수 없습니다.",
                            ephemeral=True
                        )
                        await log_to_channel(
                            self.bot,
                            f"❌ [오토밸런스] {user.display_name} 님 DB 정보 없음"
                        )
                        return

                    riotid = f"{row['riot_name']}#{row['riot_tag']}"
                    mmr = row["visible_mmr"]
                    player_infos.append({"riotid": riotid, "mmr": mmr})
                    riotid_to_member[riotid] = user

        # 3) Ensure player_infos is not empty
        if not player_infos:
            await interaction.followup.send(
                "❌ 유효한 참가자(직접 입력 혹은 custom_game)가 없습니다.",
                ephemeral=True
            )
            await log_to_channel(
                self.bot,
                f"❌ [오토밸런스] {user_display} 유효한 참가자 없음"
            )
            return

        # 4) Find best balance using combinations
        from itertools import combinations

        best_diff = float("inf")
        best_team_a = []
        best_team_b = []
        for team_a in combinations(player_infos, 5):
            team_b = [p for p in player_infos if p not in team_a]
            diff = abs(sum(p["mmr"] for p in team_a) - sum(p["mmr"] for p in team_b))
            if diff < best_diff:
                best_diff = diff
                best_team_a = list(team_a)
                best_team_b = list(team_b)

        def pretty(team):
            return "\n".join([f"{p['riotid']} (MMR: {p['mmr']})" for p in team])

        # 5) Map riotid back to real Member objects for moving
        team_a_members = [
            riotid_to_member[p["riotid"]]
            for p in best_team_a
            if p["riotid"] in riotid_to_member
        ]
        team_b_members = [
            riotid_to_member[p["riotid"]]
            for p in best_team_b
            if p["riotid"] in riotid_to_member
        ]

        embed = discord.Embed(
            title="✅ 자동 팀 밸런스 결과",
            description=(
                f"최종 팀 차이: **{best_diff}**\n\n"
                f"**팀 A:**\n{pretty(best_team_a)}\n\n"
                f"**팀 B:**\n{pretty(best_team_b)}"
            ),
            color=discord.Color.green()
        )
        view = MoveTeamsView(team_a_members, team_b_members)
        await interaction.followup.send(embed=embed, view=view)

        # 6) Log successful execution
        await log_to_channel(
            self.bot,
            f"✅ [오토밸런스] {user_display}님 팀 밸런스 실행 (차이: {best_diff})"
        )

# ── Cog setup ──────────────────────────────────────────────────────────────
async def setup(bot: commands.Bot):
    await bot.add_cog(AutoBalanceCog(bot))
