import os
import random
import asyncio
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


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Feature: respond to 'what' in category
    if (
        message.content.lower().strip() == "what"
        and message.channel.category_id == CATEGORY_ID_FOR_WHAT
    ):
        try:
            await message.reply("https://i.ibb.co/ccKSZKwj/image.png")
        except discord.HTTPException:
            pass

    await bot.process_commands(message)


@bot.command(name="picl")
async def picl_command(ctx: commands.Context):
    """Send a random photo from the photos directory."""
    if not os.path.isdir(PHOTOS_DIR):
        await ctx.send("Photos directory not found.")
        return

    # Collect valid image files
    exts = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
    files = [
        os.path.join(PHOTOS_DIR, f)
        for f in os.listdir(PHOTOS_DIR)
        if os.path.isfile(os.path.join(PHOTOS_DIR, f)) and os.path.splitext(f)[1].lower() in exts
    ]
    if not files:
        await ctx.send("No images available.")
        return

    chosen = random.choice(files)
    try:
        await ctx.send(file=discord.File(chosen))
    except discord.HTTPException:
        await ctx.send("Couldn't send image.")


def main():
    if not TOKEN:
        raise SystemExit("DISCORD_TOKEN not set. Put it in a .env file or env var.")
    bot.run(TOKEN)


if __name__ == "__main__":
    main()
