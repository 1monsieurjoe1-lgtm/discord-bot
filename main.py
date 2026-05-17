import discord
from discord.ext import commands
from discord import app_commands
import os
import psycopg2
from datetime import datetime, timedelta

# Intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.reactions = True
intents.voice_states = True

# Bot setup
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Database setup
def get_db_connection():
    return psycopg2.connect(os.environ.get("DATABASE_URL"))

def init_db():
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS welcome_settings (
            guild_id BIGINT PRIMARY KEY,
            channel_id BIGINT,
            message TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_balances (
            guild_id BIGINT,
            user_id BIGINT,
            balance BIGINT DEFAULT 0,
            last_daily TIMESTAMP,
            PRIMARY KEY (guild_id, user_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS shop_items (
            id SERIAL PRIMARY KEY,
            guild_id BIGINT,
            role_id BIGINT,
            price BIGINT,
            UNIQUE (guild_id, role_id)
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS fairy_roles (
            id SERIAL PRIMARY KEY,
            guild_id BIGINT,
            buyer_id BIGINT,
            recipient_id BIGINT,
            role_id BIGINT,
            role_name TEXT,
            purchase_date TIMESTAMP,
            is_gifted BOOLEAN,
            is_self_purchased BOOLEAN,
            removal_fee_required BOOLEAN DEFAULT FALSE
        )
    """)
    # Reaction reward tracking (prevent duplicate rewards per user/message)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS reaction_rewards (
            guild_id BIGINT,
            message_id BIGINT,
            reactor_id BIGINT,
            PRIMARY KEY (guild_id, message_id, reactor_id)
        )
    """)
    # Level-up rewards tracking (reward once per level)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS level_rewards (
            guild_id BIGINT,
            user_id BIGINT,
            level INT,
            PRIMARY KEY (guild_id, user_id, level)
        )
    """)
    # Photo post cooldown tracking (one reward per user per hour)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS photo_rewards (
            guild_id BIGINT,
            user_id BIGINT,
            last_rewarded TIMESTAMP,
            PRIMARY KEY (guild_id, user_id)
        )
    """)
    conn.commit()
    cur.close()
    conn.close()

init_db()

# Voice session tracker (in-memory: user_id -> join_timestamp)
voice_sessions = {}

# Photo reward channel IDs (more reliable than names)
PHOTO_REWARD_CHANNELS = {
    1393662784712999043,  # confess-to-the-whistleblower
    1402579226896367747,  # mirror
    1464911654016913483,  # my-manor-story
    1397976417811300473,  # dishcourse
    1371194105761370143,  # once-upon-a-canvas
    1397989152938528788,  # pawtrait-gallery
}

# Fairy Role System Constants
FAIRY_SELF_PRICE = 1000
FAIRY_GIFT_PRICE = 1000
FAIRY_REMOVAL_FEE = 200

# ─────────────────────────────────────────────
# Economy Utilities
# ─────────────────────────────────────────────

def get_balance(guild_id, user_id):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT balance FROM user_balances WHERE guild_id = %s AND user_id = %s", (guild_id, user_id))
    row = cur.fetchone()
    cur.close()
    conn.close()
    return row[0] if row else 0

def update_balance(guild_id, user_id, amount):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO user_balances (guild_id, user_id, balance)
        VALUES (%s, %s, %s)
        ON CONFLICT (guild_id, user_id) DO UPDATE SET balance = user_balances.balance + EXCLUDED.balance
    """, (guild_id, user_id, amount))
    conn.commit()
    cur.close()
    conn.close()

# ─────────────────────────────────────────────
# Events
# ─────────────────────────────────────────────

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Bot is online as {bot.user}")

# Welcome on member join
@bot.event
async def on_member_join(member):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT channel_id, message FROM welcome_settings WHERE guild_id = %s", (member.guild.id,))
    row = cur.fetchone()
    cur.close()
    conn.close()

    channel_id = row[0] if row and row[0] else None
    welcome_msg = row[1] if row and row[1] else "Welcome to {server}, {user}! You are our {memberCount}th member."

    if channel_id:
        channel = member.guild.get_channel(channel_id)
        if channel:
            formatted_msg = welcome_msg.format(
                user=member.mention,
                server=member.guild.name,
                memberCount=member.guild.member_count
            )
            await channel.send(formatted_msg)

# ─── FEATURE 1: Reaction Rewards ───
@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    # Ignore bot reactions
    if payload.member and payload.member.bot:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return

    # Only count reactions in the announcements channel (ID: 1325507534957576254)
    ANNOUNCEMENTS_CHANNEL_ID = 1325507534957576254
    channel = guild.get_channel(payload.channel_id)
    if not channel or channel.id != ANNOUNCEMENTS_CHANNEL_ID:
        return

    try:
        message = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        return

    # Ignore bots' messages and self-reactions
    if message.author.bot:
        return
    if message.author.id == payload.user_id:
        return

    # Prevent duplicate reward for same reactor on same message
    conn = get_db_connection()
    cur = conn.cursor()
    try:
        cur.execute("""
            INSERT INTO reaction_rewards (guild_id, message_id, reactor_id)
            VALUES (%s, %s, %s)
        """, (payload.guild_id, payload.message_id, payload.user_id))
        conn.commit()
    except psycopg2.errors.UniqueViolation:
        # Already rewarded this reactor on this message
        cur.close()
        conn.close()
        return
    cur.close()
    conn.close()

    # Reward the reactor 1 coin
    reactor = payload.member
    update_balance(payload.guild_id, reactor.id, 1)
    print(f"[Reaction Reward] +1 coin to {reactor} for reacting to message {payload.message_id}")
    await channel.send(f"💰 {reactor.mention} earned **1 coin** for reacting to an announcement!", delete_after=86400)

# ─── FEATURE 2: Level-Up Coin Rewards (Noctaly bot integration) ───
@bot.event
async def on_message(message):
    # Level-up detection: listen in Noctaly level updates channel (ID: 1354903685259595927)
    NOCTALY_CHANNEL_ID = 1354903685259595927
    if message.author.bot and message.channel.id == NOCTALY_CHANNEL_ID:
        # Noctaly typically mentions the user and says "level X" in the message
        if message.mentions and ("level" in message.content.lower() or (message.embeds and any("level" in str(e.description).lower() for e in message.embeds))):
            for user in message.mentions:
                if user.bot:
                    continue
                # Extract level from message content or embed
                import re
                content_to_search = message.content
                for embed in message.embeds:
                    if embed.description:
                        content_to_search += " " + embed.description
                    if embed.title:
                        content_to_search += " " + embed.title

                match = re.search(r'level[^\d]*(\d+)', content_to_search, re.IGNORECASE)
                if match:
                    level = int(match.group(1))
                    if level >= 20:
                        conn = get_db_connection()
                        cur = conn.cursor()
                        try:
                            cur.execute("""
                                INSERT INTO level_rewards (guild_id, user_id, level)
                                VALUES (%s, %s, %s)
                            """, (message.guild.id, user.id, level))
                            conn.commit()
                            cur.close()
                            conn.close()
                        except psycopg2.errors.UniqueViolation:
                            # Already rewarded this level
                            cur.close()
                            conn.close()
                            continue

                        update_balance(message.guild.id, user.id, 20)
                        print(f"[Level Reward] +20 coins to {user} for reaching level {level}")
                        await message.channel.send(f"🎉 Congrats {user.mention}, you reached level **{level}** and earned **20 coins**!")

    # ─── FEATURE 4: Photo Post Rewards ───
    if message.author.bot:
        await bot.process_commands(message)
        return

    if message.guild and message.channel.id in PHOTO_REWARD_CHANNELS:
        print(f"[Photo] Message in #{message.channel.name} from {message.author} with {len(message.attachments)} attachment(s)")
        # Reward any attachment in a dedicated photo channel
        has_image = len(message.attachments) > 0
        if has_image:
            conn = get_db_connection()
            cur = conn.cursor()
            cur.execute("SELECT last_rewarded FROM photo_rewards WHERE guild_id = %s AND user_id = %s",
                        (message.guild.id, message.author.id))
            row = cur.fetchone()
            now = datetime.now()
            # Cooldown: 1 reward per hour per user in photo channels
            if not row or (now - row[0]) >= timedelta(seconds=10):
                cur.execute("""
                    INSERT INTO photo_rewards (guild_id, user_id, last_rewarded)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (guild_id, user_id) DO UPDATE SET last_rewarded = EXCLUDED.last_rewarded
                """, (message.guild.id, message.author.id, now))
                conn.commit()
                cur.close()
                conn.close()
                update_balance(message.guild.id, message.author.id, 10)
                print(f"[Photo Reward] +10 coins to {message.author} in #{message.channel.name}")
                await message.channel.send(f"📸 {message.author.mention} earned **10 coins** for sharing a photo!", delete_after=86400)
            else:
                cur.close()
                conn.close()

    await bot.process_commands(message)

# ─── FEATURE 3: Voice Chat Activity Rewards ───
@bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return

    guild_id = member.guild.id
    user_id = member.id
    now = datetime.now()

    # User joined a voice channel
    if before.channel is None and after.channel is not None:
        # Only track if not muted/deafened at join
        if not after.self_mute and not after.self_deaf:
            voice_sessions[user_id] = now
            print(f"[Voice] {member} joined VC at {now}")

    # User left or changed state
    elif before.channel is not None:
        # If they left entirely or became muted/deafened, check session
        left_vc = after.channel is None
        became_muted = (not before.self_mute and after.self_mute) or (not before.self_deaf and after.self_deaf)

        if left_vc or became_muted:
            join_time = voice_sessions.pop(user_id, None)
            if join_time:
                elapsed = now - join_time
                # Must have been in VC for at least 5 minutes
                if elapsed >= timedelta(minutes=5):
                    update_balance(guild_id, user_id, 10)
                    print(f"[Voice Reward] +10 coins to {member} ({int(elapsed.total_seconds()//60)} min in VC)")
                    if before.channel:
                        await before.channel.send(f"🎙️ {member.mention} earned **10 coins** for being active in voice chat!", delete_after=86400)

        # If they joined a new channel (moved), restart session if not muted
        if not left_vc and after.channel is not None:
            if not after.self_mute and not after.self_deaf:
                voice_sessions[user_id] = now

# ─────────────────────────────────────────────
# Economy Commands
# ─────────────────────────────────────────────

@bot.tree.command(name="balance", description="Check your current coin balance")
async def balance(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    bal = get_balance(interaction.guild_id, target.id)
    await interaction.response.send_message(f"💰 {target.display_name}'s balance: **{bal}** coins")

@bot.tree.command(name="daily", description="Claim your daily coins")
async def daily(interaction: discord.Interaction):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT last_daily FROM user_balances WHERE guild_id = %s AND user_id = %s", (interaction.guild_id, interaction.user.id))
    row = cur.fetchone()

    now = datetime.now()
    if row and row[0] and now - row[0] < timedelta(days=1):
        wait_time = timedelta(days=1) - (now - row[0])
        hours, remainder = divmod(int(wait_time.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        await interaction.response.send_message(f"⏳ You've already claimed your daily! Wait {hours}h {minutes}m.", ephemeral=True)
        cur.close()
        conn.close()
        return

    # ─── FEATURE 5: Daily reward changed from 100 → 5 coins ───
    amount = 5
    cur.execute("""
        INSERT INTO user_balances (guild_id, user_id, balance, last_daily)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (guild_id, user_id) DO UPDATE SET
            balance = user_balances.balance + EXCLUDED.balance,
            last_daily = EXCLUDED.last_daily
    """, (interaction.guild_id, interaction.user.id, amount, now))
    conn.commit()
    cur.close()
    conn.close()
    await interaction.response.send_message(f"🎁 You claimed **{amount}** daily coins!")

@bot.tree.command(name="give", description="Give coins to another user")
async def give(interaction: discord.Interaction, member: discord.Member, amount: int):
    if amount <= 0:
        await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
        return

    sender_bal = get_balance(interaction.guild_id, interaction.user.id)
    if sender_bal < amount:
        await interaction.response.send_message("❌ You don't have enough coins.", ephemeral=True)
        return

    update_balance(interaction.guild_id, interaction.user.id, -amount)
    update_balance(interaction.guild_id, member.id, amount)
    await interaction.response.send_message(f"✅ Gave **{amount}** coins to {member.mention}!")

# ─────────────────────────────────────────────
# Admin Economy Commands
# ─────────────────────────────────────────────

@bot.tree.command(name="add_coins", description="Add coins to a user (Admins only)")
@app_commands.checks.has_permissions(administrator=True)
async def add_coins(interaction: discord.Interaction, member: discord.Member, amount: int):
    update_balance(interaction.guild_id, member.id, amount)
    await interaction.response.send_message(f"✅ Added **{amount}** coins to {member.mention}!")

@bot.tree.command(name="reset_balance", description="Reset a user's balance (Admins only)")
@app_commands.checks.has_permissions(administrator=True)
async def reset_balance(interaction: discord.Interaction, member: discord.Member):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("UPDATE user_balances SET balance = 0 WHERE guild_id = %s AND user_id = %s", (interaction.guild_id, member.id))
    conn.commit()
    cur.close()
    conn.close()
    await interaction.response.send_message(f"✅ Reset balance for {member.mention}.")

# ─────────────────────────────────────────────
# Shop Commands
# ─────────────────────────────────────────────

@bot.tree.command(name="add_shop_item", description="Add a role to the shop (Admins only)")
@app_commands.checks.has_permissions(administrator=True)
async def add_shop_item(interaction: discord.Interaction, role: discord.Role, price: int):
    if price <= 0:
        await interaction.response.send_message("❌ Price must be positive.", ephemeral=True)
        return
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO shop_items (guild_id, role_id, price)
        VALUES (%s, %s, %s)
        ON CONFLICT (guild_id, role_id) DO UPDATE SET price = EXCLUDED.price
    """, (interaction.guild_id, role.id, price))
    conn.commit()
    cur.close()
    conn.close()
    await interaction.response.send_message(f"✅ Added {role.name} to the shop for **{price}** coins!")

@bot.tree.command(name="remove_shop_item", description="Remove a role from the shop (Admins only)")
@app_commands.checks.has_permissions(manage_roles=True)
async def remove_shop_item(interaction: discord.Interaction, role: discord.Role):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM shop_items WHERE guild_id = %s AND role_id = %s", (interaction.guild_id, role.id))
    rows_deleted = cur.rowcount
    conn.commit()
    cur.close()
    conn.close()

    if rows_deleted > 0:
        await interaction.response.send_message(f"✅ Removed {role.name} from the shop.", ephemeral=True)
    else:
        await interaction.response.send_message("❌ That role is not in the shop.", ephemeral=True)

@bot.tree.command(name="shop", description="View the available roles in the shop")
async def shop(interaction: discord.Interaction):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT role_id, price FROM shop_items WHERE guild_id = %s", (interaction.guild_id,))
    items = cur.fetchall()
    cur.close()
    conn.close()

    if not items:
        await interaction.response.send_message("🏪 The shop is currently empty.")
        return

    embed = discord.Embed(title="🏪 Manor Shop", color=discord.Color.gold())
    for role_id, price in items:
        role = interaction.guild.get_role(role_id)
        role_name = role.name if role else f"Unknown Role ({role_id})"
        embed.add_field(name=role_name, value=f"💰 Price: **{price}** coins", inline=False)
    embed.add_field(name="✨ Custom Fairy Role", value="Want a personalised fairy role? Use `/buy_fairy_role` — costs **1000 coins**!", inline=False)

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="buy", description="Buy a role from the shop")
async def buy(interaction: discord.Interaction, role: discord.Role):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT price FROM shop_items WHERE guild_id = %s AND role_id = %s", (interaction.guild_id, role.id))
    row = cur.fetchone()
    cur.close()
    conn.close()

    if not row:
        await interaction.response.send_message("❌ That role is not in the shop.", ephemeral=True)
        return

    price = row[0]
    user_bal = get_balance(interaction.guild_id, interaction.user.id)

    if user_bal < price:
        await interaction.response.send_message(f"❌ You need **{price}** coins, but you only have **{user_bal}**.", ephemeral=True)
        return

    if role in interaction.user.roles:
        await interaction.response.send_message("❌ You already have this role!", ephemeral=True)
        return

    try:
        await interaction.user.add_roles(role)
        update_balance(interaction.guild_id, interaction.user.id, -price)
        await interaction.response.send_message(f"🎉 Success! You bought {role.mention} for **{price}** coins.")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to give you that role. Please check my role hierarchy.", ephemeral=True)

# ─────────────────────────────────────────────
# Fairy Role Commands
# ─────────────────────────────────────────────

@bot.tree.command(name="buy_fairy_role", description="Buy a custom fairy role for yourself (1000 coins) — enter the name after 'Fairy'")
async def buy_fairy_role(interaction: discord.Interaction, type_of_fairy: str):
    # Always prepend "Fairy of " to the role name
    full_name = f"Fairy of {type_of_fairy.strip()}"
    if len(full_name) > 32:
        await interaction.response.send_message("❌ Role name too long (max 23 chars after 'Fairy of ').", ephemeral=True)
        return

    if any(word in full_name.lower() for word in ["nazi", "admin", "moderator", "staff"]):
        await interaction.response.send_message("❌ Inappropriate or restricted role name.", ephemeral=True)
        return

    user_bal = get_balance(interaction.guild_id, interaction.user.id)
    if user_bal < FAIRY_SELF_PRICE:
        await interaction.response.send_message(f"❌ You need **{FAIRY_SELF_PRICE}** coins.", ephemeral=True)
        return

    role_name = full_name

    try:
        role = await interaction.guild.create_role(name=role_name, reason=f"Fairy role purchase by {interaction.user}")
        await interaction.user.add_roles(role)
        update_balance(interaction.guild_id, interaction.user.id, -FAIRY_SELF_PRICE)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO fairy_roles (guild_id, buyer_id, recipient_id, role_id, role_name, purchase_date, is_gifted, is_self_purchased)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (interaction.guild_id, interaction.user.id, interaction.user.id, role.id, role_name, datetime.now(), False, True))
        conn.commit()
        cur.close()
        conn.close()

        await interaction.response.send_message(f"✨ Successfully bought your fairy role: **{role_name}**!")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to create roles.", ephemeral=True)

@bot.tree.command(name="gift_fairy_role", description="Gift a custom fairy role to someone else (1000 coins) — enter the name after 'Fairy'")
async def gift_fairy_role(interaction: discord.Interaction, recipient: discord.Member, type_of_fairy: str):
    # Always prepend "Fairy of " to the role name
    full_name = f"Fairy of {type_of_fairy.strip()}"
    if len(full_name) > 32:
        await interaction.response.send_message("❌ Role name too long (max 23 chars after 'Fairy of ').", ephemeral=True)
        return

    user_bal = get_balance(interaction.guild_id, interaction.user.id)
    if user_bal < FAIRY_GIFT_PRICE:
        await interaction.response.send_message(f"❌ You need **{FAIRY_GIFT_PRICE}** coins.", ephemeral=True)
        return

    role_name = full_name

    try:
        role = await interaction.guild.create_role(name=role_name, reason=f"Fairy role gift from {interaction.user} to {recipient}")
        await recipient.add_roles(role)
        update_balance(interaction.guild_id, interaction.user.id, -FAIRY_GIFT_PRICE)

        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO fairy_roles (guild_id, buyer_id, recipient_id, role_id, role_name, purchase_date, is_gifted, is_self_purchased, removal_fee_required)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (interaction.guild_id, interaction.user.id, recipient.id, role.id, role_name, datetime.now(), True, False, True))
        conn.commit()
        cur.close()
        conn.close()

        await interaction.response.send_message(f"🎁 You gifted the fairy role **{role_name}** to {recipient.mention}!")
    except discord.Forbidden:
        await interaction.response.send_message("❌ Permission error creating roles.", ephemeral=True)

@bot.tree.command(name="remove_fairy_role", description="Remove a fairy role (Costs 200 if gifted to you)")
async def remove_fairy_role(interaction: discord.Interaction, role: discord.Role):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("SELECT id, removal_fee_required, is_self_purchased FROM fairy_roles WHERE guild_id = %s AND recipient_id = %s AND role_id = %s",
                (interaction.guild_id, interaction.user.id, role.id))
    row = cur.fetchone()

    if not row:
        await interaction.response.send_message("❌ This doesn't seem to be a fairy role registered to you.", ephemeral=True)
        cur.close()
        conn.close()
        return

    record_id, fee_required, is_self = row

    if is_self:
        await interaction.response.send_message("❌ You cannot remove a self-assigned fairy role.", ephemeral=True)
        cur.close()
        conn.close()
        return

    if fee_required:
        user_bal = get_balance(interaction.guild_id, interaction.user.id)
        if user_bal < FAIRY_REMOVAL_FEE:
            await interaction.response.send_message(f"❌ You need **{FAIRY_REMOVAL_FEE}** coins to remove a gifted role.", ephemeral=True)
            cur.close()
            conn.close()
            return
        update_balance(interaction.guild_id, interaction.user.id, -FAIRY_REMOVAL_FEE)

    try:
        await role.delete(reason="Fairy role removal by recipient")
        cur.execute("DELETE FROM fairy_roles WHERE id = %s", (record_id,))
        conn.commit()
        await interaction.response.send_message(f"✅ Fairy role removed. {f'Deducted {FAIRY_REMOVAL_FEE} coins.' if fee_required else ''}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ I don't have permission to delete this role.", ephemeral=True)
    finally:
        cur.close()
        conn.close()

@bot.tree.command(name="admin_remove_fairy", description="Admin override to remove a fairy role")
@app_commands.checks.has_permissions(administrator=True)
async def admin_remove_fairy(interaction: discord.Interaction, role: discord.Role):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("DELETE FROM fairy_roles WHERE guild_id = %s AND role_id = %s", (interaction.guild_id, role.id))
    conn.commit()
    cur.close()
    conn.close()

    try:
        await role.delete(reason=f"Admin override removal by {interaction.user}")
        await interaction.response.send_message(f"✅ Admin: Removed and deleted fairy role **{role.name}**.")
    except discord.Forbidden:
        await interaction.response.send_message("❌ Permission error deleting role.")

# ─────────────────────────────────────────────
# Welcome Commands
# ─────────────────────────────────────────────

@bot.tree.command(name="set_welcome_channel", description="Set the channel for welcome messages")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_welcome_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO welcome_settings (guild_id, channel_id)
        VALUES (%s, %s)
        ON CONFLICT (guild_id) DO UPDATE SET channel_id = EXCLUDED.channel_id
    """, (interaction.guild_id, channel.id))
    conn.commit()
    cur.close()
    conn.close()
    await interaction.response.send_message(f"✅ Welcome channel set to {channel.mention}", ephemeral=True)

@bot.tree.command(name="set_welcome_message", description="Set the custom welcome message")
@app_commands.checks.has_permissions(manage_guild=True)
async def set_welcome_message(interaction: discord.Interaction, message: str):
    conn = get_db_connection()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO welcome_settings (guild_id, message)
        VALUES (%s, %s)
        ON CONFLICT (guild_id) DO UPDATE SET message = EXCLUDED.message
    """, (interaction.guild_id, message))
    conn.commit()
    cur.close()
    conn.close()
    await interaction.response.send_message("✅ Welcome message updated!", ephemeral=True)

# ─────────────────────────────────────────────
# Text Commands
# ─────────────────────────────────────────────

@bot.command()
async def about(ctx):
    about_text = (
        "Manor de Everleigh is an active events and social server hosting challenges, "
        "games, and giveaways, paired with regular VC hangouts.\n\n"
        "We play across Roblox and other games, watch movies together, and create a fun, "
        "welcoming space where members can relax, connect, and enjoy every moment."
    )
    await ctx.send(about_text)

@bot.command()
async def help(ctx):
    await ctx.send("A moderator will be with you shortly <@&1345821183706402868>.")

@bot.tree.command(name="say", description="Make the bot say something (Mods only)")
@app_commands.checks.has_permissions(manage_messages=True)
async def say(interaction: discord.Interaction, message: str):
    await interaction.response.send_message("✅ Sent!", ephemeral=True)
    if interaction.channel and hasattr(interaction.channel, "send"):
        await interaction.channel.send(message)

@say.error
async def say_error(interaction: discord.Interaction, error):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "❌ You don't have permission to use this command.",
            ephemeral=True
        )

# ─────────────────────────────────────────────
# Run
# ─────────────────────────────────────────────

token = os.environ.get("DISCORD_TOKEN")
if not token:
    print("Error: DISCORD_TOKEN environment variable not set.")
else:
    bot.run(token)
