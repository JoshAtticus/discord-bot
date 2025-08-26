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

intents = discord.Intents.default()
intents.message_content = True
intents.members = True  # required for member join events

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("picl")

# Per-command log capture structures
_current_command_log: contextvars.ContextVar[list] = contextvars.ContextVar("current_command_log", default=None)  # type: ignore[arg-type]
_logs_by_message: dict[int, str] = {}


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
    if channel:
        try:
            await channel.send(f"Welcome to the server, {member.mention}! 🎉")
        except discord.Forbidden:
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
    if (
        content_lower == "what"
        and message.channel.category_id == CATEGORY_ID_FOR_WHAT
    ):
        try:
            await message.reply("https://i.ibb.co/ccKSZKwj/image.png")
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


if __name__ == "__main__":
    main()
