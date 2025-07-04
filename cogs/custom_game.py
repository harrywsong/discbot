# cogs/custom_game.py

import asyncio
import random
from typing import Optional, List, Union
from utils.henrik import henrik_get

import pytz
import discord
from discord import app_commands, AllowedMentions
from discord.ext import commands
from datetime import datetime, timedelta, timezone

from utils import config
from utils.logger import log_to_channel

# ── Constants ─────────────────────────────────

MAPS = [
    "Ascent", "Haven", "Icebox", "Lotus", "Pearl", "Split", "Sunset"
]


def is_privileged(user: discord.Member, creator: discord.Member) -> bool:
    """True if the user is the creator or has any admin role."""
    if user == creator:
        return True
    return any(r.id in config.CUSTOM_GAME_ADMIN_ROLE_IDS for r in user.roles)


class FakeUser:
    """Represents a bot‑added placeholder player."""
    def __init__(self, name: str):
        self.name = name

    @property
    def mention(self) -> str:
        return f"`{self.name}`"

    @property
    def display_name(self) -> str:
        return self.name


# ── UI: Lobby View & Buttons ───────────────────────────

class CustomGameView(discord.ui.View):
    def __init__(
        self,
        creator: discord.Member,
        interaction: discord.Interaction,
        voice_channel: discord.VoiceChannel
    ):
        super().__init__(timeout=None)
        self.creator = creator
        self.interaction = interaction
        self.voice_channel = voice_channel
        # participants: either discord.Member or FakeUser
        self.participants: List[Union[discord.Member, FakeUser]] = []
        self.waitlist_open: bool = False
        self.waitlist: List[discord.Member] = []
        self.ping_schedule = {10: False, 5: False, 1: False}
        self.start_time: Optional[str] = None
        self.voice_check_start: Optional[datetime] = None
        self.voice_check_end: Optional[datetime] = None
        self.lobby_message: Optional[discord.Message] = None
        self.warning_task: Optional[asyncio.Task] = None
        self.rebuild_buttons()

    def rebuild_buttons(self):
        self.clear_items()
        if len(self.participants) < 10:
            self.add_item(self.join_button)
        elif self.waitlist_open:
            self.add_item(self.waitlist_button)

        self.add_item(self.leave_button)

        if self.waitlist_open:
            self.add_item(self.waitlist_cancel_button)

        self.add_item(CancelCustomGameButton(self))

    def format_description(self) -> str:
        desc = (
            f"{self.creator.mention}님이 내전을 시작했습니다!\n\n"
            f"**시작 시간:**\n{self.start_time or '알 수 없음'}\n\n"
            f"**참가자 ({len(self.participants)}/10):**\n"
            f"{('*아직 아무도 없습니다.*' if not self.participants else chr(10).join(p.mention for p in self.participants))}"
        )
        if self.waitlist_open:
            desc += (
                "\n\n**대기열:**\n"
                f"{('*비어 있습니다.*' if not self.waitlist else chr(10).join(m.mention for m in self.waitlist))}"
            )
        return desc

    async def update_embed(self):
        assert self.lobby_message, "Lobby message not set"
        await self.lobby_message.edit(
            embed=discord.Embed(
                title="🕹️ 발로란트 내전",
                description=self.format_description(),
                color=discord.Color.green()
            ),
            view=self
        )

    @discord.ui.button(label="내전 참가", style=discord.ButtonStyle.success, custom_id="join")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user in self.participants:
            return await interaction.response.send_message("이미 참가하셨습니다!", ephemeral=True)
        if len(self.participants) >= 10:
            return await interaction.response.send_message(
                "가득 찼습니다. `/내전대기`로 대기열에 등록하세요.", ephemeral=True
            )

        self.participants.append(interaction.user)
        # ▶ Log: 참가
        await log_to_channel(
            interaction.client,
            f"🟢 [내전] {interaction.user.display_name}님이 내전에 참가했습니다."
        )

        self.rebuild_buttons()
        await self.update_embed()
        await interaction.response.send_message("✅ 참가 완료!", ephemeral=True)

    @discord.ui.button(label="참가 취소", style=discord.ButtonStyle.danger, custom_id="leave")
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user not in self.participants:
            return await interaction.response.send_message("참가 상태가 아닙니다.", ephemeral=True)

        self.participants.remove(interaction.user)
        # ▶ Log: 참가 취소
        await log_to_channel(
            interaction.client,
            f"🔴 [내전] {interaction.user.display_name}님이 내전 참가를 취소했습니다."
        )

        # Auto‑promote from waitlist
        if self.waitlist:
            next_up = self.waitlist.pop(0)
            self.participants.append(next_up)
            await interaction.channel.send(f"{next_up.mention}님, 빈 자리가 생겨 내전에 참가하셨습니다! 🎉")

        self.rebuild_buttons()
        await self.update_embed()
        await interaction.response.send_message("✅ 취소 완료!", ephemeral=True)

    @discord.ui.button(label="대기열 참가", style=discord.ButtonStyle.secondary, custom_id="waitlist_button")
    async def waitlist_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user in self.participants or interaction.user in self.waitlist:
            return await interaction.response.send_message("이미 참가 또는 대기열 중입니다.", ephemeral=True)
        self.waitlist.append(interaction.user)
        await self.update_embed()
        await interaction.response.send_message("✅ 대기열에 등록되었습니다.", ephemeral=True)

    @discord.ui.button(label="대기열 취소", style=discord.ButtonStyle.danger, custom_id="waitlist_cancel")
    async def waitlist_cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user not in self.waitlist:
            return await interaction.response.send_message("대기열에 없습니다.", ephemeral=True)
        self.waitlist.remove(interaction.user)
        self.rebuild_buttons()
        await self.update_embed()
        await interaction.response.send_message("✅ 대기열에서 빠졌습니다.", ephemeral=True)


class CancelCustomGameButton(discord.ui.Button):
    def __init__(self, parent: CustomGameView):
        super().__init__(label="내전 취소", style=discord.ButtonStyle.danger, custom_id="cancel_custom")
        self.parent = parent

    async def callback(self, interaction: discord.Interaction):
        if not is_privileged(interaction.user, self.parent.creator):
            return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)

        # 1) Clear the lobby messages in this channel
        await interaction.channel.purge(limit=100)
        await interaction.channel.send("✅ **내전이 취소되었습니다.**")

        # ▶ Log: 내전 취소
        await log_to_channel(
            interaction.client,
            f"❌ [내전] {interaction.user.display_name}님이 내전을 취소했습니다."
        )

        # 2) Cancel any warning tasks
        if self.parent.warning_task:
            self.parent.warning_task.cancel()

        # 3) Clear bot.current_custom_game
        interaction.client.current_custom_game = None


# ── Cog Definition ─────────────────────────────────────

class CustomGame(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(
        name="내전시작",
        description="10인 내전을 시작합니다."
    )
    @app_commands.describe(
        time="시작 시간 (예: 22:00 또는 10:00 PM)",
        zone="시간대: est, cst, pst"
    )
    @app_commands.check(lambda i: i.user.guild_permissions.administrator)
    async def slash_start_custom(
        self,
        interaction: discord.Interaction,
        time: str,
        zone: str
    ):
        # 1) Pre‑validate timezone
        tz_map = {"est": "US/Eastern", "cst": "US/Central", "pst": "US/Pacific"}
        if zone.lower() not in tz_map:
            return await interaction.response.send_message(
                "❌ 지원되지 않는 시간대입니다. est/cst/pst 중 하나를 입력해주세요.",
                ephemeral=True
            )

        # 2) Immediately confirm to the user
        await interaction.response.send_message(
            "✅ 발로란트 내전이 열렸습니다! 채널에서 참가 버튼을 확인하세요.",
            ephemeral=True
        )

        try:
            # 3) Parse time string
            dt = None
            for fmt in ("%H:%M", "%I:%M %p"):
                try:
                    dt = datetime.strptime(time, fmt)
                    break
                except ValueError:
                    continue
            if dt is None:
                raise ValueError("시간 형식(예: 22:00 또는 10:00 PM)이 잘못되었습니다.")

            # 4) Convert to UTC
            user_tz = pytz.timezone(tz_map[zone.lower()])
            local_date = datetime.now(user_tz).date()
            local_dt = user_tz.localize(datetime.combine(local_date, dt.time()))
            utc_dt = local_dt.astimezone(timezone.utc)
            if utc_dt < datetime.now(timezone.utc):
                utc_dt += timedelta(days=1)

            # 5) Build display string
            display = "\n".join(
                f"**{lbl}:** {utc_dt.astimezone(pytz.timezone(tz)).strftime('%I:%M %p').lstrip('0')}"
                for lbl, tz in [
                    ("EST", "US/Eastern"),
                    ("CST", "US/Central"),
                    ("PST", "US/Pacific")
                ]
            )

            # 6) Find waiting VC
            vc = self.bot.get_channel(config.CUSTOM_GAME_VOICE_CHANNEL_ID)
            if not vc:
                raise RuntimeError("대기 음성 채널을 찾을 수 없습니다.")

            # 7) Build and send lobby as a normal bot message
            view = CustomGameView(interaction.user, interaction, vc)
            view.start_time = display
            view.voice_check_start = utc_dt - timedelta(minutes=60)
            view.voice_check_end = utc_dt

            channel = interaction.channel
            lobby_embed = discord.Embed(
                title="🕹️ 발로란트 내전",
                description=view.format_description(),
                color=discord.Color.green()
            )
            lobby_msg = await channel.send(
                content=f"<@&{config.CUSTOM_GAME_ROLE_ID}>",
                embed=lobby_embed,
                view=view,
                allowed_mentions=AllowedMentions(roles=True)
            )
            view.lobby_message = lobby_msg
            self.bot.add_view(view, message_id=lobby_msg.id)

            # 8) Save to the bot object for global access
            self.bot.current_custom_game = view

            # 9) Schedule warnings / voice checks
            view.warning_task = asyncio.create_task(self._warning_30min(view))
            asyncio.create_task(self._monitor_voice_check(view))

            # ▶ Log: 내전 시작
            await log_to_channel(
                self.bot,
                f"🕹️ [내전] {interaction.user.display_name}님이 내전을 열었습니다. 시간:\n{display}"
            )

        except Exception as e:
            # If the initial send() wasn’t done, send error there; otherwise use followup
            if not interaction.response.is_done():
                await interaction.response.send_message(f"❌ 오류: {e}", ephemeral=True)
            else:
                await interaction.followup.send(f"❌ 오류: {e}", ephemeral=True)
            await log_to_channel(self.bot, f"⚠️ [내전 오류] slash_start_custom: {e}")

    async def _warning_30min(self, view: CustomGameView):
        """Sends 30‑minute warning before start."""
        while getattr(self.bot, "current_custom_game", None) is not view:
            await asyncio.sleep(0.5)

        warn_time = view.voice_check_end - timedelta(minutes=30)
        delay = (warn_time - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)

        if getattr(self.bot, "current_custom_game", None) is view:
            mentions = " ".join(
                p.mention for p in view.participants
                if isinstance(p, discord.Member)
            )
            if mentions:
                await view.interaction.channel.send(
                    f"⏰ 내전 시작 30분 전입니다!\n{mentions}\n이제부터 취소 불가합니다.",
                    allowed_mentions=AllowedMentions(users=True)
                )
            # ▶ Log: 30분 전 경고
            await log_to_channel(self.bot, "⏰ [내전] 30분 전 경고 발송")

    async def _monitor_voice_check(self, view: CustomGameView):
        """Checks at 10/5/1 minutes to ping users not in VC."""
        while getattr(self.bot, "current_custom_game", None) is not view:
            await asyncio.sleep(0.5)

        delay = (view.voice_check_start - datetime.now(timezone.utc)).total_seconds()
        if delay > 0:
            await asyncio.sleep(delay)

        while getattr(self.bot, "current_custom_game", None) is view and datetime.now(timezone.utc) < view.voice_check_end:
            remaining = (view.voice_check_end - datetime.now(timezone.utc)).total_seconds() / 60
            for mark in (10, 5, 1):
                if not view.ping_schedule[mark] and abs(remaining - mark) < 0.5:
                    not_in_vc = [
                        p.mention
                        for p in view.participants
                        if isinstance(p, discord.Member)
                        and (not p.voice or p.voice.channel.id != config.CUSTOM_GAME_VOICE_CHANNEL_ID)
                    ]
                    if not_in_vc:
                        await view.interaction.channel.send(
                            f"{' '.join(not_in_vc)} \n⏰ **{mark}분 전!** \n<#{config.CUSTOM_GAME_VOICE_CHANNEL_ID}> 으로 모여주세요!",
                            allowed_mentions=AllowedMentions(users=True)
                        )
                    view.ping_schedule[mark] = True
                    # ▶ Log: 특정 분 전 알림
                    await log_to_channel(self.bot, f"⏰ [내전] {mark}분 전 알림 발송")
            await asyncio.sleep(30)

    @app_commands.command(name="내전종료", description="최근 커스텀 경기 3개를 기록합니다.")
    @app_commands.check(lambda i: i.user.guild_permissions.administrator)
    async def slash_save_custom_games(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        try:
            # 1) Fetch invoking user's puuid
            async with self.bot.db.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT riot_name, riot_tag, puuid FROM players WHERE discord_id = $1",
                    str(interaction.user.id)
                )
            if not row:
                await interaction.followup.send(
                    "❌ 먼저 `/연동` 명령어로 계정을 연동해 주세요.",
                    ephemeral=True
                )
                return

            riot_name = row["riot_name"]
            riot_tag = row["riot_tag"]
            puuid = row["puuid"]

            # 2) Fetch recent matches from Henrik
            data = await henrik_get(f"/valorant/v3/by-puuid/matches/na/{puuid}")
            if not data or "data" not in data:
                await interaction.followup.send(
                    "❌ 최근 경기 정보를 가져오지 못했습니다.",
                    ephemeral=True
                )
                return

            # 3) Build a set of all linked puuids
            async with self.bot.db.acquire() as conn:
                rows = await conn.fetch("SELECT puuid FROM players")
                linked_puuids = {r["puuid"] for r in rows}

            # 4) Filter out only full 10‐player matches where every puuid is linked
            custom_candidates = []
            for match in data["data"]:
                players = match.get("players", {}).get("all_players", [])
                if len(players) == 10 and all(p["puuid"] in linked_puuids for p in players):
                    custom_candidates.append(match)

            count = 0
            # 5) Insert up to 3 full‐party matches into DB (match_players),
            #    and update each participant’s last_active to the match’s timestamp
            async with self.bot.db.acquire() as conn:
                for match in custom_candidates[:3]:
                    meta = match["metadata"]
                    match_id = meta["matchid"]
                    game_start = meta.get("game_start", datetime.utcnow())
                    map_name = meta.get("map", "?")
                    rounds = meta.get("rounds_played", 0)

                    # For reference: the invoking player's data (to compute their HS%, ADR, etc.)
                    player_data = next(
                        (p for p in match["players"]["all_players"] if p["puuid"] == puuid),
                        None
                    )
                    if not player_data:
                        continue

                    stats = player_data["stats"]
                    # These two lines aren’t strictly needed for the inserts below,
                    # but left here in case you want to log or use them:
                    _kda = f"{stats['kills']}/{stats['deaths']}/{stats['assists']}"
                    _hs = stats.get("headshots", 0)
                    _bs = stats.get("bodyshots", 0)
                    _ls = stats.get("legshots", 0)
                    _shots = _hs + _bs + _ls
                    _hs_pct = (_hs / _shots) * 100 if _shots else 0
                    _adr = player_data.get("damage_made", 0) // max(rounds, 1)

                    # Determine each team’s final rounds_won (adjust keys if needed)
                    team1_score = match.get("teams", {}).get("red", {}).get("rounds_won", 0)
                    team2_score = match.get("teams", {}).get("blue", {}).get("rounds_won", 0)

                    for p in match["players"]["all_players"]:
                        puuid2 = p["puuid"]
                        riot_name2 = p.get("name", "?")
                        riot_tag2 = p.get("tag", "?")
                        agent = p.get("character", "?")
                        stats2 = p["stats"]

                        kills = stats2.get("kills", 0)
                        deaths = stats2.get("deaths", 0)
                        assists = stats2.get("assists", 0)
                        score = stats2.get("score", 0)
                        kda = f"{kills}/{deaths}/{assists}"

                        hs = stats2.get("headshots", 0)
                        bs = stats2.get("bodyshots", 0)
                        ls = stats2.get("legshots", 0)
                        shots = hs + bs + ls
                        hs_pct = (hs / shots) * 100 if shots else 0
                        rounds_played = meta.get("rounds_played", 0) or 1
                        adr = p.get("damage_made", 0) // rounds_played

                        team = p.get("team", "?")
                        won = match.get("teams", {}).get(team.lower(), {}).get("has_won", False)
                        round_count = meta.get("rounds_played", 0)
                        tier = p.get("currenttier_patched", None)

                        # Insert into match_players table
                        await conn.execute(
                            """
                            INSERT INTO match_players (
                              match_id, puuid, riot_name, riot_tag, map, agent,
                              kda, kills, deaths, assists, score, adr, hs_pct,
                              team, won, round_count, team1_score, team2_score,
                              tier, game_start
                            )
                            VALUES (
                              $1, $2, $3, $4, $5, $6,
                              $7, $8, $9, $10, $11, $12, $13,
                              $14, $15, $16, $17, $18,
                              $19, $20
                            ) ON CONFLICT (match_id, puuid) DO NOTHING
                            """,
                            match_id,
                            puuid2,
                            riot_name2,
                            riot_tag2,
                            map_name,
                            agent,
                            kda,
                            kills,
                            deaths,
                            assists,
                            score,
                            adr,
                            hs_pct,
                            team,
                            won,
                            round_count,
                            team1_score,
                            team2_score,
                            tier,
                            game_start
                        )

                        # Immediately after inserting this player’s row,
                        # update their last_active to the match’s timestamp
                        await conn.execute(
                            "UPDATE players SET last_active = $1 WHERE puuid = $2",
                            game_start,
                            puuid2
                        )

                    count += 1

            # 6) Now that the database is fully updated, purge the previous lobby messages
            #    (adjust the limit as needed; here we remove up to 100)
            await interaction.channel.purge(limit=100)
            # Send a confirmation that deletion occurred
            await interaction.channel.send("✅ 이전 내전 메시지들을 삭제했습니다.")

            # 7) Finally, let the user know how many games were recorded
            await interaction.followup.send(
                f"✅ 최근 내전 {count}경기를 기록했습니다.",
                ephemeral=True
            )

        except Exception as e:
            await interaction.followup.send(f"❌ 오류 발생: {e}", ephemeral=True)
            await log_to_channel(self.bot, f"❌ [내전종료] 실패: {e}")


    @app_commands.command(name="맵추첨", description="랜덤 맵을 뽑습니다.")
    async def slash_roll_map(self, interaction: discord.Interaction):
        if not getattr(self.bot, "current_custom_game", None):
            return await interaction.response.send_message("❌ 활성 내전이 없습니다.", ephemeral=True)
        if not is_privileged(interaction.user, self.bot.current_custom_game.creator):
            return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
        choice = random.choice(MAPS)
        await interaction.response.send_message(f"🗺️ 오늘의 맵: **{choice}**!")

    @app_commands.command(name="내전대기", description="현재 내전의 대기열을 엽니다.")
    @app_commands.check(lambda i: i.user.guild_permissions.administrator)
    async def slash_open_waitlist(self, interaction: discord.Interaction):
        if not getattr(self.bot, "current_custom_game", None):
            return await interaction.response.send_message("❌ 활성 내전이 없습니다.", ephemeral=True)
        if not is_privileged(interaction.user, self.bot.current_custom_game.creator):
            return await interaction.response.send_message("❌ 권한이 없습니다.", ephemeral=True)
        if self.bot.current_custom_game.waitlist_open:
            return await interaction.response.send_message("❌ 이미 대기열이 열려 있습니다.", ephemeral=True)
        self.bot.current_custom_game.waitlist_open = True
        self.bot.current_custom_game.rebuild_buttons()
        await self.bot.current_custom_game.update_embed()
        await interaction.response.send_message("✅ 대기열이 활성화되었습니다.", ephemeral=True)

    @app_commands.command(name="봇추가", description="빈 자리에 가짜 유저를 추가합니다.")
    @app_commands.check(lambda inter: inter.user.guild_permissions.administrator)
    async def slash_add_bots(self, interaction: discord.Interaction):
        custom_game_view = getattr(self.bot, "current_custom_game", None)
        if not custom_game_view:
            return await interaction.response.send_message("❌ 활성 내전이 없습니다.", ephemeral=True)

        needed = 10 - len(custom_game_view.participants)
        if needed <= 0:
            return await interaction.response.send_message("❌ 이미 10명이 참가 중입니다.", ephemeral=True)

        for i in range(1, needed + 1):
            custom_game_view.participants.append(FakeUser(f"플레이어{i}"))

        custom_game_view.rebuild_buttons()
        await custom_game_view.update_embed()
        # ▶ Log: 봇 추가
        await log_to_channel(
            self.bot,
            f"🤖 [내전] 가짜 유저 {needed}명 추가됨"
        )
        await interaction.response.send_message(f"✅ 가짜 유저 {needed}명 추가되었습니다.", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(CustomGame(bot))
