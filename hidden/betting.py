# cogs/betting.py

import discord
import traceback
from typing import Literal

from discord import app_commands, Interaction
from discord.ext import commands
from discord.ui import View, Modal, TextInput, Button

from utils import config
from utils.logger import log_to_channel
from utils.henrik import henrik_get


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


class TeamButton(Button):
    def __init__(self, team_key: str, cog: "BettingCog"):
        label = cog.prediction["teams"][team_key]  # now cog.prediction is set
        super().__init__(custom_id=f"bet_{team_key}", label=label, style=discord.ButtonStyle.primary)
        self.team_key = team_key
        self.cog = cog

    async def callback(self, inter: Interaction):
        if not self.cog.prediction or inter.message.id != self.cog.prediction["message_id"]:
            return await inter.response.send_message("❌ 유효한 배팅 인터페이스가 아닙니다.", ephemeral=True)
        await inter.response.send_modal(BetModal(self.team_key, self.cog))


class BettingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.prediction = None  # will hold teams, bets, view, message_id

    @app_commands.command(name="bet_create", description="관리자 전용: 팀 배팅 인터페이스 생성")
    @app_commands.describe(team1="팀 1 이름", team2="팀 2 이름")
    @app_commands.checks.has_permissions(administrator=True)
    async def create_bet(self, inter: Interaction, team1: str, team2: str):
        user_display = f"{inter.user.display_name} 님"
        try:
            # 1) Initialize prediction so TeamButton can read it
            self.prediction = {
                "teams": {"team1": team1, "team2": team2},
                "bets": {"team1": {}, "team2": {}},
            }

            # 2) Build the View & Buttons
            view = View(timeout=None)
            self.prediction["view"] = view
            view.add_item(TeamButton("team1", self))
            view.add_item(TeamButton("team2", self))

            # 3) Send the embed + view to channel
            embed = self._build_embed()
            msg = await inter.channel.send(embed=embed, view=view)
            self.prediction["message_id"] = msg.id
            self.bot.add_view(view, message_id=msg.id)

            # 4) Respond to the slash command
            await inter.response.send_message("✅ 배팅 인터페이스가 생성되었습니다.", ephemeral=True)
            await log_to_channel(self.bot, f"✅ [배팅] {user_display}님 배팅 인터페이스 생성 (ID={msg.id})")
        except Exception as e:
            traceback.print_exception(type(e), e, e.__traceback__)
            if inter.response.is_done():
                await inter.followup.send(f"❌ 오류 발생: {e}", ephemeral=True)
            else:
                await inter.response.send_message(f"❌ 오류 발생: {e}", ephemeral=True)
            await log_to_channel(self.bot, f"❌ [배팅] {user_display}님 인터페이스 생성 중 오류: {e}")

    @app_commands.command(name="bet_lock", description="관리자 전용: 베팅 잠금 (추가 베팅 불가)")
    @app_commands.checks.has_permissions(administrator=True)
    async def lock_bet(self, inter: Interaction):
        user_display = f"{inter.user.display_name} 님"
        if not self.prediction:
            await inter.response.send_message("❌ 활성화된 배팅이 없습니다.", ephemeral=True)
            await log_to_channel(self.bot, f"❌ [배팅] {user_display}님 활성 배팅 없음")
            return

        for child in self.prediction["view"].children:
            child.disabled = True

        msg = await inter.channel.fetch_message(self.prediction["message_id"])
        await msg.edit(view=self.prediction["view"])
        await inter.response.send_message("🔒 베팅이 잠금 처리되었습니다.", ephemeral=True)
        await log_to_channel(self.bot, f"🔒 [배팅] {user_display}님 베팅 잠금")

    @app_commands.command(name="bet_cancel", description="관리자 전용: 배팅 취소 및 환불")
    @app_commands.checks.has_permissions(administrator=True)
    async def cancel_bet(self, inter: Interaction):
        user_display = f"{inter.user.display_name} 님"
        if not self.prediction:
            await inter.response.send_message("❌ 활성화된 배팅이 없습니다.", ephemeral=True)
            await log_to_channel(self.bot, f"❌ [배팅] {user_display}님 활성 배팅 없음")
            return

        refunds = []
        for team in ("team1", "team2"):
            for uid, amt in self.prediction["bets"][team].items():
                await self.bot.db.execute(
                    "UPDATE coins SET balance = balance + $2 WHERE user_id = $1",
                    uid, amt
                )
                refunds.append((uid, amt))

        await self.bot.get_cog("Coins").refresh_leaderboard()
        for uid, amt in refunds:
            user = self.bot.get_user(uid)
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
        msg = await inter.channel.fetch_message(self.prediction["message_id"])
        await msg.edit(embed=embed, view=None)
        self.prediction = None
        await inter.response.send_message("✅ 배팅이 취소되고 환불되었습니다.", ephemeral=True)
        await log_to_channel(self.bot, f"✅ [배팅] {user_display}님 배팅 취소 및 환불 완료")

    @app_commands.command(name="bet_close", description="관리자 전용: 배팅 종료 및 우승팀 정산")
    @app_commands.describe(winner="우승팀 (team1 또는 team2)")
    @app_commands.checks.has_permissions(administrator=True)
    async def close_bet(self, inter: Interaction, winner: Literal["team1", "team2"]):
        user_display = f"{inter.user.display_name} 님"
        if not self.prediction:
            await inter.response.send_message("❌ 활성화된 배팅이 없습니다.", ephemeral=True)
            await log_to_channel(self.bot, f"❌ [배팅] {user_display}님 활성 배팅 없음")
            return

        loser = "team2" if winner == "team1" else "team1"
        win_sum = sum(self.prediction["bets"][winner].values())
        lose_sum = sum(self.prediction["bets"][loser].values())
        total = win_sum + lose_sum

        if win_sum <= 0:
            await inter.response.send_message("❌ 우승팀에 베팅이 없습니다.", ephemeral=True)
            await log_to_channel(self.bot, f"❌ [배팅] {user_display}님 우승팀에 베팅 없음")
            return

        # Twitch-style payout
        payouts = {
            uid: int(amt + (amt / win_sum) * lose_sum)
            for uid, amt in self.prediction["bets"][winner].items()
        }
        for uid, payout in payouts.items():
            await self.bot.db.execute(
                "UPDATE coins SET balance = balance + $2 WHERE user_id = $1",
                uid, payout
            )
            user = self.bot.get_user(uid)
            if user:
                try:
                    await user.send(
                        f"🏆 배팅 승리! `{self.prediction['teams'][winner]}` 베팅으로 {payout}코인 획득."
                    )
                except:
                    pass

        await self.bot.get_cog("Coins").refresh_leaderboard()

        multiplier = round((lose_sum / win_sum) + 1, 2)
        embed = discord.Embed(
            title="🏁 배팅 종료",
            description=(
                f"우승팀: **{self.prediction['teams'][winner]}**\n"
                f"배당률: {multiplier:.2f}×"
            ),
            color=discord.Color.green()
        )
        embed.add_field(name="총 배팅액", value=f"{total} 코인", inline=False)

        msg = await inter.channel.fetch_message(self.prediction["message_id"])
        await msg.edit(embed=embed, view=None)
        self.prediction = None
        await inter.response.send_message("✅ 배팅이 종료되고 정산되었습니다.", ephemeral=True)
        await log_to_channel(self.bot, f"✅ [배팅] {user_display}님 배팅 종료 및 정산 완료")

    def _build_embed(self) -> discord.Embed:
        t1, t2 = self.prediction["teams"]["team1"], self.prediction["teams"]["team2"]
        b1 = sum(self.prediction["bets"]["team1"].values())
        b2 = sum(self.prediction["bets"]["team2"].values())
        total = b1 + b2
        pct1 = (b1 / total * 100) if total else 0
        pct2 = (b2 / total * 100) if total else 0

        m1 = round((b2 / b1) + 1, 2) if b1 else 0
        m2 = round((b1 / b2) + 1, 2) if b2 else 0

        def fmt(bets):
            if not bets:
                return "—"
            return "\n".join(
                f"{(self.bot.get_user(uid) or f'<@{uid}>').mention}: {amt} 코인"
                for uid, amt in bets.items()
            )

        embed = discord.Embed(
            title="🏆 배팅 중",
            description=(
                f"{t1} vs {t2}\n"
                f"배당률: {m1:.2f}× | {m2:.2f}×  •  비율: {pct1:.1f}% | {pct2:.1f}%"
            ),
            color=discord.Color.blurple()
        )
        embed.add_field(name=f"{t1} ({b1} 코인, 배당 {m1:.2f}×)", value=fmt(self.prediction["bets"]["team1"]), inline=False)
        embed.add_field(name=f"{t2} ({b2} 코인, 배당 {m2:.2f}×)", value=fmt(self.prediction["bets"]["team2"]), inline=False)
        embed.set_footer(text="버튼을 눌러 베팅하세요.")
        return embed

    async def process_bet(self, team_key: str, user: discord.Member, amount: int, inter: Interaction):
        other = "team2" if team_key == "team1" else "team1"
        user_display = f"{user.display_name} 님"

        if user.id in self.prediction["bets"][other]:
            await inter.followup.send("❌ 이미 다른 팀에 베팅하셨습니다.", ephemeral=True)
            await log_to_channel(self.bot, f"⚠️ [베팅] {user_display}님 이미 다른 팀에 베팅함")
            return

        row = await self.bot.db.fetchrow("SELECT balance FROM coins WHERE user_id = $1", user.id)
        bal = row["balance"] if row else 0
        if amount <= 0 or bal < amount:
            await inter.followup.send(f"❌ 잔액 부족 (현재 {bal}코인)", ephemeral=True)
            await log_to_channel(self.bot, f"❌ [베팅] {user_display}님 잔액 부족 (현재 {bal}코인)")
            return

        await self.bot.db.execute(
            "UPDATE coins SET balance = balance - $2 WHERE user_id = $1",
            user.id, amount
        )
        await log_to_channel(
            self.bot,
            f"🎲 [베팅] {user_display}님이 `{self.prediction['teams'][team_key]}`에 {amount}코인 베팅"
        )
        await self.bot.get_cog("Coins").refresh_leaderboard()

        self.prediction["bets"][team_key][user.id] = (
            self.prediction["bets"][team_key].get(user.id, 0) + amount
        )

        msg = await inter.channel.fetch_message(self.prediction["message_id"])
        await msg.edit(embed=self._build_embed())

        await inter.followup.send(f"✅ {amount}코인 베팅 완료!", ephemeral=True)
        await log_to_channel(self.bot, f"✅ [베팅] {user_display}님 {amount}코인 베팅 완료")

async def setup(bot: commands.Bot):
    await bot.add_cog(BettingCog(bot))
