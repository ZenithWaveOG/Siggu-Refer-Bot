import os
import logging
import asyncio
import random
import string
from datetime import datetime
from typing import Dict, Tuple, Optional, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ChatMember
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ChatMemberHandler
from telegram.constants import ParseMode
import httpx
from supabase import create_client, Client
from aiohttp import web

# ================= CONFIG =================
TOKEN = os.environ.get("BOT_TOKEN", "YOUR_BOT_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "YOUR_SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "YOUR_SUPABASE_KEY")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "https://your-app.onrender.com/webhook")
ADMIN_IDS = [int(id) for id in os.environ.get("ADMIN_IDS", "123456789,987654321").split(",")]
VERIFY_SITE_URL = os.environ.get("VERIFY_SITE_URL", "https://your-app.onrender.com/v")

# Default costs (will be overridden by DB)
DEFAULT_WITHDRAW_POINTS_SHEIN = 3
DEFAULT_WITHDRAW_POINTS_BIGBASKET = 1

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ================= SUPABASE INIT =================
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# ================= HELPER FUNCTIONS =================
async def is_user_joined_channels(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is member of all force-join channels."""
    try:
        channels = supabase.table("channels").select("chat_id, channel_link").execute()
    except Exception as e:
        logger.error(f"Supabase query error in is_user_joined_channels: {e}")
        channels = supabase.table("channels").select("channel_link").execute()

    if not channels.data:
        return True

    all_joined = True
    for ch in channels.data:
        chat_id = ch.get("chat_id")
        link = ch.get("channel_link")
        try:
            if chat_id:
                member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
                if member.status not in ["member", "administrator", "creator"]:
                    all_joined = False
                    break
            elif link:
                # fallback to username from link
                chat_username = link.split("/")[-1]
                if chat_username:
                    member = await context.bot.get_chat_member(chat_id=f"@{chat_username}", user_id=user_id)
                    if member.status not in ["member", "administrator", "creator"]:
                        all_joined = False
                        break
            else:
                logger.warning(f"Channel entry has no chat_id or channel_link: {ch}")
        except Exception as e:
            logger.error(f"Error checking channel {ch}: {e}")
            all_joined = False
            break
    return all_joined
    
async def is_user_verified(user_id: int) -> bool:
    if user_id in ADMIN_IDS:
        return True
    user = supabase.table("users").select("verified").eq("user_id", user_id).execute()
    return user.data and user.data[0].get("verified", False)

async def get_referral_link(user_id: int, context: ContextTypes.DEFAULT_TYPE) -> str:
    bot_username = (await context.bot.get_me()).username
    return f"https://t.me/{bot_username}?start={user_id}"

def get_withdraw_points(voucher_type: str) -> int:
    """Get cost for a voucher type from admin_settings."""
    key = f"withdraw_points_{voucher_type}"
    res = supabase.table("admin_settings").select("value").eq("key", key).execute()
    if res.data:
        return int(res.data[0]["value"])
    # fallback defaults
    if voucher_type == "shein":
        return DEFAULT_WITHDRAW_POINTS_SHEIN
    elif voucher_type == "bigbasket":
        return DEFAULT_WITHDRAW_POINTS_BIGBASKET
    return 0

def set_withdraw_points(voucher_type: str, points: int):
    """Update cost for a voucher type."""
    key = f"withdraw_points_{voucher_type}"
    supabase.table("admin_settings").upsert({"key": key, "value": str(points)}).execute()

async def grant_pending_referral_bonus(user_id: int, bot):
    """Check if user has a referrer and hasn't yet been rewarded. If so, grant bonus."""
    user = supabase.table("users").select("referred_by, rewarded").eq("user_id", user_id).execute().data
    if not user:
        return
    user = user[0]
    referred_by = user.get("referred_by")
    rewarded = user.get("rewarded", False)
    if referred_by and not rewarded:
        # Grant bonus to referrer
        referrer = supabase.table("users").select("points, referrals").eq("user_id", referred_by).execute().data
        if referrer:
            referrer = referrer[0]
            new_points = referrer["points"] + 1
            new_refs = referrer["referrals"] + 1
            supabase.table("users").update({"points": new_points, "referrals": new_refs}).eq("user_id", referred_by).execute()
            # Mark referred user as rewarded
            supabase.table("users").update({"rewarded": True}).eq("user_id", user_id).execute()
            # Notify referrer
            try:
                await bot.send_message(
                    chat_id=referred_by,
                    text="<b>🎉 Referral Bonus!</b>\n\n💰 Earned +1 pt(s)\n✅ Full reward credited!\n\n⚠️ Note: If this user leaves any channel, your point will be deducted automatically.",
                    parse_mode=ParseMode.HTML
                )
            except Exception as e:
                logger.error(f"Failed to notify referrer {referred_by}: {e}")
            logger.info(f"Granted referral bonus to {referred_by} for {user_id} (first menu access)")

async def require_verified(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Check if user is verified AND still in all channels. Also grant pending referral bonus on first menu use."""
    user_id = update.effective_user.id
    if user_id in ADMIN_IDS:
        return True

    # Check verified status
    user = supabase.table("users").select("verified, referred_by").eq("user_id", user_id).execute()
    if not user.data or not user.data[0].get("verified", False):
        await update.message.reply_text("❌ You need to verify first. Use /start to begin.")
        return False

    # Check channel membership
    if not await is_user_joined_channels(user_id, context):
        # User left channels, show force join
        await show_force_join_message(update, context)
        # Deduct from referrer
        referred_by = user.data[0].get("referred_by")
        if referred_by:
            logger.info(f"Verified user {user_id} left channels, deducting point from referrer {referred_by}")
            await deduct_referral_bonus(referred_by, user_id, context.bot)
        return False

    # User is verified and in channels -> grant pending referral bonus if any
    await grant_pending_referral_bonus(user_id, context.bot)
    return True

# ================= FORCE JOIN HANDLERS =================
async def show_force_join_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    channels = supabase.table("channels").select("channel_link").execute()
    text = "<b>🚨 Force Join Required</b>\n\nPlease join the following channels first:\n"
    keyboard = []
    row = []
    for ch in channels.data:
        link = ch["channel_link"]
        text += f"• {link}\n"
        # Add a button for this channel
        row.append(InlineKeyboardButton("🔗 Join", url=link))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    # Add any remaining single button
    if row:
        keyboard.append(row)
    # Add the final verification button
    keyboard.append([InlineKeyboardButton("✅ I have joined all", callback_data="joined_all")])
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=reply_markup)

async def joined_all_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    if await is_user_joined_channels(user_id, context):
        url = f"{VERIFY_SITE_URL.rstrip('/')}?user_id={user_id}"
        keyboard = [[InlineKeyboardButton("🛑 VERIFY NOW", url=url)]]
        await query.edit_message_text(
            "<b>✅ You have joined all channels!</b>\n\n<b>🛑 Verification required:</b> Click below to verify.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    else:
        await query.edit_message_text("❌ You haven't joined all channels yet. Please join and try again.")

# ================= BOT COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    username = update.effective_user.username or f"user_{user_id}"
    args = context.args

    # Handle referral
    if args and args[0].isdigit():
        referrer_id = int(args[0])
        if referrer_id != user_id:
            existing = supabase.table("users").select("user_id").eq("user_id", user_id).execute()
            if not existing.data:
                supabase.table("users").insert({
                    "user_id": user_id,
                    "username": username,
                    "points": 0,
                    "referrals": 0,
                    "referred_by": referrer_id,
                    "verified": False,
                    "rewarded": False
                }).execute()
                logger.info(f"User {user_id} referred by {referrer_id}")

    # Ensure user exists in DB
    existing = supabase.table("users").select("user_id").eq("user_id", user_id).execute()
    if not existing.data:
        supabase.table("users").insert({
            "user_id": user_id,
            "username": username,
            "points": 0,
            "referrals": 0,
            "referred_by": None,
            "verified": False,
            "rewarded": False
        }).execute()
        logger.info(f"New user {user_id} created")

    # If already verified and in channels, show menu
    if await is_user_verified(user_id) and await is_user_joined_channels(user_id, context):
        await show_main_menu(update, context)
        return

    # If not verified but in channels, show verify button
    if await is_user_joined_channels(user_id, context):
        url = f"{VERIFY_SITE_URL.rstrip('/')}?user_id={user_id}"
        keyboard = [[InlineKeyboardButton("🛑 VERIFY NOW", url=url)]]
        await update.message.reply_text(
            "<b>✅ You have joined all channels!</b>\n\n<b>🛑 Verification required:</b> Click below to verify.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.HTML
        )
    else:
        await show_force_join_message(update, context)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    keyboard = [
        [KeyboardButton("💰 BALANCE"), KeyboardButton("🤝 REFER")],
        [KeyboardButton("🎁 WITHDRAW"), KeyboardButton("📜 MY VOUCHERS")],
        [KeyboardButton("📦 STOCK"), KeyboardButton("🏆 LEADERBOARD")]
    ]
    if user_id in ADMIN_IDS:
        keyboard.append([KeyboardButton("👑 ADMIN PANEL")])
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("<b>🏠 Main Menu</b>", reply_markup=reply_markup, parse_mode=ParseMode.HTML)

# ================= BALANCE =================
async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_verified(update, context):
        return
    user_id = update.effective_user.id
    user = supabase.table("users").select("points, referrals").eq("user_id", user_id).execute().data[0]
    points = user["points"]
    referrals = user["referrals"]
    shein_cost = get_withdraw_points("shein")
    bigbasket_cost = get_withdraw_points("bigbasket")
    text = f"<b>💰 Your Points</b>\n\n⭐ Points: {points}\n👥 Referrals: {referrals}\n\n🎁 Shein Voucher Cost: {shein_cost} point(s)\n🎁 BigBasket Sweets Voucher Cost: {bigbasket_cost} point(s)"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ================= REFER =================
async def refer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_verified(update, context):
        return
    user_id = update.effective_user.id
    link = await get_referral_link(user_id, context)
    text = f"<b>🤝 Refer & Earn</b>\n\nInvite friends using your link:\n<code>{link}</code>\n\n✅ Each verified user gives you +1 point after they open the bot menu for the first time."
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ================= WITHDRAW =================
async def withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_verified(update, context):
        return
    user_id = update.effective_user.id
    user = supabase.table("users").select("points").eq("user_id", user_id).execute().data[0]
    points = user["points"]
    shein_cost = get_withdraw_points("shein")
    bigbasket_cost = get_withdraw_points("bigbasket")
    # Create inline keyboard with two options
    keyboard = [
        [InlineKeyboardButton(f"Shein ({shein_cost} points)", callback_data="withdraw_shein")],
        [InlineKeyboardButton(f"BigBasket Sweets ({bigbasket_cost} points)", callback_data="withdraw_bigbasket")]
    ]
    await update.message.reply_text(
        "🎁 **Select a coupon to redeem:**",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode=ParseMode.MARKDOWN
    )

async def withdraw_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    voucher_type = query.data.split("_")[1]  # "shein" or "bigbasket"
    cost = get_withdraw_points(voucher_type)

    user = supabase.table("users").select("points").eq("user_id", user_id).execute().data
    if not user:
        await query.edit_message_text("❌ User not found.")
        return
    points = user[0]["points"]
    if points < cost:
        await query.edit_message_text(f"❌ You need {cost} points to withdraw a {voucher_type.title()} voucher. You have {points}.")
        return

    # Get unused coupon of the selected type
    coupon = supabase.table("coupons").select("code").eq("used", False).eq("type", voucher_type).limit(1).execute()
    if not coupon.data:
        await query.edit_message_text(f"❌ No {voucher_type.title()} coupons available. Contact admin.")
        return
    code = coupon.data[0]["code"]

    # Mark coupon as used
    supabase.table("coupons").update({
        "used": True,
        "used_by": user_id,
        "used_at": datetime.utcnow().isoformat()
    }).eq("code", code).execute()

    # Deduct points
    new_points = points - cost
    supabase.table("users").update({"points": new_points}).eq("user_id", user_id).execute()

    # Prepare message based on type
    if voucher_type == "shein":
        order_link = "https://www.sheinindia.in/c/sverse-5939-37961?query=%3Arelevance%3Agenderfilter%3AMen&gridColumns=5#main-content"
        text = f"<b>🎉 Shein Code Generated Successfully!</b>\n\n🎫 Code: <code>{code}</code>\n🛍️ <a href='{order_link}'>Order Here</a>\n\n⚠️ Copy the code and use it immediately."
    else:  # bigbasket
        # UPDATED LINK
        order_link = "https://www.bigbasket.com/sh/f9c23/"
        text = f"<b>🎉 BigBasket Sweets Code Generated Successfully!</b>\n\n🎫 Code: <code>{code}</code>\n🛍️ <a href='{order_link}'>Order Here</a>\n\n⚠️ Copy the code and use it immediately."

    await query.edit_message_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    # Notify admins
    username = query.from_user.username or f"user_{user_id}"
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"<b>🛍️ Coupon Redeemed</b>\n\nUser: {username} (<code>{user_id}</code>)\nType: {voucher_type.title()}\nCode: <code>{code}</code>\nTime: {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC",
                parse_mode=ParseMode.HTML
            )
        except Exception as e:
            logger.error(f"Failed to notify admin {admin_id}: {e}")
            
# ================= MY VOUCHERS =================
async def my_vouchers(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_verified(update, context):
        return
    user_id = update.effective_user.id
    vouchers = supabase.table("coupons").select("code, type, used_at").eq("used_by", user_id).order("used_at", desc=True).execute()
    if not vouchers.data:
        await update.message.reply_text("<b>📜 MY VOUCHERS</b>\n\n━━━━━━━━━━━━━━━━━━━━\nNo vouchers yet.\n━━━━━━━━━━━━━━━━━━━━\n📊 Total: 0", parse_mode=ParseMode.HTML)
        return
    lines = [f"🎫 <code>{v['code']}</code> ({v['type'].title()}) - used: {v['used_at'][:10]}" for v in vouchers.data]
    total = len(vouchers.data)
    text = "<b>📜 MY VOUCHERS</b>\n━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines) + "\n━━━━━━━━━━━━━━━━━━━━\n📊 Total: " + str(total)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ================= STOCK =================
async def stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_verified(update, context):
        return
    shein_count = supabase.table("coupons").select("code", count="exact").eq("used", False).eq("type", "shein").execute().count
    bigbasket_count = supabase.table("coupons").select("code", count="exact").eq("used", False).eq("type", "bigbasket").execute().count
    text = f"<b>📦 STOCK</b>\n\nShein Coupon: {shein_count}\nBigBasket Sweets Coupon: {bigbasket_count}"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ================= LEADERBOARD =================
async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not await require_verified(update, context):
        return
    user_id = update.effective_user.id
    top = supabase.table("users").select("username, referrals").order("referrals", desc=True).limit(10).execute().data
    lines = []
    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
    for i, u in enumerate(top):
        name = u["username"] or f"user_{u['user_id']}"
        lines.append(f"{medals[i]} {name}\n     └ {u['referrals']} referrals")
    all_users = supabase.table("users").select("user_id, referrals").order("referrals", desc=True).execute().data
    rank = 1
    for u in all_users:
        if u["user_id"] == user_id:
            break
        rank += 1
    user_ref = supabase.table("users").select("referrals").eq("user_id", user_id).execute().data[0]["referrals"]
    text = "<b>🏆 Top 10 Leaderboard</b>\n━━━━━━━━━━━━━━━━━━━━\n" + "\n".join(lines) + f"\n━━━━━━━━━━━━━━━━━━━━\n📍 Your Rank: {rank} | {user_ref} referrals"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)

# ================= REFERRAL BONUS HANDLER (deduction) =================
async def deduct_referral_bonus(referrer_id: int, referred_id: int, bot):
    logger.info(f"Deducting referral bonus from {referrer_id} because {referred_id} left")
    referrer = supabase.table("users").select("points, referrals").eq("user_id", referrer_id).execute().data
    if not referrer:
        logger.error(f"Referrer {referrer_id} not found")
        return
    referrer = referrer[0]
    new_points = max(referrer["points"] - 1, 0)
    new_refs = max(referrer["referrals"] - 1, 0)
    supabase.table("users").update({"points": new_points, "referrals": new_refs}).eq("user_id", referrer_id).execute()
    logger.info(f"Deducted 1 point from {referrer_id} (now {new_points} points, {new_refs} referrals)")
    try:
        await bot.send_message(
            chat_id=referrer_id,
            text="<b>⚠️ Referral Left Channels!</b>\n\n💰 Lost -1 pt(s)\n❌ Reward deducted!\n\n⚠️ Note: A referred user has left a required channel.",
            parse_mode=ParseMode.HTML
        )
    except Exception as e:
        logger.error(f"Failed to notify referrer {referrer_id}: {e}")

# ================= ADMIN PANEL =================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    keyboard = [
        [KeyboardButton("📢 BROADCAST")],
        [KeyboardButton("➕ ADD COUPON (Shein)"), KeyboardButton("➕ ADD COUPON (BigBasket)")],
        [KeyboardButton("➖ REMOVE COUPON (Shein)"), KeyboardButton("➖ REMOVE COUPON (BigBasket)")],
        [KeyboardButton("➕ ADD CHANNEL"), KeyboardButton("➖ REMOVE CHANNEL")],
        [KeyboardButton("🎟️ GET FREE CODE (Shein)"), KeyboardButton("🎟️ GET FREE CODE (BigBasket)")],
        [KeyboardButton("💰 CHANGE WITHDRAW POINTS (Shein)"), KeyboardButton("💰 CHANGE WITHDRAW POINTS (BigBasket)")]
    ]
    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
    await update.message.reply_text("<b>👑 Admin Panel</b>", reply_markup=reply_markup, parse_mode=ParseMode.HTML)

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text("📢 Send the message you want to broadcast to all users:")
    context.user_data["awaiting_broadcast"] = True

# Add coupon handlers
async def add_coupon_shein(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    context.user_data["awaiting_coupon_add"] = "shein"
    await update.message.reply_text("📤 Send the Shein coupons line by line (one code per line):")

async def add_coupon_bigbasket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    context.user_data["awaiting_coupon_add"] = "bigbasket"
    await update.message.reply_text("📤 Send the BigBasket coupons line by line (one code per line):")

async def remove_coupon_shein(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    context.user_data["awaiting_coupon_remove"] = "shein"
    await update.message.reply_text("🔢 Send the number of Shein coupons to remove (will delete oldest unused coupons):")

async def remove_coupon_bigbasket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    context.user_data["awaiting_coupon_remove"] = "bigbasket"
    await update.message.reply_text("🔢 Send the number of BigBasket coupons to remove (will delete oldest unused coupons):")

async def add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text("🔗 Send the channel link (e.g., https://t.me/username):")
    context.user_data["awaiting_channel_add"] = True

async def remove_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    await update.message.reply_text("🔗 Send the channel link to remove:")
    context.user_data["awaiting_channel_remove"] = True

async def get_free_code_shein(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    context.user_data["awaiting_free_code"] = "shein"
    await update.message.reply_text("🔢 How many Shein coupons do you need?")

async def get_free_code_bigbasket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    context.user_data["awaiting_free_code"] = "bigbasket"
    await update.message.reply_text("🔢 How many BigBasket coupons do you need?")

async def change_withdraw_points_shein(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    context.user_data["awaiting_withdraw_points"] = "shein"
    await update.message.reply_text("💰 Send the new number of points required to withdraw a Shein voucher:")

async def change_withdraw_points_bigbasket(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    context.user_data["awaiting_withdraw_points"] = "bigbasket"
    await update.message.reply_text("💰 Send the new number of points required to withdraw a BigBasket voucher:")

# Unified admin input handler
async def handle_admin_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id not in ADMIN_IDS:
        return

    # Channel add/remove (they are simple)
    if context.user_data.get("awaiting_channel_add"):
        link = update.message.text.strip()
        try:
            if "t.me/" in link:
                username = link.split("t.me/")[-1].split("?")[0].split("/")[0]
                chat = await context.bot.get_chat(chat_id=f"@{username}")
                chat_id = chat.id
                supabase.table("channels").insert({
                    "channel_link": link,
                    "chat_id": chat_id
                }).execute()
                await update.message.reply_text(f"✅ Channel added with ID {chat_id}.")
            else:
                await update.message.reply_text("❌ Invalid link format. Use https://t.me/username")
        except Exception as e:
            logger.error(f"Error adding channel: {e}")
            await update.message.reply_text(f"❌ Error: {e}")
        context.user_data.pop("awaiting_channel_add")
        return

    if context.user_data.get("awaiting_channel_remove"):
        link = update.message.text.strip()
        try:
            supabase.table("channels").delete().eq("channel_link", link).execute()
            await update.message.reply_text("✅ Channel removed.")
        except Exception as e:
            await update.message.reply_text(f"❌ Error: {e}")
        context.user_data.pop("awaiting_channel_remove")
        return

    # Broadcast
    if context.user_data.get("awaiting_broadcast"):
        text = update.message.text
        users = supabase.table("users").select("user_id").execute().data
        success = 0
        failed = 0
        for u in users:
            try:
                await context.bot.send_message(chat_id=u["user_id"], text=text, parse_mode=ParseMode.HTML)
                success += 1
            except:
                failed += 1
        await update.message.reply_text(f"✅ Broadcast sent.\nSuccess: {success}\nFailed: {failed}")
        context.user_data.pop("awaiting_broadcast")
        return

    # Add coupons (type stored in awaiting_coupon_add)
    if context.user_data.get("awaiting_coupon_add"):
        voucher_type = context.user_data["awaiting_coupon_add"]
        codes = update.message.text.strip().split("\n")
        inserted = 0
        for code in codes:
            code = code.strip()
            if code:
                try:
                    supabase.table("coupons").insert({"code": code, "used": False, "type": voucher_type}).execute()
                    inserted += 1
                except:
                    pass
        await update.message.reply_text(f"✅ Added {inserted} {voucher_type.title()} coupons.")
        context.user_data.pop("awaiting_coupon_add")
        return

    # Remove coupons (type stored in awaiting_coupon_remove)
    if context.user_data.get("awaiting_coupon_remove"):
        voucher_type = context.user_data["awaiting_coupon_remove"]
        try:
            num = int(update.message.text)
        except:
            await update.message.reply_text("❌ Invalid number.")
            return
        coupons = supabase.table("coupons").select("id").eq("used", False).eq("type", voucher_type).order("id").limit(num).execute().data
        ids = [c["id"] for c in coupons]
        if ids:
            supabase.table("coupons").delete().in_("id", ids).execute()
            await update.message.reply_text(f"✅ Removed {len(ids)} {voucher_type.title()} coupons.")
        else:
            await update.message.reply_text(f"❌ No unused {voucher_type.title()} coupons found.")
        context.user_data.pop("awaiting_coupon_remove")
        return

    # Get free code (type stored in awaiting_free_code)
    if context.user_data.get("awaiting_free_code"):
        voucher_type = context.user_data["awaiting_free_code"]
        try:
            num = int(update.message.text)
        except:
            await update.message.reply_text("❌ Invalid number.")
            return
        coupons = supabase.table("coupons").select("code").eq("used", False).eq("type", voucher_type).limit(num).execute().data
        codes = [c["code"] for c in coupons]
        if not codes:
            await update.message.reply_text(f"❌ No unused {voucher_type.title()} coupons.")
            return
        for code in codes:
            supabase.table("coupons").update({"used": True, "used_by": user_id, "used_at": datetime.utcnow().isoformat()}).eq("code", code).execute()
        await update.message.reply_text(f"✅ Here are your {len(codes)} {voucher_type.title()} codes:\n" + "\n".join(codes))
        context.user_data.pop("awaiting_free_code")
        return

    # Change withdraw points (type stored in awaiting_withdraw_points)
    if context.user_data.get("awaiting_withdraw_points"):
        voucher_type = context.user_data["awaiting_withdraw_points"]
        try:
            points = int(update.message.text)
        except:
            await update.message.reply_text("❌ Invalid number.")
            return
        set_withdraw_points(voucher_type, points)
        await update.message.reply_text(f"✅ Withdraw points for {voucher_type.title()} updated to {points}.")
        context.user_data.pop("awaiting_withdraw_points")
        return

# ================= CHAT MEMBER HANDLER (track leaves) =================
async def track_channel_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        chat_member = update.chat_member
        if not chat_member:
            return

        # Verify chat object exists
        if not chat_member.chat:
            logger.debug("chat_member.chat is None")
            return
        chat_id = chat_member.chat.id

        # Verify new_chat_member object exists
        if not chat_member.new_chat_member:
            logger.debug("chat_member.new_chat_member is None")
            return
        new_member = chat_member.new_chat_member
        if not new_member.user:
            logger.debug("new_chat_member.user is None")
            return
        user_id = new_member.user.id

        # Verify old_chat_member object exists
        if not chat_member.old_chat_member:
            logger.debug("chat_member.old_chat_member is None")
            return
        old_status = chat_member.old_chat_member.status
        new_status = new_member.status

        logger.info(f"=== CHAT MEMBER UPDATE ===")
        logger.info(f"Chat: {chat_id} ({chat_member.chat.title})")
        logger.info(f"User: {user_id} ({new_member.user.full_name})")
        logger.info(f"Old status: {old_status}")
        logger.info(f"New status: {new_status}")

        # Get force‑join channel IDs
        channels = supabase.table("channels").select("chat_id").execute()
        if not channels.data:
            return
        channel_ids = [ch["chat_id"] for ch in channels.data if ch.get("chat_id")]
        if not channel_ids:
            return

        if chat_id not in channel_ids:
            return

        # Detect leave
        if old_status in ["member", "administrator", "creator"] and new_status in ["left", "kicked"]:
            logger.info(f"✅ User {user_id} LEFT channel {chat_id}")

            # Get referrer
            user_data = supabase.table("users").select("referred_by").eq("user_id", user_id).execute().data
            if not user_data:
                logger.info(f"User {user_id} not in database")
                return
            referrer_id = user_data[0].get("referred_by")
            if not referrer_id:
                logger.info(f"User {user_id} has no referrer")
                return

            logger.info(f"User {user_id} referred by {referrer_id}, deducting point")
            await deduct_referral_bonus(referrer_id, user_id, context.bot)
    except Exception as e:
        logger.error(f"Exception in track_channel_membership: {e}", exc_info=True)

# ================= ADMIN TEST COMMAND =================
async def test_deduct(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    if not context.args:
        await update.message.reply_text("Usage: /testdeduct <user_id>")
        return
    try:
        user_id = int(context.args[0])
        user = supabase.table("users").select("referred_by").eq("user_id", user_id).execute().data
        if not user or not user[0].get("referred_by"):
            await update.message.reply_text("User has no referrer.")
            return
        referrer_id = user[0]["referred_by"]
        await deduct_referral_bonus(referrer_id, user_id, context.bot)
        await update.message.reply_text(f"Deducted 1 point from {referrer_id} for referred user {user_id}.")
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")

# ================= VERIFICATION PAGE =================
async def verification_page(request):
    user_id = request.query.get('user_id', '')
    bot = request.app.get('bot')
    bot_username = "YOUR_BOT_USERNAME"
    if bot:
        try:
            me = await bot.get_me()
            bot_username = me.username
        except:
            pass

    html = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <title>Verify Your Device</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; font-family: Arial, sans-serif; }}
        body {{ background: linear-gradient(135deg, #667eea, #764ba2); min-height: 100vh; display: flex; align-items: center; justify-content: center; padding: 20px; }}
        .container {{ background: white; border-radius: 20px; padding: 40px; max-width: 500px; width: 100%; box-shadow: 0 20px 60px rgba(0,0,0,0.3); text-align: center; }}
        h1 {{ color: #333; font-size: 2em; margin-bottom: 20px; }}
        .btn {{ background: #667eea; color: white; border: none; padding: 15px 40px; border-radius: 50px; cursor: pointer; font-size: 1.2em; margin: 20px 0; width: 100%; }}
        .btn:disabled {{ opacity: 0.6; cursor: not-allowed; }}
        .status {{ padding: 15px; border-radius: 10px; margin-top: 20px; display: none; word-break: break-word; }}
        .success {{ background: #d4edda; color: #155724; display: block; }}
        .error {{ background: #f8d7da; color: #721c24; display: block; }}
        .loader {{ border: 4px solid #f3f3f3; border-top: 4px solid #667eea; border-radius: 50%; width: 40px; height: 40px; animation: spin 1s linear infinite; margin: 20px auto; display: none; }}
        @keyframes spin {{ 0% {{ transform: rotate(0deg); }} 100% {{ transform: rotate(360deg); }} }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🔐 Verify Your Device</h1>
        <button class="btn" id="verifyBtn">VERIFY NOW</button>
        <div class="loader" id="loader"></div>
        <div class="status" id="status"></div>
        <p style="color:#666;">One device per Telegram account.</p>
    </div>
    <script>
        const BOT_API_URL = '/verify';
        const BOT_USERNAME = '{bot_username}';

        async function getDeviceId() {{
            const canvas = document.createElement('canvas');
            canvas.width = 200; canvas.height = 50;
            const ctx = canvas.getContext('2d');
            ctx.fillStyle = '#f60'; ctx.fillRect(10,10,100,30);
            ctx.fillStyle = '#069'; ctx.fillText('Fingerprint',20,25);
            const fp = canvas.toDataURL();
            const data = fp + navigator.userAgent + screen.width + screen.height + Intl.DateTimeFormat().resolvedOptions().timeZone;
            const hash = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(data));
            return Array.from(new Uint8Array(hash)).map(b => b.toString(16).padStart(2,'0')).join('');
        }}

        document.getElementById('verifyBtn').addEventListener('click', async () => {{
            const btn = document.getElementById('verifyBtn');
            const statusDiv = document.getElementById('status');
            const loader = document.getElementById('loader');
            btn.disabled = true;
            statusDiv.className = 'status';
            loader.style.display = 'block';

            const userId = '{user_id}';
            if (!userId) {{
                statusDiv.className = 'status error';
                statusDiv.innerText = '❌ Missing user ID.';
                btn.disabled = false; loader.style.display = 'none';
                return;
            }}

            try {{
                const deviceId = await getDeviceId();
                const response = await fetch(BOT_API_URL, {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ user_id: parseInt(userId), device_id: deviceId }})
                }});
                const result = await response.json();
                if (result.status === 'success') {{
                    statusDiv.className = 'status success';
                    statusDiv.innerText = '✅ Verified! Redirecting...';
                    setTimeout(() => window.location.href = `https://t.me/${{BOT_USERNAME}}`, 2000);
                }} else {{
                    statusDiv.className = 'status error';
                    statusDiv.innerText = '❌ ' + (result.message || 'Verification failed');
                }}
            }} catch (err) {{
                console.error(err);
                statusDiv.className = 'status error';
                statusDiv.innerText = '❌ Network error. Check console.';
            }} finally {{
                btn.disabled = false;
                loader.style.display = 'none';
            }}
        }});
    </script>
</body>
</html>"""
    return web.Response(text=html, content_type='text/html')

# ================= VERIFICATION CALLBACK =================
async def verification_handler(request):
    print("=== /verify called ===")
    if request.method == 'OPTIONS':
        return web.Response(status=200, headers={
            'Access-Control-Allow-Origin': '*',
            'Access-Control-Allow-Methods': 'POST, OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
        })

    if request.method != 'POST':
        return web.json_response({"status": "error", "message": "Method not allowed"}, status=405,
                                 headers={'Access-Control-Allow-Origin': '*'})

    try:
        text_body = await request.text()
        print(f"Raw body: {text_body}")
    except Exception as e:
        text_body = "<unreadable>"

    try:
        data = await request.json()
        print(f"Parsed JSON: {data}")
    except Exception as e:
        return web.json_response(
            {"status": "error", "message": f"Invalid JSON. Raw body: {text_body}"},
            status=400,
            headers={'Access-Control-Allow-Origin': '*'}
        )

    user_id = data.get("user_id")
    device_id = data.get("device_id")
    if not user_id or not device_id:
        return web.json_response({"status": "error", "message": "Missing data"}, status=400,
                                 headers={'Access-Control-Allow-Origin': '*'})

    try:
        existing = supabase.table("user_verifications").select("user_id").eq("device_id", device_id).execute()
        if existing.data:
            return web.json_response({"status": "error", "message": "Authorized Declined: Device already used"},
                                     headers={'Access-Control-Allow-Origin': '*'})
    except Exception as e:
        return web.json_response({"status": "error", "message": "Database error"}, status=500,
                                 headers={'Access-Control-Allow-Origin': '*'})

    try:
        user = supabase.table("users").select("user_id, referred_by, verified").eq("user_id", user_id).execute()
        if not user.data:
            return web.json_response({"status": "error", "message": "User not found"},
                                     headers={'Access-Control-Allow-Origin': '*'})
        if user.data[0].get("verified", False):
            return web.json_response({"status": "error", "message": "Already verified"},
                                     headers={'Access-Control-Allow-Origin': '*'})
    except Exception as e:
        return web.json_response({"status": "error", "message": "Database error"}, status=500,
                                 headers={'Access-Control-Allow-Origin': '*'})

    try:
        supabase.table("users").update({"verified": True}).eq("user_id", user_id).execute()
        supabase.table("user_verifications").insert({
            "user_id": user_id,
            "device_id": device_id,
            "verified_at": datetime.utcnow().isoformat()
        }).execute()
        print("User verified in DB")
    except Exception as e:
        return web.json_response({"status": "error", "message": "Failed to save verification"}, status=500,
                                 headers={'Access-Control-Allow-Origin': '*'})

    try:
        bot = request.app.get('bot')
        if bot:
            await bot.send_message(chat_id=user_id, text="✅ You are verified! Welcome to the bot.")
    except Exception as e:
        print(f"Telegram send error: {e}")

    # Do NOT grant referral bonus here – wait for first menu access
    return web.json_response({"status": "success", "message": "Verified"},
                             headers={'Access-Control-Allow-Origin': '*'})

# ================= ERROR HANDLER =================
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logger.error("Exception while handling an update:", exc_info=context.error)
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(
                chat_id=admin_id,
                text=f"⚠️ Bot error:\n<code>{context.error}</code>",
                parse_mode=ParseMode.HTML
            )
        except:
            pass

# ================= MAIN =================
async def run_bot():
    application = Application.builder().token(TOKEN).build()

    # User commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("testdeduct", test_deduct))
    application.add_handler(CallbackQueryHandler(joined_all_callback, pattern="joined_all"))
    application.add_handler(CallbackQueryHandler(withdraw_callback, pattern="withdraw_"))
    application.add_handler(MessageHandler(filters.Regex("^💰 BALANCE$"), balance))
    application.add_handler(MessageHandler(filters.Regex("^🤝 REFER$"), refer))
    application.add_handler(MessageHandler(filters.Regex("^🎁 WITHDRAW$"), withdraw))
    application.add_handler(MessageHandler(filters.Regex("^📜 MY VOUCHERS$"), my_vouchers))
    application.add_handler(MessageHandler(filters.Regex("^📦 STOCK$"), stock))
    application.add_handler(MessageHandler(filters.Regex("^🏆 LEADERBOARD$"), leaderboard))

    # Admin panel buttons
    application.add_handler(MessageHandler(filters.Regex("^👑 ADMIN PANEL$"), admin_panel))
    application.add_handler(MessageHandler(filters.Regex("^📢 BROADCAST$"), broadcast))
    application.add_handler(MessageHandler(filters.Regex("^➕ ADD COUPON \\(Shein\\)$"), add_coupon_shein))
    application.add_handler(MessageHandler(filters.Regex("^➕ ADD COUPON \\(BigBasket\\)$"), add_coupon_bigbasket))
    application.add_handler(MessageHandler(filters.Regex("^➖ REMOVE COUPON \\(Shein\\)$"), remove_coupon_shein))
    application.add_handler(MessageHandler(filters.Regex("^➖ REMOVE COUPON \\(BigBasket\\)$"), remove_coupon_bigbasket))
    application.add_handler(MessageHandler(filters.Regex("^➕ ADD CHANNEL$"), add_channel))
    application.add_handler(MessageHandler(filters.Regex("^➖ REMOVE CHANNEL$"), remove_channel))
    application.add_handler(MessageHandler(filters.Regex("^🎟️ GET FREE CODE \\(Shein\\)$"), get_free_code_shein))
    application.add_handler(MessageHandler(filters.Regex("^🎟️ GET FREE CODE \\(BigBasket\\)$"), get_free_code_bigbasket))
    application.add_handler(MessageHandler(filters.Regex("^💰 CHANGE WITHDRAW POINTS \\(Shein\\)$"), change_withdraw_points_shein))
    application.add_handler(MessageHandler(filters.Regex("^💰 CHANGE WITHDRAW POINTS \\(BigBasket\\)$"), change_withdraw_points_bigbasket))

    # Fallback admin input handler
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_admin_input))

    # Chat member handler for leave detection
    application.add_handler(ChatMemberHandler(track_channel_membership, ChatMemberHandler.CHAT_MEMBER))

    # Error handler
    application.add_error_handler(error_handler)

    # Web server
    app = web.Application()
    app['bot'] = application.bot
    app.router.add_get('/v', verification_page)
    app.router.add_post('/verify', verification_handler)

    async def telegram_webhook(request):
        update = await request.json()
        await application.process_update(Update.de_json(update, application.bot))
        return web.Response(status=200)

    app.router.add_post(f'/{TOKEN}', telegram_webhook)
    app.router.add_post('/webhook', telegram_webhook)

    await application.initialize()
    await application.start()

    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', int(os.environ.get("PORT", 8080)))
    await site.start()
    print("Bot started with webhook, verification page at /v, and leave tracking enabled")

    while True:
        await asyncio.sleep(3600)

if __name__ == "__main__":
    try:
        asyncio.run(run_bot())
    except KeyboardInterrupt:
        pass
