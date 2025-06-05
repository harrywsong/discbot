# cogs/coins.py new

import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import asyncio

from utils import config
from utils.logger import log_to_channel
import pytz


class DailyCoinsView(discord.ui.View):
    def __init__(self, bot: commands.Bot):
        super().__init__(timeout=None)
        self.bot = bot

    @discord.ui.button(label="오늘의 코인 받기", style=discord.ButtonStyle.primary, custom_id="dailycoins_button")
    async def claim(self, interaction: discord.Interaction, button: discord.ui.Button):
        print("Button pressed (coins/xp)")
        try:
            await interaction.response.defer(ephemeral=True)  # Always defer FIRST
            user = interaction.user
            now_utc = datetime.now(timezone.utc)
            eastern = pytz.timezone("America/New_York")
            today_et = now_utc.astimezone(eastern).date()

            # check last claim
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

            # grant coins
            amount = config.DAILY_COINS_AMOUNT
            await self.bot.db.execute(
                """
                INSERT INTO coins (user_id, balance)
                VALUES ($1, $2) ON CONFLICT (user_id) DO
                UPDATE SET balance = coins.balance + EXCLUDED.balance
                """,
                user.id, amount
            )
            await self.bot.db.execute(
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

            user_display = f"{user.display_name}님"
            await log_to_channel(
                self.bot,
                f"🎁 [오늘의 코인] {user_display}이(가) {amount}코인 수령"
            )

            # refresh the leaderboard in place
            coins_cog = self.bot.get_cog("Coins")
            if coins_cog:
                await coins_cog.refresh_leaderboard()

        except Exception as e:
            import traceback
            traceback.print_exc()
            # Only try to followup if not already done
            try:
                await interaction.followup.send(
                    f"❌ 알 수 없는 오류가 발생했습니다.\n```{e}```", ephemeral=True
                )
            except Exception:
                pass  # Already responded or can't send


class Coins(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._setup_done = False

    @commands.Cog.listener()
    async def on_ready(self):
        if self._setup_done:
            return

        # ─── Wait for the DB pool to be created ─────────────
        while not hasattr(self.bot, "db") or self.bot.db is None:
            await asyncio.sleep(0.1)

        self._setup_done = True

        # ─── Ensure tables exist ────────────────────────────
        await self.bot.db.execute("""
            CREATE TABLE IF NOT EXISTS coins (
                user_id BIGINT PRIMARY KEY,
                balance BIGINT NOT NULL DEFAULT 0
            );
        """)
        await self.bot.db.execute("""
            CREATE TABLE IF NOT EXISTS daily_coin_claim (
                user_id BIGINT PRIMARY KEY,
                last_claim TIMESTAMPTZ NOT NULL
            );
        """)

        # ─── Unified Coins Channel ──────────────────────────
        coin_ch = self.bot.get_channel(config.DAILY_COINS_CHANNEL_ID)
        if not coin_ch:
            return

        # purge old messages
        await coin_ch.purge(limit=None)

        # send leaderboard embed
        lb_embed = await self.build_leaderboard_embed()
        lb_msg = await coin_ch.send(embed=lb_embed)
        config.COIN_LEADERBOARD_MESSAGE_ID = lb_msg.id

        # send daily‑claim button
        btn_embed = discord.Embed(
            title="🎁 오늘의 코인 받기",
            description="아래 버튼을 눌러 오늘의 코인을 받으세요!",
            color=discord.Color.gold()
        )
        view = DailyCoinsView(self.bot)
        btn_msg = await coin_ch.send(embed=btn_embed, view=view)
        config.DAILY_COINS_MESSAGE_ID = btn_msg.id

    async def build_leaderboard_embed(self) -> discord.Embed:
        rows = await self.bot.db.fetch(
            "SELECT user_id, balance FROM coins ORDER BY balance DESC LIMIT 10"
        )
        embed = discord.Embed(
            title="🏆 코인 리더보드 (Top 10)",
            color=discord.Color.gold()
        )
        if not rows:
            embed.description = "아직 코인을 획득한 유저가 없습니다."
        else:
            lines = [
                f"**{idx}.** <@{r['user_id']}> — {r['balance']} 코인"
                for idx, r in enumerate(rows, start=1)
            ]
            embed.description = "\n".join(lines)
        embed.set_footer(text=f"업데이트: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        return embed

    async def refresh_leaderboard(self):
        coin_ch = self.bot.get_channel(config.DAILY_COINS_CHANNEL_ID)
        if not coin_ch:
            return

        embed = await self.build_leaderboard_embed()
        msg_id = config.COIN_LEADERBOARD_MESSAGE_ID

        # If we have an ID, try to fetch & edit it
        if msg_id:
            try:
                msg = await coin_ch.fetch_message(msg_id)
                await msg.edit(embed=embed)
                return
            except (discord.NotFound, discord.HTTPException):
                # either the message was deleted, or the ID was bad → fall through to send a new one
                pass

        # No valid ID or fetch/edit failed: send a fresh message
        sent = await coin_ch.send(embed=embed)
        # store its ID for next time
        config.COIN_LEADERBOARD_MESSAGE_ID = sent.id

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
