# cogs/autobalance.py
import os
import re
import discord
from discord import app_commands
from discord.ext import commands
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from typing import Optional, Tuple, List

def balance_teams(
    ranks: dict[discord.Member, int]
) -> Tuple[List[discord.Member], List[discord.Member]]:
    team_a: List[discord.Member] = []
    team_b: List[discord.Member] = []
    sum_a = 0
    sum_b = 0
    for member, rank in sorted(ranks.items(), key=lambda kv: kv[1], reverse=True):
        if sum_a <= sum_b:
            team_a.append(member)
            sum_a += rank
        else:
            team_b.append(member)
            sum_b += rank
    return team_a, team_b

class AutoBalanceCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

        # Google Sheets auth
        scope = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
        creds_path = os.getenv("GOOGLE_CREDS_JSON")
        creds = ServiceAccountCredentials.from_json_keyfile_name(creds_path, scope)
        self.gc = gspread.authorize(creds)

        sheet_id = os.getenv("VALO_SHEET_ID")
        self.ws = self.gc.open_by_key(sheet_id).get_worksheet(0)

    def get_tier_for_member(self, member: discord.Member) -> Optional[int]:
        """
        1) Find the row where column A == the member's Discord ID
        2) Read the pre-computed tier from column E
        3) Return it as an int, or None if missing / non-integer
        """
        try:
            cell = self.ws.find(str(member.id), in_column=1)
        except gspread.exceptions.CellNotFound:
            return None

        tier_val = self.ws.cell(cell.row, 5).value or ""
        try:
            return int(tier_val)
        except ValueError:
            return None

    @app_commands.command(name="tier", description="📊 발로란트 티어 룩업")
    @app_commands.describe(
        member="어떤 유저를 찾아보겠습니까? (기본값: 당신 자신)"
    )
    async def slash_tier(
        self,
        interaction: discord.Interaction,
        member: discord.Member = None
    ):
        member = member or interaction.user
        tier = self.get_tier_for_member(member)
        if tier is None:
            await interaction.response.send_message(
                "❌ 해당 유저의 시트 등록 또는 티어 정보가 없습니다.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"🎖️ **{member.display_name}**님의 현재 티어: **{tier}**"
            )

    @app_commands.command(
        name="autobalance",
        description="🔀 Google Sheet 기반으로 팀 자동 균형 조정"
    )
    @app_commands.describe(
        mentions="공백으로 구분된 멘션을 입력하세요: @User1 @User2 @User3 …"
    )
    async def slash_autobalance(
        self,
        interaction: discord.Interaction,
        mentions: str
    ):
        # 멘션에서 ID만 추출
        ids = re.findall(r"<@!?(\d+)>", mentions)
        members = [
            interaction.guild.get_member(int(i))
            for i in ids
        ]
        members = [m for m in members if m is not None]

        if len(members) < 2:
            return await interaction.response.send_message(
                "❌ 최소 2명 이상의 유저를 멘션해야 합니다.",
                ephemeral=True
            )

        ranks: dict[discord.Member, int] = {}
        not_found: List[str] = []
        for m in members:
            rv = self.get_tier_for_member(m)
            if rv is None:
                not_found.append(m.display_name)
            else:
                ranks[m] = rv

        if not_found:
            return await interaction.response.send_message(
                "❌ 다음 유저들의 티어를 찾을 수 없습니다:\n" +
                ", ".join(not_found),
                ephemeral=True
            )

        team_a, team_b = balance_teams(ranks)
        embed = discord.Embed(title="🔀 Auto‑Balanced Teams")
        embed.add_field(
            name=f"Team A (합계 {sum(ranks[m] for m in team_a)})",
            value="\n".join(f"{m.display_name} • {ranks[m]}" for m in team_a),
            inline=True
        )
        embed.add_field(
            name=f"Team B (합계 {sum(ranks[m] for m in team_b)})",
            value="\n".join(f"{m.display_name} • {ranks[m]}" for m in team_b),
            inline=True
        )
        await interaction.response.send_message(embed=embed)

async def setup(bot: commands.Bot):
    cog = AutoBalanceCog(bot)
    bot.tree.add_command(cog.slash_tier)
    bot.tree.add_command(cog.slash_autobalance)
    await bot.add_cog(cog)
