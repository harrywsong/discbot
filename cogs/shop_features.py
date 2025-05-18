#shop_features.py new

import discord
import asyncio
import traceback

from discord import Interaction
from discord.ext import commands
from discord.ui import View, Select, Button
from discord.ui import Modal, TextInput

from datetime import datetime, timezone, timedelta

from utils import config
from utils.logger import log_to_channel

TEST_MODE = False

def expiry(seconds: int) -> int:
    return 15 if TEST_MODE else seconds


class NickColorSelect(Select):
    COST = 1000

    def __init__(self):
        options = [
            discord.SelectOption(label=emo, value=emo)
            for emo in config.REACTION_TO_COLOR_ROLES
        ]
        super().__init__(
            custom_id="nick_color_select",
            placeholder="닉네임 색상 선택 (12시간, 1000코인)",
            min_values=1, max_values=1,
            options=options
        )

    async def callback(self, inter: Interaction):
        # defer so we can follow up safely
        await inter.response.defer(ephemeral=True)

        user, guild = inter.user, inter.guild

        # fetch & check balance
        row = await inter.client.db.fetchrow(
            "SELECT balance FROM coins WHERE user_id=$1", user.id
        )
        bal = row["balance"] if row else 0
        if bal < self.COST:
            return await inter.followup.send(
                f"❌ 잔액이 {self.COST}코인 이상이어야 합니다. 현재 잔액: {bal}코인", ephemeral=True
            )

        # deduct & log
        await inter.client.db.execute(
            "UPDATE coins SET balance = balance - $2 WHERE user_id = $1",
            user.id, self.COST
        )
        await log_to_channel(
            inter.client,
            f"🛒 {user.display_name}님이 닉네임 색상 구매로 {self.COST}코인을 사용했습니다."
        )

        # remove old color roles
        existing = [
            guild.get_role(rid)
            for lst in config.REACTION_TO_COLOR_ROLES.values()
            for rid in lst
            if guild.get_role(rid) in user.roles
        ]
        if existing:
            await user.remove_roles(*existing, reason="Clearing previous color")

        # assign new
        choice = self.values[0]
        role_id = config.REACTION_TO_COLOR_ROLES[choice][0]
        role    = guild.get_role(role_id)
        if not role:
            return await inter.followup.send("❌ 색상 역할을 찾을 수 없습니다.")

        # make sure it’s not hoisted, but don’t touch its position
        await role.edit(hoist=False)
        await user.add_roles(role, reason="Shop: Nick color")

        # update leaderboard
        await inter.client.get_cog("Coins").refresh_leaderboard()

        # confirm
        await inter.followup.send(
            f"✅ {choice} 역할이 부여되었습니다. 만료까지 {expiry(12*3600)}초 남음.",
                ephemeral=True
        )

        await inter.followup.send(
            f"{user.mention}님이 {choice} 색을 구매하셨습니다! 지금부터 12시간 동안 적용됩니다."
        )

        # schedule removal
        asyncio.create_task(
            self._remove_later(inter.client, user, role, expiry(12*3600))
        )

    async def _remove_later(self, bot, user, role, delay):
        await asyncio.sleep(delay)
        await user.remove_roles(role, reason="Color expired")
        await log_to_channel(bot, f"{user.display_name}님의 {role.name} 역할이 만료되어 제거되었습니다.")


class CustomRoleModal(Modal):
    def __init__(self):
        super().__init__(title="커스텀 역할 생성 (12시간, 2000코인)")
        self.role_name  = TextInput(label="역할 이름", placeholder="MyRole")
        self.role_color = TextInput(label="Hex 컬러", placeholder="#FF00FF")
        self.add_item(self.role_name)
        self.add_item(self.role_color)

    async def on_submit(self, inter: Interaction):
        await inter.response.defer()

        try:
            guild = inter.guild
            rn    = self.role_name.value
            color = discord.Color(int(self.role_color.value.strip("#"), 16))

            # create and position role
            role = await guild.create_role(
                name=rn,
                color=color,
                hoist=True,
                mentionable=False
            )
            anchor = guild.get_role(config.BASE_ROLE) or discord.utils.get(guild.roles, name="정령")
            if anchor:
                await guild.edit_role_positions(positions={role: anchor.position + 1})
                await role.edit(hoist=True)

            # assign, log, refresh
            await inter.user.add_roles(role, reason="Shop: Custom role")
            await inter.client.get_cog("Coins").refresh_leaderboard()

            # compute expiry
            delay = expiry(12 * 3600)

            # announce in channel
            await inter.channel.send(
                f"{inter.user.mention}님이 커스텀 역할 `{rn}`을 구매하셨습니다! 지금부터 12시간 동안 적용됩니다."
            )

            # schedule delete
            asyncio.create_task(
                self._remove_later(inter.client, guild, role, delay)
            )

        except Exception:
            tb = traceback.format_exc()
            await inter.followup.send(
                f"❌ 역할 생성 중 오류가 발생했습니다:\n```py\n{tb}```",
                ephemeral=True
            )
            await log_to_channel(inter.client, f"[CustomRoleModal] Error:\n```{tb}```")
            raise

    async def _remove_later(self, bot, guild, role, delay):
        await asyncio.sleep(delay)
        await role.delete(reason="Custom role expired")
        await log_to_channel(bot, f"역할 `{role.name}`이 만료되어 삭제되었습니다.")

class CustomRoleButton(Button):
    COST = 2000

    def __init__(self):
        super().__init__(
            custom_id="custom_role_btn",
            label="커스텀 역할 생성 (12시간, 2000코인)",
            style=discord.ButtonStyle.primary
        )

    async def callback(self, inter: Interaction):
        user = inter.user

        # 1) 잔액 확인
        row = await inter.client.db.fetchrow(
            "SELECT balance FROM coins WHERE user_id = $1", user.id
        )
        bal = row["balance"] if row else 0
        if bal < self.COST:
            return await inter.response.send_message(
                f"❌ 잔액이 {self.COST}코인 이상이어야 합니다. 현재 잔액: {bal}코인",
                ephemeral=True
            )

        # 2) 차감 & 로깅
        await inter.client.db.execute(
            "UPDATE coins SET balance = balance - $2 WHERE user_id = $1",
            user.id, self.COST
        )
        await log_to_channel(
            inter.client,
            f"🛒 {user.display_name}님이 커스텀 역할 생성으로 {self.COST}코인을 사용했습니다."
        )

        # 3) 리더보드 갱신
        await inter.client.get_cog("Coins").refresh_leaderboard()

        # 4) 모달 띄우기
        await inter.response.send_modal(CustomRoleModal())

class XPBoosterButton(Button):
    COST = 5000
    STORE_ROLE_ID = 1372630287556804668

    def __init__(self):
        super().__init__(
            custom_id="xp_booster_btn",
            label="XP 2배 쿠폰 (12시간, 5000코인)",
            style=discord.ButtonStyle.success
        )

    async def callback(self, inter: Interaction):
        await inter.response.defer()
        user, guild = inter.user, inter.guild

        row = await inter.client.db.fetchrow(
            "SELECT balance FROM coins WHERE user_id=$1", user.id
        )
        bal = row["balance"] if row else 0
        if bal < self.COST:
            return await inter.followup.send(
                f"❌ 잔액이 {self.COST}코인 이상이어야 합니다. 현재 잔액: {bal}코인", ephemeral=True
            )

        await inter.client.db.execute(
            "UPDATE coins SET balance = balance - $2 WHERE user_id = $1",
            user.id, self.COST
        )
        await log_to_channel(
            inter.client,
            f"🛒 {user.display_name}님이 XP 2배 쿠폰 구매로 {self.COST}코인을 사용했습니다."
        )
        await inter.client.get_cog("Coins").refresh_leaderboard()

        booster = discord.utils.get(guild.roles, name="XP Booster")
        if not booster:
            booster = await guild.create_role(
                name="XP Booster", color=discord.Color.blue(), hoist=True
            )

        store = guild.get_role(self.STORE_ROLE_ID)
        if not store:
            store = await guild.create_role(
                name="Store Access", color=discord.Color.dark_gray()
            )

        await user.add_roles(booster, store, reason="Shop: XP Booster + Store Access")

        delay = expiry(12 * 3600)
        expire_dt = datetime.now(timezone.utc) + timedelta(seconds=delay)
        expire_str = f"{expire_dt.month}월 {expire_dt.day}일 {expire_dt.hour}시 {expire_dt.minute}분에 만료됩니다."

        await inter.channel.send(
            f"{user.mention}님이 XP 2배 쿠폰과 스토어 접근 역할을 구매하셨습니다! 지금부터 12시간 동안 적용됩니다."
        )

        asyncio.create_task(
            self._remove_later(inter.client, user, booster, store, delay)
        )

    async def _remove_later(self, bot, user, booster_role, store_role, delay):
        await asyncio.sleep(delay)
        # remove both roles
        await user.remove_roles(booster_role, store_role, reason="XP Booster expired")
        await log_to_channel(
            bot,
            f"{user.display_name}님의 XP Booster 역할과 스토어 접근 역할이 만료되어 제거되었습니다."
        )

class ShopPersistent(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        view = View(timeout=None)
        view.add_item(NickColorSelect())
        view.add_item(CustomRoleButton())
        view.add_item(XPBoosterButton())
        bot.add_view(view)

    @commands.Cog.listener()
    async def on_ready(self):
        shop_ch = self.bot.get_channel(config.SHOP_CHANNEL_ID)
        if not shop_ch:
            return

        await shop_ch.purge(limit=50)
        embed = discord.Embed(
            title="🏪 코인 상점",
            description="아래에서 아이템을 클릭/선택하여 구매하세요!\n\n"
                        "*⚠️**닉네임 색상 변경**과 **커스텀 역할 생성**은\n"
                        "동시에 사용할 수 없으니 유의해주시기 바랍니다.⚠️*",
            color=discord.Color.gold()
        )
        embed.add_field(name="닉네임 색상 변경", value="1000 코인 (12h)", inline=False)
        embed.add_field(name="커스텀 역할 생성", value="2000 코인 (12h)", inline=False)
        embed.add_field(name="XP 2배 쿠폰",     value="5000 코인 (12h)", inline=False)

        view = View(timeout=None)
        view.add_item(NickColorSelect())
        view.add_item(CustomRoleButton())
        view.add_item(XPBoosterButton())

        await shop_ch.send(embed=embed, view=view)


async def setup(bot: commands.Bot):
    await bot.add_cog(ShopPersistent(bot))
