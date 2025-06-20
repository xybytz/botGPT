# File: duckAI.py
# Author: ZackDaQuack, xybytz (remade for g4f)
# Last Edited: 06/20/2025

import re
import io
import json
import asyncio
import configparser
import urllib.parse
from random import randint
from datetime import datetime, timedelta

import aiohttp
from PIL import Image
from ratelimit import limits, RateLimitException

import discord
from discord.ext import commands, tasks

import g4f  # replacing gemini hehe
from modules.duckLog import logger
from storage.lists import random_ratelimit, random_justice, random_ai

config = configparser.ConfigParser()
config.read("config.ini")

# config values
guild_id = [int(config.get("GENERAL", "allowed_guild"))]
brain_memory = int(config.get("AI", "brain_memory"))
report_channel = int(config.get("AI", "report_channel"))
characters_in = int(config.get("AI", "max_characters_in"))
user_ratelimit = int(config.get("AI", "user_ratelimit"))
max_timeout = int(config.get("AI", "max_punishment_timeout"))

# minor config stuff (too lazy to add to config.ini)
blacklisted_input_words = ["jailbroken", "commands", "command", "nightfall"]
min_char_per_line = 50
message_pause_multiple = .02

# load prompt
with open("storage/duck_prompt.txt", "r", encoding="utf-8") as f:
    text_prompt = f.read()

async def censor_text(message: str) -> str:
    return (
        message.replace('@', '＠')
               .replace('discord.gg', '[NOPE]')
               .replace('https://', '')
    )

async def validate_input(text: str) -> bool:
    return any(word in text.lower() for word in blacklisted_input_words)

def split_response(text: str) -> list[str]:
    sentences = re.split(r'(?<!\w\.\w.)(?<![A-Z][a-z]\.)(?<=\.|\?|!)\s', text)
    out, i = [], 0
    while i < len(sentences):
        s = sentences[i]
        if len(s) <= 50 and i + 1 < len(sentences):
            out.append(s.strip() + ' ' + sentences[i+1].strip())
            i += 2
        else:
            out.append(s)
            i += 1
    return out

async def gen_image(prompt: str):
    try:
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=90, sock_read=90)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            url = (
                "https://pollinations.ai/p/"
                + urllib.parse.quote(prompt)
                + f"?width=512&height=512&seed={randint(10000,99999)}"
            )
            async with session.get(url) as resp:
                if resp.status == 200:
                    return io.BytesIO(await resp.read())
                logger.error("Error fetching ai image! (status error)")
    except Exception as e:
        logger.error(f"Error fetching ai image: {e}")
    return None

async def generate_payload(message, status: bool) -> str:
    now = datetime.now().isoformat()
    payload = f"""userName: {message.author.name}
userId: {message.author.id}
isStaff: {bool(discord.utils.get(message.author.roles, name="Staff"))}
isUp: {status}
currentChannel: {message.channel.name}
currentTime: {now}
message: {message.content[:characters_in]}
"""
    if message.reference:
        try:
            ref = await message.channel.fetch_message(message.reference.message_id)
            payload += (
                f"replyMessage: {ref.content}\n"
                f"replyMessageUser: {ref.author.name} {ref.author.id}\n"
            )
        except discord.NotFound:
            logger.warning("Referenced message not found.")
        except discord.HTTPException as e:
            logger.error(f"Error fetching referenced message: {e}")
    return payload

async def handle_image(message) -> io.BytesIO | None:
    if not message.attachments:
        return None
    a = message.attachments[0]
    if not any(a.filename.lower().endswith(ext) for ext in (".jpg",".jpeg",".png",".webp",".gif")):
        return None
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.get(a.url) as resp:
                if resp.status != 200:
                    logger.error(f"Error downloading image: {resp.status}")
                    await message.channel.send("Quack! I can't see the image because of an error!")
                    return None
                return io.BytesIO(await resp.read())
    except aiohttp.ClientError as e:
        logger.error(f"Image download error: {e}")
        await message.channel.send("Quack! I can't see the image because of an error!")
    return None

class DuckAI:
    def __init__(self):
        self.history = [
            {"role": "system", "content": f"SYSTEM STARTUP: BOT ONLINE; TIME: {datetime.now().isoformat()};"},
            {"role": "system", "content": text_prompt}
        ]
        self.memory = 0

    @limits(calls=10, period=60)
    async def send_chat(self, message_parts: list, ctx) -> str:
        txt = message_parts[0]
        if self.memory == 0:
            txt += f"SERVER EMOJIS: {' '.join(str(e) for e in ctx.guild.emojis)}"
        self.memory += 1
        if self.memory >= brain_memory:
            await self.brainwash()

        self.history.append({"role": "user", "content": txt})
        try:
            resp = g4f.ChatCompletion.create(
                model="gpt-4o-mini",
                messages=self.history,
                stream=False
            )
            clean = await censor_text(resp)
            # keep history bounded
            self.history.append({"role": "assistant", "content": clean})
            return clean
        except Exception as e:
            logger.error(f"g4f error: {e}")
            return json.dumps({"message": "Quack! I encountered an unexpected error."})

    async def brainwash(self, user="SYSTEM"):
        self.memory = 0
        self.history = [
            {"role": "system", "content": f"Your brain was erased at {datetime.now().isoformat()} by {user}."},
            {"role": "system", "content": text_prompt}
        ]

duck_brain = DuckAI()

class Ai(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.ratelimited = {}
        self.message_queue = asyncio.Queue()
        self.processing_queue = False
        self.process_queue_task.start()

    @tasks.loop()
    async def process_queue_task(self):
        if self.processing_queue:
            return
        self.processing_queue = True
        try:
            while not self.message_queue.empty():
                message, payload, img = await self.message_queue.get()
                try:
                    prompt_parts = [payload]
                    if img:
                        prompt_parts.append(Image.open(img).convert("RGB"))

                    raw = await duck_brain.send_chat(prompt_parts, message)
                    await self.apply_ratelimit(message.author.id)

                    response = json.loads(raw)
                except RateLimitException:
                    await message.channel.send(await random_ratelimit())
                    continue
                except json.JSONDecodeError as e:
                    await duck_brain.brainwash()
                   
                    logger.error(f"Error json: {e}")
                    continue
                except Exception as e:
                    logger.error(f"Processing error: {e}")
                    await message.channel.send("Quack! I hit an unexpected snag.")
                    continue

                # apply actions
                if msg := response.get("message"):
                    for i, chunk in enumerate(split_response(msg)):
                        if i > 10: break
                        await message.channel.trigger_typing()
                        await asyncio.sleep(min(message_pause_multiple*len(chunk), 10))
                        await message.channel.send(chunk[:1000], reference=message if i == 0 else None)

                if reaction := response.get("reaction"):
                    await message.add_reaction(reaction)

                if dm := response.get("dm"):
                    await message.author.send(dm)

                if ig := response.get("image_gen"):
                    m = await message.channel.send("Creating image...")
                    imgdata = await gen_image(ig)
                    if imgdata:
                        await m.edit(file=discord.File(imgdata, "quack_ai.jpeg"), content=None)
                    else:
                        await m.edit(content="Failed to create the image!")

                if report := response.get("report"):
                    await self.send_report(message, report)

                if response.get("deleteMessage"):
                    await message.delete()

                if timeout := response.get("timeoutUser"):
                    sec = max(0, min(max_timeout, timeout))
                    await message.author.timeout_for(timedelta(seconds=sec))

                self.message_queue.task_done()
        finally:
            self.processing_queue = False

    async def check_ratelimit(self, uid: int) -> bool:
        return uid in self.ratelimited and datetime.now() < self.ratelimited[uid]

    async def apply_ratelimit(self, uid: int):
        self.ratelimited[uid] = datetime.now() + timedelta(seconds=user_ratelimit)

    async def send_report(self, message, ai_response):
        report = (
            f"- <@{message.author.id}> triggered a duck report!\n"
            f"- userID: {message.author.id}\n- channel: {message.channel.name}"
        )
        embed = discord.Embed(title=await random_justice(), color=0xff0000)
        if message.author.avatar:
            embed.set_thumbnail(url=message.author.avatar.url)
        embed.add_field(name="Report", value=report, inline=False)
        embed.add_field(name="DuckAI", value=ai_response, inline=True)
        embed.add_field(name="Message", value=message.content[:800], inline=True)
        embed.set_footer(text="ZackDaQuack Systems")
        ch = await self.bot.fetch_channel(report_channel)
        await ch.send(embed=embed)

    @discord.slash_command(name="brainwash", description="Brainwashes the AI", guild_ids=guild_id)
    async def brainwash(self, ctx):
        if not ctx.author.guild_permissions.administrator:
            return await ctx.respond("Quack! You need to be an admin to run this!", ephemeral=True)
        await duck_brain.brainwash(ctx.author.name)
        logger.info(f"{ctx.author.name} brainwashed the AI")
        await ctx.respond("Operation successful!", ephemeral=True)

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.id == self.bot.user.id:
            return
        if self.bot.user.mentioned_in(message):
            if isinstance(message.channel, discord.DMChannel):
                return await message.author.send("Quack! I don't work in DMs! Please talk in the server.")
            if await self.check_ratelimit(message.author.id):
                logger.debug("User is ratelimited")
                return
            if await validate_input(message.content[:characters_in]):
                logger.warning("Blacklisted term used")
                return

            payload = await generate_payload(message, self.bot.isUp)
            img = await handle_image(message)

            try:
                await self.message_queue.put((message, payload, img))
                await self.apply_ratelimit(message.author.id)
            except Exception:
                logger.error("Queue error")
                await message.channel.send("Quack! I'm overwhelmed. Try again later.")

def setup(bot):
    bot.add_cog(Ai(bot))
