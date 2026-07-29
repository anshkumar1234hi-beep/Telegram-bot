#!/usr/bin/env python3
"""
Telegram Credit Reward Bot
===========================
A credit-based reward system with task completion, a store, and an admin panel.

Features:
- SQLite-backed user/credit system
- Task system (join-channel verification via Telegram API) with anti-abuse
- Inline keyboard UI (Tasks / Balance / Store)
- Dynamic, admin-controlled store (add items without touching code)
- Full admin panel with step-based input handling
- Referral system (bonus credits for both referrer and referee)

Requirements:
    pip install pyTelegramBotAPI

Run:
    python bot.py
"""

import sqlite3
import logging
import threading
from datetime import datetime

import telebot
from telebot import types

# =========================================================================
# CONFIG
# =========================================================================

BOT_TOKEN = "8877167205:AAFPQp5-kvXX7ZPxX2B2TdkAybgbmjmfvCs"          # <-- Get this from @BotFather
ADMIN_IDS = [7063394683]                     # <-- Your Telegram numeric user ID(s)

DB_PATH = "bot_database.db"

# Tasks: each task = a channel the user must join to earn a reward.
# You can add more tasks here (channel username/id, display name, invite link, reward).
TASKS = [
    {
        "id": "-1003715217878",
        "name": "Channel 1",
        "channel": "https://t.me/+bjpnPVb-M1cxY2Vl",   # used for get_chat_member check
        "invite_link": "https://t.me/+bjpnPVb-M1cxY2Vl",
        "reward": 5,
    },
    # Add more tasks below, e.g.:
    # {
    #     "id": "task_channel_2",
    #     "name": "Join Announcements Channel",
    #     "channel": "@your_second_channel",
    #     "invite_link": "https://t.me/your_second_channel",
    #     "reward": 15,
    # },
]

REFERRAL_BONUS_REFERRER = 10   # credits given to the person who invited
REFERRAL_BONUS_REFEREE = 5    # credits given to the new user who joined via referral

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

bot = telebot.TeleBot(BOT_TOKEN, parse_mode="HTML")

# =========================================================================
# DATABASE SETUP
# =========================================================================

db_lock = threading.Lock()


def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db_lock:
        conn = get_conn()
        cur = conn.cursor()

        cur.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                credits INTEGER NOT NULL DEFAULT 0,
                referred_by INTEGER,
                joined_at TEXT NOT NULL
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS completed_tasks (
                user_id INTEGER NOT NULL,
                task_id TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                PRIMARY KEY (user_id, task_id)
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS store_items (
                item_id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                price INTEGER NOT NULL,
                link TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            )
        """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                purchased_at TEXT NOT NULL
            )
        """)

        conn.commit()
        conn.close()


init_db()

# =========================================================================
# ADMIN STATE (step handlers for multi-step admin input)
# =========================================================================

# admin_id -> {"action": str, "data": dict}
admin_state = {}

ACTION_ADD_CREDITS_ID = "add_credits_id"
ACTION_ADD_CREDITS_AMOUNT = "add_credits_amount"
ACTION_CHECK_BALANCE_ID = "check_balance_id"
ACTION_ADD_ITEM_NAME = "add_item_name"
ACTION_ADD_ITEM_PRICE = "add_item_price"
ACTION_ADD_ITEM_LINK = "add_item_link"
ACTION_BROADCAST_MSG = "broadcast_msg"

# =========================================================================
# HELPER FUNCTIONS — USERS / CREDITS
# =========================================================================


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def register_user(user_id: int, username: str, referred_by: int = None):
    """Register a user if they don't already exist. Returns True if newly created."""
    with db_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users WHERE user_id = ?", (user_id,))
        existing = cur.fetchone()
        if existing:
            # keep username fresh
            cur.execute("UPDATE users SET username = ? WHERE user_id = ?", (username, user_id))
            conn.commit()
            conn.close()
            return False

        cur.execute(
            "INSERT INTO users (user_id, username, credits, referred_by, joined_at) VALUES (?, ?, 0, ?, ?)",
            (user_id, username, referred_by, datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()
        return True


def get_user(user_id: int):
    with db_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id, username, credits, referred_by FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        conn.close()
        return row


def get_balance(user_id: int) -> int:
    row = get_user(user_id)
    return row[2] if row else 0


def user_exists(user_id: int) -> bool:
    return get_user(user_id) is not None


def add_credits(user_id: int, amount: int):
    """Add (or subtract, if amount is negative) credits, clamped so it never goes below 0."""
    with db_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return False, 0

        new_balance = row[0] + amount
        if new_balance < 0:
            new_balance = 0

        cur.execute("UPDATE users SET credits = ? WHERE user_id = ?", (new_balance, user_id))
        conn.commit()
        conn.close()
        return True, new_balance


def deduct_credits_if_enough(user_id: int, amount: int):
    """Atomically deduct credits only if the user has enough. Returns (success, new_balance)."""
    with db_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT credits FROM users WHERE user_id = ?", (user_id,))
        row = cur.fetchone()
        if not row:
            conn.close()
            return False, 0

        current = row[0]
        if current < amount:
            conn.close()
            return False, current

        new_balance = current - amount
        cur.execute("UPDATE users SET credits = ? WHERE user_id = ?", (new_balance, user_id))
        conn.commit()
        conn.close()
        return True, new_balance


def get_all_user_ids():
    with db_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT user_id FROM users")
        rows = [r[0] for r in cur.fetchall()]
        conn.close()
        return rows

# =========================================================================
# HELPER FUNCTIONS — TASKS / ANTI-ABUSE
# =========================================================================


def has_completed_task(user_id: int, task_id: str) -> bool:
    with db_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM completed_tasks WHERE user_id = ? AND task_id = ?",
            (user_id, task_id),
        )
        row = cur.fetchone()
        conn.close()
        return row is not None


def mark_task_completed(user_id: int, task_id: str):
    with db_lock:
        conn = get_conn()
        cur = conn.cursor()
        try:
            cur.execute(
                "INSERT INTO completed_tasks (user_id, task_id, completed_at) VALUES (?, ?, ?)",
                (user_id, task_id, datetime.utcnow().isoformat()),
            )
            conn.commit()
            success = True
        except sqlite3.IntegrityError:
            # already claimed - anti-abuse safeguard at the DB level too
            success = False
        conn.close()
        return success


def check_user_joined_channel(user_id: int, channel: str) -> bool:
    """Uses Telegram API to check whether a user is a member of the given channel."""
    try:
        member = bot.get_chat_member(channel, user_id)
        return member.status in ("member", "administrator", "creator")
    except Exception as e:
        logger.warning(f"Channel membership check failed for {user_id} on {channel}: {e}")
        return False


def get_task_by_id(task_id: str):
    for t in TASKS:
        if t["id"] == task_id:
            return t
    return None

# =========================================================================
# HELPER FUNCTIONS — STORE
# =========================================================================


def get_active_store_items():
    with db_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT item_id, name, price, link FROM store_items WHERE active = 1")
        rows = cur.fetchall()
        conn.close()
        return rows


def get_store_item(item_id: int):
    with db_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute("SELECT item_id, name, price, link FROM store_items WHERE item_id = ? AND active = 1", (item_id,))
        row = cur.fetchone()
        conn.close()
        return row


def add_store_item(name: str, price: int, link: str):
    with db_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO store_items (name, price, link, active) VALUES (?, ?, ?, 1)",
            (name, price, link),
        )
        conn.commit()
        item_id = cur.lastrowid
        conn.close()
        return item_id


def record_purchase(user_id: int, item_id: int):
    with db_lock:
        conn = get_conn()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO purchases (user_id, item_id, purchased_at) VALUES (?, ?, ?)",
            (user_id, item_id, datetime.utcnow().isoformat()),
        )
        conn.commit()
        conn.close()

# =========================================================================
# KEYBOARDS
# =========================================================================


def main_menu_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("📋 Tasks", callback_data="menu_tasks"),
        types.InlineKeyboardButton("💰 Balance", callback_data="menu_balance"),
        types.InlineKeyboardButton("🛒 Store", callback_data="menu_store"),
    )
    return kb


def back_to_menu_keyboard():
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_main"))
    return kb


def tasks_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    for task in TASKS:
        kb.add(types.InlineKeyboardButton(f"➡️ {task['name']}", callback_data=f"view_task_{task['id']}"))
    kb.add(types.InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_main"))
    return kb


def task_detail_keyboard(task):
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(types.InlineKeyboardButton("📢 Join Channel", url=task["invite_link"]))
    kb.add(types.InlineKeyboardButton("✅ I Joined", callback_data=f"verify_task_{task['id']}"))
    kb.add(types.InlineKeyboardButton("⬅️ Back to Tasks", callback_data="menu_tasks"))
    return kb


def store_keyboard(items):
    kb = types.InlineKeyboardMarkup(row_width=1)
    if not items:
        kb.add(types.InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_main"))
        return kb
    for item_id, name, price, _link in items:
        kb.add(types.InlineKeyboardButton(f"{name} — {price} credits", callback_data=f"buy_item_{item_id}"))
    kb.add(types.InlineKeyboardButton("⬅️ Back to Menu", callback_data="menu_main"))
    return kb


def admin_panel_keyboard():
    kb = types.InlineKeyboardMarkup(row_width=1)
    kb.add(
        types.InlineKeyboardButton("➕ Add Credits to User", callback_data="admin_add_credits"),
        types.InlineKeyboardButton("🔍 Check User Balance", callback_data="admin_check_balance"),
        types.InlineKeyboardButton("🛍️ Add Store Item", callback_data="admin_add_item"),
        types.InlineKeyboardButton("📢 Broadcast Message", callback_data="admin_broadcast"),
    )
    return kb

# =========================================================================
# COMMANDS
# =========================================================================


@bot.message_handler(commands=["start"])
def handle_start(message):
    user_id = message.from_user.id
    username = message.from_user.username or message.from_user.first_name or "Unknown"

    referred_by = None
    args = message.text.split(maxsplit=1)
    if len(args) > 1:
        payload = args[1].strip()
        if payload.startswith("ref_"):
            try:
                candidate = int(payload.replace("ref_", ""))
                if candidate != user_id and user_exists(candidate):
                    referred_by = candidate
            except ValueError:
                pass

    is_new = register_user(user_id, username, referred_by)

    if is_new and referred_by:
        add_credits(referred_by, REFERRAL_BONUS_REFERRER)
        add_credits(user_id, REFERRAL_BONUS_REFEREE)
        try:
            bot.send_message(
                referred_by,
                f"🎉 Someone joined using your referral link! You earned <b>{REFERRAL_BONUS_REFERRER}</b> credits.",
            )
        except Exception as e:
            logger.info(f"Could not notify referrer {referred_by}: {e}")

    welcome_text = (
        f"👋 <b>Welcome, {username}!</b>\n\n"
        "This bot lets you earn credits by completing simple tasks, "
        "and spend them in the store to unlock rewards.\n\n"
        "Use the menu below to get started."
    )
    if is_new and referred_by:
        welcome_text += f"\n\n🎁 You received <b>{REFERRAL_BONUS_REFEREE}</b> bonus credits for joining via referral!"

    bot.send_message(message.chat.id, welcome_text, reply_markup=main_menu_keyboard())


@bot.message_handler(commands=["menu"])
def handle_menu(message):
    bot.send_message(message.chat.id, "📍 <b>Main Menu</b>", reply_markup=main_menu_keyboard())


@bot.message_handler(commands=["referral"])
def handle_referral(message):
    user_id = message.from_user.id
    if not user_exists(user_id):
        register_user(user_id, message.from_user.username or message.from_user.first_name)
    bot_username = bot.get_me().username
    link = f"https://t.me/{bot_username}?start=ref_{user_id}"
    bot.send_message(
        message.chat.id,
        "🔗 <b>Your Referral Link</b>\n\n"
        f"{link}\n\n"
        f"Share this with friends. You earn <b>{REFERRAL_BONUS_REFERRER}</b> credits per referral, "
        f"and they get <b>{REFERRAL_BONUS_REFEREE}</b> credits for joining!",
    )


@bot.message_handler(commands=["admin"])
def handle_admin(message):
    if not is_admin(message.from_user.id):
        bot.send_message(message.chat.id, "⛔ You are not authorized to use this command.")
        return
    bot.send_message(message.chat.id, "🛠️ <b>Admin Panel</b>", reply_markup=admin_panel_keyboard())

# =========================================================================
# CALLBACK HANDLERS — MAIN MENU
# =========================================================================


@bot.callback_query_handler(func=lambda call: call.data == "menu_main")
def cb_menu_main(call):
    bot.edit_message_text(
        "📍 <b>Main Menu</b>",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=main_menu_keyboard(),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "menu_balance")
def cb_menu_balance(call):
    user_id = call.from_user.id
    if not user_exists(user_id):
        register_user(user_id, call.from_user.username or call.from_user.first_name)
    balance = get_balance(user_id)
    bot.edit_message_text(
        f"💰 <b>Your Balance</b>\n\nYou currently have <b>{balance}</b> credits.",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=back_to_menu_keyboard(),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data == "menu_tasks")
def cb_menu_tasks(call):
    if not TASKS:
        bot.edit_message_text(
            "📋 <b>Tasks</b>\n\nNo tasks are available right now. Check back later!",
            call.message.chat.id,
            call.message.message_id,
            reply_markup=back_to_menu_keyboard(),
        )
        bot.answer_callback_query(call.id)
        return

    bot.edit_message_text(
        "📋 <b>Available Tasks</b>\n\nSelect a task below to view details and earn credits.",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=tasks_keyboard(),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("view_task_"))
def cb_view_task(call):
    task_id = call.data.replace("view_task_", "")
    task = get_task_by_id(task_id)
    if not task:
        bot.answer_callback_query(call.id, "This task no longer exists.", show_alert=True)
        return

    user_id = call.from_user.id
    already_done = has_completed_task(user_id, task_id)

    status = "✅ Already completed" if already_done else f"🎁 Reward: {task['reward']} credits"
    text = f"📌 <b>{task['name']}</b>\n\n{status}\n\nJoin the channel, then tap “I Joined” to verify."

    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=task_detail_keyboard(task),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("verify_task_"))
def cb_verify_task(call):
    task_id = call.data.replace("verify_task_", "")
    task = get_task_by_id(task_id)
    user_id = call.from_user.id

    if not task:
        bot.answer_callback_query(call.id, "This task no longer exists.", show_alert=True)
        return

    if not user_exists(user_id):
        register_user(user_id, call.from_user.username or call.from_user.first_name)

    if has_completed_task(user_id, task_id):
        bot.answer_callback_query(call.id, "You've already claimed this reward.", show_alert=True)
        return

    joined = check_user_joined_channel(user_id, task["channel"])
    if not joined:
        bot.answer_callback_query(
            call.id,
            "❌ You haven't joined the channel yet. Please join first, then tap 'I Joined'.",
            show_alert=True,
        )
        return

    # Anti-abuse: mark_task_completed uses a PRIMARY KEY constraint, so a
    # double-claim race condition is rejected at the DB level too.
    claimed = mark_task_completed(user_id, task_id)
    if not claimed:
        bot.answer_callback_query(call.id, "You've already claimed this reward.", show_alert=True)
        return

    add_credits(user_id, task["reward"])
    bot.answer_callback_query(call.id, f"✅ Verified! You earned {task['reward']} credits.", show_alert=True)

    new_balance = get_balance(user_id)
    bot.edit_message_text(
        f"📌 <b>{task['name']}</b>\n\n"
        f"✅ Task completed! You earned <b>{task['reward']}</b> credits.\n"
        f"💰 New balance: <b>{new_balance}</b> credits.",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=tasks_keyboard(),
    )


@bot.callback_query_handler(func=lambda call: call.data == "menu_store")
def cb_menu_store(call):
    items = get_active_store_items()
    if not items:
        text = "🛒 <b>Store</b>\n\nNo items available right now. Check back later!"
    else:
        text = "🛒 <b>Store</b>\n\nSpend your credits to unlock exclusive links and rewards."

    bot.edit_message_text(
        text,
        call.message.chat.id,
        call.message.message_id,
        reply_markup=store_keyboard(items),
    )
    bot.answer_callback_query(call.id)


@bot.callback_query_handler(func=lambda call: call.data.startswith("buy_item_"))
def cb_buy_item(call):
    item_id = int(call.data.replace("buy_item_", ""))
    user_id = call.from_user.id

    if not user_exists(user_id):
        register_user(user_id, call.from_user.username or call.from_user.first_name)

    item = get_store_item(item_id)
    if not item:
        bot.answer_callback_query(call.id, "This item is no longer available.", show_alert=True)
        return

    _item_id, name, price, link = item

    success, new_balance = deduct_credits_if_enough(user_id, price)
    if not success:
        bot.answer_callback_query(
            call.id,
            f"❌ Not enough credits. You need {price}, but you have {new_balance}.",
            show_alert=True,
        )
        return

    record_purchase(user_id, item_id)
    bot.answer_callback_query(call.id, "✅ Purchase successful!", show_alert=True)

    bot.send_message(
        call.message.chat.id,
        f"✅ <b>Purchase Successful</b>\n\n"
        f"Item: <b>{name}</b>\n"
        f"Price: {price} credits\n"
        f"💰 Remaining balance: {new_balance} credits\n\n"
        f"🔗 Your link: {link}",
    )

    items = get_active_store_items()
    bot.edit_message_text(
        "🛒 <b>Store</b>\n\nSpend your credits to unlock exclusive links and rewards.",
        call.message.chat.id,
        call.message.message_id,
        reply_markup=store_keyboard(items),
    )

# =========================================================================
# CALLBACK HANDLERS — ADMIN PANEL
# =========================================================================


@bot.callback_query_handler(func=lambda call: call.data.startswith("admin_"))
def cb_admin_actions(call):
    user_id = call.from_user.id
    if not is_admin(user_id):
        bot.answer_callback_query(call.id, "⛔ Not authorized.", show_alert=True)
        return

    action = call.data

    if action == "admin_add_credits":
        admin_state[user_id] = {"action": ACTION_ADD_CREDITS_ID, "data": {}}
        bot.send_message(call.message.chat.id, "✏️ Send the <b>User ID</b> you want to add credits to:")

    elif action == "admin_check_balance":
        admin_state[user_id] = {"action": ACTION_CHECK_BALANCE_ID, "data": {}}
        bot.send_message(call.message.chat.id, "✏️ Send the <b>User ID</b> to check:")

    elif action == "admin_add_item":
        admin_state[user_id] = {"action": ACTION_ADD_ITEM_NAME, "data": {}}
        bot.send_message(call.message.chat.id, "✏️ Send the <b>item name</b>:")

    elif action == "admin_broadcast":
        admin_state[user_id] = {"action": ACTION_BROADCAST_MSG, "data": {}}
        bot.send_message(call.message.chat.id, "✏️ Send the <b>message</b> you want to broadcast to all users:")

    bot.answer_callback_query(call.id)


@bot.message_handler(
    func=lambda message: message.from_user.id in admin_state and is_admin(message.from_user.id)
)
def handle_admin_input(message):
    user_id = message.from_user.id
    state = admin_state.get(user_id)
    if not state:
        return

    action = state["action"]
    data = state["data"]
    text = message.text.strip() if message.text else ""

    # ---------------- Add Credits ----------------
    if action == ACTION_ADD_CREDITS_ID:
        if not text.isdigit():
            bot.send_message(message.chat.id, "❌ Invalid User ID. Please send numbers only.")
            return
        data["target_id"] = int(text)
        state["action"] = ACTION_ADD_CREDITS_AMOUNT
        bot.send_message(message.chat.id, "✏️ Now send the <b>amount of credits</b> to add:")
        return

    if action == ACTION_ADD_CREDITS_AMOUNT:
        try:
            amount = int(text)
        except ValueError:
            bot.send_message(message.chat.id, "❌ Invalid amount. Please send a whole number.")
            return

        target_id = data["target_id"]
        if not user_exists(target_id):
            bot.send_message(message.chat.id, f"❌ User {target_id} was not found in the database.")
            admin_state.pop(user_id, None)
            return

        success, new_balance = add_credits(target_id, amount)
        admin_state.pop(user_id, None)
        if success:
            bot.send_message(
                message.chat.id,
                f"✅ Added {amount} credits to user {target_id}.\nNew balance: <b>{new_balance}</b>",
            )
            try:
                bot.send_message(target_id, f"🎉 An admin added <b>{amount}</b> credits to your account!")
            except Exception as e:
                logger.info(f"Could not notify user {target_id}: {e}")
        else:
            bot.send_message(message.chat.id, "❌ Failed to update credits.")
        return

    # ---------------- Check Balance ----------------
    if action == ACTION_CHECK_BALANCE_ID:
        if not text.isdigit():
            bot.send_message(message.chat.id, "❌ Invalid User ID. Please send numbers only.")
            return
        target_id = int(text)
        admin_state.pop(user_id, None)
        if not user_exists(target_id):
            bot.send_message(message.chat.id, f"❌ User {target_id} was not found in the database.")
            return
        balance = get_balance(target_id)
        bot.send_message(message.chat.id, f"💰 User {target_id} has <b>{balance}</b> credits.")
        return

    # ---------------- Add Store Item ----------------
    if action == ACTION_ADD_ITEM_NAME:
        if not text:
            bot.send_message(message.chat.id, "❌ Item name cannot be empty. Try again:")
            return
        data["name"] = text
        state["action"] = ACTION_ADD_ITEM_PRICE
        bot.send_message(message.chat.id, "✏️ Now send the <b>price</b> (in credits):")
        return

    if action == ACTION_ADD_ITEM_PRICE:
        if not text.isdigit() or int(text) <= 0:
            bot.send_message(message.chat.id, "❌ Invalid price. Please send a positive whole number.")
            return
        data["price"] = int(text)
        state["action"] = ACTION_ADD_ITEM_LINK
        bot.send_message(message.chat.id, "✏️ Now send the <b>link</b> to unlock:")
        return

    if action == ACTION_ADD_ITEM_LINK:
        if not text.startswith("http://") and not text.startswith("https://"):
            bot.send_message(message.chat.id, "❌ Invalid link. It must start with http:// or https://")
            return
        item_id = add_store_item(data["name"], data["price"], text)
        admin_state.pop(user_id, None)
        bot.send_message(
            message.chat.id,
            f"✅ Store item added!\n\n"
            f"ID: {item_id}\nName: {data['name']}\nPrice: {data['price']} credits\nLink: {text}",
        )
        return

    # ---------------- Broadcast ----------------
    if action == ACTION_BROADCAST_MSG:
        admin_state.pop(user_id, None)
        all_ids = get_all_user_ids()
        sent, failed = 0, 0
        status_msg = bot.send_message(message.chat.id, f"📢 Broadcasting to {len(all_ids)} users...")

        for uid in all_ids:
            try:
                bot.send_message(uid, text)
                sent += 1
            except Exception as e:
                failed += 1
                logger.info(f"Broadcast failed for {uid}: {e}")

        bot.edit_message_text(
            f"✅ Broadcast complete.\n\nSent: {sent}\nFailed (blocked/invalid): {failed}",
            message.chat.id,
            status_msg.message_id,
        )
        return

# =========================================================================
# FALLBACK
# =========================================================================


@bot.message_handler(func=lambda message: True, content_types=["text"])
def handle_fallback(message):
    user_id = message.from_user.id
    if not user_exists(user_id):
        register_user(user_id, message.from_user.username or message.from_user.first_name)
    bot.send_message(
        message.chat.id,
        "🤔 I didn't understand that. Use the menu below to navigate.",
        reply_markup=main_menu_keyboard(),
    )


# =========================================================================
# ENTRY POINT
# =========================================================================

if __name__ == "__main__":
    logger.info("Bot is starting...")
    bot.infinity_polling(skip_pending=True)
