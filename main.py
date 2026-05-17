import discord
from discord.ext import commands
from discord import app_commands
import os
from datetime import datetime, timedelta

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# ─────────────────────────────
# SIMPLE IN-MEMORY BALANCE (NO DATABASE)
# ─────────────────────────────

balances = {}

def get_balance(user_id):
    return balances.get(user_id, 0)

def update_balance(user_id, amount):
    balances[user_id] = get_balance(user_id) + amount

# ─────────────────────────────
# EVENTS
# ─────────────────────────────

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Bot is online as {bot.user}")

# ─────────────────────────────
# SIMPLE COMMANDS
# ─────────────────────────────

@bot.tree.command(name="balance", description="Check your coins")
async def balance(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    bal = get_balance(target.id)
    await interaction.response.send_message(f"💰 {target.display_name}: **{bal} coins**")

@bot.tree.command(name="daily", description="Get daily coins")
async def daily(interaction: discord.Interaction):
    update_balance(interaction.user.id, 5)
    await interaction.response.send_message("🎁 You got **5 coins**!")

@bot.tree.command(name="give", description="Give coins")
async def give(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount <= 0:
        return await interaction.response.send_message("Invalid amount", ephemeral=True)

    if get_balance(interaction.user.id) < amount:
        return await interaction.response.send_message("Not enough coins", ephemeral=True)

    update_balance(interaction.user.id, -amount)
    update_balance(member.id, amount)

    await interaction.response.send_message(f"✅ Gave {amount} coins to {member.mention}")

# ─────────────────────────────
# SAY COMMAND
# ─────────────────────────────

@bot.tree.command(name="say", description="Bot says message")
@app_commands.checks.has_permissions(manage_messages=True)
async def say(interaction: discord.Interaction, message: str):
    await interaction.response.send_message("Sent!", ephemeral=True)
    await interaction.channel.send(message)

# ─────────────────────────────
# RUN BOT
# ─────────────────────────────

token = os.environ.get("DISCORD_TOKEN")

if not token:
    print("DISCORD_TOKEN missing")
else:
    bot.run(token)
