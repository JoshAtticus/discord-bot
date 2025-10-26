import os
import random
import asyncio
import logging
import contextvars
from datetime import datetime
from typing import Optional

import discord
from discord.ext import commands
from dotenv import load_dotenv

# Load environment variables from a .env file if present
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_WELCOME_CHANNEL_ID = os.getenv("WELCOME_CHANNEL_ID")  # optional override
CATEGORY_ID_FOR_WHAT = int(os.getenv("WHAT_CATEGORY_ID", "1373594566997053472"))
PHOTOS_DIR = os.getenv("PHOTOS_DIR", "photos")
BOT_PREFIX = os.getenv("BOT_PREFIX", "!")
RULES_CHANNEL_ID = int(os.getenv("RULES_CHANNEL_ID", "1373596179203489812"))  # rules channel
MOD_RELAY_CHANNEL_ID = int(os.getenv("MOD_RELAY_CHANNEL_ID", "1409855949560221818"))  # channel where DMs are forwarded for mods

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # required for member join events

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("picl")

# Per-command log capture structures
_current_command_log: contextvars.ContextVar[list] = contextvars.ContextVar("current_command_log", default=None)  # type: ignore[arg-type]
_logs_by_message: dict[int, str] = {}
_relay_message_map: dict[int, int] = {}  # forwarded message id -> user id
_active_senders: set[int] = set()  # user IDs who have spoken in guild text channels
_inactivity_task_started = False

INACTIVE_KICK_DAYS = int(os.getenv("INACTIVE_KICK_DAYS", "7"))
INACTIVE_CHECK_INTERVAL_SECONDS = int(os.getenv("INACTIVE_CHECK_INTERVAL_SECONDS", str(3600)))  # 1h default
CHANNEL_HISTORY_SAMPLING_LIMIT = int(os.getenv("CHANNEL_HISTORY_SAMPLING_LIMIT", "200"))  # per channel on startup


class _BufferLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord):
        buf = _current_command_log.get()
        if buf is not None:
            try:
                msg = self.format(record)
            except Exception:
                msg = record.getMessage()
            buf.append(msg)


_handler = _BufferLogHandler()
_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", datefmt="%H:%M:%S"))
logger.addHandler(_handler)
logger.propagate = True

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents, help_command=None)


@bot.event
async def on_ready():
    print(f"Picl is online as {bot.user} (ID: {bot.user.id})")
    # Start inactivity task once per process
    global _inactivity_task_started
    if not _inactivity_task_started:
        _inactivity_task_started = True
        bot.loop.create_task(_populate_active_senders_initial())
        bot.loop.create_task(_inactivity_enforcement_loop())


@bot.event
async def on_member_join(member: discord.Member):
    # Try a specified welcome channel first
    channel: Optional[discord.TextChannel] = None
    if GUILD_WELCOME_CHANNEL_ID:
        try:
            channel = await bot.fetch_channel(int(GUILD_WELCOME_CHANNEL_ID))  # type: ignore[arg-type]
        except Exception:
            channel = None
    # Fallback: first text channel the bot can send messages to
    if channel is None:
        for c in member.guild.text_channels:
            if c.permissions_for(member.guild.me).send_messages:  # type: ignore[union-attr]
                channel = c
                break
    welcome_text = (
        f"**Welcome to the server {member.mention}! 🎉**\n"
        f"Please read the <#{RULES_CHANNEL_ID}> before you start chatting in this server.\n\n"
        "If you have any questions, feel free to ask Josh or any of the moderators (or anyone else, I think most people here would be happy to answer your questions 😁)\n\n"
        f"Wanna know what *I* can do? Just type `{BOT_PREFIX}help` in chat!"
    )
    if channel:
        try:
            await channel.send(welcome_text)
        except discord.Forbidden:
            pass
    # Also DM the user (ignore if they have DMs closed)
    try:
        await member.send(welcome_text)
    except discord.HTTPException:
        pass


def _start_command_log():
    lst: list = []
    token = _current_command_log.set(lst)
    return lst, token


def _end_command_log(token, message: discord.Message | None = None):
    try:
        buf = _current_command_log.get()
    finally:
        _current_command_log.reset(token)
    if message and buf:
        joined = "\n".join(buf)
        if len(joined) > 8000:
            joined = joined[-8000:]
        _logs_by_message[message.id] = joined


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    content_lower = message.content.lower().strip()

    # Track active sender for guild messages only
    if message.guild is not None:
        _active_senders.add(message.author.id)

    # --- DM Relay: user -> mods ---
    if message.guild is None:
        # This is a DM from a user to the bot
        if not message.author.bot:
            # Forward to mod relay channel
            relay_channel = bot.get_channel(MOD_RELAY_CHANNEL_ID)
            if relay_channel is None:
                try:
                    relay_channel = await bot.fetch_channel(MOD_RELAY_CHANNEL_ID)  # type: ignore
                except Exception:
                    relay_channel = None
            if isinstance(relay_channel, discord.TextChannel):
                # Build an embed to show DM content
                embed = discord.Embed(
                    title="New DM",
                    description=message.content[:4000] or "(no text)",
                    color=0x3498DB,
                    timestamp=discord.utils.utcnow(),
                )
                embed.add_field(name="User", value=f"{message.author} (ID: {message.author.id})", inline=False)
                if message.attachments:
                    attach_list = "\n".join(a.url for a in message.attachments[:10])
                    embed.add_field(name="Attachments", value=attach_list[:1000], inline=False)
                try:
                    fwd_msg = await relay_channel.send(embed=embed)
                    _relay_message_map[fwd_msg.id] = message.author.id
                except discord.HTTPException:
                    pass
        # Still allow commands in DMs
        await bot.process_commands(message)
        return

    # --- DM Relay: mods -> user (reply in relay channel) ---
    if (
        message.channel.id == MOD_RELAY_CHANNEL_ID
        and message.reference
        and message.reference.message_id in _relay_message_map
    ):
        target_user_id = _relay_message_map[message.reference.message_id]
        user = bot.get_user(target_user_id)
        if user is None:
            try:
                user = await bot.fetch_user(target_user_id)
            except discord.HTTPException:
                user = None
        if user:
            msg_body = message.content or "(no text)"
            if len(msg_body) > 1900:
                msg_body = msg_body[:1900] + "…"
            try:
                files = []
                for a in message.attachments[:5]:
                    try:
                        fp = await a.to_file()
                        files.append(fp)
                    except Exception:
                        pass
                await user.send(f"**Moderator Reply:**\n{msg_body}", files=files or None)
                # Reaction acknowledgment
                try:
                    await message.add_reaction("✅")
                except discord.HTTPException:
                    pass
            except discord.HTTPException:
                try:
                    await message.add_reaction("⚠️")
                except discord.HTTPException:
                    pass

    # Feature: respond to 'what' in category (only if guild & category match)
    if (
        content_lower == "what"
        and message.guild is not None
        and getattr(message.channel, "category_id", None) == CATEGORY_ID_FOR_WHAT
    ):
        try:
            await message.reply("https://i.ibb.co/ccKSZKwj/image.png")
        except discord.HTTPException:
            pass
    
    # Feature: respond to 'crazy' in category (only if guild & category match)
    if (
        content_lower == "crazy"
        and message.guild is not None
        and getattr(message.channel, "category_id", None) == CATEGORY_ID_FOR_WHAT
    ):
        try:
            await message.reply("https://i.ibb.co/9k8tmgm0/image0.jpg")
        except discord.HTTPException:
            pass

    # Reply with !log to a bot message to retrieve its captured log
    if content_lower == "!log" and message.reference and message.reference.message_id:
        parent_id = message.reference.message_id
        if parent_id in _logs_by_message:
            log_text = _logs_by_message[parent_id]
            if not log_text:
                await message.reply("No log recorded.")
            else:
                for chunk in _chunk(log_text):
                    try:
                        await message.reply(f"```log\n{chunk}\n```")
                    except discord.HTTPException:
                        break
        return  # don't treat !log as a command

    await bot.process_commands(message)


@bot.command(name="picl")
async def picl_command(ctx: commands.Context):
    """Send a random photo from the photos directory."""
    log_buf, token = _start_command_log()
    message_obj: discord.Message | None = None
    logger.info("picl command invoked by %s (%s)", ctx.author, ctx.author.id)
    try:
        if not os.path.isdir(PHOTOS_DIR):
            logger.warning("Photos directory '%s' missing", PHOTOS_DIR)
            message_obj = await ctx.send("Photos directory not found.")
            return

        exts = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        files = [
            os.path.join(PHOTOS_DIR, f)
            for f in os.listdir(PHOTOS_DIR)
            if os.path.isfile(os.path.join(PHOTOS_DIR, f)) and os.path.splitext(f)[1].lower() in exts
        ]
        logger.info("Found %d candidate images", len(files))
        if not files:
            message_obj = await ctx.send("No images available.")
            return

        chosen = random.choice(files)
        logger.info("Chosen image: %s", chosen)
        try:
            size = os.path.getsize(chosen)
            logger.info("Image size: %d bytes", size)
            if size == 0:
                message_obj = await ctx.send("Image file is empty, skipping.")
                return
        except OSError:
            logger.exception("Failed to stat image")
            message_obj = await ctx.send("Problem accessing the image file.")
            return

        try:
            file = discord.File(fp=chosen, filename=os.path.basename(chosen))
            message_obj = await ctx.send(content=f"Here's your picl: {os.path.basename(chosen)}", file=file)
            logger.info("Image sent successfully (message id %s)", message_obj.id)
        except (discord.HTTPException, OSError):
            logger.exception("Failed to send image")
            message_obj = await ctx.send("Couldn't send image.")
    except Exception:
        logger.exception("Unhandled exception in picl command")
        if not message_obj:
            try:
                message_obj = await ctx.send("An unexpected error occurred.")
            except discord.HTTPException:
                pass
    finally:
        _end_command_log(token, message_obj)


def _chunk(text: str, size: int = 1900):
    for i in range(0, len(text), size):
        yield text[i : i + size]


@bot.event
async def on_command_error(ctx: commands.Context, error: Exception):
    if hasattr(ctx.command, 'on_error'):
        return
    if isinstance(error, commands.CommandNotFound):
        return
    logger.exception("Error running command %s", ctx.command)
    try:
        await ctx.send("Command failed.")
    except discord.HTTPException:
        pass


@bot.command(name="help")
async def help_command(ctx: commands.Context):
    """Display help information for Picl."""
    prefix = BOT_PREFIX
    embed = discord.Embed(
        title="Help",
        description="meow",
        color=0x2ECC71,
    )
    embed.add_field(
        name=f"{prefix}picl",
        value="gives you a picl photo",
        inline=False,
    )
    embed.add_field(
        name="what",
        value="dawg",
        inline=False,
    )
    embed.add_field(
        name="crazy",
        value="i was crazy once",
        inline=False,
    )
    embed.set_footer(text="picl • made by joshatticus")
    try:
        await ctx.send(embed=embed)
    except discord.HTTPException:
        # Fallback plain text
        lines = [
            "Picl Help:",
            f"{prefix}picl - gives you a picl photo",
            "what - dawg",
        ]
        try:
            await ctx.send("\n".join(lines))
        except discord.HTTPException:
            pass


def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN not set. Put it in a .env file or env var.")
    bot.run(TOKEN)


# ---------------- Inactivity Enforcement ---------------- #

async def _populate_active_senders_initial():
    """Sample recent channel history to avoid false positives after a restart.

    We scan a limited number of messages per text channel (configurable) to mark users
    who have previously spoken so they aren't kicked incorrectly.
    """
    await bot.wait_until_ready()
    if not bot.guilds:
        return
    guild = bot.guilds[0]
    logger.info("Sampling recent history in %s for active senders", guild.name)
    for channel in guild.text_channels:
        if not channel.permissions_for(guild.me).read_message_history:  # type: ignore
            continue
        try:
            async for msg in channel.history(limit=CHANNEL_HISTORY_SAMPLING_LIMIT, oldest_first=False):
                if msg.author.bot:
                    continue
                _active_senders.add(msg.author.id)
        except Exception:
            # Ignore history fetch errors / missing perms
            continue
    logger.info("Active sender sample size: %d", len(_active_senders))


async def _inactivity_enforcement_loop():
    await bot.wait_until_ready()
    while not bot.is_closed():
        try:
            await _run_inactivity_check()
        except Exception:
            logger.exception("Inactivity enforcement loop iteration failed")
        await asyncio.sleep(INACTIVE_CHECK_INTERVAL_SECONDS)


async def _run_inactivity_check():
    if not bot.guilds:
        return
    guild = bot.guilds[0]
    if not guild.me.guild_permissions.kick_members:  # type: ignore
        logger.debug("Skipping inactivity check: missing kick_members permission")
        return
    now = discord.utils.utcnow()
    threshold = now.timestamp() - (INACTIVE_KICK_DAYS * 86400)
    to_kick: list[discord.Member] = []
    for member in guild.members:
        if member.bot:
            continue
        # If they have spoken we skip
        if member.id in _active_senders:
            continue
        joined_at = member.joined_at
        if not joined_at:
            continue
        if joined_at.timestamp() <= threshold:
            to_kick.append(member)
    if not to_kick:
        return
    logger.info("Found %d inactive silent members to kick", len(to_kick))
    for member in to_kick:
        dm_text = (
            "You've joined our server but haven't said anything yet. To prevent message scraping bots, "
            "we've kicked you from the server. Feel free to rejoin at a later time."
        )
        try:
            try:
                await member.send(dm_text)
            except discord.HTTPException:
                pass
            await guild.kick(member, reason="Possible message scraping bot; no messages for 7 days")
            logger.info("Kicked inactive member %s (%s)", member, member.id)
        except discord.Forbidden:
            logger.warning("Forbidden kicking %s", member)
        except discord.HTTPException:
            logger.exception("HTTPException kicking %s", member)


if __name__ == "__main__":
    main()
