# web.py
"""
Render-ready Telegram bot:
- FastAPI provides health endpoints for Render.
- Telegram bot runs as a background task (polling) integrated with uvicorn's event loop.
- SQLite stores every awarded point.
- DMs -> "Bawal na boy 😎", group messages must be >= MIN_CHARS to earn points (COOLDOWN_SECONDS).
"""

import os
import sys
import types
import time
import re
import sqlite3
import asyncio
from typing import Optional, List, Tuple

# imghdr shim (keeps compatibility with your environment)
if "imghdr" not in sys.modules:
    fake_imghdr = types.ModuleType("imghdr")
    def what(file, h=None):
        return None
    fake_imghdr.what = what
    sys.modules["imghdr"] = fake_imghdr

# Web and server
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

# Telegram
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# Configuration (use Render environment variables)
TOKEN = os.environ.get("TOKEN")
if not TOKEN:
    raise SystemExit("ERROR: TOKEN environment variable is required.")

DB_PATH = os.environ.get("DB_PATH", "points.db")
MIN_CHARS = int(os.environ.get("MIN_CHARS", "15"))
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "20"))

# Validation logic
def is_valid_text_for_points(text: str) -> bool:
    t = text.strip()
    if len(t) < MIN_CHARS:
        return False
    if not re.search(r"[a-zA-Z]", t):
        return False
    if not re.search(r"[aeiouAEIOU]", t):
        return False
    if re.fullmatch(r"(.)\1{2,}", t):
        return False
    return True

# SQLite helpers
def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn

def init_db():
    with get_conn() as c:
        c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            first_name TEXT,
            last_name TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
        """)
        c.execute("""
        CREATE TABLE IF NOT EXISTS points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ts INTEGER NOT NULL,
            meta TEXT,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        );
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_points_user_ts ON points(user_id, ts);")
        c.commit()

def ensure_user_in_db(user_id: int, first_name: Optional[str], last_name: Optional[str]):
    with get_conn() as c:
        c.execute("""
            INSERT INTO users (user_id, first_name, last_name)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                first_name=excluded.first_name,
                last_name=excluded.last_name
        """, (user_id, first_name, last_name))
        c.commit()

def add_point_to_db(user_id: int, meta: Optional[str] = None):
    with get_conn() as c:
        c.execute("INSERT INTO points (user_id, ts, meta) VALUES (?, ?, ?)",
                  (user_id, int(time.time() * 1000), meta))
        c.commit()

def get_user_points_from_db(user_id: int) -> int:
    with get_conn() as c:
        cur = c.execute("SELECT COUNT(*) FROM points WHERE user_id = ?", (user_id,))
        return cur.fetchone()[0] or 0

def get_leaderboard_from_db(limit: int = 10) -> List[Tuple[int, str, int]]:
    with get_conn() as c:
        cur = c.execute("""
            SELECT u.user_id,
                   TRIM(u.first_name || ' ' || IFNULL(u.last_name, '')) AS full_name,
                   COUNT(p.id) AS points
            FROM users u
            LEFT JOIN points p ON p.user_id = u.user_id
            GROUP BY u.user_id
            ORDER BY points DESC, u.first_name ASC
            LIMIT ?
        """, (limit,))
        rows = cur.fetchall()
        return rows

# In-memory cooldown cache (single-instance assumption)
last_time_cache = {}

# Bot handlers
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user_in_db(user.id, user.first_name, user.last_name)
    last_time_cache.setdefault(user.id, 0)
    await update.message.reply_text(
        "👋 Welcome! Type messages with at least "
        f"{MIN_CHARS} characters to earn points. ⏳ 1 point every {COOLDOWN_SECONDS} seconds."
    )

async def give_point_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user
    text = (update.message.text or "").strip()

    # If DM -> reject and reply
    if chat and chat.type == "private":
        await update.message.reply_text("Bawal na boy 😎")
        return

    ensure_user_in_db(user.id, user.first_name, user.last_name)

    # Too short -> reply
    if len(text) < MIN_CHARS:
        await update.message.reply_text("Bawal na boy 😎")
        return

    # Other validity checks
    if not is_valid_text_for_points(text):
        await update.message.reply_text("Bawal na boy 😎")
        return

    # Cooldown
    now = time.time()
    last = last_time_cache.get(user.id, 0)
    if now - last < COOLDOWN_SECONDS:
        return  # silently ignore during cooldown

    # Award point
    add_point_to_db(user.id, meta=None)
    last_time_cache[user.id] = now
    # silent award (no reply)

async def points_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user_in_db(user.id, user.first_name, user.last_name)
    pts = get_user_points_from_db(user.id)
    await update.message.reply_text(f"🏆 You currently have {pts} points!")

async def leaderboard_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_leaderboard_from_db(limit=20)
    if not rows:
        await update.message.reply_text("No points yet.")
        return

    lines = ["🏅 Leaderboard:"]
    for rank, (user_id, full_name, pts) in enumerate(rows, start=1):
        display_name = full_name if (full_name and full_name.strip()) else f"User {user_id}"
        lines.append(f"{rank}. {display_name} — {pts} pts")
    await update.message.reply_text("\n".join(lines))

# --- Telegram Application management integrated with uvicorn loop ---
telegram_app = None
telegram_polling_task: Optional[asyncio.Task] = None

async def start_telegram_bot_background():
    """
    Initialize Application and start polling inside the running event loop.
    Uses Application.initialize()/.start() then schedules updater.start_polling() as a task.
    """
    global telegram_app, telegram_polling_task
    if telegram_app is not None:
        return

    telegram_app = ApplicationBuilder().token(TOKEN).build()

    # register handlers
    telegram_app.add_handler(CommandHandler("start", start_handler))
    telegram_app.add_handler(CommandHandler("points", points_handler))
    telegram_app.add_handler(CommandHandler("leaderboard", leaderboard_handler))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, give_point_handler))

    # Prepare and start the app without creating a new event loop
    await telegram_app.initialize()
    await telegram_app.start()

    # Prefer using the updater's start_polling coroutine to avoid run_polling() loop handling.
    updater = getattr(telegram_app, "updater", None)
    if updater is not None:
        # start_polling is a coroutine; schedule it as a background task
        telegram_polling_task = asyncio.create_task(updater.start_polling())
    else:
        # Fallback: schedule run_polling() as task (some library versions may require this)
        telegram_polling_task = asyncio.create_task(telegram_app.run_polling())

async def stop_telegram_bot_background():
    """
    Stop polling and shutdown the Application cleanly.
    """
    global telegram_app, telegram_polling_task
    if telegram_polling_task is not None:
        try:
            updater = getattr(telegram_app, "updater", None)
            if updater is not None:
                await updater.stop_polling()
        except Exception:
            pass

        if not telegram_polling_task.done():
            telegram_polling_task.cancel()
            try:
                await asyncio.wait_for(telegram_polling_task, timeout=5.0)
            except Exception:
                pass
        telegram_polling_task = None

    if telegram_app is not None:
        try:
            await telegram_app.stop()
        except Exception:
            pass
        try:
            await telegram_app.shutdown()
        except Exception:
            pass
        telegram_app = None

# FastAPI app and lifecycle events
api = FastAPI()

@api.get("/")
async def root():
    return JSONResponse({"status": "ok"})

@api.get("/health")
async def health():
    return JSONResponse({"status": "healthy"})

@api.on_event("startup")
async def on_startup():
    init_db()
    # start telegram bot background task integrated with uvicorn's loop
    await start_telegram_bot_background()

@api.on_event("shutdown")
async def on_shutdown():
    await stop_telegram_bot_background()

# For local dev
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("web:api", host="0.0.0.0", port=port, log_level="info")
