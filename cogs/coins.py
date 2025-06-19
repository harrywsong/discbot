# cogs/coins.py

import discord
import asyncio
import pytz
from discord.ext import commands
from discord import app_commands
from discord.ui import View, Button
from datetime import datetime, timedelta, timezone
from utils import config
from utils.logger import log_to_channel

# Database table creation SQL
CREATE_COINS_TABLE_SQL = """
                         CREATE TABLE IF NOT EXISTS coins \
                         ( \
                             user_id \
                             BIGINT \
                             PRIMARY \
                             KEY, \
                             balance \
                             BIGINT \
                             NOT \
                             NULL \
                             DEFAULT \
                             0
                         ); \
                         """

CREATE_DAILY_CLAIM_TABLE_SQL = """
                               CREATE TABLE IF NOT EXISTS daily_coin_claim \
                               ( \
                                   user_id \
                                   BIGINT \
                                   PRIMARY \
                                   KEY, \
                                   last_claim \
                                   TIMESTAMPTZ \
                                   NOT \
                                   NULL
                               ); \
                               """


class LeaderboardView(View):
    def __init__(self, cog, page=0, per_page=10):
        super().__init__(timeout=None)
        self.cog = cog
        self.page = page
        self.per_page = per_page

    async def update_embed(self, interaction: discord.Interaction):
        embed = await self.cog.build_leaderboard_embed(page=self.page, per_page=self.per_page)
        await interaction.response.edit_message(
            embed=embed,
            view=LeaderboardView(self.cog, page=self.page, per_page=self.per_page)
        )

    @discord.ui.button(label="⏮️", style=discord.ButtonStyle.secondary, custom_id="prev_page")
    async def prev_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
            await self.update_embed(interaction)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="⏭️", style=discord.ButtonStyle.secondary, custom_id="next_page")
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        total_count = await self.cog.bot.db.fetchval("SELECT COUNT(*) FROM coins")
        max_page = (total_count - 1) // self.per_page
        if self.page < max_page:
            self.page += 1
            await self.update_embed(interaction)
        else:
            await interaction.response.defer()


class DailyCoinsView(View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="오늘의 코인 받기", style=discord.ButtonStyle.primary, custom_id="dailycoins_button")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        user = interaction.user
        now_utc = datetime.now(timezone.utc)
        eastern = pytz.timezone("America/New_York")
        today_et = now_utc.astimezone(eastern).date()

        try:
            # Check last claim
            row = await self.bot.db.fetchrow(
                "SELECT last_claim FROM daily_coin_claim WHERE user_id = $1",
                user.id
            )
            if row and row["last_claim"].astimezone(eastern).date() == today_et:
                now_et = now_utc.astimezone(eastern)
                next_midnight = (now_et + timedelta(days=1)).replace(
                    hour=0, minute=0, second=0, microsecond=0
                )
                delta = next_midnight - now_et
                hrs, rem = divmod(int(delta.total_seconds()), 3600)
                mins = rem // 60
                await interaction.followup.send(
                    f"⏳ 이미 오늘의 보상을 받으셨습니다. 다음 보상은 `{hrs}시간 {mins}분` 후 자정(12AM 동부 시간)에 리셋됩니다.",
                    ephemeral=True
                )
                return

            # Grant coins in transaction
            async with self.bot.db.acquire() as conn:
                async with conn.transaction():
                    amount = config.DAILY_COINS_AMOUNT
                    await conn.execute(
                        """
                        INSERT INTO coins (user_id, balance)
                        VALUES ($1, $2) ON CONFLICT (user_id) DO
                        UPDATE SET balance = coins.balance + EXCLUDED.balance
                        """,
                        user.id, amount
                    )
                    await conn.execute(
                        """
                        INSERT INTO daily_coin_claim (user_id, last_claim)
                        VALUES ($1, $2) ON CONFLICT (user_id) DO
                        UPDATE SET last_claim = EXCLUDED.last_claim
                        """,
                        user.id, now_utc
                    )

            await interaction.followup.send(
                f"✅ 오늘의 **{amount}** 코인을 받으셨습니다!", ephemeral=True
            )
            await log_to_channel(
                self.bot,
                f"🎁 [오늘의 코인] {user.display_name}님이 {amount}코인 수령"
            )

            # Refresh leaderboard
            coins_cog = self.bot.get_cog("Coins")
            if coins_cog:
                await coins_cog.refresh_leaderboard()

        except Exception as e:
            await interaction.followup.send(
                f"❌ 오류 발생: {str(e)}", ephemeral=True
            )
            await log_to_channel(self.bot, f"❌ [코인 지급 오류] {e}")


class Coins(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._setup_done = False
        self._last_leaderboard_update = None
        self._leaderboard_cache = None
        self._update_lock = asyncio.Lock()
        self._backoff_time = 5
        self._leaderboard_message = None

    @commands.Cog.listener()
    async def on_ready(self):
        if self._setup_done:
            return

        while not hasattr(self.bot, "db") or self.bot.db is None:
            await asyncio.sleep(0.1)

        self._setup_done = True

        async with self.bot.db.acquire() as conn:
            await conn.execute(CREATE_COINS_TABLE_SQL)
            await conn.execute(CREATE_DAILY_CLAIM_TABLE_SQL)

        coin_ch = self.bot.get_channel(config.DAILY_COINS_CHANNEL_ID)
        if not coin_ch:
            return

        try:
            await coin_ch.purge(limit=10)
        except Exception as e:
            await log_to_channel(self.bot, f"⚠️ 메시지 정리 실패: {e}")

        try:
            lb_embed = await self.build_leaderboard_embed()
            self._leaderboard_message = await coin_ch.send(
                embed=lb_embed,
                view=LeaderboardView(self)
            )
            config.COIN_LEADERBOARD_MESSAGE_ID = self._leaderboard_message.id
        except Exception as e:
            await log_to_channel(self.bot, f"❌ 리더보드 생성 실패: {e}")

        try:
            btn_embed = discord.Embed(
                title="🎁 오늘의 코인 받기",
                description="아래 버튼을 눌러 오늘의 코인을 받으세요!",
                color=discord.Color.gold()
            )
            view = DailyCoinsView(self.bot)
            await coin_ch.send(embed=btn_embed, view=view)
        except Exception as e:
            await log_to_channel(self.bot, f"❌ 코인 버튼 생성 실패: {e}")

    async def build_leaderboard_embed(self, page=0, per_page=10) -> discord.Embed:
        offset = page * per_page
        try:
            total_count = await self.bot.db.fetchval("SELECT COUNT(*) FROM coins")
            rows = await self.bot.db.fetch(
                "SELECT user_id, balance FROM coins ORDER BY balance DESC LIMIT $1 OFFSET $2",
                per_page, offset
            )
        except Exception as e:
            await log_to_channel(self.bot, f"❌ 리더보드 조회 오류: {e}")
            rows = []

        embed = discord.Embed(
            title=f"🏆 코인 리더보드 (Top {offset + 1}-{offset + len(rows)})",
            color=discord.Color.gold()
        )

        if not rows:
            embed.description = "아직 코인을 획득한 유저가 없습니다."
        else:
            lines = [
                f"**{idx}.** <@{r['user_id']}> — {r['balance']} 코인"
                for idx, r in enumerate(rows, start=offset + 1)
            ]
            embed.description = "\n".join(lines)

        max_page = max(0, (total_count - 1) // per_page)
        embed.set_footer(text=f"페이지 {page + 1}/{max_page + 1} | 업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return embed

    async def refresh_leaderboard(self, force=False):
        if self._update_lock.locked():
            return

        async with self._update_lock:
            try:
                current_time = datetime.now(timezone.utc)
                if not force and self._last_leaderboard_update and \
                        (current_time - self._last_leaderboard_update).total_seconds() < 300:
                    return

                coin_ch = self.bot.get_channel(config.DAILY_COINS_CHANNEL_ID)
                if not coin_ch:
                    return

                embed = await self.build_leaderboard_embed()

                if not force and self._leaderboard_cache and self._leaderboard_cache == embed.description:
                    return

                self._leaderboard_cache = embed.description

                if self._leaderboard_message:
                    try:
                        await self._leaderboard_message.edit(
                            embed=embed,
                            view=LeaderboardView(self)
                        )
                        self._last_leaderboard_update = current_time
                        return
                    except discord.NotFound:
                        self._leaderboard_message = None
                    except discord.HTTPException as e:
                        if e.status == 429 or e.code == 30046:
                            try:
                                await coin_ch.purge(limit=10)
                                await log_to_channel(self.bot, "♻️ Rate limit hit - cleared old messages")
                            except Exception as purge_error:
                                await log_to_channel(self.bot, f"⚠️ Failed to purge: {purge_error}")

                            await asyncio.sleep(self._backoff_time)
                            self._backoff_time = min(60, self._backoff_time * 2)
                            self._leaderboard_message = await coin_ch.send(
                                embed=embed,
                                view=LeaderboardView(self)
                            )
                            config.COIN_LEADERBOARD_MESSAGE_ID = self._leaderboard_message.id
                            self._last_leaderboard_update = current_time
                            return

                self._leaderboard_message = await coin_ch.send(
                    embed=embed,
                    view=LeaderboardView(self)
                )
                config.COIN_LEADERBOARD_MESSAGE_ID = self._leaderboard_message.id
                self._last_leaderboard_update = current_time
                self._backoff_time = 5

            except Exception as e:
                await log_to_channel(self.bot, f"❌ 리더보드 업데이트 오류: {e}")
                await asyncio.sleep(self._backoff_time)
                self._backoff_time = min(60, self._backoff_time * 2)

    @app_commands.command(
        name="coins",
        description="내 코인 잔액을 확인합니다."
    )
    async def coins(self, interaction: discord.Interaction):
        row = await self.bot.db.fetchrow(
            "SELECT balance FROM coins WHERE user_id = $1",
            interaction.user.id
        )
        bal = row["balance"] if row else 0
        await interaction.response.send_message(
            f"{interaction.user.mention}님, 현재 **{bal}** 코인을 보유 중입니다."
        )

    @app_commands.command(
        name="coin_leaderboard",
        description="Top 10 코인 보유자 순위를 표시합니다."
    )
    async def coin_leaderboard(self, interaction: discord.Interaction):
        embed = await self.build_leaderboard_embed()
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="coins_modify",
        description="관리자가 여러 사용자의 코인을 추가/제거/설정합니다."
    )
    @app_commands.describe(
        users="공백으로 구분된 멘션 (예: @User1 @User2)",
        action="add: 추가, remove: 제거, set: 설정",
        amount="적용할 코인 양"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="추가", value="add"),
        app_commands.Choice(name="제거", value="remove"),
        app_commands.Choice(name="설정", value="set"),
    ])
    async def coins_modify(
        self,
        interaction: discord.Interaction,
        users: str,
        action: app_commands.Choice[str],
        amount: int
    ):
        if not interaction.user.guild_permissions.manage_guild:
            return await interaction.response.send_message(
                "❌ 이 명령을 사용할 권한이 없습니다.", ephemeral=True
            )

        # parse member mentions
        members = [
            m for m in interaction.guild.members
            if any(mention in users for mention in (m.mention, f"<@{m.id}>"))
        ]
        if not members:
            return await interaction.response.send_message(
                "❌ 올바른 멘션을 입력해주세요.", ephemeral=True
            )

        summary = []
        for m in members:
            row = await self.bot.db.fetchrow(
                "SELECT balance FROM coins WHERE user_id = $1", m.id
            )
            old_bal = row["balance"] if row else 0

            if action.value == "add":
                new_bal = old_bal + amount
                delta = amount
            elif action.value == "remove":
                new_bal = max(0, old_bal - amount)
                delta = new_bal - old_bal
            else:  # set
                new_bal = max(0, amount)
                delta = new_bal - old_bal

            await self.bot.db.execute(
                """
                INSERT INTO coins (user_id, balance)
                VALUES ($1, $2)
                ON CONFLICT (user_id) DO UPDATE
                  SET balance = EXCLUDED.balance
                """,
                m.id, new_bal
            )

            sign = "+" if delta > 0 else ""
            summary.append(f"{m.mention}: {sign}{delta} 코인 ({old_bal} → {new_bal})")
            actor_display = f"{interaction.user.display_name}님"
            target_display = f"{m.display_name}님"
            action_ko = "추가" if action.value == "add" else ("제거" if action.value == "remove" else "설정")
            await log_to_channel(
                self.bot,
                f"🛠️ [코인 수정] {actor_display}이(가) {target_display}님의 코인을 "
                f"{old_bal} → {new_bal}으로 {action_ko}했습니다."
            )

        # refresh the in‑channel leaderboard
        await self.refresh_leaderboard()

        embed = discord.Embed(
            title="🛠️ 코인 수정 결과",
            description="\n".join(summary),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        await interaction.response.send_message(embed=embed)

    @app_commands.command(
        name="코인거래",
        description="친구에게 코인을 전송합니다 (10% 수수료 포함)."
    )
    @app_commands.describe(
        member="코인을 받을 사용자",
        amount="전송할 코인 수"
    )
    async def coins_tip(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        amount: int
    ):
        sender = interaction.user
        recipient = member

        # 기본 검증
        if amount <= 0:
            return await interaction.response.send_message(
                "❌ 전송할 코인 수는 1 이상이어야 합니다.", ephemeral=True
            )
        if sender.id == recipient.id:
            return await interaction.response.send_message(
                "❌ 자신에게는 전송할 수 없습니다.", ephemeral=True
            )

        # 잔액 확인
        row = await self.bot.db.fetchrow(
            "SELECT balance FROM coins WHERE user_id = $1",
            sender.id
        )
        sender_bal = row["balance"] if row else 0
        if sender_bal < amount:
            return await interaction.response.send_message(
                "❌ 잔액이 부족합니다.", ephemeral=True
            )

        # 수수료 및 실수령액 계산
        fee = int(amount * 0.10)
        net = amount - fee

        # 트랜잭션: 수신자 지급, 송금자 차감
        await self.bot.db.execute(
            """
            INSERT INTO coins (user_id, balance)
            VALUES ($1, $2)
            ON CONFLICT (user_id) DO UPDATE
              SET balance = coins.balance + $2
            """,
            recipient.id, net
        )
        await self.bot.db.execute(
            """
            UPDATE coins
               SET balance = balance - $2
             WHERE user_id = $1
            """,
            sender.id, amount
        )

        # 리더보드 갱신
        await self.refresh_leaderboard()

        # 1) Sender에게 응답
        await interaction.response.send_message(
            f"✅ {sender.mention}님이 {recipient.mention}님께 "
            f"{net} 코인(수수료 {fee}코인)을 전송했습니다."
        )

        # 2) Recipient에게 DM 알림
        try:
            await recipient.send(
                f"🎉 {recipient.mention}님, {sender.display_name}님이 당신에게 "
                f"{net} 코인(수수료 {fee}코인)을 전송하셨습니다!"
            )
        except discord.Forbidden:
            # DM 차단 상태면 무시
            pass

        # 3) 로깅
        sender_display = f"{sender.display_name}님"
        recipient_display = f"{recipient.display_name}님"
        await log_to_channel(
            self.bot,
            f"💸 [코인거래] {sender_display} → {recipient_display}: "
            f"{amount}코인 전송 (수수료 {fee}코인), 실수령 {net}코인"
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Coins(bot))
