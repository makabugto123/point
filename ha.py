# web.py
import os
import asyncio
import sys, types

# imghdr shim (keep your original fix)
if "imghdr" not in sys.modules:
    fake_imghdr = types.ModuleType("imghdr")
    def what(file, h=None):
        return None
    fake_imghdr.what = what
    sys.modules["imghdr"] = fake_imghdr

from fastapi import FastAPI
import uvicorn
import sqlite3
import time
import re
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from typing import Optional

# Config from env
TOKEN = os.environ.get("TOKEN") or "8560458009:AAHm4moFh35Nm-fEIJk4JPtb4nfzFxx3oww"
DB_PATH = os.environ.get("DB_PATH", "points.db")

# --- validation & sqlite helpers (same as your async bot) ---
def is_valid_text(text: str) -> bool:
    text = text.strip()
    if len(text) < 3:
        return False
    if not re.search(r"[a-zA-Z]", text):
        return False
    if not re.search(r"[aeiouAEIOU]", text):
        return False
    if re.fullmatch(r"(.)\1{2,}", text):
        return False
    return True

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
        )""")
        c.execute("""
        CREATE TABLE IF NOT EXISTS points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            ts INTEGER NOT NULL,
            FOREIGN KEY(user_id) REFERENCES users(user_id)
        )""")
        c.commit()

def ensure_user(user_id: int, first_name: str, last_name: Optional[str]):
    with get_conn() as c:
        c.execute("""
        INSERT INTO users(user_id, first_name, last_name)
        VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET
          first_name=excluded.first_name,
          last_name=excluded.last_name
        """, (user_id, first_name, last_name))
        c.commit()

def add_point(user_id: int):
    with get_conn() as c:
        c.execute("INSERT INTO points (user_id, ts) VALUES (?, ?)", (user_id, int(time.time() * 1000)))
        c.commit()

def get_points(user_id: int) -> int:
    with get_conn() as c:
        cur = c.execute("SELECT COUNT(*) FROM points WHERE user_id=?", (user_id,))
        return cur.fetchone()[0]

def get_leaderboard(limit: int = 10):
    with get_conn() as c:
        cur = c.execute("""
        SELECT u.user_id,
               TRIM(u.first_name || ' ' || IFNULL(u.last_name, '')) AS full_name,
               COUNT(p.id) AS points
        FROM users u
        LEFT JOIN points p ON p.user_id = u.user_id
        GROUP BY u.user_id
        ORDER BY points DESC, u.first_name ASC
        LIMIT ?;
        """, (limit,))
        return cur.fetchall()

# cooldown
last_time = {}

# --- bot handlers (async) ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    ensure_user(u.id, u.first_name, u.last_name)
    last_time.setdefault(u.id, 0)
    await update.message.reply_text("👋 Welcome! Type valid words to earn points silently. ⏳ 1 point every 20 seconds.")

async def give_point(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    text = (update.message.text or "").strip()
    if not is_valid_text(text):
        return
    now = time.time()
    if now - last_time.get(u.id, 0) < 20:
        return
    ensure_user(u.id, u.first_name, u.last_name)
    add_point(u.id)
    last_time[u.id] = now

async def points(update: Update, context: ContextTypes.DEFAULT_TYPE):
    pts = get_points(update.effective_user.id)
    await update.message.reply_text(f"🏆 You currently have {pts} points!")

async def leaderboard(update: Update, context: ContextTypes.DEFAULT_TYPE):
    rows = get_leaderboard(20)
    if not rows:
        await update.message.reply_text("No points yet.")
        return
    lines = ["🏅 Leaderboard:"]
    for rank, (_, name, pts) in enumerate(rows, start=1):
        display_name = name if name.strip() else f"User {rank}"
        lines.append(f"{rank}. {display_name} — {pts} pts")
    await update.message.reply_text("\n".join(lines))

# --- create and start the bot application (but do not block) ---
async def start_bot_app():
    # initialize DB first
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("points", points))
    app.add_handler(CommandHandler("leaderboard", leaderboard))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, give_point))

    # start the bot (non-blocking)
    await app.initialize()
    await app.start()
    # start polling in background - this will run until app.stop() is called
    asyncio.create_task(app.updater.start_polling())  # app.updater is available under v20+ as wrapper
    # Note: if app.updater isn't present in some ptb versions, use `await app.run_polling()` instead in a dedicated task.

# --- FastAPI webserver for health checks ----
api = FastAPI()

@api.get("/")
def root():
    return {"status": "ok"}

@api.get("/health")
def health():
    return {"status": "healthy"}

# On startup, launch the bot background task
@api.on_event("startup")
async def on_startup():
    # start bot in background
    asyncio.create_task(start_bot_app())

# Ensure clean shutdown: stop the bot when Render stops the process.
@api.on_event("shutdown")
async def on_shutdown():
    # nothing special here — app will quit with the process
    pass

if __name__ == "__main__":
    # local dev: run uvicorn directly
    init_db()
    uvicorn.run("web:api", host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), reload=False)
