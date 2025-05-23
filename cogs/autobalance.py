# cogs/autobalance.py
import os
import re
import asyncio
import aiohttp
import discord
from discord import app_commands, ui, ButtonStyle, Interaction
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from typing import Optional, Tuple, List

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

class MoveTeamsView(ui.View):
    def __init__(self, team_a: List[discord.Member], team_b: List[discord.Member]):
        super().__init__(timeout=None)
        self.team_a = team_a
        self.team_b = team_b

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: ui.Item):
        # signature is (self, interaction, error, item)
        await interaction.response.send_message(
            f"⚠️ 버튼 처리 중 오류: `{error}`", ephemeral=True
        )

    @ui.button(label="A팀 보이스로 이동", style=ButtonStyle.primary, custom_id="move_team_a")
    async def move_team_a(self, interaction: discord.Interaction, button: ui.Button):
        # correct order: (self, interaction, button)
        await interaction.response.defer(ephemeral=True)
        try:
            vc = interaction.guild.get_channel(TEAM_A_VC_ID)
            if not isinstance(vc, discord.VoiceChannel):
                raise RuntimeError("Team A voice channel not found")

            moved = []
            for member in self.team_a:
                try:
                    await member.move_to(vc)
                    moved.append(member.mention)
                except:
                    pass

            text = "A팀으로 이동:\n" + "\n".join(moved) if moved else "A팀에서 이동할 사람이 더이상 없습니다."
        except Exception as e:
            text = f"⚠️ Team A 이동 오류: `{e}`"

        await interaction.followup.send(text, ephemeral=True)

    @ui.button(label="B팀 보이스로 이동", style=ButtonStyle.secondary, custom_id="move_team_b")
    async def move_team_b(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.defer(ephemeral=True)
        try:
            vc = interaction.guild.get_channel(TEAM_B_VC_ID)
            if not isinstance(vc, discord.VoiceChannel):
                raise RuntimeError("Team B voice channel not found")

            moved = []
            for member in self.team_b:
                try:
                    await member.move_to(vc)
                    moved.append(member.mention)
                except:
                    pass

            text = "B팀 보이스로 이동:\n" + "\n".join(moved) if moved else "B팀에서 이동할 사람이 더이상 없습니다."
        except Exception as e:
            text = f"⚠️ Team B 이동 오류: `{e}`"

        await interaction.followup.send(text, ephemeral=True)

class AutoBalanceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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

    @app_commands.command(name="autobalance", description="🔀 2–10명 팀 자동 균형 (멘션으로 지정)")
    @app_commands.describe(
        m1="Player 1 (required)",
        m2="Player 2 (required)",
        m3="Player 3 (optional)",
        m4="Player 4 (optional)",
        m5="Player 5 (optional)",
        m6="Player 6 (optional)",
        m7="Player 7 (optional)",
        m8="Player 8 (optional)",
        m9="Player 9 (optional)",
        m10="Player 10 (optional)"
    )
    async def slash_autobalance(
        self,
        interaction: discord.Interaction,
        m1: discord.Member,
        m2: discord.Member,
        m3: discord.Member = None,
        m4: discord.Member = None,
        m5: discord.Member = None,
        m6: discord.Member = None,
        m7: discord.Member = None,
        m8: discord.Member = None,
        m9: discord.Member = None,
        m10: discord.Member = None
    ):
        members = [m for m in (m1,m2,m3,m4,m5,m6,m7,m8,m9,m10) if m]
        await interaction.response.defer(thinking=True)

        ranks: dict[discord.Member, str] = {}
        not_found: List[str] = []
        for m in members:
            info = await self.bot.loop.run_in_executor(None, self.find_riot_info, m)
            if not info:
                not_found.append(m.mention)
                continue
            riot_name, riot_tag = info
            try:
                tier = await self.call_get_tier(riot_name, riot_tag)
            except Exception as e:
                return await interaction.followup.send(
                    f"⚠️ `{m.display_name}` 조회 오류: `{e}`", ephemeral=True
                )
            if not tier:
                not_found.append(m.mention)
            else:
                ranks[m] = tier

        if not_found:
            return await interaction.followup.send(
                "❌ 다음 유저들의 티어를 가져올 수 없습니다:\n"
                + " ".join(not_found),
                ephemeral=True
            )

        team_a, team_b = balance_teams(ranks)
        embed = discord.Embed(title="🔀 Auto‑Balanced Teams")
        embed.add_field(
            name=f"Team A (총합 {len(team_a)})",
            value="\n".join(f"{member.mention} • {ranks[member]}" for member in team_a),
            inline=True
        )
        embed.add_field(
            name=f"Team B (총합 {len(team_b)})",
            value="\n".join(f"{member.mention} • {ranks[member]}" for member in team_b),
            inline=True
        )

        view = MoveTeamsView(team_a, team_b)
        await interaction.followup.send(embed=embed, view=view)

async def setup(bot: commands.Bot):
    await bot.add_cog(AutoBalanceCog(bot))
