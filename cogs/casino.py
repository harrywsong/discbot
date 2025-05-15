# cogs/casino.py

import functools

from discord import AllowedMentions

import random
import re
import discord
from discord import app_commands, Interaction
from discord.ext import commands
from discord.ui import View, Button
from utils import config
from utils.logger import log_to_channel

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
        await public.send(f"🎲 **주사위 대결 결과**\n{result}", allowed_mentions=AllowedMentions(users=True))

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
        await log_to_channel(self.bot, f"{interaction.user.mention}님 블랙잭 베팅 {bet}코인 시도")
        # 1) 잔액 체크
        row = await self.bot.db.fetchrow(
            "SELECT balance FROM coins WHERE user_id = $1",
            interaction.user.id
        )
        balance = row["balance"] if row else 0
        if bet <= 0 or balance < bet:
            return await interaction.response.send_message(
                "❌ 유효하지 않은 베팅이거나 잔액이 부족합니다.", ephemeral=True
            )

        # 2) 시간을 벌기 위해 defer
        await interaction.response.defer(thinking=True)

        # 3) 덱 생성 및 셔플
        ranks = ["A","2","3","4","5","6","7","8","9","10","J","Q","K"]
        suits = ["♠️","♥️","♦️","♣️"]
        deck = [r+s for r in ranks for s in suits]
        random.shuffle(deck)

        # 4) 핸드 초기화
        hands = [[deck.pop(), deck.pop()]]
        hand_bets = [bet]
        is_doubled = [False]
        current = 0
        dealer = [deck.pop(), deck.pop()]

        def hand_value(cards: list[str]) -> int:
            vals = {"J":10,"Q":10,"K":10,"A":11}
            total = aces = 0
            for c in cards:
                m = re.match(r'^(10|\d|[JQKA])', c)
                r = m.group(1)
                total += vals[r] if r in vals else int(r)
                if r == "A": aces += 1
            while total > 21 and aces:
                total -= 10; aces -= 1
            return total

        # 5) 초기 값 계산 & 로그
        values = [hand_value(hands[0])]
        dealer_val = hand_value(dealer)
        await log_to_channel(self.bot,
            f"{interaction.user.mention}님 블랙잭 시작: 플레이어 {values[0]}, 딜러 {dealer_val}"
        )

        # 6) Embed & View 준비
        embed = discord.Embed(title="♠️ 블랙잭", color=discord.Color.dark_green())
        view = discord.ui.View(timeout=60)
        player = interaction.user

        async def update_embed():
            embed.clear_fields()
            for idx, hand in enumerate(hands, start=1):
                prefix = "▶ " if idx-1 == current else ""
                embed.add_field(
                    name=f"{prefix}핸드 {idx}",
                    value=f"{' '.join(hand)} ({hand_value(hand)})",
                    inline=False
                )
            embed.add_field(name="딜러", value=dealer[0], inline=False)

        # 첫 임베드 세팅
        await update_embed()

        # 7) 자연 블랙잭 처리
        if values[0] == 21:
            while dealer_val < 17:
                dealer.append(deck.pop())
                dealer_val = hand_value(dealer)
            embed.title = f"🎉 블랙잭 승리! (+{bet} 코인)"
            embed.set_field_at(
                1,
                name="딜러",
                value=f"{' '.join(dealer)} ({dealer_val})",
                inline=False
            )
            view.clear_items()
            await interaction.followup.send(embed=embed, view=view)
            await self.bot.db.execute(
                "UPDATE coins SET balance = balance + $2 WHERE user_id = $1",
                player.id, bet
            )
            await self.bot.get_cog("Coins").refresh_leaderboard()
            return

        # 8) 버튼 정의
        hit_btn   = discord.ui.Button(label="히트",     style=discord.ButtonStyle.primary)
        stand_btn = discord.ui.Button(label="스탠드",   style=discord.ButtonStyle.secondary)
        dbl_btn   = discord.ui.Button(label="더블다운", style=discord.ButtonStyle.success)
        split_btn = discord.ui.Button(label="스플릿",   style=discord.ButtonStyle.danger)

        # 9) 히트 콜백
        async def hit_cb(i: Interaction):
            if i.user != player:
                return await i.response.send_message("❌ 당신만 사용할 수 있습니다.", ephemeral=True)
            hands[current].append(deck.pop())
            values[current] = hand_value(hands[current])
            await update_embed()
            if values[current] >= 21:
                return await stand_cb(i)
            await i.response.edit_message(embed=embed, view=view)

        # 10) 스탠드 콜백
        async def stand_cb(i: Interaction):
            nonlocal current, dealer_val
            if i.user != player:
                return await i.response.send_message("❌ 당신만 사용할 수 있습니다.", ephemeral=True)
            # 스플릿 중 다음 핸드 있으면 이동
            if len(hands) > 1 and current < len(hands) - 1:
                current += 1
                await update_embed()
                return await i.response.edit_message(embed=embed, view=view)
            # 딜러 플레이
            while dealer_val < 17:
                dealer.append(deck.pop())
                dealer_val = hand_value(dealer)
            await update_embed()
            embed.set_field_at(
                len(hands),
                name="딜러",
                value=f"{' '.join(dealer)} ({dealer_val})",
                inline=False
            )
            view.clear_items()
            # 결과 계산 & DB 반영
            summary = []
            for idx, hand in enumerate(hands, start=1):
                hv = hand_value(hand)
                stake = hand_bets[idx-1] * (2 if is_doubled[idx-1] else 1)
                if hv > 21:
                    net, res = -stake, "버스트"
                elif dealer_val > 21 or hv > dealer_val:
                    net, res = stake, "승리"
                elif hv < dealer_val:
                    net, res = -stake, "패배"
                else:
                    net, res = 0, "무승부"
                summary.append(f"핸드 {idx}: {res} ({net:+} 코인)")
                if net:
                    await self.bot.db.execute(
                        "UPDATE coins SET balance = balance + $2 WHERE user_id = $1",
                        player.id, net
                    )
            embed.title = "\n".join(summary)
            await i.response.edit_message(embed=embed, view=view)
            await self.bot.get_cog("Coins").refresh_leaderboard()
            await log_to_channel(self.bot,
                f"{player.display_name}님 블랙잭 결과: {'; '.join(summary)}"
            )
            return

        # 11) 더블다운 콜백
        async def dbl_cb(i: Interaction):
            # 1) only the original player can press
            if i.user != player:
                return await i.response.send_message("❌ 당신만 사용할 수 있습니다.", ephemeral=True)

            # 2) only allowed on first two cards
            if len(hands[current]) != 2:
                return await i.response.send_message("ℹ️ 첫 2장에서만 더블다운 가능합니다.", ephemeral=True)

            # 3) mark as doubled, draw one card
            is_doubled[current] = True
            hands[current].append(deck.pop())

            # 4) update embed to show new hand value
            await update_embed()

            # 5) calculate current hand total
            curr_val = hand_value(hands[current])

            # 6) if bust (>21), handle immediately
            if curr_val > 21:
                # build bust title
                loss_amount = hand_bets[current] * 2
                embed.title = f"💥 버스트! (-{loss_amount} 코인)"

                # reveal dealer’s full hand
                while dealer_val < 17:
                    dealer.append(deck.pop())
                    dealer_val = hand_value(dealer)
                embed.set_field_at(
                    1,
                    name="딜러",
                    value=f"{' '.join(dealer)} ({dealer_val})",
                    inline=False
                )

                # disable all buttons
                for btn in view.children:
                    btn.disabled = True

                # edit the message once
                await i.response.edit_message(embed=embed, view=view)

                # apply 2× loss
                await self.bot.db.execute(
                    "UPDATE coins SET balance = balance - $2 WHERE user_id = $1",
                    player.id, loss_amount
                )
                await self.bot.get_cog("Coins").refresh_leaderboard()
                await log_to_channel(
                    self.bot,
                    f"{player.display_name}님 더블다운 버스트 → -{loss_amount}코인"
                )
                return

            # 7) not busted? fall back to stand logic
            return await stand_cb(i)

        # 12) 스플릿 콜백
        async def split_cb(i: Interaction):
            if i.user != player:
                return await i.response.send_message("❌ 당신만 사용할 수 있습니다.", ephemeral=True)
            if len(hands) > 1:
                return await i.response.send_message("ℹ️ 이미 스플릿되었습니다.", ephemeral=True)
            r0 = re.match(r'^(10|\d|[JQKA])', hands[0][0]).group(1)
            r1 = re.match(r'^(10|\d|[JQKA])', hands[0][1]).group(1)
            if r0 != r1:
                return await i.response.send_message("ℹ️ 같은 값의 카드 두 장에서만 스플릿 가능합니다.", ephemeral=True)
            row = await self.bot.db.fetchrow(
                "SELECT balance FROM coins WHERE user_id = $1", player.id
            )
            if (row["balance"] if row else 0) < hand_bets[0]:
                return await i.response.send_message("❌ 잔액이 부족합니다.", ephemeral=True)

            # 스플릿 베팅 차감
            await self.bot.db.execute(
                "UPDATE coins SET balance = balance - $2 WHERE user_id = $1",
                player.id, hand_bets[0]
            )
            c1, c2 = hands[0]
            hands[:]     = [[c1, deck.pop()], [c2, deck.pop()]]
            hand_bets[:] = [hand_bets[0], hand_bets[0]]
            is_doubled[:] = [False, False]
            await update_embed()
            await i.response.edit_message(embed=embed, view=view)

        # 13) 콜백 연결 & 뷰에 추가
        hit_btn.callback   = hit_cb
        stand_btn.callback = stand_cb
        dbl_btn.callback   = dbl_cb
        split_btn.callback = split_cb
        view.add_item(hit_btn)
        view.add_item(stand_btn)
        view.add_item(dbl_btn)
        view.add_item(split_btn)

        # 14) 메시지 전송
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

    @app_commands.command(
        name="가위바위보",
        description="✌️✊🖐️ 봇과 가위바위보! 이기면 2코인 획득"
    )
    @app_commands.checks.cooldown(1, 180, key=lambda i: i.user.id)
    @app_commands.choices(
        choice=[
            app_commands.Choice(name="✌️ 가위", value="scissors"),
            app_commands.Choice(name="✊ 바위", value="rock"),
            app_commands.Choice(name="🖐️ 보", value="paper"),
        ]
    )
    @channel_only(config.RPC_CHANNEL_ID)
    async def rps(self, interaction: Interaction, choice: app_commands.Choice[str]):
        user_choice = choice.value
        bot_choice = random.choice(["rock", "paper", "scissors"])
        wins = {"rock": "scissors", "scissors": "paper", "paper": "rock"}

        if user_choice == bot_choice:
            result, delta = "⚖️ 무승부! 코인은 변동 없습니다.", 0
        elif wins[user_choice] == bot_choice:
            result, delta = "🏆 당신의 승리! +2 코인", 2
        else:
            result, delta = "❌ 패배... 다음 기회에!", 0

        if delta > 0:
            await self.bot.db.execute(
                "UPDATE coins SET balance = balance + $2 WHERE user_id = $1",
                interaction.user.id, delta
            )
            await self.bot.get_cog("Coins").refresh_leaderboard()
            await log_to_channel(
                self.bot,
                f"{interaction.user.display_name}님이 가위바위보 승리로 {delta}코인 획득!"
            )

        emoji_map = {"rock": "✊", "paper": "🖐️", "scissors": "✌️"}
        text = (
            f"**숯검댕이** 🆚 **{interaction.user.display_name}**\n\n"
            f"숯검댕이: {emoji_map[bot_choice]}  당신: {emoji_map[user_choice]}\n\n"
            f"{result}"
        )
        await interaction.response.send_message(text, allowed_mentions=None)

    async def cog_app_command_error(self, interaction: Interaction, error: app_commands.AppCommandError):
        # Handle cooldowns
        if isinstance(error, app_commands.CommandOnCooldown):
            retry = int(error.retry_after)
            m, s = divmod(retry, 60)
            await interaction.response.send_message(
                f"⏳ {m}분 {s}초 후에 다시 시도해주세요.", ephemeral=True
            )
        else:
            # Let other errors bubble up (or handle them here)
            raise error

async def setup(bot: commands.Bot):
    await bot.add_cog(Casino(bot))
