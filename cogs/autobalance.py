# cogs/autobalance.py
import os
import re
from utils.henrik import henrik_get

import aiohttp
import discord
from discord import app_commands, ui, ButtonStyle, Interaction, Member
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from typing import Optional, Tuple, List

from typing import Optional

TEAM_A_VC_ID = 1333666313066905640
TEAM_B_VC_ID = 1333666377743073372

# ─── Helper: Team Balancing ────────────────────────────────────────────────
def balance_teams(
    ranks: dict[discord.Member, str]
) -> Tuple[List[discord.Member], List[discord.Member]]:
    def rank_value(t: str) -> int:
        m = re.search(r"(\d+)$", t)
        return int(m.group(1)) if m else 0

    items = sorted(ranks.items(), key=lambda kv: rank_value(kv[1]), reverse=True)
    team_a, team_b = [], []
    sum_a = sum_b = 0
    for member, tier_str in items:
        val = rank_value(tier_str)
        if sum_a <= sum_b:
            team_a.append(member); sum_a += val
        else:
            team_b.append(member); sum_b += val
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
            if hasattr(m, "id"):  # Only real users, skip fake users
                try:
                    await m.move_to(vc)
                    moved.append(m.mention)
                except Exception:
                    pass
        msg = "A팀 이동 완료:\n" + "\n".join(moved) if moved else "이동할 인원이 없습니다."
        await interaction.response.send_message(msg)

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
        await interaction.response.send_message(msg)


class AutoBalanceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        async def henrik_get(self, endpoint: str) -> Optional[dict]:
            base = "https://api.henrikdev.xyz"
            headers = {"Authorization": os.getenv("HENRIK_API_KEY")}
            timeout = aiohttp.ClientTimeout(total=10)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(base + endpoint, headers=headers) as resp:
                    print(f"[Henrik] {resp.status} {endpoint}")
                    if resp.status == 200:
                        return await resp.json()
                    else:
                        print(f"Henrik API error: {resp.status} - {await resp.text()}")
                        return None

        creds_path = os.getenv("GOOGLE_CREDS_JSON")
        if not creds_path or not os.path.isfile(creds_path):
            raise RuntimeError(f"Invalid GOOGLE_CREDS_JSON: {creds_path!r}")

        sheet_id = os.getenv("VALO_SHEET_ID")
        if not sheet_id:
            raise RuntimeError("VALO_SHEET_ID not set")

        webapp_url = os.getenv("VALO_WEBAPP_URL")
        if not webapp_url:
            raise RuntimeError("VALO_WEBAPP_URL not set")
        self.webapp_url = webapp_url

        scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scopes)
        self.gc = gspread.authorize(creds)
        try:
            self.ws = self.gc.open_by_key(sheet_id).get_worksheet(0)
        except Exception as e:
            raise RuntimeError(f"Failed to open sheet: {e}")

    def find_riot_info(self, member: discord.Member) -> Optional[Tuple[str,str]]:
        rows = self.ws.get_all_values()
        for row in rows[1:]:
            if len(row) >= 2 and row[1] == member.name:
                rn = row[2].strip() if len(row)>=3 else ""
                rt = row[3].strip() if len(row)>=4 else ""
                if rn and rt:
                    return rn, rt
                break
        return None

    async def call_get_tier(self, riot_name: str, riot_tag: str) -> str:
        params = {"riotName": riot_name, "riotTag": riot_tag, "region": "na"}
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as sess:
            async with sess.get(self.webapp_url, params=params) as resp:
                text = await resp.text()
                try:
                    data = await resp.json()
                    return data.get("tier", "").strip()
                except:
                    return text.strip()

    @app_commands.command(
        name="오토밸런스",
        description="내전 참가자 또는 직접 입력한 명단을 기반으로 팀 밸런싱을 자동으로 해줍니다."
    )
    @app_commands.describe(
        m1="1번 참가자 (@멘션)", m2="2번 참가자", m3="3번 참가자", m4="4번 참가자", m5="5번 참가자",
        m6="6번 참가자", m7="7번 참가자", m8="8번 참가자", m9="9번 참가자", m10="10번 참가자"
    )
    @commands.has_permissions(administrator=True)
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
        await interaction.response.defer()

        input_members = [m for m in [m1, m2, m3, m4, m5, m6, m7, m8, m9, m10] if m]
        player_infos = []
        riotid_to_member = {}

        if input_members:
            print(f"[LOG] Manual input: {len(input_members)} members: {[m.display_name for m in input_members]}")
            async with self.bot.db.acquire() as conn:
                for member in input_members:
                    row = await conn.fetchrow(
                        "SELECT visible_mmr, riot_name, riot_tag FROM players WHERE discord_id = $1", str(member.id))
                    if not row:
                        await interaction.followup.send(f"❌ {member.display_name}: DB에서 정보를 찾을 수 없습니다.", ephemeral=True)
                        return
                    riotid = f"{row['riot_name']}#{row['riot_tag']}"
                    player_infos.append({"riotid": riotid, "mmr": row['visible_mmr']})
                    riotid_to_member[riotid] = member
        else:
            current_custom_game = getattr(self.bot, "current_custom_game", None)
            print(f"[LOG] current_custom_game: {current_custom_game}")

            if not current_custom_game or not hasattr(current_custom_game, "participants"):
                await interaction.followup.send("❌ 활성화된 내전 참가자를 찾을 수 없습니다.", ephemeral=True)
                print("[LOG] current_custom_game missing or has no participants")
                return

            print(f"[LOG] Number of participants: {len(current_custom_game.participants)}")
            for idx, p in enumerate(current_custom_game.participants, 1):
                print(f"[LOG] 참가자 {idx}: {p} (ID: {getattr(p, 'id', None)})")

            if len(current_custom_game.participants) != 10:
                await interaction.followup.send("❌ 참가자 10명이 필요합니다.", ephemeral=True)
                return

            async with self.bot.db.acquire() as conn:
                for user in current_custom_game.participants:
                    if hasattr(user, "display_name") and not hasattr(user, "id"):
                        # FakeUser
                        player_infos.append({"riotid": user.display_name, "mmr": 1})
                        continue
                    # Real users
                    row = await conn.fetchrow(
                        "SELECT visible_mmr, riot_name, riot_tag FROM players WHERE discord_id = $1", str(user.id))
                    if not row:
                        await interaction.followup.send(f"❌ {user.display_name} 님: DB에서 정보를 찾을 수 없습니다.", ephemeral=True)
                        return
                    riotid = f"{row['riot_name']}#{row['riot_tag']}"
                    player_infos.append({"riotid": riotid, "mmr": row['visible_mmr']})
                    riotid_to_member[riotid] = user

        from itertools import combinations
        best_diff = float('inf')
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

        # Get real members for buttons
        team_a_members = [riotid_to_member.get(p['riotid']) for p in best_team_a if riotid_to_member.get(p['riotid'])]
        team_b_members = [riotid_to_member.get(p['riotid']) for p in best_team_b if riotid_to_member.get(p['riotid'])]

        embed = discord.Embed(
            title="✅ 자동 팀 밸런스 결과",
            description=f"최종 팀 차이: **{best_diff}**\n\n"
                        f"**팀 A:**\n{pretty(best_team_a)}\n\n**팀 B:**\n{pretty(best_team_b)}",
            color=discord.Color.green()
        )
        view = MoveTeamsView(team_a_members, team_b_members)
        await interaction.followup.send(embed=embed, view=view)

async def setup(bot: commands.Bot):
    await bot.add_cog(AutoBalanceCog(bot))
