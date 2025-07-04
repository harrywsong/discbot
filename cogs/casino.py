# cogs/casino.py rps update

import discord

import random
import functools
import io
import asyncio
import re

from discord import AllowedMentions, app_commands, Interaction, File
from discord.ext import commands
from discord.ui import View, Button
from utils import config
from utils.logger import log_to_channel
from PIL import Image, ImageDraw
from utils.henrik import henrik_get

# 실제 유럽식 룰렛의 빨강 번호 집합
RED_NUMBERS = {
    1, 3, 5, 7, 9, 12, 14, 16, 18,
    19, 21, 23, 25, 27, 30, 32, 34, 36
}


class RPSView(View):
    def __init__(self, user: discord.Member, bot: commands.Bot):
        super().__init__(timeout=60)
        self.user = user
        self.bot = bot

    async def disable_all(self):
        for child in self.children:
            child.disabled = True

    @discord.ui.button(label="✊ 바위", style=discord.ButtonStyle.primary)
    async def rock_button(self, interaction: Interaction, button: discord.ui.Button):
        await self.resolve(interaction, "rock")

    @discord.ui.button(label="✌️ 가위", style=discord.ButtonStyle.success)
    async def scissors_button(self, interaction: Interaction, button: discord.ui.Button):
        await self.resolve(interaction, "scissors")

    @discord.ui.button(label="🖐️ 보", style=discord.ButtonStyle.secondary)
    async def paper_button(self, interaction: Interaction, button: discord.ui.Button):
        await self.resolve(interaction, "paper")

    async def resolve(self, interaction: Interaction, user_choice: str):
        if interaction.user != self.user:
            return await interaction.response.send_message(
                "❌ 도전한 사용자만 버튼을 누를 수 있습니다.", ephemeral=True
            )

        bot_choice = random.choice(["rock", "paper", "scissors"])
        wins = {"rock": "scissors", "scissors": "paper", "paper": "rock"}

        if user_choice == bot_choice:
            text, delta = "⚖️ 무승부! 코인은 변동 없습니다.", 0
        elif wins[user_choice] == bot_choice:
            text, delta = "🏆 승리! +2 코인", 2
        else:
            text, delta = "❌ 패배... 다음 기회에!", 0

        if delta:
            await self.bot.db.execute(
                "UPDATE coins SET balance = GREATEST(balance + $2, 0) WHERE user_id = $1",
                self.user.id, delta
            )
            await self.bot.get_cog("Coins").refresh_leaderboard()

            user_display = f"{self.user.display_name} 님"
            await log_to_channel(
                self.bot,
                f"🎮 [가위바위보] {user_display}님이 승리하여 {delta}코인 획득"
            )

        emoji = {"rock": "✊", "paper": "🖐️", "scissors": "✌️"}
        result_msg = (
            f"**숯검댕이** 🆚 **{self.user.display_name}**\n\n"
            f"숯검댕이: {emoji[bot_choice]}  {self.user.display_name}: {emoji[user_choice]}\n\n"
            f"{text}"
        )

        await self.disable_all()
        await interaction.response.edit_message(content=result_msg, view=self)
        self.stop()


def draw_roulette_wheel(size: int = 400) -> Image.Image:
    """
    Returns a square RGBA PIL image, size x size px,
    with 37 equal‑angle pie slices representing a Euro wheel.
    Pocket 0 is centered at the very top.
    """
    img = Image.new("RGBA", (size, size), (255, 255, 255, 0))
    draw = ImageDraw.Draw(img)
    cx, cy = size / 2, size / 2
    r = size / 2 - 20  # leave a 20px margin
    deg_per = 360 / 37
    # start so that pocket 0 is centered at 12 o'clock
    start_angle = -90 - deg_per / 2

    pockets = [0] + list(range(1, 37))
    for i, pocket in enumerate(pockets):
        a0 = start_angle + i * deg_per
        a1 = a0 + deg_per
        color = (
            "green" if pocket == 0
            else "red" if pocket in RED_NUMBERS
            else "black"
        )
        draw.pieslice(
            [cx - r, cy - r, cx + r, cy + r],
            start=a0, end=a1,
            fill=color,
            outline="white"
        )

    # <-- now return after drawing *all* slices
    return img


def make_spin_gif(
    wheel_img: Image.Image,
    result_pocket: int,
    frames: int = 25
) -> io.BytesIO:
    """
    Rotate wheel_img so that result_pocket lands at 12 o'clock,
    easing out over `frames` frames. Returns a BytesIO of a GIF.
    """
    size = wheel_img.width
    deg_per = 360 / 37
    spins = 3
    final_rotation = - (360 * spins + result_pocket * deg_per)

    gif_frames = []
    for i in range(frames):
        t = i / (frames - 1)
        # ease‑out curve
        angle = final_rotation * (1 - (1 - t) ** 2)
        frame = wheel_img.rotate(angle, resample=Image.BICUBIC, expand=False)

        # draw the fixed pointer triangle at 12 o'clock
        draw = ImageDraw.Draw(frame)
        triangle = [
            (size / 2 - 12, 6),
            (size / 2 + 12, 6),
            (size / 2, 30)
        ]
        draw.polygon(triangle, fill="yellow")

        gif_frames.append(frame)

    out = io.BytesIO()
    gif_frames[0].save(
        out,
        format="GIF",
        save_all=True,
        append_images=gif_frames[1:],
        duration=40,  # ms per frame
        loop=1,       # play exactly once
        disposal=2    # clear each frame before drawing next
    )
    out.seek(0)
    return out


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
        self.opponent = opponent
        self.bet = bet

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

        await db.execute(
            "UPDATE coins SET balance = GREATEST(balance - $2, 0) WHERE user_id = $1",
            self.challenger.id, self.bet
        )
        await db.execute(
            "UPDATE coins SET balance = GREATEST(balance - $2, 0) WHERE user_id = $1",
            self.opponent.id, self.bet
        )

        d1, d2 = random.randint(1, 6), random.randint(1, 6)
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
            await db.execute(
                "UPDATE coins SET balance = GREATEST(balance + $2, 0) WHERE user_id = $1",
                winner.id, net
            )
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
            user_display = f"{interaction.user.display_name} 님"
            await log_to_channel(
                self.bot,
                f"🎰 [슬롯] {user_display}님 베팅 {bet}코인 시도"
            )
        except Exception:
            pass

        # 2) 심볼별 가중치 설정 (총합 100)
        symbols = ["🍒", "🍋", "🍀", "💎", "7️⃣"]
        weights = [50, 25, 15, 8, 2]
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
            "UPDATE coins SET balance = GREATEST(balance + $2, 0) WHERE user_id = $1",
            interaction.user.id, net
        )
        await self.bot.get_cog("Coins").refresh_leaderboard()

        # ▶ Log here: 슬롯 결과 기록
        try:
            user_display = f"{interaction.user.display_name} 님"
            sign = f"{net:+}" if net != 0 else "0"
            await log_to_channel(
                self.bot,
                f"🎰 [슬롯] {user_display}님 결과: {' '.join(roll)}, {outcome}, {sign}코인"
            )
        except Exception:
            pass

        # 6) 결과 전송
        await interaction.response.send_message(text)

    @app_commands.command(name="블랙잭", description="♠️ 딜러를 이기세요")
    @app_commands.describe(bet="베팅할 코인 수")
    @channel_only(config.BLACKJACK_CHANNEL_ID)
    async def blackjack(self, interaction: Interaction, bet: int):
        user_display = f"{interaction.user.display_name} 님"

        # ── 1) 잔액 체크 & 기록
        await log_to_channel(
            self.bot,
            f"♠️ [블랙잭] {user_display}님 베팅 {bet}코인 시도"
        )
        row = await self.bot.db.fetchrow(
            "SELECT balance FROM coins WHERE user_id = $1",
            interaction.user.id
        )
        balance = row["balance"] if row else 0
        if bet <= 0 or balance < bet:
            return await interaction.response.send_message(
                "❌ 유효하지 않은 베팅이거나 잔액이 부족합니다.", ephemeral=True
            )

        # ── 2) 스테이크 차감 (up front)
        await self.bot.db.execute(
            "UPDATE coins SET balance = balance - $2 WHERE user_id = $1",
            interaction.user.id, bet
        )
        await self.bot.get_cog("Coins").refresh_leaderboard()

        # ── 3) defer & 덱 준비
        await interaction.response.defer(thinking=True)
        ranks = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
        suits = ["♠️", "♥️", "♦️", "♣️"]
        deck = [r + s for r in ranks for s in suits]
        random.shuffle(deck)

        # ── 4) 핸드 초기화
        hands = [[deck.pop(), deck.pop()]]
        hand_bets = [bet]
        is_doubled = [False]
        current = 0
        dealer = [deck.pop(), deck.pop()]

        def hand_value(cards: list[str]) -> int:
            vals = {"J": 10, "Q": 10, "K": 10, "A": 11}
            total = aces = 0
            for c in cards:
                m = re.match(r'^(10|\d|[JQKA])', c)
                r = m.group(1)
                total += vals[r] if r in vals else int(r)
                if r == "A":
                    aces += 1
            while total > 21 and aces:
                total -= 10
                aces -= 1
            return total

        # ── 5) 초기 값 계산 & 로그
        values = [hand_value(hands[0])]
        dealer_val = hand_value(dealer)
        await log_to_channel(
            self.bot,
            f"♠️ [블랙잭] {user_display}님 시작: 플레이어 {values[0]}, 딜러 {dealer_val}"
        )

        # ── 6) 임베드 & 뷰 준비
        embed = discord.Embed(title="♠️ 블랙잭", color=discord.Color.dark_green())
        view = discord.ui.View(timeout=60)
        player = interaction.user

        async def update_embed():
            embed.clear_fields()
            for idx, hand in enumerate(hands, start=1):
                prefix = "▶ " if idx - 1 == current else ""
                embed.add_field(
                    name=f"{prefix}핸드 {idx}",
                    value=f"{' '.join(hand)} ({hand_value(hand)})",
                    inline=False
                )
            embed.add_field(name="딜러", value=dealer[0], inline=False)

        await update_embed()

        # ── 7) 자연 블랙잭 처리
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

            # natural payout: stake*2 (net +bet)
            await self.bot.db.execute(
                "UPDATE coins SET balance = balance + $2 WHERE user_id = $1",
                player.id, bet * 2
            )
            await self.bot.get_cog("Coins").refresh_leaderboard()
            return

        # ── 8) 버튼 정의 & 활성화 체크
        hit_btn = discord.ui.Button(label="히트", style=discord.ButtonStyle.primary)
        stand_btn = discord.ui.Button(label="스탠드", style=discord.ButtonStyle.secondary)
        dbl_btn = discord.ui.Button(label="더블다운", style=discord.ButtonStyle.success)
        split_btn = discord.ui.Button(label="스플릿", style=discord.ButtonStyle.danger)

        row2 = await self.bot.db.fetchrow(
            "SELECT balance FROM coins WHERE user_id = $1", player.id
        )
        bal2 = row2["balance"] if row2 else 0
        if bal2 < bet * 2:
            dbl_btn.disabled = True
            split_btn.disabled = True

        # ── 9) 히트 콜백
        async def hit_cb(i: Interaction):
            if i.user != player:
                return await i.response.send_message("❌ 본인만 사용할 수 있습니다.", ephemeral=True)
            hands[current].append(deck.pop())
            values[current] = hand_value(hands[current])
            await update_embed()
            if values[current] >= 21:
                return await stand_cb(i)
            await i.response.edit_message(embed=embed, view=view)

        # ──10) 스탠드 콜백
        async def stand_cb(i: Interaction):
            nonlocal current, dealer_val
            if i.user != player:
                return await i.response.send_message("❌ 본인만 사용할 수 있습니다.", ephemeral=True)
            # split 다음 핸드 이동
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

            # 정산
            summary = []
            for idx, hand in enumerate(hands, start=1):
                hv = hand_value(hand)
                stake = hand_bets[idx - 1]
                if hv > 21:
                    res, payout = "버스트", 0
                elif dealer_val > 21 or hv > dealer_val:
                    res, payout = "승리", stake * 2
                elif hv < dealer_val:
                    res, payout = "패배", 0
                else:
                    res, payout = "무승부", stake

                summary.append(f"핸드 {idx}: {res} ({payout - stake:+} 코인)")
                if payout > 0:
                    await self.bot.db.execute(
                        "UPDATE coins SET balance = GREATEST(balance + $2, 0) WHERE user_id = $1",
                        player.id, payout
                    )

            embed.title = "\n".join(summary)
            await i.response.edit_message(embed=embed, view=view)
            await self.bot.get_cog("Coins").refresh_leaderboard()

            user_display = f"{player.display_name} 님"
            await log_to_channel(
                self.bot,
                f"♠️ [블랙잭] {user_display}님 결과: {'; '.join(summary)}"
            )

        # ──11) 더블다운 콜백
        async def dbl_cb(i: Interaction):
            nonlocal current, dealer_val
            if i.user != player:
                return await i.response.send_message("❌ 본인만 사용할 수 있습니다.", ephemeral=True)
            if len(hands[current]) != 2:
                return await i.response.send_message("ℹ️ 첫 2장에서만 더블다운 가능합니다.", ephemeral=True)

            extra = hand_bets[current]
            row3 = await self.bot.db.fetchrow(
                "SELECT balance FROM coins WHERE user_id = $1", player.id
            )
            bal3 = row3["balance"] if row3 else 0
            if bal3 < extra:
                return await i.response.send_message("❌ 잔액이 부족하여 더블다운할 수 없습니다.", ephemeral=True)

            # 추가 베팅만 차감
            await self.bot.db.execute(
                "UPDATE coins SET balance = balance - $2 WHERE user_id = $1",
                player.id, extra
            )
            await self.bot.get_cog("Coins").refresh_leaderboard()

            is_doubled[current] = True
            hand_bets[current] *= 2
            hands[current].append(deck.pop())
            await update_embed()

            curr_val = hand_value(hands[current])
            if curr_val > 21:
                loss_amount = hand_bets[current]
                embed.title = f"💥 버스트! (-{loss_amount} 코인)"
                while hand_value(dealer) < 17:
                    dealer.append(deck.pop())
                dealer_score = hand_value(dealer)
                embed.set_field_at(
                    len(hands),
                    name="딜러",
                    value=f"{' '.join(dealer)} ({dealer_score})",
                    inline=False
                )
                for btn in view.children:
                    btn.disabled = True
                await i.response.edit_message(embed=embed, view=view)

                user_display = f"{player.display_name} 님"
                await log_to_channel(
                    self.bot,
                    f"♠️ [블랙잭] {user_display}님 더블다운 버스트 → -{loss_amount}코인"
                )
                return

            return await stand_cb(i)

        # ──12) 스플릿 콜백 (기존대로)
        async def split_cb(i: Interaction):
            if i.user != player:
                return await i.response.send_message("❌ 본인만 사용할 수 있습니다.", ephemeral=True)
            if len(hands) > 1:
                return await i.response.send_message("ℹ️ 이미 스플릿되었습니다.", ephemeral=True)
            r0 = re.match(r'^(10|\d|[JQKA])', hands[0][0]).group(1)
            r1 = re.match(r'^(10|\d|[JQKA])', hands[0][1]).group(1)
            if r0 != r1:
                return await i.response.send_message("ℹ️ 같은 값의 카드 두 장에서만 스플릿 가능합니다.", ephemeral=True)

            original = hand_bets[0]
            row4 = await self.bot.db.fetchrow(
                "SELECT balance FROM coins WHERE user_id = $1", player.id
            )
            bal4 = row4["balance"] if row4 else 0
            if bal4 < original:
                return await i.response.send_message("❌ 잔액이 부족하여 스플릿할 수 없습니다.", ephemeral=True)

            await self.bot.db.execute(
                "UPDATE coins SET balance = balance - $2 WHERE user_id = $1",
                player.id, original
            )
            await self.bot.get_cog("Coins").refresh_leaderboard()

            c1, c2 = hands[0]
            hands[:] = [[c1, deck.pop()], [c2, deck.pop()]]
            hand_bets[:] = [original, original]
            is_doubled[:] = [False, False]

            await update_embed()
            await i.response.edit_message(embed=embed, view=view)

        # ──13) 뷰에 콜백 연결 & 전송
        hit_btn.callback = hit_cb
        stand_btn.callback = stand_cb
        dbl_btn.callback = dbl_cb
        split_btn.callback = split_cb

        view.add_item(hit_btn)
        view.add_item(stand_btn)
        view.add_item(dbl_btn)
        view.add_item(split_btn)

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

        # ▶ Log here: 동전 뒤집기 도전 기록
        try:
            user_display = f"{interaction.user.display_name} 님"
            await log_to_channel(
                self.bot,
                f"🔀 [동전뒤집기] {user_display}님 베팅 {bet}코인, 선택={side.value}"
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
            "UPDATE coins SET balance = GREATEST(balance + $2, 0) WHERE user_id = $1",
            interaction.user.id, net
        )

        # ▶ Log here: 동전 뒤집기 결과 기록
        try:
            user_display = f"{interaction.user.display_name} 님"
            sign = f"{net:+}" if net != 0 else "0"
            await log_to_channel(
                self.bot,
                f"🔀 [동전뒤집기] {user_display}님 결과: {flip}, {sign}코인"
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
            user_display = f"{interaction.user.display_name} 님"
            opp_display = f"{opponent.display_name} 님"
            await log_to_channel(
                self.bot,
                f"🎲 [주사위대결] {user_display}님이 {opp_display}님에게 {bet}코인으로 도전"
            )
        except Exception:
            pass

    @app_commands.command(
        name="가위바위보",
        description="✌️✊🖐️ 버튼으로 봇과 가위바위보! 이기면 2코인 획득"
    )
    @app_commands.checks.cooldown(1, 60, key=lambda i: i.user.id)
    @channel_only(config.RPC_CHANNEL_ID)
    async def rps(self, interaction: Interaction):
        """버튼으로 가위바위보를 시작합니다."""
        view = RPSView(interaction.user, self.bot)
        await interaction.response.send_message(
            f"{interaction.user.mention} 가위바위보! 버튼을 눌러 선택하세요.",
            view=view,
            allowed_mentions=AllowedMentions.none()
        )

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

    @app_commands.command(
        name="룰렛",
        description="🎡 실제 유럽식 룰렛 (0–36 숫자 또는 색상:red,black,green)"
    )
    @app_commands.describe(
        bet="베팅할 코인 수",
        guess="0–36 숫자 또는 🔴red ⚫black 🟢green"
    )
    @channel_only(config.ROULETTE_CHANNEL_ID)
    async def roulette(
        self,
        interaction: Interaction,
        bet: int,
        guess: str
    ):
        # 1) 잔액 확인
        row = await self.bot.db.fetchrow(
            "SELECT balance FROM coins WHERE user_id=$1",
            interaction.user.id
        )
        bal = row["balance"] if row else 0
        if bet <= 0 or bal < bet:
            return await interaction.response.send_message(
                "❌ 베팅 금액이 유효하지 않거나 잔액이 부족합니다.", ephemeral=True
            )

        # 2) 판정: 숫자 or 색상
        g = guess.strip().lower()
        is_number = g.isdigit() and 0 <= int(g) <= 36
        if is_number:
            target_num = int(g)
            payout_mult = 35
        elif g in ("red", "black", "green"):
            target_color = g
            payout_mult = 1
        else:
            return await interaction.response.send_message(
                "❌ 올바른 숫자(0–36)나 색상(red, black, green)을 입력해주세요.", ephemeral=True
            )

        # 3) 스핀: 0–36
        spin = random.randint(0, 36)
        if spin == 0:
            spin_color = "green"
        elif spin in RED_NUMBERS:
            spin_color = "red"
        else:
            spin_color = "black"

        # 4) 결과 계산
        if is_number:
            if spin == target_num:
                net = bet * payout_mult
                text = f"🎡 룰렛 결과: **{spin}** ({spin_color})\n✅ 숫자 맞추기 성공! +**{net}** 코인"
            else:
                net = -bet
                text = f"🎡 룰렛 결과: **{spin}** ({spin_color})\n❌ 숫자 맞추기 실패... -**{bet}** 코인"
        else:
            if spin_color == target_color:
                net = bet * payout_mult
                text = f"🎡 룰렛 결과: **{spin}** ({spin_color})\n✅ 색상 맞추기 성공! +**{net}** 코인"
            else:
                net = -bet
                text = f"🎡 룰렛 결과: **{spin}** ({spin_color})\n❌ 색상 맞추기 실패... -**{bet}** 코인"

        # 5) DB 업데이트 및 리더보드 갱신
        await self.bot.db.execute(
            "UPDATE coins SET balance = GREATEST(balance + $2, 0) WHERE user_id = $1",
            interaction.user.id, net
        )
        await self.bot.get_cog("Coins").refresh_leaderboard()

        # ▶ Log here: 룰렛 결과 기록
        user_display = f"{interaction.user.display_name} 님"
        sign = f"{net:+}" if net != 0 else "0"
        await log_to_channel(
            self.bot,
            f"🎡 [룰렛] {user_display}님 베팅 {bet}코인, 선택={guess} → {spin}({spin_color}), {sign}코인"
        )

        # 6) Generate + send spin GIF as your initial interaction response
        wheel = draw_roulette_wheel(400)
        gif = make_spin_gif(wheel, spin)  # spin is your 0–36 result
        file = discord.File(gif, "roulette_spin.gif")
        embed = discord.Embed(title="🎡 룰렛 스핀 중…", color=discord.Color.blue())
        embed.set_image(url="attachment://roulette_spin.gif")
        await interaction.response.send_message(embed=embed, file=file)

        # 7) Follow up with the text result
        await asyncio.sleep(2)
        await interaction.followup.send(text)


async def setup(bot: commands.Bot):
    await bot.add_cog(Casino(bot))
