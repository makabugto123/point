# web.py
"""
Render-ready Telegram bot:
- FastAPI provides health endpoints for Render.
- Telegram bot runs as a background task (polling).
- SQLite stores every awarded point.
"""

import os
import asyncio
import sys
import types
import time
import re
import sqlite3
from typing import Optional, List, Tuple

# === imghdr shim (from your original) ===
if "imghdr" not in sys.modules:
    fake_imghdr = types.ModuleType("imghdr")
    def what(file, h=None):
        return None
    fake_imghdr.what = what
    sys.modules["imghdr"] = fake_imghdr

# === web / bot libs ===
from fastapi import FastAPI
from fastapi.responses import JSONResponse
import uvicorn

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# === CONFIG from environment ===
TOKEN = os.environ.get("TOKEN")  # set TOKEN in Render dashboard (required)
if not TOKEN:
    raise SystemExit("ERROR: TOKEN environment variable is required.")

DB_PATH = os.environ.get("DB_PATH", "points.db")
MIN_CHARS = int(os.environ.get("MIN_CHARS", "15"))  # min chars to earn point
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", "20"))

# === Validation logic ===
def is_valid_text_for_points(text: str) -> bool:
    """
    Returns True if text should be considered valid for awarding points.
    Requirements:
      - At least MIN_CHARS characters (this is checked earlier too)
      - Contains letters
      - Contains a vowel
      - Not trivial like same character repeated many times
    """
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

# === SQLite helpers ===
def get_conn():
    # check_same_thread=False so background tasks & FastAPI endpoints can share safely
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
        c.execute("INSERT INTO points (user_id, ts, meta) VALUES (?, ?, ?)", (user_id, int(time.time() * 1000), meta))
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
        # rows are tuples (user_id, full_name, points)
        return rows

# === In-memory cooldown cache ===
# This is acceptable for a single Render instance; if you scale to multiple instances, use shared storage.
last_time_cache = {}

# === Bot handlers ===
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    ensure_user_in_db(user.id, user.first_name, user.last_name)
    last_time_cache.setdefault(user.id, 0)
    await update.message.reply_text("👋 Welcome! Type messages with at least 15 characters to earn points. ⏳ 1 point every 20 seconds.")

async def give_point_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Behavior:
      - If DM (private chat): reply "Bawal na boy 😎" and DO NOT award points.
      - If group/supergroup: enforce 15-char rule + validation + cooldown; award silently if passes.
      - If message is too short or invalid: reply "Bawal na boy 😎".
    """
    chat = update.effective_chat
    user = update.effective_user
    text = (update.message.text or "").strip()

    # If private DM -> reject
    if chat and chat.type == "private":
        await update.message.reply_text("Bawal na boy 😎")
        return

    # Ensure user exists in DB for proper leaderboard/name storage
    ensure_user_in_db(user.id, user.first_name, user.last_name)

    # Too short -> reply
    if len(text) < MIN_CHARS:
        await update.message.reply_text("Bawal na boy 😎")
        return

    # Other validity checks
    if not is_valid_text_for_points(text):
        await update.message.reply_text("Bawal na boy 😎")
        return

    # Cooldown check
    now = time.time()
    last = last_time_cache.get(user.id, 0)
    if now - last < COOLDOWN_SECONDS:
        # silently ignore during cooldown
        return

    # Award one point (store in DB)
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

# === Create Application (global ref for startup/shutdown) ===
telegram_app = None
telegram_task = None

async def start_telegram_bot_background():
    """
    Build the Application and run polling in a background task.
    We use ApplicationBuilder().token(TOKEN).build() and run its run_polling coroutine
    in an asyncio Task so FastAPI can continue serving.
    """
    global telegram_app, telegram_task
    if telegram_app is not None:
        return  # already started

    telegram_app = ApplicationBuilder().token(TOKEN).build()

    # register handlers
    telegram_app.add_handler(CommandHandler("start", start_handler))
    telegram_app.add_handler(CommandHandler("points", points_handler))
    telegram_app.add_handler(CommandHandler("leaderboard", leaderboard_handler))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, give_point_handler))

    # run polling in background task
    telegram_task = asyncio.create_task(telegram_app.run_polling())
    # run_polling runs until cancelled/stop, so we don't await it here

# === FastAPI app / health endpoints ===
api = FastAPI()

@api.get("/")
async def root():
    return JSONResponse({"status": "ok"})

@api.get("/health")
async def health():
    return JSONResponse({"status": "healthy"})

@api.on_event("startup")
async def on_startup():
    """
    Called when FastAPI starts. Initialize DB and launch the Telegram bot background task.
    """
    init_db()
    # Launch the telegram bot background task
    await start_telegram_bot_background()

@api.on_event("shutdown")
async def on_shutdown():
    """
    Clean shutdown: cancel polling task and stop application.
    """
    global telegram_app, telegram_task
    try:
        if telegram_app is not None:
            # ask the app to stop
            await telegram_app.stop()
    except Exception:
        pass
    try:
        if telegram_task is not None:
            telegram_task.cancel()
            # optional: await telegram_task to finish cancelation (with timeout)
            try:
                await asyncio.wait_for(telegram_task, timeout=5.0)
            except Exception:
                pass
    except Exception:
        pass

# For local dev: allow running with `python web.py`
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 8000))
    # For local testing, run uvicorn
    uvicorn.run("web:api", host="0.0.0.0", port=port, log_level="info")
