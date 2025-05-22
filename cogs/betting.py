# cogs/betting.py

import discord
import traceback
from typing import Literal

from discord import app_commands, Interaction
from discord.ext import commands
from discord.ui import View, Modal, TextInput, Button

from utils import config
from utils.logger import log_to_channel

# ─── Modal to enter bet amount ─────────────────────────────────────────
class BetModal(Modal):
    def __init__(self, team_key: str, cog: "BettingCog"):
        super().__init__(title=f"{cog.prediction['teams'][team_key]} 베팅")
        self.team_key = team_key
        self.cog = cog
        self.amount_input = TextInput(label="베팅할 코인 수", placeholder="예: 100")
        self.add_item(self.amount_input)

    async def on_submit(self, inter: Interaction):
        try:
            amount = int(self.amount_input.value)
        except ValueError:
            return await inter.response.send_message("❌ 유효한 숫자를 입력해 주세요.", ephemeral=True)
        await inter.response.defer(ephemeral=True)
        await self.cog.process_bet(self.team_key, inter.user, amount, inter)


# ─── Button for each team ───────────────────────────────────────────────
class TeamButton(Button):
    def __init__(self, team_key: str, cog: "BettingCog"):
        label = cog.prediction["teams"][team_key]
        super().__init__(custom_id=f"bet_{team_key}", label=label, style=discord.ButtonStyle.primary)
        self.team_key = team_key
        self.cog = cog

    async def callback(self, inter: Interaction):
        if not self.cog.prediction or inter.message.id != self.cog.prediction["message_id"]:
            return await inter.response.send_message("❌ 유효한 배팅 인터페이스가 아닙니다.", ephemeral=True)
        await inter.response.send_modal(BetModal(self.team_key, self.cog))


# ─── Cog implementing the betting system ─────────────────────────────────
class BettingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.prediction = None  # current bet state

    @app_commands.command(name="create_bet", description="관리자 전용: 팀 배팅 인터페이스 생성")
    @app_commands.describe(team1="팀 1 이름", team2="팀 2 이름")
    @app_commands.checks.has_permissions(administrator=True)
    async def create_bet(self, inter: Interaction, team1: str, team2: str):
        self.prediction = {
            "teams": {"team1": team1, "team2": team2},
            "bets": {"team1": {}, "team2": {}},
        }
        embed = self._build_embed()
        view = View(timeout=None)
        view.add_item(TeamButton("team1", self))
        view.add_item(TeamButton("team2", self))

        msg = await inter.channel.send(embed=embed, view=view)
        self.prediction["message_id"] = msg.id
        self.bot.add_view(view, message_id=msg.id)
        await inter.response.send_message("✅ 배팅 인터페이스가 생성되었습니다.", ephemeral=True)

    @app_commands.command(name="cancel_bet", description="관리자 전용: 배팅 취소 및 환불")
    @app_commands.checks.has_permissions(administrator=True)
    async def cancel_bet(self, inter: Interaction):
        if not self.prediction:
            return await inter.response.send_message("❌ 활성화된 배팅이 없습니다.", ephemeral=True)

        # refund everyone
        refunds = []
        for team in ("team1", "team2"):
            for user_id, amt in self.prediction["bets"][team].items():
                await self.bot.db.execute(
                    "UPDATE coins SET balance = balance + $2 WHERE user_id = $1",
                    user_id, amt
                )
                refunds.append((user_id, amt))

        await self.bot.get_cog("Coins").refresh_leaderboard()

        for user_id, amt in refunds:
            user = self.bot.get_user(user_id)
            if user:
                try:
                    await user.send(f"❌ 배팅이 취소되어 {amt} 코인을 환불받았습니다.")
                except:
                    pass

        embed = discord.Embed(
            title="❌ 배팅 취소",
            description="관리자에 의해 모든 배팅이 취소되고 환불되었습니다.",
            color=discord.Color.red()
        )
        ch = inter.channel
        msg = await ch.fetch_message(self.prediction["message_id"])
        await msg.edit(embed=embed, view=None)
        self.prediction = None
        await inter.response.send_message("✅ 배팅이 취소되고 환불되었습니다.", ephemeral=True)

    @app_commands.command(name="close_bet", description="관리자 전용: 배팅 종료 및 우승팀 정산")
    @app_commands.describe(winner="우승팀 (team1 또는 team2)")
    @app_commands.checks.has_permissions(administrator=True)
    async def close_bet(self, inter: Interaction, winner: Literal["team1", "team2"]):
        if not self.prediction:
            return await inter.response.send_message("❌ 활성화된 배팅이 없습니다.", ephemeral=True)

        loser = "team2" if winner == "team1" else "team1"
        win_sum = sum(self.prediction["bets"][winner].values())
        lose_sum = sum(self.prediction["bets"][loser].values())
        total = win_sum + lose_sum
        if win_sum <= 0:
            return await inter.response.send_message("❌ 우승팀에 베팅이 없습니다.", ephemeral=True)

        odds = total / win_sum
        payouts = {uid: int(amt * odds) for uid, amt in self.prediction["bets"][winner].items()}

        for user_id, payout in payouts.items():
            await self.bot.db.execute(
                "UPDATE coins SET balance = balance + $2 WHERE user_id = $1",
                user_id, payout
            )
            user = self.bot.get_user(user_id)
            if user:
                try:
                    await user.send(f"🏆 배팅 승리! `{self.prediction['teams'][winner]}`에 베팅하셔서 {payout} 코인을 획득하셨습니다.")
                except:
                    pass

        await self.bot.get_cog("Coins").refresh_leaderboard()

        embed = discord.Embed(
            title="🏁 배팅 종료",
            description=f"우승팀: **{self.prediction['teams'][winner]}**",
            color=discord.Color.green()
        )
        embed.add_field(name="총 배팅액", value=f"{total} 코인", inline=False)
        embed.add_field(name="배당률", value=f"{odds:.2f}×", inline=False)

        ch = inter.channel
        msg = await ch.fetch_message(self.prediction["message_id"])
        await msg.edit(embed=embed, view=None)
        self.prediction = None
        await inter.response.send_message("✅ 배팅이 종료되고 정산되었습니다.", ephemeral=True)

    def _build_embed(self) -> discord.Embed:
        t1 = self.prediction["teams"]["team1"]
        t2 = self.prediction["teams"]["team2"]
        bets1 = self.prediction["bets"]["team1"]
        bets2 = self.prediction["bets"]["team2"]
        b1 = sum(bets1.values())
        b2 = sum(bets2.values())
        total = b1 + b2

        # percentages
        pct1 = (b1 / total * 100) if total > 0 else 0
        pct2 = (b2 / total * 100) if total > 0 else 0

        # odds multipliers
        odd1 = round((total / b1), 2) if b1 > 0 else 0
        odd2 = round((total / b2), 2) if b2 > 0 else 0

        # format as ratios "1:2.02"
        ratio1 = f"1:{odd1:.2f}" if odd1 else "—"
        ratio2 = f"1:{odd2:.2f}" if odd2 else "—"

        # prepare bettor lists
        def fmt_bettors(bets: dict[int, int]) -> str:
            if not bets:
                return "—"
            lines = []
            for uid, amt in bets.items():
                user = self.bot.get_user(uid)
                mention = user.mention if user else f"<@{uid}>"
                lines.append(f"{mention}: {amt} 코인")
            return "\n".join(lines)

        bettors1 = fmt_bettors(bets1)
        bettors2 = fmt_bettors(bets2)

        embed = discord.Embed(
            title="🏆 배팅 중",
            description=(
                f"{t1} vs {t2}\n"
                f"비율: {pct1:.1f}% | {pct2:.1f}%  •  배당: {ratio1} | {ratio2}"
            ),
            color=discord.Color.blurple()
        )
        embed.add_field(
            name=f"{t1} ({b1} 코인, 배당 {ratio1})",
            value=bettors1,
            inline=False
        )
        embed.add_field(
            name=f"{t2} ({b2} 코인, 배당 {ratio2})",
            value=bettors2,
            inline=False
        )
        embed.set_footer(text="버튼을 눌러 베팅하세요.")
        return embed

    async def process_bet(self, team_key: str, user: discord.Member, amount: int, inter: Interaction):
        other = "team2" if team_key == "team1" else "team1"
        if user.id in self.prediction["bets"][other]:
            return await inter.followup.send("❌ 이미 다른 팀에 베팅하셨습니다.", ephemeral=True)

        row = await self.bot.db.fetchrow("SELECT balance FROM coins WHERE user_id = $1", user.id)
        bal = row["balance"] if row else 0
        if amount <= 0 or bal < amount:
            return await inter.followup.send(f"❌ 베팅 실패: 잔액 부족 (현재 {bal}코인)", ephemeral=True)

        await self.bot.db.execute(
            "UPDATE coins SET balance = balance - $2 WHERE user_id = $1",
            user.id, amount
        )
        await log_to_channel(
            self.bot,
            f"🎲 {user.display_name}님이 {self.prediction['teams'][team_key]}에 {amount}코인 베팅"
        )

        await self.bot.get_cog("Coins").refresh_leaderboard()

        self.prediction["bets"][team_key][user.id] = (
            self.prediction["bets"][team_key].get(user.id, 0) + amount
        )

        ch = inter.channel
        msg = await ch.fetch_message(self.prediction["message_id"])
        await msg.edit(embed=self._build_embed())

        await inter.followup.send(f"✅ {amount}코인 베팅 완료!", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BettingCog(bot))
