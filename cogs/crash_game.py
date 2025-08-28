# cogs/crash_game.py - Fixed memory leaks and better resource management

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
import weakref
import gc

# Font setup (same as before)
here = os.path.dirname(os.path.dirname(__file__))
font_path = os.path.join(here, "assets", "fonts", "NotoSansKR-Bold.ttf")

font_manager.fontManager.addfont(font_path)
font_name = font_manager.FontProperties(fname=font_path).get_name()
plt.rcParams['font.family'] = font_name
plt.rcParams['axes.unicode_minus'] = False

font_prop = font_manager.FontProperties(fname=font_path)
matplotlib.rc('font', family=font_prop.get_name())
matplotlib.rcParams['axes.unicode_minus'] = False

# Constants
HOUSE_EDGE = 0.05
MAX_MULTIPLIER = 20.0
MIN_MULT = 1.02
DESIRED_M = 20.0
DESIRED_P = 0.01
POWER = math.log(DESIRED_P) / math.log(MIN_MULT / DESIRED_M)


class CrashView(View):
    def __init__(self, round_obj):
        super().__init__(timeout=300)  # 5 minute timeout instead of None
        self.round = weakref.ref(round_obj)  # Use weak reference to prevent memory leaks
        self.cashouts: dict[int, float] = {}
        btn = Button(label="💸 캐쉬아웃", style=discord.ButtonStyle.success, custom_id="cashout_button")
        btn.callback = self.on_cashout
        self.add_item(btn)

    async def on_cashout(self, interaction: Interaction):
        round_obj = self.round()
        if not round_obj:
            return await interaction.response.send_message("❌ 게임이 더 이상 활성화되지 않았습니다.", ephemeral=True)

        uid = interaction.user.id
        if uid not in [m.id for m, _ in round_obj.queue]:
            return await interaction.response.send_message("❌ 아직 게임에 참여하지 않았습니다.", ephemeral=True)
        if uid in self.cashouts:
            return await interaction.response.send_message("ℹ️ 이미 캐쉬아웃하셨습니다.", ephemeral=True)

        self.cashouts[uid] = round_obj.current_mult
        await interaction.response.send_message(
            f"💰 {round_obj.current_mult:.2f}× 에 캐쉬아웃 완료!", ephemeral=True
        )
        await round_obj.update_embed()

        user_display = f"{interaction.user.display_name}님"
        await log_to_channel(
            round_obj.bot,
            f"💸 [크래시 캐쉬아웃] {user_display}이(가) {round_obj.current_mult:.2f}×에 캐쉬아웃"
        )

    async def on_timeout(self):
        """Clean up when view times out"""
        for item in self.children:
            item.disabled = True


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
        self._cleanup_scheduled = False

    def join(self, member: discord.Member, bet: int) -> bool:
        if self.active or any(m.id == member.id for m, _ in self.queue):
            return False
        self.queue.append((member, bet))
        if len(self.queue) == 1:
            self._task = asyncio.create_task(self._start_delay())
        return True

    async def _start_delay(self):
        try:
            await asyncio.sleep(20)
            if not self.active:  # Check if still valid
                await self.start_round()
        except asyncio.CancelledError:
            pass  # Task was cancelled, cleanup will happen elsewhere

    async def start_round(self):
        if self.active:
            return  # Already started

        self.active = True
        self.current_mult = 1.0
        self.history = [1.0]

        # Generate crash point
        u = random.random()
        raw = MIN_MULT * (u ** (-1 / POWER))
        crash = min(max(raw, MIN_MULT), MAX_MULTIPLIER)
        self.crash_point = math.ceil(crash * 100) / 100

        # Notify target user
        target_user = self.bot.get_user(config.CRASH_NOTIFY_USER_ID)
        if target_user:
            try:
                await target_user.send(
                    f"🎲 크래시 게임 시작! 목표 포인트: {self.crash_point:.2f}×"
                )
            except discord.Forbidden:
                await log_to_channel(
                    self.bot,
                    f"⚠️ [크래시 알림] DM 전송 실패 - {target_user.display_name}님의 DM이 차단됨"
                )
        else:
            await log_to_channel(
                self.bot,
                f"⚠️ [크래시 알림 실패] 사용자 {config.CRASH_NOTIFY_USER_ID}를 찾을 수 없음"
            )

        channel = self.bot.get_channel(config.CRASH_CHANNEL_ID)
        if not channel:
            await log_to_channel(self.bot, "❌ [크래시] 채널을 찾을 수 없음")
            await self.cleanup()
            return

        self.view = CrashView(self)

        embed = discord.Embed(
            title="🎲 크래시 게임 시작!",
            description="💸 '캐쉬아웃' 버튼을 눌러 베팅을 확정하세요!",
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

        try:
            buf = self.draw_chart()
            file = File(buf, filename="crash_chart.png")
            self.msg = await channel.send(embed=embed, view=self.view, file=file)
        except Exception as e:
            await log_to_channel(self.bot, f"❌ [크래시] 메시지 전송 실패: {e}")
            try:
                self.msg = await channel.send(
                    "🎲 크래시 게임 시작! 💸 '캐쉬아웃' 버튼을 눌러 베팅을 확정하세요!",
                    view=self.view
                )
            except Exception as e2:
                await log_to_channel(self.bot, f"❌ [크래시] 폴백 메시지도 실패: {e2}")
                await self.cleanup()
                return

        # Game loop with better error handling
        try:
            while self.current_mult < self.crash_point and self.active:
                await asyncio.sleep(1)
                self.current_mult = round(self.current_mult * 1.05, 2)
                self.history.append(self.current_mult)
                await self.update_embed()
        except asyncio.CancelledError:
            await log_to_channel(self.bot, "⚠️ [크래시] 게임 루프가 취소됨")
            return
        except Exception as e:
            await log_to_channel(self.bot, f"❌ [크래시] 게임 루프 오류: {e}")
        finally:
            await self.end_round()

    async def update_embed(self):
        if not self.msg or not self.view or not self.active:
            return

        try:
            embed = self.msg.embeds[0] if self.msg.embeds else discord.Embed(title="🎲 크래시 게임")
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
            if buf:
                await self.msg.edit(embed=embed, view=self.view, attachments=[File(buf, "crash_chart.png")])
            else:
                await self.msg.edit(embed=embed, view=self.view)
        except discord.NotFound:
            await log_to_channel(self.bot, "⚠️ [크래시] 메시지가 삭제됨, 게임 종료")
            await self.cleanup()
        except Exception as e:
            await log_to_channel(self.bot, f"❌ [크래시] 임베드 업데이트 실패: {e}")

    def draw_chart(self) -> io.BytesIO | None:
        """Draw chart with proper memory management"""
        try:
            fig, ax = plt.subplots(figsize=(8, 4))
            ax.plot(self.history)
            ax.set_xlabel('초', fontproperties=font_prop)
            ax.set_ylabel('배수', fontproperties=font_prop)
            ax.set_title(f'현재: {self.current_mult:.2f}×', fontproperties=font_prop)

            buf = io.BytesIO()
            plt.savefig(buf, format='png', bbox_inches='tight', dpi=100)
            plt.close(fig)  # Explicitly close figure
            buf.seek(0)

            # Force garbage collection
            gc.collect()

            return buf
        except Exception as e:
            # Log error synchronously to avoid await in sync function
            print(f"❌ [크래시] 차트 생성 실패: {e}")
            # Schedule async logging (wrapped in try/except to prevent task creation errors)
            try:
                asyncio.create_task(log_to_channel(self.bot, f"❌ [크래시] 차트 생성 실패: {e}"))
            except Exception:
                pass  # If we can't even schedule the logging, just continue
            return None

    async def end_round(self):
        if not self.active:
            return  # Already ended

        try:
            # Disable buttons
            if self.view:
                for b in self.view.children:
                    b.disabled = True

            # Update final message
            if self.msg and self.msg.embeds:
                try:
                    embed = self.msg.embeds[0]
                    embed.set_field_at(
                        1,
                        name="👥 참가자",
                        value="\n".join(
                            f"{m.mention} — "
                            + (
                                f"✅ 캐쉬아웃 @ {self.view.cashouts[m.id]:.2f}×"
                                if self.view and m.id in self.view.cashouts
                                else "❌ 크래시 아웃"
                            )
                            for m, _ in self.queue
                        ),
                        inline=False
                    )
                    await self.msg.edit(embed=embed, view=self.view)
                except Exception as e:
                    await log_to_channel(self.bot, f"⚠️ [크래시] 최종 메시지 업데이트 실패: {e}")

            # Process results
            cp = self.crash_point
            summary_lines = [f"💥 크래시 결과: **{cp:.2f}×**"]

            for m, bet in self.queue:
                cashed = self.view.cashouts.get(m.id) if self.view else None
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

                # Update database with error handling
                try:
                    await self.bot.db.execute(
                        "UPDATE coins SET balance = GREATEST(balance + $2, 0) WHERE user_id = $1",
                        m.id, net
                    )

                    coins_cog = self.bot.get_cog("Coins")
                    if coins_cog:
                        await coins_cog.refresh_leaderboard()
                except Exception as e:
                    await log_to_channel(self.bot, f"❌ [크래시] DB 업데이트 실패: {e}")

                # Log results
                participant_display = f"{m.display_name}님"
                await log_to_channel(
                    self.bot,
                    f"📊 [크래시 결과] {participant_display} 베팅 {bet}코인 → 결과: {result}, {net:+}코인"
                )

            # Send final message
            if self.msg:
                try:
                    final_msg = "🛑 **라운드 종료!**\n\n" + "\n".join(summary_lines)
                    await self.msg.reply(final_msg)
                except Exception as e:
                    await log_to_channel(self.bot, f"⚠️ [크래시] 최종 결과 전송 실패: {e}")

        except Exception as e:
            await log_to_channel(self.bot, f"❌ [크래시] 라운드 종료 중 오류: {e}")
        finally:
            await self.cleanup()

    async def cleanup(self):
        """Clean up resources"""
        if self._cleanup_scheduled:
            return
        self._cleanup_scheduled = True

        self.active = False
        self.queue.clear()
        self.history.clear()

        if self._task and not self._task.done():
            self._task.cancel()

        if self.view:
            self.view.stop()
            self.view = None

        self.msg = None

        # Force garbage collection
        gc.collect()


class CrashGame(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.round = CrashRound(bot)

    async def cog_unload(self):
        """Clean up when cog is unloaded"""
        if self.round:
            await self.round.cleanup()

    @app_commands.command(name="크래시", description="🎲 다음 크래시 게임에 참여합니다")
    @app_commands.describe(bet="베팅할 코인 수")
    async def crash(self, interaction: Interaction, bet: int):
        if self.round.active:
            return await interaction.response.send_message("❌ 현재 진행 중인 라운드가 있습니다.", ephemeral=True)

        try:
            row = await self.bot.db.fetchrow("SELECT balance FROM coins WHERE user_id=$1", interaction.user.id)
            bal = row["balance"] if row else 0
        except Exception as e:
            await log_to_channel(self.bot, f"❌ [크래시] DB 조회 실패: {e}")
            return await interaction.response.send_message("❌ 잔액 조회 중 오류가 발생했습니다.", ephemeral=True)

        if bet < 1 or bal < bet:
            return await interaction.response.send_message("❌ 유효한 베팅 금액이 아니거나 잔액이 부족합니다.", ephemeral=True)

        if not self.round.join(interaction.user, bet):
            return await interaction.response.send_message("❌ 게임 참가에 실패했습니다.", ephemeral=True)

        user_display = f"{interaction.user.display_name}님"
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
            try:
                ann = f"🎲 {interaction.user.mention}님이 크래시 게임에 참여했습니다!"
                if len(self.round.queue) == 1:
                    ann += " \n20초 후 시작됩니다."
                await ch.send(ann)
            except Exception as e:
                await log_to_channel(self.bot, f"⚠️ [크래시] 참가 공지 실패: {e}")


async def setup(bot: commands.Bot):
    await bot.add_cog(CrashGame(bot))