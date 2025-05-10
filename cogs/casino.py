# cogs/casino.py

import functools
import discord
import random
import re

from discord.ext import commands
from discord import app_commands, AllowedMentions, Interaction
from utils.logger import log_to_channel
from utils import config

def channel_only(channel_id: int):
    def decorator(func):
        @functools.wraps(func)
        async def wrapper(self, interaction: Interaction, *args, **kwargs):
            if interaction.channel.id != channel_id:
                return await interaction.response.send_message(
                    f"❌ 이 명령은 <#{channel_id}> 채널에서만 사용할 수 있습니다.",
                    ephemeral=True
                )
            return await func(self, interaction, *args, **kwargs)
        return wrapper
    return decorator

class DuelView(discord.ui.View):
    def __init__(self, challenger: discord.Member, opponent: discord.Member, bet: int):
        super().__init__(timeout=60)
        self.challenger = challenger
        self.opponent    = opponent
        self.bet         = bet

    @discord.ui.button(label="수락", style=discord.ButtonStyle.success)
    async def accept(self, interaction: Interaction, button: discord.ui.Button):
        if interaction.user != self.opponent:
            return await interaction.response.send_message("❌ 이 버튼은 도전 대상만 사용할 수 있습니다.", ephemeral=True)

        await interaction.response.send_message(
            f"✅ {self.opponent.mention}님이 도전을 수락했습니다!",
            allowed_mentions=AllowedMentions(users=True)
        )

        db = interaction.client.db
        row_c = await db.fetchrow("SELECT balance FROM coins WHERE user_id=$1", self.challenger.id)
        row_o = await db.fetchrow("SELECT balance FROM coins WHERE user_id=$1", self.opponent.id)
        bal_c = row_c["balance"] if row_c else 0
        bal_o = row_o["balance"] if row_o else 0
        if bal_c < self.bet or bal_o < self.bet:
            return await interaction.followup.send("❌ 둘 다 베팅 금액만큼 코인이 필요합니다.")

        await db.execute("UPDATE coins SET balance=balance-$2 WHERE user_id=$1", self.challenger.id, self.bet)
        await db.execute("UPDATE coins SET balance=balance-$2 WHERE user_id=$1", self.opponent.id, self.bet)

        d1, d2 = random.randint(1,6), random.randint(1,6)
        if d1 > d2:
            winner, net = self.challenger, 2 * self.bet
        elif d2 > d1:
            winner, net = self.opponent, 2 * self.bet
        else:
            winner, net = None, 0

        result = (
            f"{self.challenger.mention} rolled 🎲 **{d1}**\n"
            f"{self.opponent.mention} rolled 🎲 **{d2}**\n\n"
        )
        if winner:
            result += f"🏆 승자: {winner.mention}! (+{net} 코인)"
            await db.execute("UPDATE coins SET balance=balance+$2 WHERE user_id=$1", winner.id, net)
        else:
            result += "⚖️ 무승부! (원금 반환)"
            await db.execute("UPDATE coins SET balance=balance+$2 WHERE user_id=$1", self.challenger.id, self.bet)
            await db.execute("UPDATE coins SET balance=balance+$2 WHERE user_id=$1", self.opponent.id, self.bet)

        public = interaction.guild.get_channel(config.DICE_DUEL_CHANNEL_ID)
        await public.send(f"🎲 **Dice Duel 결과**\n{result}", allowed_mentions=AllowedMentions(users=True))

        await interaction.client.get_cog("Coins").refresh_leaderboard()

        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

    @discord.ui.button(label="거절", style=discord.ButtonStyle.danger)
    async def decline(self, interaction: Interaction, button: discord.ui.Button):
        if interaction.user != self.opponent:
            return await interaction.response.send_message("❌ 이 버튼은 도전 대상만 사용할 수 있습니다.", ephemeral=True)

        await interaction.response.send_message("❌ 도전이 거절되었습니다.", ephemeral=False)
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)


class Casino(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="슬롯", description="🎰 슬롯 게임")
    @app_commands.describe(bet="베팅할 코인 수")
    @channel_only(config.SLOTS_CHANNEL_ID)
    async def slots(self, interaction: Interaction, bet: int):
        # 1) 잔액 확인
        row = await self.bot.db.fetchrow(
            "SELECT balance FROM coins WHERE user_id=$1",
            interaction.user.id
        )
        bal = row["balance"] if row else 0
        if bet <= 0 or bal < bet:
            return await interaction.response.send_message(
                "❌ 배팅 금액이 유효하지 않거나 잔액이 부족합니다.",
                ephemeral=True
            )

        # ▶ Log here: 슬롯 도전 기록
        try:
            await log_to_channel(self.bot,
                f"{interaction.user.mention}님 슬롯 베팅 {bet}코인 시도"
            )
        except Exception:
            pass

        # 2) 심볼별 가중치 설정 (총합 100)
        symbols = ["🍒", "🍋", "🍀", "💎", "7️⃣"]
        weights = [50,   25,   15,   8,    2]
        roll = random.choices(symbols, weights, k=3)

        # 3) 페이아웃 배수 정의 (총 반환 배수)
        three_payout = {
            "🍒": 1.5,
            "🍋": 2.5,
            "🍀": 5,
            "💎": 12,
            "7️⃣": 30
        }
        two_payout = {s: 1 for s in symbols}

        # 4) 결과 계산 및 메시지 생성
        if roll.count(roll[0]) == 3:
            sym = roll[0]
            ret_mult = three_payout[sym]
            profit = int(bet * (ret_mult - 1))
            text = (
                f"{' '.join(roll)}\n"
                f"✅ 3개 {sym} 일치! \n+**{profit}** 코인 획득"
            )
            net = profit
            outcome = "승리"
        elif any(roll.count(s) == 2 for s in symbols):
            sym = next(s for s in symbols if roll.count(s) == 2)
            text = (
                f"{' '.join(roll)}\n"
                f"ℹ️ 2개 {sym} 일치! \n원금 반환"
            )
            net = 0
            outcome = "무승부"
        else:
            text = (
                f"{' '.join(roll)}\n"
                f"❌ 일치 없음... \n-**{bet}** 코인 손실"
            )
            net = -bet
            outcome = "패배"

        # 5) DB 업데이트 및 리더보드 갱신
        await self.bot.db.execute(
            "UPDATE coins SET balance = balance + $2 WHERE user_id = $1",
            interaction.user.id, net
        )
        await self.bot.get_cog("Coins").refresh_leaderboard()

        # ▶ Log here: 슬롯 결과 기록
        try:
            await log_to_channel(self.bot,
                f"{interaction.user.mention}님 슬롯 결과 → {' '.join(roll)}, {outcome}, +{net}코인"
            )
        except Exception:
            pass

        # 6) 결과 전송
        await interaction.response.send_message(text)

    @app_commands.command(name="블랙잭", description="♠️ 딜러를 이기세요")
    @app_commands.describe(bet="베팅할 코인 수")
    @channel_only(config.BLACKJACK_CHANNEL_ID)
    async def blackjack(self, interaction: Interaction, bet: int):
        # 1) log invocation
        await log_to_channel(self.bot,
                             f"{interaction.user.mention}님 블랙잭 베팅 {bet}코인 시도"
                             )
        # 2) Check balance
        row = await self.bot.db.fetchrow(
            "SELECT balance FROM coins WHERE user_id = $1",
            interaction.user.id
        )
        balance = row["balance"] if row else 0
        if bet <= 0 or balance < bet:
            return await interaction.response.send_message(
                "❌ 유효하지 않은 베팅이거나 잔액이 부족합니다.",
                ephemeral=True
            )

        # 3) Defer to buy more thinking time
        await interaction.response.defer(thinking=True)

        # 4) Build and shuffle deck
        ranks = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
        suits = ["♠️", "♥️", "♦️", "♣️"]
        deck = [r + s for r in ranks for s in suits]
        random.shuffle(deck)

        # 5) Deal initial hands
        player = [deck.pop(), deck.pop()]
        dealer = [deck.pop(), deck.pop()]

        # 6) Value calculator
        def hand_value(hand: list[str]) -> int:
            vals = {"J": 10, "Q": 10, "K": 10, "A": 11}
            total = aces = 0

            for card in hand:
                m = re.match(r'^(10|\d|[JQKA])', card)
                rank = m.group(1)

                if rank in vals:
                    v = vals[rank]
                else:
                    v = int(rank)

                total += v
                if rank == "A":
                    aces += 1

            while total > 21 and aces:
                total -= 10
                aces -= 1

            return total

        player_value = hand_value(player)
        dealer_value = hand_value(dealer)
        await log_to_channel(self.bot,
                             f"{interaction.user.mention}님 블랙잭 시작: 플레이어 {player_value}, 딜러 {dealer_value}"
                             )
        # 7) Build embed
        embed = discord.Embed(title="♠️ 블랙잭", color=discord.Color.dark_green())
        embed.add_field(
            name="내 패",
            value=f"{' '.join(player)} ({player_value})",
            inline=False
        )
        embed.add_field(
            name="딜러",
            value=dealer[0],
            inline=False
        )

        # 8) Create buttons
        view = discord.ui.View(timeout=60)
        player_user = interaction.user

        # Natural 21 on initial deal
        if player_value == 21:
            # reveal dealer
            while dealer_value < 17:
                dealer.append(deck.pop())
                dealer_value = hand_value(dealer)
            embed.title = f"🎉 블랙잭 승리! (+{bet} 코인)"
            embed.add_field(
                name="딜러",
                value=f"{' '.join(dealer)} ({dealer_value})",
                inline=False
            )
            for child in view.children:
                child.disabled = True
            await interaction.followup.send(embed=embed, view=view)
            await self.bot.db.execute(
                "UPDATE coins SET balance = balance + $2 WHERE user_id = $1",
                player_user.id, bet
            )
            await log_to_channel(self.bot,
                f"{player_user.mention}님 히트로 21 달성 → 자동 승리, +{bet}코인"
            )
            await self.bot.get_cog("Coins").refresh_leaderboard()
            return

        hit_btn = discord.ui.Button(label="히트", style=discord.ButtonStyle.primary)

        async def hit_callback(btn_inter: Interaction):
            if btn_inter.user != player_user:
                return await btn_inter.response.send_message(
                    "❌ 이 버튼은 명령을 실행한 사용자만 사용할 수 있습니다.",
                    ephemeral=True
                )

            nonlocal player_value
            player.append(deck.pop())
            player_value = hand_value(player)
            embed.set_field_at(
                0,
                name="내 패",
                value=f"{' '.join(player)} ({player_value})",
                inline=False
            )

            # Auto‑win on hitting 21
            if player_value == 21:
                while dealer_value < 17:
                    dealer.append(deck.pop())
                    dealer_value = hand_value(dealer)
                embed.title = f"🎉 21 달성! 자동 승리! (+{bet} 코인)"
                embed.add_field(
                    name="Dealer Hand",
                    value=f"{' '.join(dealer)} ({dealer_value})",
                    inline=False
                )
                for child in view.children:
                    child.disabled = True
                await btn_inter.response.edit_message(embed=embed, view=view)
                await self.bot.db.execute(
                    "UPDATE coins SET balance = balance + $2 WHERE user_id = $1",
                    player_user.id, bet
                )
                await log_to_channel(self.bot,
                                     f"{player_user.mention}님 히트로 21 달성 → 자동 승리, +{bet}코인"
                                     )
                await self.bot.get_cog("Coins").refresh_leaderboard()
                return

            if player_value > 21:
                embed.title = f"💥 버스트! (-{bet} 코인)"
                for c in view.children:
                    c.disabled = True
                await btn_inter.response.edit_message(embed=embed, view=view)
                await self.bot.db.execute(
                    "UPDATE coins SET balance = balance - $2 WHERE user_id = $1",
                    player_user.id, bet
                )
                await log_to_channel(self.bot,
                                     f"{player_user.mention}님 버스트 → -{bet}코인"
                                     )
            else:
                await btn_inter.response.edit_message(embed=embed, view=view)

            await self.bot.get_cog("Coins").refresh_leaderboard()

        hit_btn.callback = hit_callback
        view.add_item(hit_btn)

        stand_btn = discord.ui.Button(label="스탠드", style=discord.ButtonStyle.secondary)

        async def stand_callback(btn_inter: Interaction):
            if btn_inter.user != player_user:
                return await btn_inter.response.send_message(
                    "❌ 이 버튼은 명령을 실행한 사용자만 사용할 수 있습니다.",
                    ephemeral=True
                )

            nonlocal dealer_value
            while dealer_value < 17:
                dealer.append(deck.pop())
                dealer_value = hand_value(dealer)

            for c in view.children:
                c.disabled = True

            # determine outcome
            if dealer_value > 21 or player_value > dealer_value:
                net = bet
                title = f"🎉 승리! (+{net} 코인)"
                outcome = "승리"
            elif player_value < dealer_value:
                net = -bet
                title = f"😞 패배... ({net} 코인)"
                outcome = "패배"
            else:
                net = 0
                title = f"⚖️ 무승부. ({net} 코인)"
                outcome = "무승부"

            embed.title = title
            embed.add_field(
                name="딜러",
                value=f"{' '.join(dealer)} ({dealer_value})",
                inline=False
            )
            await btn_inter.response.edit_message(embed=embed, view=view)
            await log_to_channel(self.bot,
                                 f"{player_user.mention}님 스탠드 → 딜러 {dealer_value}, 결과 {outcome}, +{net}코인"
                                 )
            if net:
                await self.bot.db.execute(
                    "UPDATE coins SET balance = balance + $2 WHERE user_id = $1",
                    player_user.id, net
                )
                await self.bot.get_cog("Coins").refresh_leaderboard()

        stand_btn.callback = stand_callback
        view.add_item(stand_btn)

        # 9) Send embed + view
        await interaction.followup.send(embed=embed, view=view)

    @app_commands.command(name="동전", description="🔀 동전 뒤집기 (50/50)")
    @app_commands.describe(bet="베팅할 코인 수", side="heads 또는 tails")
    @app_commands.choices(side=[
        app_commands.Choice(name="heads", value="heads"),
        app_commands.Choice(name="tails", value="tails")
    ])
    @channel_only(config.COINFLIP_CHANNEL_ID)
    async def coinflip(self, interaction: Interaction, bet: int, side: app_commands.Choice[str]):
        # 1) 잔액 확인
        row = await self.bot.db.fetchrow(
            "SELECT balance FROM coins WHERE user_id=$1",
            interaction.user.id
        )
        bal = row["balance"] if row else 0
        if bet <= 0 or bal < bet:
            return await interaction.response.send_message(
                "❌ 배팅 금액이 유효하지 않거나 잔액이 부족합니다.",
                ephemeral=True
            )

        # ▶ Log here: 동전뒤집기 도전 기록
        try:
            await log_to_channel(self.bot,
                f"{interaction.user.mention}님이 동전 뒤집기 베팅 {bet}코인, 선택={side.value}"
            )
        except Exception:
            pass

        # 2) 결과 결정
        flip = random.choice(["heads", "tails"])
        net = bet if side.value == flip else -bet

        if net > 0:
            text = (
                f"🎉 동전 뒤집기 결과: **{flip}**\n"
                f"✅ 승리! +**{net}** 코인 획득"
            )
        else:
            text = (
                f"🎲 동전 뒤집기 결과: **{flip}**\n"
                f"❌ 패배... -**{abs(net)}** 코인 손실"
            )

        # 3) DB 업데이트
        await self.bot.db.execute(
            "UPDATE coins SET balance=balance+$2 WHERE user_id=$1",
            interaction.user.id, net
        )

        # ▶ Log here: 동전뒤집기 결과 기록
        try:
            await log_to_channel(self.bot,
                f"{interaction.user.mention}님 동전 뒤집기 → {flip}, +{net}코인"
            )
        except Exception:
            pass

        # 4) 응답 & 리더보드 갱신
        await interaction.response.send_message(text)
        await self.bot.get_cog("Coins").refresh_leaderboard()

    @app_commands.command(name="주사위", description="🎲 PvP 주사위 대결")
    @app_commands.describe(opponent="도전할 상대 멘션", bet="베팅할 코인 수")
    @channel_only(config.DICE_DUEL_CHANNEL_ID)
    async def dice_duel(self, interaction: Interaction, opponent: discord.Member, bet: int):
        # 1) 베팅 유효성 검사
        if bet <= 0:
            return await interaction.response.send_message("❌ 배팅 금액은 1 이상입니다.", ephemeral=True)
        if opponent.bot or opponent == interaction.user:
            return await interaction.response.send_message("❌ 유효한 상대를 지정하세요.", ephemeral=True)

        # 2) 도전 메시지 발송
        await interaction.response.send_message(
            f"{opponent.mention}, {interaction.user.mention}님이 **{bet}** 코인으로 주사위 대결에 도전했습니다!",
            view=DuelView(interaction.user, opponent, bet)
        )

        # ▶ Log here: 주사위 대결 도전 기록
        try:
            await log_to_channel(self.bot,
                                 f"{interaction.user.mention}님이 {opponent.mention}님에게 주사위 대결을 베팅 {bet}코인으로 도전"
                                 )
        except Exception:
            pass

async def setup(bot: commands.Bot):
    await bot.add_cog(Casino(bot))
