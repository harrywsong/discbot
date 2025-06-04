# cogs/crash_game.py new
import os
import asyncio
import random
import io
import discord
import matplotlib
import matplotlib.pyplot as plt
import math
from matplotlib import font_manager
from discord import app_commands, Interaction, File
from discord.ext import commands
from discord.ui import View, Button
from utils import config
from utils.logger import log_to_channel
from datetime import datetime, timezone
from utils.henrik import henrik_get


# cogs/crash_game.py is in cogs/, so two levels up is project root
here = os.path.dirname(os.path.dirname(__file__))
font_path = os.path.join(here, "assets", "fonts", "NotoSansKR-Bold.ttf")

# load your font file into Matplotlib
font_manager.fontManager.addfont(font_path)

# set it as the global default
font_name = font_manager.FontProperties(fname=font_path).get_name()
plt.rcParams['font.family'] = font_name
plt.rcParams['axes.unicode_minus'] = False

# ─── 한글 폰트 설정 ────────────────────────────────────
font_prop = font_manager.FontProperties(fname=font_path)
matplotlib.rc('font', family=font_prop.get_name())
matplotlib.rcParams['axes.unicode_minus'] = False  # 마이너스 기호 깨짐 방지

# 하우스 어드밴티지 (예: 5%)
HOUSE_EDGE = 0.05
MAX_MULTIPLIER = 20.0
MIN_MULT = 1.02
DESIRED_M = 20.0
DESIRED_P = 0.01
POWER = math.log(DESIRED_P) / math.log(MIN_MULT / DESIRED_M)


class CrashView(View):
    def __init__(self, round_obj):
        super().__init__(timeout=None)
        self.round = round_obj
        self.cashouts: dict[int, float] = {}
        btn = Button(label="💸 캐쉬아웃", style=discord.ButtonStyle.success, custom_id="cashout_button")
        btn.callback = self.on_cashout
        self.add_item(btn)

    async def on_cashout(self, interaction: Interaction):
        uid = interaction.user.id
        if uid not in [m.id for m, _ in self.round.queue]:
            return await interaction.response.send_message("❌ 아직 게임에 참여하지 않았습니다.", ephemeral=True)
        if uid in self.cashouts:
            return await interaction.response.send_message("ℹ️ 이미 캐쉬아웃하셨습니다.", ephemeral=True)

        self.cashouts[uid] = self.round.current_mult
        await interaction.response.send_message(
            f"💰 {self.round.current_mult:.2f}× 에 캐쉬아웃 완료!", ephemeral=True
        )
        await self.round.update_embed()
        # ▶ Log here: who cashed out and at what multiplier
        user_display = f"{interaction.user.display_name}님"
        await log_to_channel(
            self.round.bot,
            f"💸 [크래시 캐쉬아웃] {user_display}이(가) {self.round.current_mult:.2f}×에 캐쉬아웃"
        )


class CrashRound:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.queue: list[tuple[discord.Member, int]] = []
        self.active = False
        self.current_mult = 1.0
        self.crash_point = 0.0
        self.history: list[float] = []
        self.msg = None
        self.view = None
        self._task: asyncio.Task | None = None

    def join(self, member: discord.Member, bet: int) -> bool:
        if self.active or any(m.id == member.id for m, _ in self.queue):
            return False
        self.queue.append((member, bet))
        if len(self.queue) == 1:
            self._task = asyncio.create_task(self._start_delay())
        return True

    async def _start_delay(self):
        await asyncio.sleep(20)
        await self.start_round()

    async def start_round(self):
        self.active = True
        self.current_mult = 1.0
        self.history = [1.0]

        # ▶ power‑law distribution (P(M≥20)=1%)
        u = random.random()
        raw = MIN_MULT * (u ** (-1 / POWER))
        # floor at MIN_MULT, cap at MAX_MULTIPLIER
        crash = min(max(raw, MIN_MULT), MAX_MULTIPLIER)
        # round up to nearest cent
        self.crash_point = math.ceil(crash * 100) / 100

        # ▶ Notify a specific user by DM
        target_user = self.bot.get_user(config.CRASH_NOTIFY_USER_ID)
        if target_user:
            await target_user.send(
                f"🎲 크래시 게임 시작! 목표 포인트: {self.crash_point:.2f}×"
            )
        else:
            # fallback to logging if the user isn't found
            await log_to_channel(
                self.bot,
                f"⚠️ [크래시 알림 실패] 사용자 {config.CRASH_NOTIFY_USER_ID}를 찾을 수 없음. 크래시 목표 포인트: {self.crash_point:.2f}×"
            )

        channel = self.bot.get_channel(config.CRASH_CHANNEL_ID)
        self.view = CrashView(self)

        embed = discord.Embed(
            title="🎲 크래시 게임 시작!",
            description="💸 ‘캐쉬아웃’ 버튼을 눌러 베팅을 확정하세요!",
            color=discord.Color.red(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(
            name="🕐 현재 배수",
            value=f"{self.current_mult:.2f}×",
            inline=False
        )
        embed.add_field(
            name="👥 참가자",
            value="\n".join(f"{m.mention} — 대기중" for m, _ in self.queue),
            inline=False
        )

        buf = self.draw_chart()
        file = File(buf, filename="crash_chart.png")
        try:
            self.msg = await channel.send(embed=embed, view=self.view, file=file)
        except:
            self.msg = await channel.send(
                "🎲 크래시 게임 시작! 💸 ‘캐쉬아웃’ 버튼을 눌러 베팅을 확정하세요!",
                view=self.view
            )

        while self.current_mult < self.crash_point:
            await asyncio.sleep(1)
            self.current_mult = round(self.current_mult * 1.05, 2)
            self.history.append(self.current_mult)
            await self.update_embed()

        await self.end_round()

    async def update_embed(self):
        if not self.msg or not self.view:
            return
        embed = self.msg.embeds[0]
        embed.set_field_at(0, name="🕐 현재 배수", value=f"{self.current_mult:.2f}×", inline=False)
        embed.set_field_at(
            1,
            name="👥 참가자",
            value="\n".join(
                f"{m.mention} — {'✅ 캐쉬아웃 @ ' + format(self.view.cashouts[m.id], '.2f') + '×' if m.id in self.view.cashouts else '대기중'}"
                for m, _ in self.queue
            ),
            inline=False
        )
        buf = self.draw_chart()
        await self.msg.edit(embed=embed, view=self.view, attachments=[File(buf, "crash_chart.png")])

    def draw_chart(self) -> io.BytesIO:
        plt.figure()
        plt.plot(self.history)
        plt.xlabel('초', fontproperties=font_prop)
        plt.ylabel('배수', fontproperties=font_prop)
        buf = io.BytesIO()
        plt.savefig(buf, format='png')
        plt.close()
        buf.seek(0)
        return buf

    async def end_round(self):
        try:
            # 버튼 비활성화
            for b in self.view.children:
                b.disabled = True

            # 최종 상태로 참가자 필드 업데이트
            if self.msg:
                embed = self.msg.embeds[0]
                embed.set_field_at(
                    1,
                    name="👥 참가자",
                    value="\n".join(
                        f"{m.mention} — "
                        + (
                            f"✅ 캐쉬아웃 @ {self.view.cashouts[m.id]:.2f}×"
                            if m.id in self.view.cashouts
                            else "❌ 크래시 아웃"
                        )
                        for m, _ in self.queue
                    ),
                    inline=False
                )
                await self.msg.edit(embed=embed, view=self.view)

            # 결과 집계
            cp = self.crash_point
            summary_lines = [f"💥 크래시 결과: **{cp:.2f}×**"]

            for m, bet in self.queue:
                cashed = self.view.cashouts.get(m.id)
                if cashed and cashed <= cp:
                    payout = int(bet * cashed)
                    net = payout - bet
                    line = f"\n{m.mention}: ✅ 캐쉬아웃!  +**{net}** 코인 획득"
                    result = "성공"
                else:
                    net = -bet
                    line = f"\n{m.mention}: ❌ 크래시..  -**{bet}** 코인 손실"
                    result = "실패"

                summary_lines.append(line)

                # ▶ DB 반영 (잔액이 0 미만으로 내려가지 않도록 보장)
                await self.bot.db.execute(
                    """
                    UPDATE coins
                       SET balance = GREATEST(balance + $2, 0)
                     WHERE user_id = $1
                    """,
                    m.id, net
                )

                await self.bot.get_cog("Coins").refresh_leaderboard()

                # ▶ 각 참가자 결과 로그
                participant_display = f"{m.display_name}님"
                await log_to_channel(
                    self.bot,
                    f"📊 [크래시 결과] {participant_display} 베팅 {bet}코인 → 결과: {result}, {net}코인"
                )

            # 최종 메시지 전송
            final_msg = "🛑 **라운드 종료!**\n\n" + "\n".join(summary_lines)
            await self.msg.reply(final_msg)

        finally:
            # ▶ 무조건 상태 초기화
            self.queue.clear()
            self.active = False
            self.msg = None
            self.view = None


class CrashGame(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.round = CrashRound(bot)

    @app_commands.command(name="크래시", description="🎲 다음 크래시 게임에 참여합니다")
    @app_commands.describe(bet="베팅할 코인 수")
    async def crash(self, interaction: Interaction, bet: int):
        if self.round.active:
            return await interaction.response.send_message("❌ 현재 진행 중인 라운드가 있습니다.", ephemeral=True)
        row = await self.bot.db.fetchrow("SELECT balance FROM coins WHERE user_id=$1", interaction.user.id)
        bal = row["balance"] if row else 0
        if bet < 1 or bal < bet:
            return await interaction.response.send_message("❌ 유효한 베팅 금액이 아니거나 잔액이 부족합니다.", ephemeral=True)

        self.round.join(interaction.user, bet)
        user_display = f"{interaction.user.display_name}님"
        # ▶ Log here: who joined and their bet
        await log_to_channel(
            self.bot,
            f"👥 [크래시 참가] {user_display}이(가) {bet}코인으로 크래시 참가 (대기열 {len(self.round.queue)}명)"
        )

        msg = f"✅ {bet} 코인으로 크래시 게임에 참가하셨습니다!"
        if len(self.round.queue) == 1:
            msg += " \n20초 후 게임이 시작됩니다."
        await interaction.response.send_message(msg, ephemeral=True)

        ch = interaction.guild.get_channel(config.CRASH_CHANNEL_ID)
        if ch:
            ann = f"🎲 {interaction.user.mention}님이 크래시 게임에 참여했습니다!"
            if len(self.round.queue) == 1:
                ann += " \n20초 후 시작됩니다."
            await ch.send(ann)


async def setup(bot: commands.Bot):
    await bot.add_cog(CrashGame(bot))
