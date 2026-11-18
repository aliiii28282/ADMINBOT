#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ربات نگهبان گروه تلگرام (نسخه کامل و اصلاح‌شده)

نصب:
    pip install -U "python-telegram-bot[job-queue]"
    (اگر پروکسی SOCKS داری:  pip install -U "python-telegram-bot[socks]")

اجرا (متغیرهای محیطی):
    Linux / Termux:
        export BOT_TOKEN="123456:ABC..."
        export OWNER_ID="123456789"
        export PROXY_URL="socks5://127.0.0.1:1080"   # اختیاری، فقط اگر تلگرام فیلتر است
        python guard_bot.py
    Windows (CMD):
        set BOT_TOKEN=123456:ABC...
        set OWNER_ID=123456789
        python guard_bot.py
"""
import asyncio
import functools
import html
import json
import logging
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import deque
from datetime import datetime, timedelta, timezone

from telegram import (BotCommand, ChatPermissions, InlineKeyboardButton,
                      InlineKeyboardMarkup, Update)
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.error import BadRequest, RetryAfter
from telegram.ext import (Application, CallbackQueryHandler, ChatMemberHandler,
                          CommandHandler, ContextTypes, Defaults, MessageHandler, TypeHandler, filters)

# ============================ تنظیمات ============================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
OWNER_ID = int(os.environ.get("OWNER_ID", "0"))
PROXY_URL = os.environ.get("PROXY_URL", "")
DB_PATH = os.environ.get("DB_PATH", "guard_bot.db")
LOG_PATH = "bot.log"
IRAN_TZ = timezone(timedelta(hours=3, minutes=30))
ANON_IDS = {1087968824, 777000}  # GroupAnonymousBot و پیام‌های کانال

# ============================ زمان واقعی تهران + تقویم شمسی ============================
# ساعت ربات با زمان سرور تلگرام همگام می‌شود (تاریخ پیام‌های دریافتی)، پس به ساعت اشتباه سیستم وابسته نیست.
CLOCK_OFFSET = 0.0
CLOCK_SAMPLES = deque(maxlen=40)  # (زمان محلی، اختلاف)
_FA2EN = str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789")
_EN2FA = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
JALALI_MONTHS = ["فروردین", "اردیبهشت", "خرداد", "تیر", "مرداد", "شهریور",
                 "مهر", "آبان", "آذر", "دی", "بهمن", "اسفند"]
WEEKDAYS = ["دوشنبه", "سه‌شنبه", "چهارشنبه", "پنجشنبه", "جمعه", "شنبه", "یکشنبه"]  # بر اساس weekday()


def now_ts():
    return time.time() + CLOCK_OFFSET


def now_utc():
    return datetime.fromtimestamp(now_ts(), timezone.utc)


def now_tehran():
    return datetime.fromtimestamp(now_ts(), IRAN_TZ)


async def clock_sync(update, context):
    """با هر پیام جدید، اختلاف ساعت سیستم با سرور تلگرام را حساب می‌کند."""
    global CLOCK_OFFSET
    m = update.message
    if not m or not m.date:
        return
    now = time.time()
    CLOCK_SAMPLES.append((now, m.date.timestamp() - now))
    fresh = [o for t, o in CLOCK_SAMPLES if now - t <= 1800]
    if fresh:
        CLOCK_OFFSET = max(fresh)  # کمترین تأخیر شبکه نزدیک‌ترین به واقعیت است


def g2j(gy, gm, gd):
    g_d_m = [0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334]
    gy2 = gy + 1 if gm > 2 else gy
    days = 355666 + (365 * gy) + ((gy2 + 3) // 4) - ((gy2 + 99) // 100) + ((gy2 + 399) // 400) + gd + g_d_m[gm - 1]
    jy = -1595 + 33 * (days // 12053)
    days %= 12053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + days // 31, 1 + days % 31
    else:
        jm, jd = 7 + (days - 186) // 30, 1 + (days - 186) % 30
    return jy, jm, jd


def j2g(jy, jm, jd):
    jy += 1595
    days = -355668 + (365 * jy) + ((jy // 33) * 8) + (((jy % 33) + 3) // 4) + jd
    days += (jm - 1) * 31 if jm < 7 else ((jm - 7) * 30) + 186
    gy = 400 * (days // 146097)
    days %= 146097
    if days > 36524:
        days -= 1
        gy += 100 * (days // 36524)
        days %= 36524
        if days >= 365:
            days += 1
    gy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        gy += (days - 1) // 365
        days = (days - 1) % 365
    gd = days + 1
    leap = (gy % 4 == 0 and gy % 100 != 0) or gy % 400 == 0
    months = [0, 31, 29 if leap else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31]
    gm = 0
    while gm < 13 and gd > months[gm]:
        gd -= months[gm]
        gm += 1
    return gy, gm, gd


def fa(text):
    return str(text).translate(_EN2FA)


def fmt_jalali(dt, secs=False):
    dt = dt.astimezone(IRAN_TZ)
    jy, jm, jd = g2j(dt.year, dt.month, dt.day)
    out = f"{jy}/{jm:02}/{jd:02} {dt.hour:02}:{dt.minute:02}" + (f":{dt.second:02}" if secs else "")
    return fa(out)


def fmt_jalali_long(dt, secs=False):
    dt = dt.astimezone(IRAN_TZ)
    jy, jm, jd = g2j(dt.year, dt.month, dt.day)
    clock = f"{dt.hour:02}:{dt.minute:02}" + (f":{dt.second:02}" if secs else "")
    return fa(f"{WEEKDAYS[dt.weekday()]} {jd} {JALALI_MONTHS[jm - 1]} {jy}، ساعت {clock}")


def fmt_ts(ts):
    return fmt_jalali(datetime.fromtimestamp(ts, IRAN_TZ))


def stamp_file():
    dt = now_tehran()
    jy, jm, jd = g2j(dt.year, dt.month, dt.day)
    return f"{jy}-{jm:02}-{jd:02}_{dt:%H%M}"


def parse_when(text):
    """زمان شمسی تهران: «1405/09/05 23:30» یا «+90» (دقیقه دیگر، یا +2h / +1d) یا «23:30»."""
    t = text.strip().translate(_FA2EN)
    now = now_tehran()
    m = re.fullmatch(r"\+?\s*(\d+)", t)
    if m:
        dt = now + timedelta(minutes=int(m.group(1)))
    elif re.fullmatch(r"\+?\d+[mhdMHD]", t):
        dt = now + parse_duration(t.lstrip("+").lower())
    else:
        m = re.fullmatch(r"(\d{4})[/\-.](\d{1,2})[/\-.](\d{1,2})\s*[ ,T]\s*(\d{1,2}):(\d{2})", t)
        if m:
            jy, jm, jd, hh, mm = (int(x) for x in m.groups())
            if not (1 <= jm <= 12 and 1 <= jd <= 31 and hh < 24 and mm < 60):
                raise ValueError("تاریخ یا ساعت نامعتبر است.")
            gy, gm, gd = j2g(jy, jm, jd)
            if g2j(gy, gm, gd) != (jy, jm, jd):
                raise ValueError("این روز در تقویم شمسی وجود ندارد.")
            dt = datetime(gy, gm, gd, hh, mm, tzinfo=IRAN_TZ)
        else:
            m = re.fullmatch(r"(\d{1,2}):(\d{2})", t)
            if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
                raise ValueError("فرمت زمان نامعتبر. مثال: 1405/09/05 23:30  یا  +90  یا  23:30")
            dt = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
            if dt <= now:
                dt += timedelta(days=1)
    if dt <= now:
        raise ValueError("این زمان گذشته است.")
    return dt


def parse_range(text):
    """«23:30-07:00» یا «23-7» یا «off» را به شکل استاندارد HH:MM-HH:MM برمی‌گرداند."""
    t = text.strip().translate(_FA2EN).lower()
    if t == "off":
        return "off"
    m = re.fullmatch(r"(\d{1,2})(?::(\d{2}))?\s*-\s*(\d{1,2})(?::(\d{2}))?", t)
    if not m:
        raise ValueError("فرمت نامعتبر. مثال: 23:30-07:00 یا off")
    h1, m1, h2, m2 = int(m[1]), int(m[2] or 0), int(m[3]), int(m[4] or 0)
    if h1 > 23 or h2 > 23 or m1 > 59 or m2 > 59 or (h1, m1) == (h2, m2):
        raise ValueError("ساعت یا دقیقه نامعتبر است.")
    return f"{h1:02}:{m1:02}-{h2:02}:{m2:02}"


def in_range(rng, now):
    a, b = rng.split("-")
    s = int(a[:2]) * 60 + int(a[3:])
    e = int(b[:2]) * 60 + int(b[3:])
    cur = now.hour * 60 + now.minute
    return (s <= cur < e) if s < e else (cur >= s or cur < e)


class JalaliFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created + CLOCK_OFFSET, IRAN_TZ)
        jy, jm, jd = g2j(dt.year, dt.month, dt.day)
        return f"{jy}/{jm:02}/{jd:02} {dt:%H:%M:%S}"


_handlers = [logging.StreamHandler(), logging.FileHandler(LOG_PATH, encoding="utf-8")]
for _h in _handlers:
    _h.setFormatter(JalaliFormatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s"))
logging.basicConfig(level=logging.INFO, handlers=_handlers)
logging.getLogger("httpx").setLevel(logging.WARNING)  # جلوگیری از لو رفتن توکن در لاگ
log = logging.getLogger("guard")

# ============================ سبک پیام‌های سیستمی (bold / italic / نقل‌قول / ایموجی) ============================
def E(x):
    return html.escape(str(x))


def card(emoji, title, lines=(), note=None):
    """کارت پیام: عنوان bold، متن داخل نقل‌قول (blockquote)، یادداشت italic."""
    out = f"{emoji} <b>{title}</b>"
    if lines:
        out += "\n<blockquote>" + "\n".join(str(x) for x in lines) + "</blockquote>"
    if note:
        out += f"\n<i>{note}</i>"
    return out


def fail_card(e):
    return card("❌", "خطا", [E(e)], "دسترسی‌های ربات (ادمین بودن، حذف پیام، محدود کردن کاربران) را بررسی کنید 🔧")


def usage_card(*lines, title="راهنمای دستور"):
    return card("💡", title, list(lines), "برای راهنمای کامل: /help 📖")


def fnum(n):
    return fa(f"{int(n):,}".replace(",", "٬"))


def bar(frac, n=10):
    k = round(max(0.0, min(1.0, frac)) * n)
    return "▰" * k + "▱" * (n - k)


def spark(values):
    chars = "▁▂▃▄▅▆▇█"
    mx = max(values) if values else 0
    return "".join(chars[min(7, int(v / mx * 7.999))] if mx and v else "▁" for v in values)


def human_delta(seconds):
    sec = int(max(0, seconds))
    d, sec = divmod(sec, 86400)
    h, sec = divmod(sec, 3600)
    parts = []
    if d:
        parts.append(f"{d} روز")
    if h:
        parts.append(f"{h} ساعت")
    if sec // 60 or not parts:
        parts.append(f"{sec // 60} دقیقه")
    return fa(" و ".join(parts))


MEDALS = ["🥇", "🥈", "🥉"]


LOCKS = {  # کلید دستور -> (ستون دیتابیس، برچسب فارسی)
    "link": ("lock_link", "لینک"),
    "photo": ("lock_photo", "عکس"),
    "video": ("lock_video", "ویدیو"),
    "gif": ("lock_gif", "گیف"),
    "forward": ("lock_forward", "فوروارد"),
    "phone": ("lock_phone", "شماره تلفن"),
}

SEED_WORDS = ["کیر", "کیری", "کون", "کونی", "جنده", "کصکش", "کسکش",
              "حرومزاده", "حرامزاده", "گاییدم", "مادرجنده"]


# ============================ دیتابیس ============================
def q(sql, params=(), one=False, many=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(sql, params)
        res = cur.fetchone() if one else (cur.fetchall() if many else None)
        conn.commit()
        return res
    finally:
        conn.close()


def init_db():
    db_parent = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(db_parent, exist_ok=True)
    q("""CREATE TABLE IF NOT EXISTS chats (
        chat_id INTEGER PRIMARY KEY, title TEXT,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    q("""CREATE TABLE IF NOT EXISTS roles (
        chat_id INTEGER, user_id INTEGER, role TEXT,
        PRIMARY KEY (chat_id, user_id))""")
    q("""CREATE TABLE IF NOT EXISTS warnings (
        chat_id INTEGER, user_id INTEGER, count INTEGER DEFAULT 0,
        PRIMARY KEY (chat_id, user_id))""")
    q("""CREATE TABLE IF NOT EXISTS settings (
        chat_id INTEGER PRIMARY KEY,
        lock_link INTEGER DEFAULT 0, lock_photo INTEGER DEFAULT 0,
        lock_video INTEGER DEFAULT 0, lock_gif INTEGER DEFAULT 0,
        lock_forward INTEGER DEFAULT 0, lock_phone INTEGER DEFAULT 0)""")
    q("CREATE TABLE IF NOT EXISTS global_settings (key TEXT PRIMARY KEY, value TEXT)")
    q("CREATE TABLE IF NOT EXISTS words (word TEXT PRIMARY KEY)")
    q("""CREATE TABLE IF NOT EXISTS schedules (
        id INTEGER PRIMARY KEY AUTOINCREMENT, run_at INTEGER, kind TEXT,
        payload TEXT, done INTEGER DEFAULT 0)""")
    q("""CREATE TABLE IF NOT EXISTS user_stats (
        chat_id INTEGER, user_id INTEGER, name TEXT,
        msgs INTEGER DEFAULT 0, chars INTEGER DEFAULT 0, texts INTEGER DEFAULT 0,
        photos INTEGER DEFAULT 0, videos INTEGER DEFAULT 0, stickers INTEGER DEFAULT 0,
        voices INTEGER DEFAULT 0, docs INTEGER DEFAULT 0, gifs INTEGER DEFAULT 0,
        viol INTEGER DEFAULT 0, first_seen INTEGER, last_seen INTEGER,
        PRIMARY KEY (chat_id, user_id))""")
    q("""CREATE TABLE IF NOT EXISTS chat_daily (
        chat_id INTEGER, day INTEGER, hour INTEGER, msgs INTEGER DEFAULT 0,
        PRIMARY KEY (chat_id, day, hour))""")
    q("""CREATE TABLE IF NOT EXISTS user_daily (
        chat_id INTEGER, user_id INTEGER, day INTEGER, msgs INTEGER DEFAULT 0,
        PRIMARY KEY (chat_id, user_id, day))""")
    q("""CREATE TABLE IF NOT EXISTS chat_counters (
        chat_id INTEGER, key TEXT, value INTEGER DEFAULT 0, PRIMARY KEY (chat_id, key))""")
    # پشتیبانی: تیکت‌ها و پیام‌های هر گفتگو؛ ایجاد جدول‌ها بدون دست‌کاری داده‌های قبلی
    q("""CREATE TABLE IF NOT EXISTS support_tickets (
        id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, username TEXT,
        full_name TEXT, source_chat_id INTEGER NOT NULL, source_chat_title TEXT,
        source_message_id INTEGER, source_bot_message_id INTEGER, source_type TEXT DEFAULT 'group',
        status TEXT DEFAULT 'open', created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL)""")
    q("""CREATE TABLE IF NOT EXISTS support_messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL, sender_id INTEGER NOT NULL,
        sender_role TEXT NOT NULL, body TEXT NOT NULL, created_at INTEGER NOT NULL,
        FOREIGN KEY(ticket_id) REFERENCES support_tickets(id))""")
    q("CREATE INDEX IF NOT EXISTS idx_support_status_updated ON support_tickets(status, updated_at DESC)")
    q("""CREATE TABLE IF NOT EXISTS support_pending (
        chat_id INTEGER, user_id INTEGER, source_type TEXT, source_message_id INTEGER, created_at INTEGER,
        PRIMARY KEY (chat_id,user_id))""")
    if get_global("words_seeded") != "1":
        for w in SEED_WORDS:
            q("INSERT OR IGNORE INTO words VALUES (?)", (w,))
        set_global("words_seeded", "1")


KNOWN_CHATS = set()


def add_chat(chat_id, title):
    if chat_id in KNOWN_CHATS:
        return
    q("INSERT OR IGNORE INTO chats (chat_id, title) VALUES (?, ?)", (chat_id, title))
    KNOWN_CHATS.add(chat_id)


def get_all_chats():
    return [(r["chat_id"], r["title"]) for r in q("SELECT chat_id, title FROM chats", many=True)]


def remove_chat(chat_id):
    q("DELETE FROM chats WHERE chat_id=?", (chat_id,))
    KNOWN_CHATS.discard(chat_id)


def add_role(cid, uid, role):
    q("INSERT OR REPLACE INTO roles VALUES (?,?,?)", (cid, uid, role))


def remove_role(cid, uid):
    q("DELETE FROM roles WHERE chat_id=? AND user_id=?", (cid, uid))


def get_role(cid, uid):
    r = q("SELECT role FROM roles WHERE chat_id=? AND user_id=?", (cid, uid), one=True)
    return r["role"] if r else None


def list_roles(cid, role):
    return [r["user_id"] for r in q("SELECT user_id FROM roles WHERE chat_id=? AND role=?", (cid, role), many=True)]


def get_warn(cid, uid):
    r = q("SELECT count FROM warnings WHERE chat_id=? AND user_id=?", (cid, uid), one=True)
    return r["count"] if r else 0


def add_warn(cid, uid):
    q("INSERT INTO warnings VALUES (?,?,1) ON CONFLICT(chat_id,user_id) DO UPDATE SET count=count+1", (cid, uid))


def reset_warn(cid, uid):
    q("DELETE FROM warnings WHERE chat_id=? AND user_id=?", (cid, uid))


def reset_all_warns(cid):
    q("DELETE FROM warnings WHERE chat_id=?", (cid,))


def get_lock(cid, key):
    if get_global("glock_" + key) == "1":
        return True
    col = LOCKS[key][0]  # فقط کلیدهای مجاز وارد SQL می‌شوند
    r = q(f"SELECT {col} FROM settings WHERE chat_id=?", (cid,), one=True)
    return bool(r[col]) if r else False


def set_lock(cid, key, value):
    col = LOCKS[key][0]
    q(f"INSERT INTO settings (chat_id,{col}) VALUES (?,?) ON CONFLICT(chat_id) DO UPDATE SET {col}=?",
      (cid, value, value))


def get_global(key, default=None):
    r = q("SELECT value FROM global_settings WHERE key=?", (key,), one=True)
    return r["value"] if r else default


def set_global(key, value):
    q("INSERT OR REPLACE INTO global_settings VALUES (?,?)", (key, str(value)))


# ============================ موتور ضدفحش ============================
_DIGITS = dict(zip("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
_TRANS = str.maketrans({**_DIGITS, "ي": "ی", "ك": "ک", "ة": "ه", "ۀ": "ه",
                        "أ": "ا", "إ": "ا", "ٱ": "ا", "ؤ": "و"})
_STRIP = re.compile("[\u064B-\u065F\u0670\u0640\u200B\u200D-\u200F\u202A-\u202E\u2060\uFEFF]")
_TOKEN = re.compile(r"[^\W_]+")
_WORDS = None  # (set_for_3plus, set_for_2plus, long_words_3plus, long_words_2plus)


def normalize_base(text):
    text = text.translate(_TRANS).replace("\u200c", " ")
    return _STRIP.sub("", text).lower()


def _collapse(s, n):
    return re.sub(r"(.)\1{%d,}" % (n - 1), r"\1", s)


def invalidate_words():
    global _WORDS
    _WORDS = None


def get_words():
    global _WORDS
    if _WORDS is None:
        raw = [normalize_base(r["word"]) for r in q("SELECT word FROM words", many=True)]
        w3 = {_collapse(w, 3) for w in raw}
        w2 = {_collapse(w, 2) for w in raw}
        _WORDS = (w3, w2, [w for w in w3 if len(w) >= 4], [w for w in w2 if len(w) >= 4])
    return _WORDS


def merge_singles(tokens):
    """«ک ی ر» که حرف‌به‌حرف نوشته شده را یک کلمه حساب می‌کند."""
    out, buf = [], ""
    for t in tokens:
        if len(t) == 1:
            buf += t
        else:
            if buf:
                out.append(buf)
                buf = ""
            out.append(t)
    if buf:
        out.append(buf)
    return out


def contains_profanity(text):
    if not text:
        return False
    w3, w2, long3, long2 = get_words()
    if not w3:
        return False
    base = normalize_base(text)
    for n, wset, longs in ((3, w3, long3), (2, w2, long2)):
        tokens = merge_singles(_TOKEN.findall(_collapse(base, n)))
        for t in tokens:
            if t in wset or any(w in t for w in longs):
                return True
    return False


# ============================ مجوزها ============================
_TG_CACHE = {}


async def tg_status(context, cid, uid):
    key = (cid, uid)
    now = datetime.now().timestamp()
    hit = _TG_CACHE.get(key)
    if hit and now - hit[0] < 60:
        return hit[1]
    try:
        st = (await context.bot.get_chat_member(cid, uid)).status
    except Exception:
        st = None
    _TG_CACHE[key] = (now, st)
    return st


async def user_level(context, cid, uid):
    """3=مالک، 2=مدیر ویژه، 1=مدیر، 0=عادی"""
    if uid == OWNER_ID:
        return 3
    role = get_role(cid, uid)
    if role == "special_admin":
        return 2
    if role == "admin":
        return 1
    st = await tg_status(context, cid, uid)
    if st == ChatMemberStatus.OWNER:
        return 2
    if st == ChatMemberStatus.ADMINISTRATOR:
        return 1
    return 0


def is_exempt(cid, uid):
    return get_role(cid, uid) == "exempt"


def require(min_level, group_only=True):
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
            msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
            if not msg or not user:
                return
            if get_global("maintenance_mode", "0") == "1" and user.id != OWNER_ID:
                return  # ربات خاموش است
            if group_only and chat.type == ChatType.PRIVATE:
                return await msg.reply_text(card("👥", "فقط در گروه", ["این دستور فقط در گروه کار می‌کند."]))
            if await user_level(context, chat.id, user.id) < min_level:
                return await msg.reply_text(card("⛔", "دسترسی ندارید", ["این دستور مخصوص مدیران است."], "اگر فکر می‌کنید اشتباه است با مدیر گروه صحبت کنید 💬"))
            return await fn(update, context)
        return wrapper
    return deco


# ============================ توابع کمکی ============================
def mention(user):
    return f'<a href="tg://user?id={user.id}">{html.escape(user.first_name or "کاربر")}</a>'


def parse_duration(s):
    if not s or len(s) < 2:
        return None
    try:
        n = int(s[:-1])
    except ValueError:
        return None
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}.get(s[-1].lower())


async def get_target(update):
    r = update.effective_message.reply_to_message
    if r and r.from_user:
        return r.from_user
    await update.effective_message.reply_text(card("↩️", "ریپلای کنید", ["روی پیام کاربر ریپلای کنید و دستور را بفرستید."], "مثال: ریپلای + «بن» 😉"))
    return None


async def can_act_on(update, context, target):
    """فقط روی کسی که سطحش پایین‌تر است و معاف/ربات نیست."""
    cid = update.effective_chat.id
    actor = await user_level(context, cid, update.effective_user.id)
    if target.id == context.bot.id or is_exempt(cid, target.id) or \
            await user_level(context, cid, target.id) >= actor:
        await update.effective_message.reply_text(card("🛡️", "اقدام ممکن نیست", ["این کاربر معاف یا هم‌سطح/بالاتر از شماست."], "ربات روی مدیران و معاف‌ها اقدامی نمی‌کند 🙏"))
        return False
    return True


async def _delete_job(context: ContextTypes.DEFAULT_TYPE):
    d = context.job.data
    try:
        await context.bot.delete_message(d[0], d[1])
    except Exception:
        pass


async def notice(context, chat_id, text, ttl=10):
    """پیام کوتاه که بعد از چند ثانیه پاک می‌شود."""
    m = await context.bot.send_message(chat_id, text, parse_mode="HTML")
    if context.job_queue:
        context.job_queue.run_once(_delete_job, ttl, data=(chat_id, m.message_id))
    return m


EPHEMERAL_OK = None  # None=هنوز تست نشده، True=کار می‌کند، False=API پشتیبانی نمی‌کند


async def send_ephemeral(context, chat_id, user, text, ttl=15):
    """پیامی که فقط کاربر مقصد می‌بیند (ephemeral_message_parameters).
    اگر تلگرام این پارامتر را نپذیرد، پیام عادی می‌فرستد که بعد از ttl ثانیه پاک می‌شود."""
    global EPHEMERAL_OK
    m = None
    if EPHEMERAL_OK is not False:
        try:
            m = await context.bot.send_message(
                chat_id, text, parse_mode="HTML",
                api_kwargs={"ephemeral_message_parameters": {"receiver_user_id": user.id}})
            EPHEMERAL_OK = True
        except Exception as e:
            if EPHEMERAL_OK is None and isinstance(e, BadRequest):
                EPHEMERAL_OK = False
                log.warning("ephemeral پشتیبانی نمی‌شود، از پیام موقت عادی استفاده می‌شود: %s", e)
            else:
                log.warning("ephemeral failed: %s", e)
    if m is None:
        return await notice(context, chat_id, text, ttl)
    if context.job_queue:  # اگر پارامتر نادیده گرفته شده باشد پیام عمومی نماند
        context.job_queue.run_once(_delete_job, ttl, data=(chat_id, m.message_id))
    return m


async def full_default_perms(context, cid):
    try:
        p = (await context.bot.get_chat(cid)).permissions
        if p:
            return p
    except Exception:
        pass
    return ChatPermissions(can_send_messages=True, can_send_polls=True,
                           can_send_other_messages=True, can_add_web_page_previews=True)


async def do_warn(context, chat, target, reason=""):
    cid = chat.id
    if is_exempt(cid, target.id):
        return
    add_warn(cid, target.id)
    bump(cid, "warns")
    count = get_warn(cid, target.id)
    limit = max(1, int(get_global("warn_limit", 3)))
    why = [f"📝 دلیل: <i>{E(reason)}</i>"] if reason else []
    if count >= limit:
        try:
            await context.bot.ban_chat_member(cid, target.id)
            reset_warn(cid, target.id)
            bump(cid, "bans")
            await notice(context, cid, card(
                "🚫", "کاربر به‌خاطر اخطارها بن شد",
                [f"👤 کاربر: {mention(target)}", f"⚠️ اخطار: {fa(count)}/{fa(limit)} {bar(1, limit)}", *why],
                "قوانین گروه را جدی بگیریم 🤝"))
        except Exception as e:
            await notice(context, cid, fail_card(e))
    else:
        await notice(context, cid, card(
            "⚠️", "اخطار ثبت شد",
            [f"👤 کاربر: {mention(target)}", f"📊 اخطار: {fa(count)}/{fa(limit)} {bar(count / limit, limit)}", *why],
            f"با {fa(limit - count)} اخطار دیگر بن می‌شوید ‼️"))


def locks_text(cid):
    lines = [f"{'🔒' if get_lock(cid, k) else '🔓'} {v[1]} — <code>{k}</code>" for k, v in LOCKS.items()]
    return card("🔐", "وضعیت قفل‌ها", lines, "/lock نوع  •  /unlock نوع 🗝️")


# ============================ آمار ============================
STAT_FIELDS = ("msgs", "chars", "texts", "photos", "videos", "stickers", "voices", "docs", "gifs", "viol")
STAT_USER = {}    # (chat, user) -> شمارنده‌ها (قبل از ذخیره در دیتابیس)
STAT_DAILY = {}   # (chat, day, hour) -> n
STAT_UDAILY = {}  # (chat, user, day) -> n
STAT_COUNT = {}   # (chat, key) -> n
STAT_COOLDOWN = {}


def day_int(dt):
    jy, jm, jd = g2j(dt.year, dt.month, dt.day)
    return jy * 10000 + jm * 100 + jd


def days_back(n):
    now = now_tehran()
    return [day_int(now - timedelta(days=k)) for k in range(n)]


def day_to_date(d):
    gy, gm, gd = j2g(d // 10000, d // 100 % 100, d % 100)
    return datetime(gy, gm, gd).date()


def day_fa(d):
    return fa(f"{d // 10000}/{d // 100 % 100:02}/{d % 100:02}")


def _urec(cid, uid):
    r = STAT_USER.get((cid, uid))
    if r is None:
        r = {f: 0 for f in STAT_FIELDS}
        r.update(name=None, first=None, last=None)
        STAT_USER[(cid, uid)] = r
    return r


def bump(cid, key, n=1):
    STAT_COUNT[(cid, key)] = STAT_COUNT.get((cid, key), 0) + n


def bump_user(cid, uid, field, n=1):
    _urec(cid, uid)[field] += n


def record_message(msg, chat, user):
    cid = chat.id
    dt = msg.date.astimezone(IRAN_TZ) if msg.date else now_tehran()
    ts = int(dt.timestamp())
    r = _urec(cid, user.id)
    r["name"] = user.full_name
    r["msgs"] += 1
    r["last"] = ts
    if r["first"] is None:
        r["first"] = ts
    if msg.text:
        r["texts"] += 1
        r["chars"] += len(msg.text)
    else:
        if msg.caption:
            r["chars"] += len(msg.caption)
        if msg.photo:
            r["photos"] += 1
        elif msg.video or msg.video_note:
            r["videos"] += 1
        elif msg.sticker:
            r["stickers"] += 1
        elif msg.voice or msg.audio:
            r["voices"] += 1
        elif msg.animation:
            r["gifs"] += 1
        elif msg.document:
            r["docs"] += 1
    day = day_int(dt)
    k = (cid, day, dt.hour)
    STAT_DAILY[k] = STAT_DAILY.get(k, 0) + 1
    k2 = (cid, user.id, day)
    STAT_UDAILY[k2] = STAT_UDAILY.get(k2, 0) + 1


def flush_stats():
    """شمارنده‌های حافظه را (هر ۲۰ ثانیه و قبل از نمایش آمار) در دیتابیس ذخیره می‌کند."""
    global STAT_USER, STAT_DAILY, STAT_UDAILY, STAT_COUNT
    if not (STAT_USER or STAT_DAILY or STAT_UDAILY or STAT_COUNT):
        return
    users, daily, udaily, counts = STAT_USER, STAT_DAILY, STAT_UDAILY, STAT_COUNT
    STAT_USER, STAT_DAILY, STAT_UDAILY, STAT_COUNT = {}, {}, {}, {}
    conn = sqlite3.connect(DB_PATH)
    try:
        for (cid, uid), r in users.items():
            conn.execute(
                """INSERT INTO user_stats (chat_id,user_id,name,msgs,chars,texts,photos,videos,stickers,voices,
                   docs,gifs,viol,first_seen,last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(chat_id,user_id) DO UPDATE SET
                   name=COALESCE(excluded.name,name), msgs=msgs+excluded.msgs, chars=chars+excluded.chars,
                   texts=texts+excluded.texts, photos=photos+excluded.photos, videos=videos+excluded.videos,
                   stickers=stickers+excluded.stickers, voices=voices+excluded.voices, docs=docs+excluded.docs,
                   gifs=gifs+excluded.gifs, viol=viol+excluded.viol,
                   first_seen=COALESCE(first_seen,excluded.first_seen),
                   last_seen=MAX(COALESCE(last_seen,0),COALESCE(excluded.last_seen,0))""",
                (cid, uid, r["name"], r["msgs"], r["chars"], r["texts"], r["photos"], r["videos"], r["stickers"],
                 r["voices"], r["docs"], r["gifs"], r["viol"], r["first"], r["last"]))
        for (cid, day, hour), n in daily.items():
            conn.execute("INSERT INTO chat_daily VALUES (?,?,?,?) ON CONFLICT(chat_id,day,hour) "
                         "DO UPDATE SET msgs=msgs+excluded.msgs", (cid, day, hour, n))
        for (cid, uid, day), n in udaily.items():
            conn.execute("INSERT INTO user_daily VALUES (?,?,?,?) ON CONFLICT(chat_id,user_id,day) "
                         "DO UPDATE SET msgs=msgs+excluded.msgs", (cid, uid, day, n))
        for (cid, key), n in counts.items():
            conn.execute("INSERT INTO chat_counters VALUES (?,?,?) ON CONFLICT(chat_id,key) "
                         "DO UPDATE SET value=value+excluded.value", (cid, key, n))
        conn.commit()
    finally:
        conn.close()


async def stats_tick(context):
    flush_stats()


def _qv(sql, params=()):
    r = q(sql, params, one=True)
    return (r[0] or 0) if r else 0


def _qs(sql, params=()):
    return q(sql, params, many=True)


def day_sum(cid, days):
    ph = ",".join("?" * len(days))
    return _qv(f"SELECT SUM(msgs) FROM chat_daily WHERE chat_id=? AND day IN ({ph})", (cid, *days))


def uname(row):
    return E((row["name"] or f"کاربر {row['user_id']}")[:22])


async def _cooldown(context, update, secs=12):
    uid = update.effective_user.id
    if await user_level(context, update.effective_chat.id, uid) >= 1:
        return False
    now = time.time()
    if now - STAT_COOLDOWN.get(uid, 0) < secs:
        return True
    STAT_COOLDOWN[uid] = now
    return False


CS_VIEWS = [("top", "🏆 فعال‌ترین‌ها"), ("total", "📈 مجموع و روند"), ("hours", "⏰ ساعت‌های اوج"),
            ("types", "🎨 نوع پیام‌ها"), ("members", "👥 اعضای فعال"), ("today", "🔥 امروز"),
            ("bot", "🛡️ عملکرد ربات")]


def cs_kb(view, cid):
    btns = [InlineKeyboardButton(("• " if k == view else "") + t, callback_data=f"cs:{k}:{cid}") for k, t in CS_VIEWS]
    rows = [btns[i:i + 2] for i in range(0, len(btns), 2)]
    rows.append([InlineKeyboardButton("🔄 بروزرسانی", callback_data=f"cs:{view}:{cid}")])
    return InlineKeyboardMarkup(rows)


def chatstats_text(view, cid):
    first_day = _qv("SELECT MIN(day) FROM chat_daily WHERE chat_id=?", (cid,))
    since = f"📅 آمار از {day_fa(first_day)} (به وقت تهران)" if first_day else "🌱 هنوز آماری ثبت نشده"
    total = _qv("SELECT SUM(msgs) FROM user_stats WHERE chat_id=?", (cid,))
    if not total and view != "bot":
        return card("📊", "آمار گپ", ["هنوز پیامی ثبت نشده 🌱"], "با فعالیت اعضا آمار ساخته می‌شود ✨")

    if view == "top":
        rows = _qs("SELECT user_id,name,msgs FROM user_stats WHERE chat_id=? AND msgs>0 ORDER BY msgs DESC LIMIT 10", (cid,))
        lines = []
        for i, r in enumerate(rows):
            icon = MEDALS[i] if i < 3 else f"{fa(i + 1)}."
            share = r["msgs"] / total
            lines.append(f"{icon} <b>{uname(r)}</b> — {fnum(r['msgs'])} پیام\n      {bar(share)} {fa(round(share * 100))}٪")
        members = _qv("SELECT COUNT(*) FROM user_stats WHERE chat_id=? AND msgs>0", (cid,))
        return (card("🏆", "آمار گپ — فعال‌ترین‌ها", lines) +
                f"\n\n💬 مجموع پیام‌ها: <b>{fnum(total)}</b>  •  👥 اعضای ثبت‌شده: <b>{fnum(members)}</b>\n<i>{since}</i>")

    if view == "total":
        t_today = day_sum(cid, days_back(1))
        d2 = days_back(2)[1]
        t_yest = day_sum(cid, [d2])
        t7 = day_sum(cid, days_back(7))
        t30 = day_sum(cid, days_back(30))
        ndays = 1
        if first_day:
            ndays = max(1, (now_tehran().date() - day_to_date(first_day)).days + 1)
        best = _qs("SELECT day, SUM(msgs) s FROM chat_daily WHERE chat_id=? GROUP BY day ORDER BY s DESC LIMIT 1", (cid,))
        vals = [day_sum(cid, [d]) for d in reversed(days_back(14))]
        trend = ""
        if t_yest:
            ch = (t_today - t_yest) / t_yest * 100
            trend = f"{'📈' if ch >= 0 else '📉'} نسبت به دیروز: {fa(f'{ch:+.0f}')}٪"
        lines = [f"🌍 کل پیام‌ها: <b>{fnum(total)}</b>",
                 f"☀️ امروز: <b>{fnum(t_today)}</b>  •  🌙 دیروز: <b>{fnum(t_yest)}</b>",
                 f"🗓️ ۷ روز اخیر: <b>{fnum(t7)}</b>  •  ۳۰ روز اخیر: <b>{fnum(t30)}</b>",
                 f"⚖️ میانگین روزانه: <b>{fnum(round(total / ndays))}</b>"]
        if best:
            lines.append(f"🏅 رکورد یک روز: <b>{fnum(best[0]['s'])}</b> پیام ({day_fa(best[0]['day'])})")
        lines.append(f"📊 ۱۴ روز اخیر: {spark(vals)}")
        if trend:
            lines.append(trend)
        return card("📈", "آمار گپ — مجموع و روند", lines, since)

    if view == "hours":
        vals = [0] * 24
        for r in _qs("SELECT hour, SUM(msgs) s FROM chat_daily WHERE chat_id=? GROUP BY hour", (cid,)):
            vals[r["hour"]] = r["s"]
        tot = sum(vals) or 1
        top3 = sorted(range(24), key=lambda h: -vals[h])[:3]
        quiet = min(range(24), key=lambda h: vals[h])
        parts = [("🌅 صبح (۶ تا ۱۲)", sum(vals[6:12])), ("☀️ ظهر و عصر (۱۲ تا ۱۸)", sum(vals[12:18])),
                 ("🌆 غروب و شب (۱۸ تا ۲۴)", sum(vals[18:24])), ("🌙 نیمه‌شب (۰ تا ۶)", sum(vals[0:6]))]
        lines = [f"🕛 {spark(vals)} 🕚", "<i>(از ساعت ۰۰ تا ۲۳ به وقت تهران)</i>", ""]
        for i, h in enumerate(top3):
            lines.append(f"{MEDALS[i]} ساعت {fa(f'{h:02}')} تا {fa(f'{(h + 1) % 24:02}')} — {fnum(vals[h])} پیام ({fa(round(vals[h] / tot * 100))}٪)")
        lines.append(f"😴 ساکت‌ترین: ساعت {fa(f'{quiet:02}')}")
        lines.append("")
        lines += [f"{n}: {bar(v / tot, 8)} {fa(round(v / tot * 100))}٪" for n, v in parts]
        return card("⏰", "آمار گپ — ساعت‌های اوج", lines, since)

    if view == "types":
        r = q("SELECT SUM(texts) t, SUM(photos) p, SUM(videos) v, SUM(stickers) s, SUM(voices) o, SUM(docs) d, SUM(gifs) g "
              "FROM user_stats WHERE chat_id=?", (cid,), one=True)
        items = [("📝 متن", r["t"]), ("🖼️ عکس", r["p"]), ("🎬 ویدیو", r["v"]), ("🎭 استیکر", r["s"]),
                 ("🎙️ ویس و صوت", r["o"]), ("📎 فایل", r["d"]), ("🎞️ گیف", r["g"])]
        items = sorted(((n, v or 0) for n, v in items), key=lambda x: -x[1])
        lines = [f"{n}: <b>{fnum(v)}</b>  {bar(v / total, 8)} {fa(round(v / total * 100))}٪" for n, v in items if v]
        return card("🎨", "آمار گپ — ترکیب پیام‌ها", lines, since)

    if view == "members":
        a_today = _qv("SELECT COUNT(DISTINCT user_id) FROM user_daily WHERE chat_id=? AND day=?", (cid, days_back(1)[0]))
        ph7 = ",".join("?" * 7)
        a7 = _qv(f"SELECT COUNT(DISTINCT user_id) FROM user_daily WHERE chat_id=? AND day IN ({ph7})", (cid, *days_back(7)))
        ph30 = ",".join("?" * 30)
        a30 = _qv(f"SELECT COUNT(DISTINCT user_id) FROM user_daily WHERE chat_id=? AND day IN ({ph30})", (cid, *days_back(30)))
        tracked = _qv("SELECT COUNT(*) FROM user_stats WHERE chat_id=? AND msgs>0", (cid,))
        quiet = _qv("SELECT COUNT(*) FROM user_stats WHERE chat_id=? AND msgs>0 AND msgs<5", (cid,))
        midnight = now_tehran().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        new_today = _qv("SELECT COUNT(*) FROM user_stats WHERE chat_id=? AND first_seen>=?", (cid, int(midnight)))
        writer = _qs("SELECT user_id,name,chars*1.0/texts a FROM user_stats WHERE chat_id=? AND texts>=20 ORDER BY a DESC LIMIT 1", (cid,))
        lines = [f"🔥 فعال امروز: <b>{fnum(a_today)}</b> نفر",
                 f"📅 فعال ۷ روز اخیر: <b>{fnum(a7)}</b> نفر",
                 f"🗓️ فعال ۳۰ روز اخیر: <b>{fnum(a30)}</b> نفر",
                 f"👥 کل اعضای ثبت‌شده: <b>{fnum(tracked)}</b>",
                 f"🆕 اولین پیام‌شان امروز: <b>{fnum(new_today)}</b> نفر",
                 f"👻 کم‌حرف‌ها (کمتر از ۵ پیام): <b>{fnum(quiet)}</b> نفر",
                 f"⚖️ میانگین پیام برای هر نفر: <b>{fnum(round(total / max(1, tracked)))}</b>"]
        if writer:
            lines.append(f"✍️ پرحرف‌ترین (طول پیام): <b>{uname(writer[0])}</b> — {fnum(writer[0]['a'])} حرف")
        return card("👥", "آمار گپ — اعضای فعال", lines, since)

    if view == "today":
        today = days_back(1)[0]
        rows = _qs("SELECT u.user_id, s.name, u.msgs FROM user_daily u LEFT JOIN user_stats s "
                   "ON s.chat_id=u.chat_id AND s.user_id=u.user_id WHERE u.chat_id=? AND u.day=? "
                   "ORDER BY u.msgs DESC LIMIT 5", (cid, today))
        t_today = day_sum(cid, [today])
        if not rows:
            return card("🔥", "آمار گپ — امروز", ["امروز هنوز پیامی ثبت نشده 🌙"], day_fa(today))
        lines = [f"💬 مجموع پیام‌های امروز: <b>{fnum(t_today)}</b>", ""]
        for i, r in enumerate(rows):
            icon = MEDALS[i] if i < 3 else "🏅"
            lines.append(f"{icon} <b>{uname(r)}</b> — {fnum(r['msgs'])} پیام ({fa(round(r['msgs'] / max(1, t_today) * 100))}٪)")
        return card("🔥", "آمار گپ — امروز", lines, f"📅 {fmt_jalali_long(now_tehran())}")

    # bot
    c = {r["key"]: r["value"] for r in _qs("SELECT key, value FROM chat_counters WHERE chat_id=?", (cid,))}
    for (k_cid, key), n in STAT_COUNT.items():
        if k_cid == cid:
            c[key] = c.get(key, 0) + n
    lines = [f"🗑️ پیام حذف‌شده: <b>{fnum(c.get('deleted', 0))}</b>",
             f"🚨 اسپم مهارشده: <b>{fnum(c.get('spam', 0))}</b>",
             f"🤬 الفاظ نامناسب: <b>{fnum(c.get('profanity', 0))}</b>",
             f"🔒 تخلف قفل‌ها: <b>{fnum(c.get('locks', 0))}</b>",
             f"⚠️ اخطار صادرشده: <b>{fnum(c.get('warns', 0))}</b>",
             f"🔇 سکوت: <b>{fnum(c.get('mutes', 0))}</b>  •  🚫 بن: <b>{fnum(c.get('bans', 0))}</b>"]
    viol = _qs("SELECT user_id,name,viol FROM user_stats WHERE chat_id=? AND viol>0 ORDER BY viol DESC LIMIT 3", (cid,))
    if viol:
        lines.append("")
        lines.append("🚔 بیشترین تخلف:")
        lines += [f"{MEDALS[i]} {uname(r)} — {fnum(r['viol'])}" for i, r in enumerate(viol)]
    return card("🛡️", "آمار گپ — عملکرد ربات", lines, since)


async def chatstats_cmd(update, context):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type == ChatType.PRIVATE:
        return await msg.reply_text(card("📊", "آمار گپ", ["این دستور فقط در گروه کار می‌کند."]))
    if get_global("maintenance_mode", "0") == "1" and update.effective_user.id != OWNER_ID:
        return
    if await _cooldown(context, update):
        return
    flush_stats()
    await msg.reply_text(chatstats_text("top", chat.id), reply_markup=cs_kb("top", chat.id))


async def chatstats_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, view, cid = query.data.split(":", 2)
    if view not in dict(CS_VIEWS):
        return await query.answer()
    await query.answer("⏳")
    flush_stats()
    try:
        await query.edit_message_text(chatstats_text(view, int(cid)), reply_markup=cs_kb(view, int(cid)))
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def stats_cmd(update, context):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        return await msg.reply_text(card("📊", "آمار کاربر", ["این دستور فقط در گروه کار می‌کند."]))
    if get_global("maintenance_mode", "0") == "1" and user.id != OWNER_ID:
        return
    if await _cooldown(context, update):
        return
    flush_stats()
    r = msg.reply_to_message
    target = r.from_user if r and r.from_user else user
    cid = chat.id
    row = q("SELECT * FROM user_stats WHERE chat_id=? AND user_id=?", (cid, target.id), one=True)
    level = await user_level(context, cid, target.id)
    role = {3: "👑 مالک ربات", 2: "⭐ مدیر ویژه", 1: "🛡️ مدیر", 0: "👤 عضو عادی"}[level]
    st = await tg_status(context, cid, target.id)
    if st == ChatMemberStatus.OWNER:
        role += " • سازنده گروه"
    elif st == ChatMemberStatus.ADMINISTRATOR:
        role += " • ادمین تلگرام"
    warns = get_warn(cid, target.id)
    limit = max(1, int(get_global("warn_limit", 3)))
    status = "🟢 آزاد و فعال"
    try:
        cm = await context.bot.get_chat_member(cid, target.id)
        if cm.status == ChatMemberStatus.RESTRICTED and not getattr(cm, "can_send_messages", True):
            until = getattr(cm, "until_date", None)
            status = "🔇 در سکوت" + (f" تا {fmt_jalali(until)}" if until else "")
        elif cm.status == ChatMemberStatus.BANNED:
            status = "🚫 بن‌شده"
        elif cm.status == ChatMemberStatus.LEFT:
            status = "🚪 از گروه خارج شده"
    except Exception:
        pass
    head = card("📊", "آمار کاربر", [
        f"👤 نام: {mention(target)}",
        f"🆔 شناسه: <code>{target.id}</code>",
        f"🎖️ مقام: <b>{role}</b>",
        f"🛡️ معافیت: {'✅ معاف است' if is_exempt(cid, target.id) else '❌ معاف نیست'}",
        f"⚠️ اخطار: <b>{fa(warns)}/{fa(limit)}</b> {bar(warns / limit, limit)}",
        f"📡 وضعیت: {status}",
    ])
    if not row or not row["msgs"]:
        return await msg.reply_text(head + "\n\n" + card("💬", "فعالیت", ["هنوز پیامی از این کاربر ثبت نشده 🌱"]))
    total = _qv("SELECT SUM(msgs) FROM user_stats WHERE chat_id=?", (cid,))
    rank = _qv("SELECT COUNT(*) FROM user_stats WHERE chat_id=? AND msgs>?", (cid, row["msgs"])) + 1
    n_users = _qv("SELECT COUNT(*) FROM user_stats WHERE chat_id=? AND msgs>0", (cid,))
    ph7 = ",".join("?" * 7)
    d_today = _qv("SELECT SUM(msgs) FROM user_daily WHERE chat_id=? AND user_id=? AND day=?", (cid, target.id, days_back(1)[0]))
    d7 = _qv(f"SELECT SUM(msgs) FROM user_daily WHERE chat_id=? AND user_id=? AND day IN ({ph7})", (cid, target.id, *days_back(7)))
    share = row["msgs"] / total if total else 0
    kinds = [("📝 متن", row["texts"]), ("🖼️ عکس", row["photos"]), ("🎬 ویدیو", row["videos"]), ("🎭 استیکر", row["stickers"]),
             ("🎙️ ویس", row["voices"]), ("📎 فایل", row["docs"]), ("🎞️ گیف", row["gifs"])]
    kinds_txt = " • ".join(f"{n} {fnum(v)}" for n, v in kinds if v)
    act = [f"✉️ مجموع پیام: <b>{fnum(row['msgs'])}</b>",
           f"🏆 رتبه: <b>{fa(rank)}</b> از {fa(n_users)}  •  سهم: {bar(share, 8)} {fa(round(share * 100))}٪",
           f"☀️ امروز: <b>{fnum(d_today)}</b>  •  🗓️ ۷ روز اخیر: <b>{fnum(d7)}</b>",
           f"🧩 {kinds_txt}"]
    if row["texts"]:
        act.append(f"🔤 میانگین طول متن: <b>{fnum(row['chars'] / row['texts'])}</b> حرف")
    act.append(f"🚨 تخلف ثبت‌شده: <b>{fnum(row['viol'])}</b>")
    foot = []
    if row["first_seen"]:
        foot.append(f"🕰️ اولین پیام ثبت‌شده: {fmt_ts(row['first_seen'])}")
    if row["last_seen"]:
        foot.append(f"⏱️ آخرین فعالیت: {fmt_ts(row['last_seen'])}")
    await msg.reply_text(head + "\n\n" + card("💬", "فعالیت", act, " | ".join(foot) or None))


# ============================ دستورات گروه ============================
def actor(update):
    return mention(update.effective_user)


def open_perms():
    kw = dict(can_send_messages=True, can_send_polls=True, can_send_other_messages=True,
              can_add_web_page_previews=True, can_invite_users=True, can_send_audios=True,
              can_send_documents=True, can_send_photos=True, can_send_videos=True,
              can_send_video_notes=True, can_send_voice_notes=True)
    try:
        return ChatPermissions(**kw)
    except TypeError:
        return ChatPermissions(can_send_messages=True, can_send_polls=True, can_send_other_messages=True,
                               can_add_web_page_previews=True, can_invite_users=True)


async def set_chat_closed(context, cid, closed):
    """بستن/باز کردن گپ؛ دسترسی‌های قبلی گروه ذخیره و هنگام باز شدن برگردانده می‌شود."""
    if closed:
        if get_global(f"chat_closed_{cid}") != "1":
            cur = (await context.bot.get_chat(cid)).permissions
            if cur:
                set_global(f"closed_perms_{cid}", json.dumps(cur.to_dict()))
        await context.bot.set_chat_permissions(cid, ChatPermissions(can_send_messages=False))
        set_global(f"chat_closed_{cid}", "1")
    else:
        saved = get_global(f"closed_perms_{cid}")
        perms = ChatPermissions.de_json(json.loads(saved), context.bot) if saved else open_perms()
        if not perms.can_send_messages:
            perms = open_perms()
        await context.bot.set_chat_permissions(cid, perms)
        set_global(f"chat_closed_{cid}", "0")


async def _chat_toggle(update, context, closed):
    msg, chat = update.effective_message, update.effective_chat
    arg = " ".join(context.args).strip()
    word = "بسته" if closed else "باز"
    if arg:
        try:
            dt = parse_when(arg)
        except ValueError as e:
            return await msg.reply_text(card("❌", "زمان نامعتبر", [E(e)],
                                             f"مثال: «گپ {word} 23:45» یا «گپ {word} +30» ⏰"))
        sid = add_schedule(dt.timestamp(), "chat_close" if closed else "chat_open",
                           json.dumps({"chat_id": chat.id, "by": update.effective_user.id}))
        return await msg.reply_text(card(
            "⏰", "زمان‌بندی ثبت شد",
            [f"{'🔒' if closed else '🔓'} گپ در <b>{fmt_jalali(dt)}</b> {word} می‌شود",
             f"⌛ تا آن موقع: {human_delta(dt.timestamp() - now_ts())}",
             f"🆔 شماره: <code>#{sid}</code>", f"👮 توسط: {actor(update)}"],
            "لغو: /unschedule شماره 🗑️"))
    try:
        await set_chat_closed(context, chat.id, closed)
    except Exception as e:
        return await msg.reply_text(fail_card(e))
    if closed:
        await msg.reply_text(card("🔒", "گپ بسته شد", [f"👮 توسط: {actor(update)}", f"🕒 {fmt_jalali(now_tehran())} (تهران)",
                                                         "💬 فقط مدیران می‌توانند پیام بدهند"],
                                  "برای باز کردن: «گپ باز» 🔓"))
    else:
        await msg.reply_text(card("🔓", "گپ باز شد", [f"👮 توسط: {actor(update)}", f"🕒 {fmt_jalali(now_tehran())} (تهران)",
                                                       "💬 همه می‌توانند پیام بدهند 🎉"],
                                  "گپ را گرم نگه دارید 💕"))


@require(1)
async def closechat_cmd(update, context):
    await _chat_toggle(update, context, True)


@require(1)
async def openchat_cmd(update, context):
    await _chat_toggle(update, context, False)


def _chat_schedules(cid):
    out = []
    for r in _qs("SELECT id, run_at, kind, payload FROM schedules WHERE done=0 AND kind IN ('chat_open','chat_close') ORDER BY run_at"):
        try:
            if json.loads(r["payload"] or "{}").get("chat_id") == cid:
                out.append(r)
        except ValueError:
            pass
    return out


@require(1)
async def schedules_cmd(update, context):
    rows = _chat_schedules(update.effective_chat.id)
    lines = [f"<code>#{r['id']}</code> {'🔒 بستن' if r['kind'] == 'chat_close' else '🔓 باز کردن'} — {fmt_ts(r['run_at'])}"
             for r in rows] or ["زمان‌بندی فعالی برای این گروه نیست 🌿"]
    await update.effective_message.reply_text(card("⏰", "زمان‌بندی‌های این گروه", lines, "لغو: /unschedule شماره 🗑️"))


@require(1)
async def unschedule_cmd(update, context):
    msg = update.effective_message
    if not context.args or not context.args[0].lstrip("#").isdigit():
        return await msg.reply_text(usage_card("/unschedule 3", "شماره را از /schedules ببینید", title="لغو زمان‌بندی"))
    sid = int(context.args[0].lstrip("#"))
    mine = {r["id"] for r in _chat_schedules(update.effective_chat.id)}
    if sid not in mine and update.effective_user.id != OWNER_ID:
        return await msg.reply_text(card("🔎", "پیدا نشد", ["چنین زمان‌بندی‌ای برای این گروه نیست."]))
    q("UPDATE schedules SET done=1 WHERE id=? AND done=0", (sid,))
    await msg.reply_text(card("🗑️", "زمان‌بندی لغو شد", [f"🆔 شماره: <code>#{sid}</code>", f"👮 توسط: {actor(update)}"]))


@require(2)
async def ban_cmd(update, context):
    target = await get_target(update)
    if not target or not await can_act_on(update, context, target):
        return
    dur = parse_duration(context.args[0]) if context.args else None
    until = now_utc() + dur if dur else None
    try:
        await context.bot.ban_chat_member(update.effective_chat.id, target.id, until_date=until)
        bump(update.effective_chat.id, "bans")
        await update.effective_message.reply_text(card(
            "🚫", "کاربر بن شد",
            [f"👤 کاربر: {mention(target)}", f"⏳ مدت: {'تا ' + fmt_jalali(until) if dur else 'دائمی ♾️'}",
             f"👮 توسط: {actor(update)}"], "رعایت قوانین باعث آرامش همه است 🕊️"))
    except Exception as e:
        await update.effective_message.reply_text(fail_card(e))


@require(2)
async def unban_cmd(update, context):
    if not context.args or not re.fullmatch(r"\d+", context.args[0]):
        return await update.effective_message.reply_text(usage_card("/unban user_id", "مثال: /unban 123456789", title="رفع بن"))
    try:
        await context.bot.unban_chat_member(update.effective_chat.id, int(context.args[0]), only_if_banned=True)
        await update.effective_message.reply_text(card(
            "✅", "بن برداشته شد", [f"🆔 کاربر: <code>{E(context.args[0])}</code>", f"👮 توسط: {actor(update)}"],
            "می‌تواند دوباره به گروه بپیوندد 🎈"))
    except Exception as e:
        await update.effective_message.reply_text(fail_card(e))


@require(1)
async def mute_cmd(update, context):
    target = await get_target(update)
    if not target or not await can_act_on(update, context, target):
        return
    dur = parse_duration(context.args[0]) if context.args else timedelta(hours=1)
    if dur is None:
        return await update.effective_message.reply_text(usage_card("مدت نامعتبر ⏳", "مثال: 30m ، 2h ، 1d", title="سکوت"))
    try:
        await context.bot.restrict_chat_member(
            update.effective_chat.id, target.id, ChatPermissions(can_send_messages=False),
            until_date=now_utc() + dur)
        bump(update.effective_chat.id, "mutes")
        await update.effective_message.reply_text(card(
            "🔇", "کاربر در سکوت قرار گرفت",
            [f"👤 کاربر: {mention(target)}", f"⏳ تا: {fmt_jalali(now_utc() + dur)}", f"👮 توسط: {actor(update)}"],
            "کمی استراحت برای همه خوب است 🌙"))
    except Exception as e:
        await update.effective_message.reply_text(fail_card(e))


@require(1)
async def unmute_cmd(update, context):
    target = await get_target(update)
    if not target:
        return
    try:
        perms = await full_default_perms(context, update.effective_chat.id)
        await context.bot.restrict_chat_member(update.effective_chat.id, target.id, perms)
        await update.effective_message.reply_text(card(
            "🔊", "سکوت برداشته شد", [f"👤 کاربر: {mention(target)}", f"👮 توسط: {actor(update)}"],
            "دوباره خوش آمدید 🌷"))
    except Exception as e:
        await update.effective_message.reply_text(fail_card(e))


@require(1)
async def warn_cmd(update, context):
    target = await get_target(update)
    if not target or not await can_act_on(update, context, target):
        return
    await do_warn(context, update.effective_chat, target, " ".join(context.args))


@require(1)
async def unwarn_cmd(update, context):
    target = await get_target(update)
    if not target:
        return
    reset_warn(update.effective_chat.id, target.id)
    await update.effective_message.reply_text(card(
        "🧹", "اخطارها پاک شد", [f"👤 کاربر: {mention(target)}", f"👮 توسط: {actor(update)}"], "صفحه‌ی جدید، شروع تازه ✨"))


@require(2)
async def exempt_cmd(update, context):
    target = await get_target(update)
    if not target:
        return
    add_role(update.effective_chat.id, target.id, "exempt")
    await update.effective_message.reply_text(card(
        "✅", "کاربر معاف شد", [f"👤 کاربر: {mention(target)}", "🛡️ فیلترها روی او اعمال نمی‌شود", f"👮 توسط: {actor(update)}"]))


@require(2)
async def unexempt_cmd(update, context):
    target = await get_target(update)
    if not target:
        return
    cid = update.effective_chat.id
    if get_role(cid, target.id) == "exempt":
        remove_role(cid, target.id)
    await update.effective_message.reply_text(card(
        "❎", "معافیت برداشته شد", [f"👤 کاربر: {mention(target)}", f"👮 توسط: {actor(update)}"]))


@require(3)
async def special_cmd(update, context):
    target = await get_target(update)
    if not target:
        return
    add_role(update.effective_chat.id, target.id, "special_admin")
    await update.effective_message.reply_text(card(
        "⭐", "مدیر ویژه جدید", [f"👤 کاربر: {mention(target)}", "🔑 دسترسی: بن، معاف، مدیر کردن و ..."], "مبارک باشد 🎉"))


@require(2)
async def admin_cmd(update, context):
    target = await get_target(update)
    if not target:
        return
    cid = update.effective_chat.id
    if get_role(cid, target.id) == "special_admin":
        return await update.effective_message.reply_text(card("ℹ️", "نیازی نیست", ["این کاربر مدیر ویژه است."]))
    add_role(cid, target.id, "admin")
    await update.effective_message.reply_text(card(
        "🛡️", "مدیر جدید", [f"👤 کاربر: {mention(target)}", "🔑 دسترسی: سکوت، اخطار، اسپم، قفل‌ها"], "موفق باشید 🌟"))


@require(3)
async def demote_cmd(update, context):
    target = await get_target(update)
    if not target:
        return
    cid = update.effective_chat.id
    remove_role(cid, target.id)
    try:  # گرفتن دسترسی‌های ادمین تلگرام (اگر ربات اجازه داشته باشد)
        await context.bot.promote_chat_member(
            cid, target.id, can_change_info=False, can_delete_messages=False, can_restrict_members=False,
            can_invite_users=False, can_pin_messages=False, can_promote_members=False)
    except Exception:
        pass
    _TG_CACHE.pop((cid, target.id), None)
    await update.effective_message.reply_text(card("⬇️", "مدیر عزل شد", [f"👤 کاربر: {mention(target)}", f"👮 توسط: {actor(update)}"]))


@require(1)
async def specials_cmd(update, context):
    cid = update.effective_chat.id
    specials = [f"• <code>{u}</code>" for u in list_roles(cid, "special_admin")] or ["—"]
    admins = [f"• <code>{u}</code>" for u in list_roles(cid, "admin")] or ["—"]
    await update.effective_message.reply_text(card(
        "👑", "مدیران ربات در این گروه",
        [f"👑 مالک: <code>{OWNER_ID}</code>", "", "⭐ <b>مدیران ویژه</b>", *specials, "", "🛡️ <b>مدیران</b>", *admins]))


@require(1)
async def whisper_cmd(update, context):
    """ریپلای روی کاربر + /whisper متن: پیام فقط برای همان کاربر نمایش داده می‌شود."""
    target = await get_target(update)
    if not target:
        return
    if not context.args:
        return await update.effective_message.reply_text(usage_card("ریپلای + /whisper متن پیام", title="پیام محرمانه"))
    text = E(" ".join(context.args))
    await send_ephemeral(context, update.effective_chat.id, target,
                         card("🤫", f"پیام محرمانه برای {mention(target)}", [text], "این پیام فقط برای شماست 💌"), ttl=60)
    try:
        await update.effective_message.delete()
    except Exception:
        pass


async def rules_cmd(update, context):
    rules = get_global("rules_text")
    if not rules:
        return await update.effective_message.reply_text(card("📜", "قوانین گروه", ["هنوز قوانینی تنظیم نشده است."]))
    await update.effective_message.reply_text(card("📜", "قوانین گروه", [E(rules)], "رعایت قوانین = گروهی شاد و آرام 🌸"))


async def _set_lock(update, context, value):
    if not context.args or context.args[0] not in LOCKS:
        return await update.effective_message.reply_text(usage_card(
            "/lock نوع  •  /unlock نوع", "نوع‌ها: " + " ، ".join(f"<code>{k}</code>" for k in LOCKS),
            "مثال: «قفل link» یا «بازکردن photo»", title="قفل‌ها"))
    set_lock(update.effective_chat.id, context.args[0], value)
    label = LOCKS[context.args[0]][1]
    await update.effective_message.reply_text(card(
        "🔒" if value else "🔓", "قفل فعال شد" if value else "قفل باز شد",
        [f"📌 {label}", f"👮 توسط: {actor(update)}"],
        "ارسال این نوع پیام برای اعضا ممنوع شد ⛔" if value else "ارسال این نوع پیام آزاد شد 🎈"))


@require(1)
async def lock_cmd(update, context):
    await _set_lock(update, context, 1)


@require(1)
async def unlock_cmd(update, context):
    await _set_lock(update, context, 0)


async def locks_cmd(update, context):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if chat.type == ChatType.PRIVATE:
        if user.id != OWNER_ID or not context.args or not re.fullmatch(r"-?\d+", context.args[0]):
            return await msg.reply_text(usage_card("/locks chat_id", "فقط مالک", title="وضعیت قفل‌ها"))
        return await msg.reply_text(locks_text(int(context.args[0])))
    if await user_level(context, chat.id, user.id) < 1:
        return await msg.reply_text(card("⛔", "دسترسی ندارید", ["این دستور مخصوص مدیران است."]))
    await msg.reply_text(locks_text(chat.id))


# ============================ راهنما و ساعت ============================
HELP = [
"""📖 <b>راهنمای مدیر @P@ — شروع و نقش‌ها</b>

<b>راه‌اندازی</b>
۱. ربات را به گروه اضافه و ادمین کنید (دسترسی‌ها: حذف پیام، محدود کردن کاربران، دعوت کاربران).
۲. برای کانال: ربات را ادمین کانال کنید و دسترسی ارسال پیام بدهید.
۳. گروه با افزودن ربات یا اولین پیام، خودکار ثبت می‌شود.

<b>نقش‌ها</b>
👑 مالک: همه‌چیز + پنل و تنظیمات سراسری
⭐ مدیر ویژه: بن، رفع بن، معاف، مدیر کردن
🛡️ مدیر: سکوت، اخطار، اسپم، قفل‌ها
✅ معاف: هیچ فیلتر و محدودیتی روی او اعمال نمی‌شود
ادمین‌های خود تلگرام خودکار «مدیر» و سازنده گروه «مدیر ویژه» حساب می‌شود. هیچ‌کس روی هم‌سطح یا بالاتر از خودش اقدام نمی‌کند.

<b>دستور فارسی</b>
مدیران می‌توانند دستورها را بدون اسلش و فارسی بنویسند؛ مثلاً ریپلای روی پیام کاربر و نوشتن «بن» یا «سکوت 30m».

<b>ساعت و تاریخ</b>
همه زمان‌ها شمسی و به وقت واقعی تهران است. دستور /time""",

"""📖 <b>راهنمای مدیر @P@ — دستورهای مدیریتی</b>
(روی پیام کاربر ریپلای کنید)

/ban یا «بن» — بن؛ مدت اختیاری: 30m ، 2h ، 1d (مثال: «بن 1d»). [مدیر ویژه]
/unban یا «آنبن» + user_id — رفع بن [مدیر ویژه]
/mute یا «سکوت» — پیش‌فرض ۱ ساعت؛ مثال: «سکوت 30m» [مدیر]
/unmute یا «حذف_سکوت»
/warn یا «اخطار» + دلیل اختیاری — با رسیدن به سقف اخطار، کاربر بن می‌شود
/unwarn یا «حذف_اخطار» — پاک کردن اخطارها
/spam یا «اسپم» — حذف پیام + پیام‌های اخیر او، سکوت (پیش‌فرض ۱ ساعت) و یک اخطار
/whisper یا «نجوا» + متن — پیامی که فقط همان کاربر می‌بیند
/exempt یا «معاف» ، /unexempt یا «حذف_معاف» [مدیر ویژه]
/special یا «مدیر_ویژه» [مالک] ، /admin یا «مدیر» [مدیر ویژه]
/demote یا «عزل» [مالک]
/specials یا «لیست_ویژه» — لیست مدیران
/rules یا «قوانین» — قوانین گروه (همه)

«گپ بسته» / «گپ باز» (یا با ساعت: «گپ بسته 23:45») — بستن و باز کردن گپ [مدیر]
«آمار» — آمار شما یا (با ریپلای) هر کاربر ، «آمار گپ» — آمار گروه

پایان زمان بن و سکوت به تاریخ شمسی نمایش داده می‌شود.""",

"""📖 <b>راهنمای مدیر @P@ — قفل‌ها، ضدفحش، ضد اسپم</b>

<b>قفل‌ها</b>
/lock نوع ، /unlock نوع ، /locks
نوع‌ها: link photo video gif forward phone
مثال: «قفل link» یا «بازکردن photo»

<b>ضدفحش</b>
متن و کپشن بررسی می‌شود (حروف کشیده، فاصله‌دار و عربی/فارسی هم گرفته می‌شود). پیام حذف و اخطار ثبت می‌شود.
کلمات (فقط مالک): /addword ، /delword ، /listwords

<b>ضد اسپم</b> (روی همه نوع پیام)
• فلود: پیش‌فرض ۵ پیام در ۸ ثانیه ← حذف پیام‌ها + ۱۰ دقیقه سکوت
• تکرار: یک پیام/فایل یکسان ۳ بار در ۱ دقیقه
/antispam on|off ، /spamlimit 5 8 (تعداد و ثانیه)

<b>کپچا و خوش‌آمد</b>
کپچا: کاربر تازه‌وارد تا زدن دکمه ساکت است و ۲ دقیقه فرصت دارد، وگرنه اخراج می‌شود.
متن خوش‌آمد با {name} و {chat}؛ فقط برای خود کاربر نمایش داده می‌شود.

ادمین‌ها، مدیران ربات و معاف‌ها مشمول فیلترها نیستند.""",

"""📖 <b>راهنمای مدیر @P@ — آمار و باز/بسته کردن گپ</b>

<b>🔒 بستن و باز کردن گپ</b> (مدیران)
«گپ بسته» — همین الان بسته می‌شود (فقط مدیران می‌نویسند)
«گپ باز» — همین الان باز می‌شود
«گپ بسته 23:45» — ساعت ۲۳:۴۵ (تهران) بسته می‌شود
«گپ باز 07:30» — ساعت ۰۷:۳۰ باز می‌شود
زمان می‌تواند <code>1405/09/05 23:30</code> یا <code>+30</code> (۳۰ دقیقه دیگر) هم باشد.
دستورها: /closechat ، /openchat ، /schedules (زمان‌بندی‌های این گروه) ، /unschedule شماره
زمان‌بندی‌ها ذخیره می‌شوند و با ری‌استارت از بین نمی‌روند.

<b>📊 آمار کاربر</b> — «آمار» یا /stats
خودتان یا (با ریپلای) هر کاربر: مقام، معاف بودن، اخطارها، وضعیت سکوت/بن، تعداد پیام و رتبه، نوع پیام‌ها، تخلف‌ها، اولین و آخرین فعالیت.

<b>🏆 آمار گپ</b> — «آمار گپ» یا /chatstats
فعال‌ترین‌ها + ۶ بخش دیگر با دکمه: 📈 مجموع و روند، ⏰ ساعت‌های اوج، 🎨 ترکیب نوع پیام‌ها، 👥 اعضای فعال، 🔥 امروز، 🛡️ عملکرد ربات.
آمار از لحظه‌ی فعال شدن این نسخه ثبت می‌شود.""",

"""📖 <b>راهنمای مدیر @P@ — پنل مالک (۱)</b>
پنل: /panel در پی‌وی ربات

📢 ارسال سراسری — پیام بعدی شما (هر نوع) به همه گروه‌ها و کانال‌های ثبت‌شده کپی می‌شود
📊 آمار — گروه‌ها، مجموع اخطارها، نقش‌ها
✏️ تغییر نام ربات
⚙️ خلاصه تنظیمات
💾 پشتیبان‌گیری — فایل دیتابیس به پی‌وی شما می‌آید (نام فایل شامل تاریخ شمسی). مرتب بگیرید.
📤 بازیابی — فایل .db را بفرستید؛ دیتابیس فعلی جایگزین می‌شود
📋 لیست گروه‌ها
🗑️ حذف گروه — فقط از لیست ربات حذف می‌شود (/remove_chat)
🚫 کلمات فیلتر — /addword /delword /listwords
🛡️ ضدفحش روشن/خاموش (سراسری)
🛡️ مدیران ، ✅ معاف‌ها — لیست نقش‌ها
🔌 روشن/خاموش ربات — فوری
📜 لاگ‌ها — ۱۵ خط آخر، با زمان شمسی تهران""",

"""📖 <b>راهنمای مدیر @P@ — پنل مالک (۲)</b>

⚠️ محدودیت اخطار — تعداد اخطار تا بن (پیش‌فرض ۳)
🔒 قفل رسانه سراسری — روی همه گروه‌ها
✉️ پیام به یک گروه — انتخاب از لیست یا وارد کردن chat_id / @username (گروه یا کانال). دستور: /send @channel متن
🧹 پاکسازی اخطار گروه / کاربر ، 📊 اخطارهای کاربر — /clear_warns /clear_user_warns /user_warns
🔍 وضعیت قفل‌ها — /locks chat_id
💬 متن خوش‌آمد ، 🔛 خوش‌آمد روشن/خاموش
📜 قوانین — متن دستور /rules
🤖 کپچا روشن/خاموش
🌙 سکوت شبانه — بسته شدن گروه‌ها در بازه‌ی ساعتی
🏷️ نام و توضیحات — فرمت: نام|توضیحات
🚪 خروج از گروه — /leave chat_id
🔄 ری‌استارت — ربات دوباره بالا می‌آید
⏱️ زمان‌بندی و ساعت — صفحه‌ی بعد

برای لغو هر مرحله: /cancel""",

"""📖 <b>راهنمای مدیر @P@ — زمان‌بندی و تاریخ شمسی</b>

همه‌ی زمان‌ها شمسی و به وقت تهران است و در حد دقیقه. ساعت ربات با سرور تلگرام همگام می‌شود، پس به ساعت اشتباه گوشی/سرور وابسته نیست (/time وضعیت را نشان می‌دهد).

<b>فرمت‌های قابل قبول</b>
• <code>1405/09/05 23:30</code> تاریخ شمسی + ساعت
• <code>+90</code> نود دقیقه دیگر (یا <code>+2h</code> ، <code>+1d</code>)
• <code>23:30</code> همین ساعت (امروز، یا فردا اگر گذشته باشد)
اعداد فارسی هم قبول است.

<b>پنل ← ⏱️ زمان‌بندی و ساعت</b>
🔴 خاموش شدن در زمان — ربات در لحظه‌ی تعیین‌شده خاموش می‌شود
🟢 روشن شدن در زمان
🕒 بازه روزانه — مثلاً <code>07:00-23:30</code> یعنی هر روز فقط در این بازه روشن است؛ برای غیرفعال کردن: <code>off</code>
⏰ ارسال زمان‌بندی‌شده — ارسال سراسری در زمان دلخواه (یا با ریپلای: <code>/schedule_broadcast 1405/09/05 23:30</code>)
🗑 لغو زمان‌بندی — با شماره یا all
🌙 سکوت شبانه — مثل <code>23:30-07:00</code>

<b>«ربات خاموش» یعنی؟</b>
نظارت (فحش، اسپم، قفل‌ها، کپچا، خوش‌آمد) و دستورهای گروه غیرفعال است؛ فقط مالک و پنل کار می‌کند.

زمان‌بندی‌ها در دیتابیس ذخیره می‌شوند و با ری‌استارت از بین نمی‌روند. ارسال سراسریِ بیش از ۱ ساعت عقب‌افتاده (ربات خاموش بوده) اجرا نمی‌شود و به شما خبر می‌دهد. ایران تغییر ساعت تابستانی ندارد (UTC+3:30).""",

"""📖 <b>راهنمای مدیر @P@ — نکات و رفع مشکل</b>

• ربات پیام‌های گروه را نمی‌بیند ← در BotFather: /setprivacy ← Disable، بعد ربات را یک‌بار از گروه حذف و دوباره اضافه کنید.
• پیام حذف نمی‌شود / بن و سکوت کار نمی‌کند ← ربات ادمین نیست یا دسترسی حذف پیام و محدود کردن کاربران ندارد.
• «روی ادمین‌ها اثر ندارد» ← عمدی است: ادمین‌ها و معاف‌ها از فیلترها مستثنا هستند.
• کانال در لیست نیست ← ربات را دوباره ادمین کانال کنید، یا از «وارد کردن دستی» و /send استفاده کنید.
• ربات جواب نمی‌دهد ← شاید خاموش باشد (🔌 پنل)؛ مالک همیشه کار می‌کند.
• تلگرام فیلتر است ← متغیر PROXY_URL را تنظیم کنید یا روی سرور خارج اجرا کنید.
• هر چند وقت یک‌بار 💾 پشتیبان بگیرید.
• توکن ربات را جایی منتشر نکنید.

پشتیبانی از پنل: /panel — لغو هر مرحله: /cancel""",
]


HELP = [h.replace("@P@", fa(f"{i + 1}/{len(HELP)}")) for i, h in enumerate(HELP)]


def help_kb(page, owner_private):
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️ قبلی", callback_data=f"help:{page - 1}"))
    if page < len(HELP) - 1:
        nav.append(InlineKeyboardButton("بعدی ▶️", callback_data=f"help:{page + 1}"))
    rows = [nav] if nav else []
    if owner_private:
        rows.append([InlineKeyboardButton("🎛️ پنل", callback_data="p:home")])
    return InlineKeyboardMarkup(rows)


async def help_cmd(update, context):
    chat, user = update.effective_chat, update.effective_user
    await update.effective_message.reply_text(
        HELP[0], reply_markup=help_kb(0, chat.type == ChatType.PRIVATE and user.id == OWNER_ID), parse_mode="HTML")


async def help_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    page = max(0, min(int(query.data.split(":")[1]), len(HELP) - 1))
    await query.answer()
    owner_private = query.from_user.id == OWNER_ID and query.message.chat.type == ChatType.PRIVATE
    try:
        await query.edit_message_text(HELP[page], reply_markup=help_kb(page, owner_private), parse_mode="HTML")
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def time_cmd(update, context):
    now = now_tehran()
    text = (f"🕒 <b>ساعت تهران</b>\n{fmt_jalali_long(now, secs=True)}\n📅 {now:%Y-%m-%d}\n")
    if CLOCK_SAMPLES:
        text += f"🔄 همگام با سرور تلگرام (اختلاف با ساعت سیستم: {abs(CLOCK_OFFSET):.1f} ثانیه)"
        if abs(CLOCK_OFFSET) > 60:
            text += "\n⚠️ ساعت سیستمی که ربات روی آن اجراست دقیق نیست، ولی ربات خودش را اصلاح می‌کند."
    else:
        text += "ℹ️ هنوز با سرور تلگرام همگام نشده؛ فعلاً از ساعت سیستم استفاده می‌شود."
    await update.effective_message.reply_text(text, parse_mode="HTML")


# ============================ سیستم ضد اسپم ============================
SPAM_HIST = {}  # (chat_id, user_id) -> deque[(زمان, message_id, امضای پیام)]


def spam_cfg(cid):
    on = get_global(f"spam_on_{cid}", "1") == "1"
    try:
        n, secs = (int(x) for x in get_global(f"spam_limit_{cid}", "5/8").split("/"))
    except ValueError:
        n, secs = 5, 8
    return on, n, secs


def msg_signature(msg):
    """امضای پیام برای تشخیص تکرار؛ برای همه نوع پیام (متن، عکس، استیکر، ...)."""
    text = msg.text or msg.caption
    if text:
        return "t:" + normalize_base(text).strip()
    if msg.photo:
        return "photo:" + msg.photo[-1].file_unique_id
    for attr in ("sticker", "video", "animation", "voice", "audio", "document", "video_note"):
        obj = getattr(msg, attr, None)
        if obj:
            return f"{attr}:{obj.file_unique_id}"
    return "other"


async def _delete_ids(context, cid, ids):
    for mid in ids:
        try:
            await context.bot.delete_message(cid, mid)
        except Exception:
            pass


async def check_spam(context, chat, user, msg):
    """True یعنی اسپم تشخیص داده و برخورد شد: فلود سریع یا تکرار یک پیام (برای همه نوع پیام)."""
    cid = chat.id
    on, n, secs = spam_cfg(cid)
    if not on:
        return False
    now = time.time()
    h = SPAM_HIST.setdefault((cid, user.id), deque(maxlen=30))
    sig = msg_signature(msg)
    h.append((now, msg.message_id, sig))
    recent = [x for x in h if now - x[0] <= secs]
    same = [x for x in h if now - x[0] <= 60 and x[2] == sig]
    if len(recent) >= n:
        victims, why = recent, "ارسال پیام پشت‌سرهم"
    elif len(same) >= 3:
        victims, why = same, "تکرار یک پیام"
    else:
        return False
    h.clear()
    bump(cid, "spam")
    bump(cid, "deleted", len({x[1] for x in victims}))
    bump_user(cid, user.id, "viol")
    await _delete_ids(context, cid, {x[1] for x in victims})
    try:
        await context.bot.restrict_chat_member(
            cid, user.id, ChatPermissions(can_send_messages=False),
            until_date=now_utc() + timedelta(minutes=10))
        extra = f"تا {fmt_jalali(now_utc() + timedelta(minutes=10))} سکوت شد."
        bump(cid, "mutes")
    except Exception:
        extra = "(ربات دسترسی محدود کردن کاربران را ندارد)"
    await notice(context, cid, card(
        "🚨", "اسپم شناسایی شد",
        [f"👤 کاربر: {mention(user)}", f"📌 دلیل: {why}", f"🔇 {extra}"], "لطفاً آرام‌تر پیام بدهید 🙏"))
    return True


@require(1)
async def antispam_cmd(update, context):
    cid = update.effective_chat.id
    if context.args:
        v = context.args[0].lower()
        if v in ("on", "روشن"):
            set_global(f"spam_on_{cid}", "1")
        elif v in ("off", "خاموش"):
            set_global(f"spam_on_{cid}", "0")
        else:
            return await update.effective_message.reply_text(usage_card("/antispam on  یا  /antispam off", title="ضد اسپم"))
    on, n, secs = spam_cfg(cid)
    await update.effective_message.reply_text(card(
        "🚨", "ضد اسپم", [f"📡 وضعیت: {'🟢 روشن' if on else '🔴 خاموش'}",
                          f"⚡ حد مجاز: <b>{fa(n)}</b> پیام در <b>{fa(secs)}</b> ثانیه"],
        "تغییر حد: /spamlimit 5 8 🎚️"))


@require(1)
async def spamlimit_cmd(update, context):
    a = context.args
    if len(a) != 2 or not all(x.isdigit() for x in a) or not (2 <= int(a[0]) <= 30) or not (2 <= int(a[1]) <= 60):
        return await update.effective_message.reply_text(usage_card(
            "/spamlimit تعداد ثانیه", "تعداد: ۲ تا ۳۰  •  ثانیه: ۲ تا ۶۰", "مثال: /spamlimit 5 8", title="حد اسپم"))
    set_global(f"spam_limit_{update.effective_chat.id}", f"{a[0]}/{a[1]}")
    await update.effective_message.reply_text(card(
        "✅", "حد اسپم ثبت شد", [f"⚡ {fa(a[0])} پیام در {fa(a[1])} ثانیه", f"👮 توسط: {actor(update)}"]))


@require(1)
async def spam_cmd(update, context):
    """ریپلای روی هر پیام (متن، عکس، استیکر، ...) + /spam یا «اسپم»: پیام و پیام‌های اخیر کاربر پاک و کاربر سکوت می‌شود."""
    target = await get_target(update)
    if not target or not await can_act_on(update, context, target):
        return
    cid = update.effective_chat.id
    dur = parse_duration(context.args[0]) if context.args else timedelta(hours=1)
    if dur is None:
        return await update.effective_message.reply_text(usage_card("مدت نامعتبر ⏳", "مثال: 30m ، 2h ، 1d", title="اسپم"))
    ids = {update.effective_message.reply_to_message.message_id, update.effective_message.message_id}
    ids |= {x[1] for x in SPAM_HIST.pop((cid, target.id), [])}
    await _delete_ids(context, cid, ids)
    bump(cid, "spam")
    bump(cid, "deleted", len(ids))
    bump_user(cid, target.id, "viol")
    try:
        await context.bot.restrict_chat_member(cid, target.id, ChatPermissions(can_send_messages=False),
                                               until_date=now_utc() + dur)
        bump(cid, "mutes")
    except Exception as e:
        await notice(context, cid, fail_card(e))
    await do_warn(context, update.effective_chat, target, "اسپم")


# ============================ نظارت بر پیام‌ها ============================
URL_RE = re.compile(r"(https?://|www\.|t\.me/|telegram\.me/|@[A-Za-z0-9_]{5,}\b)", re.I)
PHONE_RE = re.compile(r"(?<!\d)(\+?98|0)?9\d{9}(?!\d)")


async def message_watcher(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not user or chat.type == ChatType.PRIVATE or user.id in ANON_IDS:
        return
    add_chat(chat.id, chat.title or "بدون نام")
    if not update.edited_message:
        record_message(msg, chat, user)
    if get_global("maintenance_mode", "0") == "1":
        return
    cid = chat.id
    if is_exempt(cid, user.id) or await user_level(context, cid, user.id) >= 1:
        return

    async def punish(text=None, warn_reason=None, key="locks"):
        try:
            await msg.delete()
        except Exception:
            pass
        bump(cid, "deleted")
        bump(cid, key)
        bump_user(cid, user.id, "viol")
        if warn_reason:
            await do_warn(context, chat, user, warn_reason)
        elif text:
            await send_ephemeral(context, cid, user, card(
                "🔒", "پیام شما حذف شد", [f"👤 {mention(user)}", text], "این نوع پیام در گروه ممنوع است ⛔"))

    if await check_spam(context, chat, user, msg):
        return

    body = msg.text or msg.caption or ""
    if get_global("global_antispam", "1") == "1" and contains_profanity(body):
        return await punish(warn_reason="استفاده از الفاظ نامناسب", key="profanity")

    if msg.photo and get_lock(cid, "photo"):
        return await punish("🖼️ ارسال عکس ممنوع است.")
    if msg.video and get_lock(cid, "video"):
        return await punish("🎬 ارسال ویدیو ممنوع است.")
    if msg.animation and get_lock(cid, "gif"):
        return await punish("🎞️ ارسال گیف ممنوع است.")
    if msg.forward_origin and get_lock(cid, "forward"):
        return await punish("↩️ فوروارد ممنوع است.")

    ents = list(msg.entities or []) + list(msg.caption_entities or [])
    if get_lock(cid, "link"):
        if any(e.type in ("url", "text_link", "mention") for e in ents) or URL_RE.search(body):
            return await punish("🔗 ارسال لینک ممنوع است.")
    if get_lock(cid, "phone"):
        if any(e.type == "phone_number" for e in ents) or PHONE_RE.search(normalize_base(body)):
            return await punish("📞 ارسال شماره ممنوع است.")


# ============================ ثبت گروه / خوش‌آمد / کپچا ============================
async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cm, chat = update.my_chat_member, update.effective_chat
    if not cm or chat.type == ChatType.PRIVATE:
        return
    if cm.new_chat_member.status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        icon = "📢 " if chat.type == ChatType.CHANNEL else ""
        add_chat(chat.id, icon + (chat.title or "بدون نام"))
        log.info("گروه ثبت شد: %s (%s)", chat.title, chat.id)
    elif cm.new_chat_member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        remove_chat(chat.id)


PENDING_CAPTCHA = set()


async def captcha_timeout(context: ContextTypes.DEFAULT_TYPE):
    cid, uid, mid = context.job.data
    if (cid, uid) not in PENDING_CAPTCHA:
        return
    PENDING_CAPTCHA.discard((cid, uid))
    try:
        await context.bot.ban_chat_member(cid, uid)
        await context.bot.unban_chat_member(cid, uid)  # فقط اخراج
        await context.bot.delete_message(cid, mid)
    except Exception as e:
        log.warning("captcha kick failed: %s", e)


async def on_new_members(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, chat = update.effective_message, update.effective_chat
    add_chat(chat.id, chat.title or "بدون نام")
    if get_global("maintenance_mode", "0") == "1" or not msg.new_chat_members:
        return
    for m in msg.new_chat_members:
        if m.is_bot or is_exempt(chat.id, m.id):
            continue
        if get_global("captcha_enabled", "0") == "1":
            try:
                await context.bot.restrict_chat_member(chat.id, m.id, ChatPermissions(can_send_messages=False))
            except Exception as e:
                log.warning("captcha restrict failed: %s", e)
                continue
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("🤖 من ربات نیستم", callback_data=f"cap:{m.id}")]])
            sent = await context.bot.send_message(
                chat.id, card("🤖", "تأیید هویت",
                              [f"👤 {mention(m)}", "برای فعال شدن ارسال پیام، دکمه‌ی زیر را بزن 👇", "⏳ ۲ دقیقه فرصت داری"],
                              "کاربرانی که تأیید نکنند اخراج می‌شوند 🚪"),
                reply_markup=kb, parse_mode="HTML")
            PENDING_CAPTCHA.add((chat.id, m.id))
            if context.job_queue:
                context.job_queue.run_once(captcha_timeout, 120, data=(chat.id, m.id, sent.message_id))
        elif get_global("welcome_enabled", "1") == "1":
            await send_welcome(context, chat, m)


async def send_welcome(context, chat, user):
    custom = get_global("welcome_text")
    title = E(chat.title or "")
    if custom:
        text = "🎉 " + E(custom).replace("{name}", mention(user)).replace("{chat}", title)
    else:
        text = card("🎉", f"خوش آمدی {mention(user)}!",
                    [f"🏠 به گروه <b>{title}</b> خوش اومدی ✨", "📜 قبل از شروع، قوانین را با /rules بخوان",
                     "💬 راحت باش، سؤال بپرس و لذت ببر 💕"], "امیدواریم لحظات خوبی داشته باشی 🌸")
    await send_ephemeral(context, chat.id, user, text, ttl=60)


async def captcha_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = int(query.data.split(":")[1])
    if query.from_user.id != uid:
        return await query.answer("این دکمه برای شما نیست.", show_alert=True)
    cid = query.message.chat.id
    PENDING_CAPTCHA.discard((cid, uid))
    try:
        await context.bot.restrict_chat_member(cid, uid, await full_default_perms(context, cid))
    except Exception as e:
        log.warning("captcha restore failed: %s", e)
    await query.answer("✅ تایید شد.")
    try:
        await query.message.delete()
    except Exception:
        pass
    if get_global("welcome_enabled", "1") == "1":
        await send_welcome(context, query.message.chat, query.from_user)


# ============================ سکوت شبانه ============================
KIND_LABELS = {"off": "🔴 خاموش شدن ربات", "on": "🟢 روشن شدن ربات", "broadcast": "📢 ارسال سراسری",
               "chat_close": "🔒 بستن گپ", "chat_open": "🔓 باز کردن گپ"}


def bot_is_on():
    return get_global("maintenance_mode", "0") != "1"


def set_bot_active(active):
    set_global("maintenance_mode", "0" if active else "1")


def add_schedule(run_at_ts, kind, payload=""):
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute("INSERT INTO schedules (run_at, kind, payload) VALUES (?,?,?)",
                           (int(run_at_ts), kind, payload))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


async def notify_owner(context, text):
    try:
        await context.bot.send_message(OWNER_ID, text, parse_mode="HTML")
    except Exception as e:
        log.warning("notify_owner failed: %s", e)


async def schedules_tick(context):
    now = now_ts()
    rows = q("SELECT id, run_at, kind, payload FROM schedules WHERE done=0 AND run_at<=? ORDER BY run_at",
             (int(now),), many=True)
    for r in rows:
        # زمان‌بندی باز/بسته‌کردن گپ را پس از اجرای عملیات نهایی می‌کنیم؛
        # تا اگر پردازش درست پیش از اجرا متوقف شد، دستور بی‌دلیل از دست نرود.
        is_chat_toggle = r["kind"] in ("chat_close", "chat_open")
        if not is_chat_toggle:
            q("UPDATE schedules SET done=1 WHERE id=?", (r["id"],))
        label = KIND_LABELS.get(r["kind"], r["kind"])
        if r["kind"] in ("off", "on"):
            set_bot_active(r["kind"] == "on")
            await notify_owner(context, f"⏰ زمان‌بندی #{r['id']} اجرا شد: {label}\n🕒 {fmt_ts(now)}")
        elif r["kind"] in ("chat_close", "chat_open"):
            try:
                d = json.loads(r["payload"])
                if now - r["run_at"] > 3600:
                    await notify_owner(context, f"⚠️ زمان‌بندی #{r['id']} ({label}) برای {fmt_ts(r['run_at'])} از دست رفت.")
                    continue
                closed = r["kind"] == "chat_close"
                await set_chat_closed(context, d["chat_id"], closed)
                await context.bot.send_message(d["chat_id"], card(
                    "🔒" if closed else "🔓", "گپ طبق برنامه بسته شد" if closed else "گپ طبق برنامه باز شد",
                    [f"🕒 {fmt_ts(now)} (به وقت تهران)",
                     "💬 فقط مدیران می‌توانند پیام بدهند" if closed else "💬 همه می‌توانند پیام بدهند 🎉"],
                    "زمان‌بندی خودکار ⏰"))
            except Exception as e:
                await notify_owner(context, f"❌ زمان‌بندی #{r['id']} ({label}) ناموفق بود: {E(e)}")
            finally:
                q("UPDATE schedules SET done=1 WHERE id=?", (r["id"],))
        elif r["kind"] == "broadcast":
            if now - r["run_at"] > 3600:
                await notify_owner(context, f"⚠️ زمان‌بندی #{r['id']} ({label}) برای {fmt_ts(r['run_at'])} "
                                            "به‌خاطر خاموش بودن ربات از دست رفت و اجرا نشد.")
                continue
            try:
                d = json.loads(r["payload"])
                ok, fail = await broadcast(context, d["from_chat"], d["message_id"])
                await notify_owner(context, f"⏰ زمان‌بندی #{r['id']} اجرا شد: {label}\nموفق: {ok} | ناموفق: {fail}\n"
                                            f"🕒 {fmt_ts(now)}")
            except Exception as e:
                await notify_owner(context, f"❌ زمان‌بندی #{r['id']} با خطا روبه‌رو شد: {html.escape(str(e))}")


async def auto_hours_tick(context):
    rng = get_global("auto_hours", "off")
    if rng == "off":
        return
    desired = in_range(rng, now_tehran())
    flag = "1" if desired else "0"
    if get_global("auto_state") == flag:
        return
    set_global("auto_state", flag)
    set_bot_active(desired)
    await notify_owner(context, f"{'🟢 ربات طبق بازه‌ی روزانه روشن شد' if desired else '🔴 ربات طبق بازه‌ی روزانه خاموش شد'}"
                                f"\n🕒 {fmt_jalali(now_tehran())}")


async def night_tick(context):
    rng = get_global("night_range", "off")
    try:
        active = rng != "off" and in_range(rng, now_tehran())
    except (ValueError, IndexError):
        return
    was = get_global("night_active", "0") == "1"
    if active == was:
        return
    set_global("night_active", "1" if active else "0")
    for cid, _ in get_all_chats():
        try:
            if active:
                cur = (await context.bot.get_chat(cid)).permissions
                if cur:
                    set_global(f"night_perms_{cid}", json.dumps(cur.to_dict()))
                await context.bot.set_chat_permissions(cid, ChatPermissions(can_send_messages=False))
            else:
                saved = get_global(f"night_perms_{cid}")
                perms = ChatPermissions.de_json(json.loads(saved), context.bot) if saved \
                    else await full_default_perms(context, cid)
                await context.bot.set_chat_permissions(cid, perms)
        except Exception as e:
            log.warning("night mode failed for %s: %s", cid, e)
        await asyncio.sleep(0.1)


async def timer_job(context: ContextTypes.DEFAULT_TYPE):
    """هر ۲۰ ثانیه: زمان‌بندی‌ها، بازه‌ی روزانه‌ی ربات و سکوت شبانه (به وقت واقعی تهران)."""
    for fn in (stats_tick, schedules_tick, auto_hours_tick, night_tick):
        try:
            await fn(context)
        except Exception:
            log.exception("timer_job: %s failed", fn.__name__)


def timers_text():
    rows = q("SELECT id, run_at, kind FROM schedules WHERE done=0 ORDER BY run_at LIMIT 15", many=True)
    lst = "\n".join(f"#{r['id']} — {KIND_LABELS.get(r['kind'], r['kind'])} — {fmt_ts(r['run_at'])}"
                    for r in rows) or "—"
    return ("⏱️ <b>زمان‌بندی و ساعت</b>\n\n"
            f"🕒 اکنون (تهران): {fmt_jalali_long(now_tehran(), secs=True)}\n"
            f"🔌 وضعیت ربات: {'🟢 روشن' if bot_is_on() else '🔴 خاموش'}\n"
            f"🕒 بازه‌ی روزانه‌ی روشن بودن: {html.escape(get_global('auto_hours', 'off'))}\n"
            f"🌙 سکوت شبانه: {html.escape(get_global('night_range', 'off'))}\n\n"
            f"<b>زمان‌بندی‌های در انتظار:</b>\n{lst}\n\n"
            "فرمت زمان: <code>1405/09/05 23:30</code> یا <code>+90</code> (دقیقه دیگر) یا <code>23:30</code>")


def timers_kb():
    b = InlineKeyboardButton
    return back_kb([
        [b("🔴 خاموش شدن در زمان", callback_data="p:sch_off"), b("🟢 روشن شدن در زمان", callback_data="p:sch_on")],
        [b("🕒 بازه روزانه روشن بودن", callback_data="p:autohours"), b("🌙 سکوت شبانه", callback_data="p:night")],
        [b("⏰ ارسال زمان‌بندی‌شده", callback_data="p:sched"), b("🗑 لغو زمان‌بندی", callback_data="p:schdel")],
        [b("🔌 روشن/خاموش فوری ربات", callback_data="p:maint")],
    ])


# ============================ ارسال سراسری ============================
async def broadcast(context, from_chat, message_id):
    ok = fail = 0
    for cid, _ in get_all_chats():
        for attempt in range(2):
            try:
                await context.bot.copy_message(cid, from_chat, message_id)
                ok += 1
                break
            except RetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except Exception as e:
                log.error("Broadcast failed to %s: %s", cid, e)
                fail += 1
                break
        await asyncio.sleep(0.05)
    return ok, fail


# ============================ دستورات مالک ============================
def owner_only(fn):
    @functools.wraps(fn)
    async def wrapper(update, context):
        if not update.effective_user or update.effective_user.id != OWNER_ID:
            return await update.effective_message.reply_text("⛔ فقط مالک.")
        return await fn(update, context)
    return wrapper


STATE = {}  # owner_id -> (mode, data)


async def start_cmd(update, context):
    if update.effective_chat.type != ChatType.PRIVATE:
        return
    owner = update.effective_user.id == OWNER_ID
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🎫 دریافت پشتیبانی", callback_data="s:new")],
                               [InlineKeyboardButton("📖 راهنما", callback_data="help:0")]])
    await update.effective_message.reply_text(card(
        "🛡️", E(get_global("bot_name", "ربات نگهبان")),
        ["👋 سلام! من نگهبان گروه شما هستم.", "➕ مرا به گروه اضافه و ادمین کنید تا محافظت را شروع کنم.",
         "📖 راهنما: /help  •  🕒 ساعت: /time", "🎫 برای ارتباط با پشتیبانی از دکمه زیر یا /support استفاده کنید."] + (["🎛️ پنل مدیریت: /panel"] if owner else []),
        "با عشق ساخته شد 💕"), reply_markup=kb)


@owner_only
async def cancel_cmd(update, context):
    STATE.pop(OWNER_ID, None)
    await update.effective_message.reply_text(card("↩️", "لغو شد", ["عملیات قبلی لغو شد."], "برای ادامه از /panel استفاده کنید 🎛️"))


@owner_only
async def addword_cmd(update, context):
    if not context.args:
        return await update.effective_message.reply_text("استفاده: /addword کلمه")
    for w in context.args:
        q("INSERT OR IGNORE INTO words VALUES (?)", (w.strip(),))
    invalidate_words()
    await update.effective_message.reply_text(f"✅ {len(context.args)} کلمه اضافه شد.")


@owner_only
async def delword_cmd(update, context):
    if not context.args:
        return await update.effective_message.reply_text("استفاده: /delword کلمه")
    for w in context.args:
        q("DELETE FROM words WHERE word=?", (w.strip(),))
    invalidate_words()
    await update.effective_message.reply_text("✅ حذف شد.")


@owner_only
async def listwords_cmd(update, context):
    rows = [r["word"] for r in q("SELECT word FROM words ORDER BY word", many=True)]
    await update.effective_message.reply_text(
        f"🚫 کلمات فیلتر ({len(rows)}):\n" + (" ، ".join(rows)[:3800] or "—"))


@owner_only
async def schedule_cmd(update, context):
    m = update.effective_message
    r = m.reply_to_message
    if not r or not context.args:
        return await m.reply_text("روی پیام موردنظر ریپلای کنید و بنویسید:\n/schedule_broadcast 1405/09/05 23:30\n"
                                  "یا /schedule_broadcast +30 (۳۰ دقیقه دیگر)")
    try:
        dt = parse_when(" ".join(context.args))
    except ValueError as e:
        return await m.reply_text(f"❌ {E(e)}")
    sid = add_schedule(dt.timestamp(), "broadcast", json.dumps({"from_chat": r.chat_id, "message_id": r.message_id}))
    await m.reply_text(f"⏰ ارسال #{sid} برای {fmt_jalali(dt)} (به وقت تهران) زمان‌بندی شد.")


async def resolve_chat(context, text):
    """chat_id عددی یا @username (گروه/کانال) را به چت تلگرام تبدیل می‌کند؛ ربات باید عضو باشد."""
    text = text.strip()
    ref = int(text) if re.fullmatch(r"-?\d+", text) else (text if text.startswith("@") else "@" + text)
    return await context.bot.get_chat(ref)


@owner_only
async def send_cmd(update, context):
    m = update.effective_message
    usage = ("استفاده:\n/send «chat_id یا @username» متن پیام\n"
             "یا روی یک پیام (متن/عکس/ویدیو/...) ریپلای کنید و بنویسید: /send «chat_id یا @username»")
    if not context.args:
        return await m.reply_text(usage)
    try:
        chat = await resolve_chat(context, context.args[0])
        r = m.reply_to_message
        if r:
            await context.bot.copy_message(chat.id, r.chat_id, r.message_id)
        else:
            parts = m.text.split(None, 2)
            if len(parts) < 3:
                return await m.reply_text(usage)
            await context.bot.send_message(chat.id, parts[2])
        await m.reply_text(f"✅ به «{E(chat.title or chat.username)}» ارسال شد.")
    except Exception as e:
        await m.reply_text(f"❌ خطا (ربات باید عضو و در کانال ادمین باشد): {E(e)}")


async def run_chat_action(mode, value, context):
    if mode == "rmchat":
        remove_chat(value)
        return "✅ از لیست گروه‌ها حذف شد."
    if mode == "clrgroup":
        reset_all_warns(value)
        return "✅ اخطارهای گروه پاک شد."
    if mode == "clruser":
        q("DELETE FROM warnings WHERE user_id=?", (value,))
        return "✅ اخطارهای کاربر در همه گروه‌ها پاک شد."
    if mode == "userwarns":
        rows = q("SELECT chat_id, count FROM warnings WHERE user_id=?", (value,), many=True)
        return "\n".join(f"گروه {r['chat_id']}: {r['count']} اخطار" for r in rows) or "این کاربر اخطاری ندارد."
    if mode == "locks":
        return locks_text(value)
    if mode == "leave":
        await context.bot.leave_chat(value)
        remove_chat(value)
        return "🚪 ربات از گروه خارج شد."
    return "؟"


def owner_chat_cmd(mode, usage):
    @owner_only
    async def cmd(update, context):
        if not context.args or not re.fullmatch(r"-?\d+", context.args[0]):
            return await update.effective_message.reply_text(f"استفاده: {usage}")
        try:
            res = await run_chat_action(mode, int(context.args[0]), context)
        except Exception as e:
            res = f"❌ خطا: {E(e)}"
        await update.effective_message.reply_text(res)
    return cmd


# ============================ سامانه پشتیبانی و تیکت ============================
SUPPORT_PENDING = {}  # (chat_id, user_id) -> (source_type, source_message_id, created_at)


def support_ticket(ticket_id):
    return q("SELECT * FROM support_tickets WHERE id=?", (ticket_id,), one=True)


def support_add_message(ticket_id, sender_id, sender_role, body):
    now = int(time.time())
    q("INSERT INTO support_messages (ticket_id,sender_id,sender_role,body,created_at) VALUES (?,?,?,?,?)",
      (ticket_id, sender_id, sender_role, body, now))
    q("UPDATE support_tickets SET updated_at=? WHERE id=?", (now, ticket_id))


def support_kb(ticket_id, status="open"):
    rows = [[InlineKeyboardButton("✍️ پاسخ به کاربر", callback_data=f"sup:reply:{ticket_id}")]]
    if status == "open":
        rows.append([InlineKeyboardButton("✅ بستن تیکت", callback_data=f"sup:close:{ticket_id}")])
    else:
        rows.append([InlineKeyboardButton("🔄 بازگشایی تیکت", callback_data=f"sup:reopen:{ticket_id}")])
    rows.append([InlineKeyboardButton("🔙 فهرست تیکت‌ها", callback_data="sup:list:open")])
    return InlineKeyboardMarkup(rows)


def support_ticket_text(ticket_id):
    t = support_ticket(ticket_id)
    if not t:
        return "❌ تیکت پیدا نشد."
    history = q("SELECT sender_role,body,created_at FROM support_messages WHERE ticket_id=? ORDER BY id DESC LIMIT 12",
                (ticket_id,), many=True)
    history = list(reversed(history))
    lines = [f"🎫 شماره تیکت: <b>#{ticket_id}</b>",
             f"📌 وضعیت: {'🟢 باز' if t['status']=='open' else '⚫ بسته'}",
             f"👤 کاربر: {E(t['full_name'] or 'بدون نام')}  •  <code>{t['user_id']}</code>",
             f"🔗 شناسه مبدأ: <code>{t['source_chat_id']}</code>",
             f"💬 محل درخواست: {E(t['source_chat_title'] or t['source_type'])}", "", "🗨️ <b>آخرین پیام‌ها</b>"]
    for m in history:
        who = "👤 کاربر" if m['sender_role'] == 'user' else "🛡️ پشتیبانی"
        body = E(m['body'][:1200])
        lines.append(f"{who}: {body}")
    return "\n".join(lines)


def support_list_text(status="open"):
    rows = q("SELECT id,full_name,user_id,source_chat_title,updated_at FROM support_tickets "
             "WHERE status=? ORDER BY updated_at DESC LIMIT 20", (status,), many=True)
    title = "تیکت‌های باز" if status == "open" else "تیکت‌های بسته"
    lines = [f"🎫 <b>{title}</b>", ""]
    if not rows:
        lines.append("موردی برای نمایش وجود ندارد. 🌱")
    for t in rows:
        lines.append(f"• <b>#{t['id']}</b> — {E(t['full_name'] or 'کاربر')} (<code>{t['user_id']}</code>)")
        lines.append(f"  📍 {E(t['source_chat_title'] or 'گفتگوی خصوصی')}")
    return "\n".join(lines), rows


def support_list_kb(status="open"):
    _, rows = support_list_text(status)
    buttons = [[InlineKeyboardButton(f"🎫 #{t['id']} · {(t['full_name'] or str(t['user_id']))[:22]}",
                                     callback_data=f"sup:view:{t['id']}")] for t in rows]
    buttons.append([InlineKeyboardButton("🟢 باز", callback_data="sup:list:open"),
                    InlineKeyboardButton("⚫ بسته", callback_data="sup:list:closed")])
    buttons.append([InlineKeyboardButton("🔙 پنل مدیریت", callback_data="p:home")])
    return InlineKeyboardMarkup(buttons)


async def support_begin(update, context, source_type):
    msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if not msg or not user:
        return
    source_message_id = msg.message_id if source_type == "private" else msg.reply_to_message.message_id
    started = int(time.time())
    SUPPORT_PENDING[(chat.id, user.id)] = (source_type, source_message_id, started)
    q("INSERT OR REPLACE INTO support_pending VALUES (?,?,?,?,?)",
      (chat.id, user.id, source_type, source_message_id, started))
    if source_type == "group":
        return await msg.reply_text(card("🎫", "درخواست پشتیبانی", [
            "پیام بعدی‌ات را به‌عنوان متن درخواست پشتیبانی می‌فرستی.",
            "درخواست برای تیم پشتیبانی ارسال می‌شود؛ نیازی به ورود به پی‌وی ربات نیست.",
            "برای لغو، /cancel_support را بزن."], "لطفاً اطلاعات حساس مثل رمز عبور را ارسال نکنید 🔐"))
    return await msg.reply_text(card("🎫", "پشتیبانی", [
        "لطفاً متن درخواستت را در پیام بعدی بفرست.",
        "پس از ثبت، شماره تیکت برایت نمایش داده می‌شود."], "برای لغو: /cancel_support ↩️"))


async def supp_cmd(update, context):
    msg, chat = update.effective_message, update.effective_chat
    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return await support_begin(update, context, "private")
    replied = msg.reply_to_message
    if not replied or not replied.from_user or replied.from_user.id != context.bot.id:
        return await msg.reply_text(card("💡", "روش ثبت درخواست", [
            "روی یکی از پیام‌های خود ربات در گروه ریپلای کن و دستور /supp را بفرست.",
            "سپس متن درخواستت را در پیام بعدی بنویس." ]))
    if context.args:
        return await support_create(update, context, "group", replied.message_id, " ".join(context.args))
    return await support_begin(update, context, "group")


async def support_cmd(update, context):
    if update.effective_chat.type != ChatType.PRIVATE:
        return await update.effective_message.reply_text("برای ثبت خصوصی درخواست، در پی‌وی ربات از /support استفاده کن؛ برای ثبت از داخل گروه از /supp روی پیام ربات ریپلای کن.")
    return await support_begin(update, context, "private")


async def support_create(update, context, source_type, source_message_id, body):
    msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
    body = (body or "").strip()
    if not body:
        return await msg.reply_text("متن درخواست خالی است؛ لطفاً توضیحت را بفرست.")
    if len(body) > 3500:
        return await msg.reply_text("درخواست خیلی طولانی است؛ لطفاً حداکثر ۳۵۰۰ نویسه بفرست.")
    now = int(time.time())
    title = chat.title or ("گفتگوی خصوصی" if source_type == "private" else str(chat.id))
    conn = sqlite3.connect(DB_PATH)
    try:
        cur = conn.execute("INSERT INTO support_tickets (user_id,username,full_name,source_chat_id,source_chat_title,source_message_id,source_bot_message_id,source_type,status,created_at,updated_at) "
                           "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                           (user.id, user.username, user.full_name, chat.id, title,
                            msg.message_id,
                            source_message_id if source_type == "group" else None, source_type, "open", now, now))
        ticket_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()
    support_add_message(ticket_id, user.id, "user", body)
    SUPPORT_PENDING.pop((chat.id, user.id), None)
    q("DELETE FROM support_pending WHERE chat_id=? AND user_id=?", (chat.id, user.id))
    await msg.reply_text(card("✅", "درخواست ثبت شد", [f"🎫 شماره پیگیری: <b>#{ticket_id}</b>",
        "🛡️ درخواستت برای پشتیبانی ارسال شد.",
        "برای ادامه گفتگو، همین‌جا پیام بعدی را بفرست یا در پی‌وی از /support استفاده کن."], "ممنون که گزارش دادی ❤️"))
    admin_text = card("🎫", f"درخواست پشتیبانی جدید #{ticket_id}", [
        f"👤 {E(user.full_name)}  •  <code>{user.id}</code>",
        f"📍 {E(title)}  •  <code>{chat.id}</code>",
        f"📝 {E(body)}"], "برای رسیدگی از پنل مالک ← پشتیبانی استفاده کن.")
    try:
        await context.bot.send_message(OWNER_ID, admin_text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔎 مشاهده و رسیدگی", callback_data=f"sup:view:{ticket_id}")]]))
    except Exception as e:
        log.exception("support notification failed for ticket %s", ticket_id)
        await msg.reply_text("⚠️ تیکت ذخیره شد، اما اعلان فوری به پشتیبان نرسید؛ از پنل پشتیبانی بررسی خواهد شد.")


async def support_capture_message(update, context):
    msg, user, chat = update.effective_message, update.effective_user, update.effective_chat
    if not msg or not user or user.id == OWNER_ID or not msg.text or msg.text.startswith("/"):
        return
    key = (chat.id, user.id)
    pending = SUPPORT_PENDING.get(key)
    if not pending:
        row = q("SELECT source_type,source_message_id,created_at FROM support_pending WHERE chat_id=? AND user_id=?", key, one=True)
        if row:
            pending = (row["source_type"], row["source_message_id"], row["created_at"])
    if not pending:
        return
    source_type, source_message_id, started = pending
    if time.time() - started > 900:
        SUPPORT_PENDING.pop(key, None)
        q("DELETE FROM support_pending WHERE chat_id=? AND user_id=?", key)
        return await msg.reply_text("⌛ مهلت ثبت درخواست تمام شد؛ دوباره /supp یا /support را بزن.")
    await support_create(update, context, source_type, source_message_id, msg.text)


async def cancel_support_cmd(update, context):
    key = (update.effective_chat.id, update.effective_user.id)
    SUPPORT_PENDING.pop(key, None)
    q("DELETE FROM support_pending WHERE chat_id=? AND user_id=?", key)
    await update.effective_message.reply_text("↩️ ثبت درخواست پشتیبانی لغو شد.")


async def support_button(update, context):
    query = update.callback_query
    await query.answer()
    if query.data == "s:new":
        return await support_begin(update, context, "private")


async def support_admin_button(update, context):
    query = update.callback_query
    if query.from_user.id != OWNER_ID:
        return await query.answer("⛔ فقط مالک می‌تواند تیکت‌ها را بررسی کند.", show_alert=True)
    await query.answer()
    _, action, value = query.data.split(":", 2)
    if action == "list":
        text, _ = support_list_text(value)
        return await query.edit_message_text(text, reply_markup=support_list_kb(value), parse_mode="HTML")
    ticket_id = int(value)
    t = support_ticket(ticket_id)
    if not t:
        return await query.edit_message_text("❌ تیکت پیدا نشد.", reply_markup=back_kb())
    if action == "view":
        return await query.edit_message_text(support_ticket_text(ticket_id), reply_markup=support_kb(ticket_id, t['status']), parse_mode="HTML")
    if action in ("close", "reopen"):
        status = "closed" if action == "close" else "open"
        q("UPDATE support_tickets SET status=?,updated_at=? WHERE id=?", (status, int(time.time()), ticket_id))
        support_add_message(ticket_id, OWNER_ID, "admin", "تیکت بسته شد." if status == "closed" else "تیکت دوباره باز شد.")
        t = support_ticket(ticket_id)
        return await query.edit_message_text(support_ticket_text(ticket_id), reply_markup=support_kb(ticket_id, status), parse_mode="HTML")
    if action == "reply":
        STATE[OWNER_ID] = ("support_reply", ticket_id)
        return await query.edit_message_text(f"✍️ پاسخ تیکت <b>#{ticket_id}</b> را در همین پی‌وی بفرست.\nبرای لغو: /cancel",
                                             reply_markup=back_kb(), parse_mode="HTML")


def mention_id(uid):
    return f'<a href="tg://user?id={int(uid)}">کاربر</a>'


async def support_send_reply(ticket_id, body, context):
    t = support_ticket(ticket_id)
    if not t:
        raise ValueError("تیکت وجود ندارد.")
    body = body.strip()
    if not body or len(body) > 3500:
        raise ValueError("متن پاسخ باید بین ۱ تا ۳۵۰۰ نویسه باشد.")
    support_add_message(ticket_id, OWNER_ID, "admin", body)
    outgoing = card("🛡️", f"پاسخ پشتیبانی — تیکت #{ticket_id}", [E(body)], "برای پیگیری، شماره تیکت را نگه دار.")
    if t['source_type'] == "private":
        await context.bot.send_message(t['user_id'], outgoing, parse_mode="HTML")
    else:
        # پاسخ در همان گروه و در ادامه پیام درخواست؛ کاربر برای دریافت پاسخ مجبور به شروع پی‌وی نیست.
        try:
            await context.bot.send_message(t['source_chat_id'], outgoing, parse_mode="HTML",
                                           reply_to_message_id=t['source_message_id'], allow_sending_without_reply=True)
        except Exception:
            await context.bot.send_message(t['source_chat_id'], f"{mention_id(t['user_id'])}\n{outgoing}", parse_mode="HTML")
    q("UPDATE support_tickets SET status='open',updated_at=? WHERE id=?", (int(time.time()), ticket_id))


# ============================ پنل مالک ============================
MAIN_ROWS = [
    [("🎫 پشتیبانی و تیکت‌ها", "support")],
    [("📢 ارسال سراسری", "bc"), ("📊 آمار", "stats")],
    [("✏️ تغییر نام ربات", "rename"), ("⚙️ خلاصه تنظیمات", "summary")],
    [("💾 پشتیبان‌گیری", "backup"), ("📤 بازیابی دیتابیس", "restore")],
    [("📋 لیست گروه‌ها", "chats"), ("🗑️ حذف گروه", "rmchat")],
    [("🚫 کلمات فیلتر", "words"), ("🛡️ ضدفحش روشن/خاموش", "antispam")],
    [("🛡️ مدیران", "gadmins"), ("✅ معاف‌ها", "gexempts")],
    [("🔌 روشن/خاموش ربات", "maint"), ("📜 لاگ‌ها", "logs")],
    [("⚠️ محدودیت اخطار", "warnlim"), ("🔒 قفل رسانه سراسری", "glocks")],
    [("⏱️ زمان‌بندی و ساعت", "timers"), ("✉️ پیام به یک گروه", "msgchat")],
    [("🧹 پاکسازی اخطار گروه", "clrgroup"), ("🧹 پاکسازی اخطار کاربر", "clruser")],
    [("📊 اخطارهای کاربر", "userwarns"), ("🔍 وضعیت قفل‌ها", "locks")],
    [("💬 متن خوش‌آمد", "welcometext"), ("🔛 خوش‌آمد روشن/خاموش", "welcometoggle")],
    [("📜 قوانین گروه", "rules"), ("🤖 کپچا روشن/خاموش", "captcha")],
    [("🌙 سکوت شبانه", "night"), ("🏷️ نام و توضیحات", "botinfo")],
    [("🚪 خروج از گروه", "leave"), ("🔄 ری‌استارت", "restart")],
]

PROMPTS = {
    "bc": ("bc", "📢 پیام خود را (متن/عکس/ویدیو/...) بفرستید تا به همه گروه‌ها ارسال شود."),
    "rename": ("rename", "✏️ نام جدید ربات را بفرستید (حداکثر ۶۴ کاراکتر):"),
    "rmchat": ("rmchat", "🗑️ chat_id گروهی که از لیست حذف شود:"),
    "warnlim": ("warn", "⚠️ عدد جدید محدودیت اخطار (مثلاً 3):"),
    "sched": ("sched_when", "⏰ زمان ارسال سراسری (شمسی، به وقت تهران):\nمثال: 1405/09/05 23:30\nیا +30 (۳۰ دقیقه دیگر)\nیا 23:30"),
    "sch_off": ("sch_off", "🔴 زمان خاموش شدن ربات (شمسی، به وقت تهران):\nمثال: 1405/09/05 23:30\nیا +90 (۹۰ دقیقه دیگر)\nیا 23:30"),
    "sch_on": ("sch_on", "🟢 زمان روشن شدن ربات (شمسی، به وقت تهران):\nمثال: 1405/09/06 07:00\nیا +90 (۹۰ دقیقه دیگر)\nیا 07:00"),
    "autohours": ("autohours", "🕒 بازه‌ای که ربات هر روز روشن است (به وقت تهران)، مثل 07:00-23:30\nخارج از این بازه ربات خاموش می‌شود. برای غیرفعال کردن: off"),
    "schdel": ("schdel", "🗑 شماره‌ی زمان‌بندی (مثلاً 3) یا all برای لغو همه:"),
    "clrgroup": ("clrgroup", "🧹 chat_id گروه:"),
    "clruser": ("clruser", "🧹 user_id کاربر:"),
    "userwarns": ("userwarns", "📊 user_id کاربر:"),
    "welcometext": ("welcome", "💬 متن خوش‌آمد را بفرستید. می‌توانید {name} و {chat} بنویسید:"),
    "rules": ("rules", "📜 متن قوانین را بفرستید:"),
    "night": ("night", "🌙 بازه‌ی سکوت شبانه‌ی گروه‌ها (به وقت تهران)، مثل 23:30-07:00\n(ساعت ساده مثل 23-7 هم قبول است). برای خاموش کردن: off"),
    "locks": ("locks", "🔍 chat_id گروه:"),
    "restore": ("restore", "📤 فایل .db را بفرستید.\n⚠️ دیتابیس فعلی جایگزین می‌شود."),
    "botinfo": ("botinfo", "🏷️ بفرستید با فرمت:  نام|توضیحات"),
    "leave": ("leave", "🚪 chat_id گروهی که ربات از آن خارج شود:"),
}


def main_kb():
    rows = [[InlineKeyboardButton(t, callback_data="p:" + k) for t, k in row] for row in MAIN_ROWS]
    rows.append([InlineKeyboardButton("📖 راهنمای کامل", callback_data="help:0")])
    return InlineKeyboardMarkup(rows)


def back_kb(extra_rows=None):
    rows = list(extra_rows or [])
    rows.append([InlineKeyboardButton("🔙 بازگشت", callback_data="p:home")])
    return InlineKeyboardMarkup(rows)


def onoff(v):
    return "روشن" if v else "خاموش"


def panel_text():
    return card("🎛️", f"پنل مدیریت {E(get_global('bot_name', '🛡️ ربات نگهبان'))}", [
        f"📊 گروه‌ها و کانال‌های فعال: <b>{fnum(len(get_all_chats()))}</b>",
        f"🛡️ ضدفحش: {'🟢 روشن' if get_global('global_antispam', '1') == '1' else '🔴 خاموش'}",
        f"🔌 وضعیت ربات: {'🟢 روشن' if bot_is_on() else '🔴 خاموش'}",
        f"🕒 {fmt_jalali(now_tehran())} (تهران)"], "یک گزینه را انتخاب کنید 👇✨")


def summary_text():
    return ("⚙️ <b>خلاصه تنظیمات</b>\n\n"
            f"⚠️ محدودیت اخطار: {get_global('warn_limit', 3)}\n"
            f"🛡️ ضدفحش: {onoff(get_global('global_antispam', '1') == '1')}\n"
            f"🔌 ربات: {'روشن' if bot_is_on() else 'خاموش'}\n"
            f"🕒 بازه روزانه: {html.escape(get_global('auto_hours', 'off'))}\n"
            f"🕒 اکنون: {fmt_jalali(now_tehran())}\n"
            f"🔛 خوش‌آمد: {onoff(get_global('welcome_enabled', '1') == '1')}\n"
            f"🤖 کپچا: {onoff(get_global('captcha_enabled', '0') == '1')}\n"
            f"🌙 سکوت شبانه: {html.escape(get_global('night_range', 'off'))}\n"
            f"🚫 تعداد کلمات فیلتر: {len(q('SELECT word FROM words', many=True))}")


def glocks_kb():
    rows = [[InlineKeyboardButton(f"{'🔒' if get_global('glock_' + k) == '1' else '🔓'} {v[1]}",
                                  callback_data=f"p:gl:{k}")] for k, v in LOCKS.items()]
    return back_kb(rows)


async def show(query, text, kb=None):
    try:
        await query.edit_message_text(text, reply_markup=kb or back_kb(), parse_mode="HTML")
    except BadRequest as e:
        if "not modified" not in str(e).lower():
            raise


async def owner_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query.from_user.id != OWNER_ID:
        return await query.answer("⛔ فقط مالک.", show_alert=True)
    await query.answer()
    if get_global("restart_update_id") == str(update.update_id):
        set_global("restart_update_id", "0")  # همین دکمه بعد از ری‌استارت دوباره تحویل داده شده
        return await show(query, panel_text(), main_kb())
    parts = query.data.split(":", 2)
    key = parts[1]

    if key == "home":
        STATE.pop(OWNER_ID, None)
        return await show(query, panel_text(), main_kb())
    if key == "support":
        text, _ = support_list_text("open")
        return await show(query, text, support_list_kb("open"))
    if key == "timers":
        return await show(query, timers_text(), timers_kb())
    if key == "msgchat":
        rows = [[InlineKeyboardButton((t or str(i))[:40], callback_data=f"p:to:{i}")]
                for i, t in get_all_chats()[:40]]
        rows.append([InlineKeyboardButton("✍️ وارد کردن دستی (chat_id یا @username)", callback_data="p:tomanual")])
        return await show(query, "✉️ مقصد را انتخاب کنید (گروه‌ها و کانال‌هایی که ربات در آن‌ها عضو است):", back_kb(rows))
    if key == "tomanual":
        STATE[OWNER_ID] = ("msgchat_id", None)
        return await show(query, "✉️ chat_id یا @username گروه/کانال مقصد را بفرستید:\n\nبرای لغو: /cancel")
    if key == "to" and len(parts) == 3 and re.fullmatch(r"-?\d+", parts[2]):
        cid = int(parts[2])
        title = next((t for i, t in get_all_chats() if i == cid), str(cid))
        STATE[OWNER_ID] = ("msgchat_msg", (cid, title))
        return await show(query, f"✉️ پیام خود را بفرستید تا به «{html.escape(title or str(cid))}» ارسال شود "
                                 "(متن/عکس/ویدیو/فایل/...).\n\nبرای لغو: /cancel")
    if key in PROMPTS:
        mode, prompt = PROMPTS[key]
        STATE[OWNER_ID] = (mode, None)
        return await show(query, f"✍️ {html.escape(prompt)}\n\n<i>برای لغو: /cancel ↩️</i>")
    if key == "stats":
        warns = q("SELECT SUM(count) AS s FROM warnings", one=True)["s"] or 0
        roles = len(q("SELECT 1 FROM roles", many=True))
        return await show(query, f"📊 <b>آمار کلی</b>\n\n👥 گروه‌ها: {len(get_all_chats())}\n"
                                 f"⚠️ مجموع اخطارها: {warns}\n🎭 نقش‌های ثبت‌شده: {roles}")
    if key == "summary":
        return await show(query, summary_text())
    if key == "backup":
        try:
            tmp = os.path.join(tempfile.gettempdir(), f"backup_{stamp_file()}.db")
            src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(tmp)
            src.backup(dst)
            src.close()
            dst.close()
            with open(tmp, "rb") as f:
                await context.bot.send_document(OWNER_ID, f, filename=os.path.basename(tmp))
            os.remove(tmp)
            return await show(query, "✅ دیتابیس ارسال شد.")
        except Exception as e:
            return await show(query, f"❌ خطا: {html.escape(str(e))}")
    if key == "chats":
        chats = get_all_chats()
        text = "\n".join(f"• {html.escape(t or '')} (<code>{i}</code>)" for i, t in chats[:40]) or "گروهی ثبت نشده."
        return await show(query, f"📋 <b>گروه‌ها ({len(chats)})</b>\n\n{text}")
    if key == "words":
        n = len(q("SELECT word FROM words", many=True))
        return await show(query, f"🚫 <b>کلمات فیلتر</b> ({n})\n\nافزودن: <code>/addword کلمه</code>\n"
                                 "حذف: <code>/delword کلمه</code>\nلیست: /listwords")
    if key in ("gadmins", "gexempts"):
        role = "special_admin" if key == "gadmins" else "exempt"
        rows = q("SELECT user_id, chat_id, role FROM roles WHERE role IN (?, ?)",
                 (role, "admin" if key == "gadmins" else role), many=True)
        text = "\n".join(f"• {r['user_id']} ({r['role']}) — گروه {r['chat_id']}" for r in rows) or "—"
        return await show(query, f"{'🛡️ مدیران' if key == 'gadmins' else '✅ معاف‌ها'}:\n{text}")
    if key == "maint":
        cur = get_global("maintenance_mode", "0")
        set_global("maintenance_mode", "0" if cur == "1" else "1")
        return await show(query, f"🔌 ربات: {'🟢 روشن شد' if cur == '1' else '🔴 خاموش شد'}")
    if key == "logs":
        try:
            with open(LOG_PATH, "r", encoding="utf-8") as f:
                tail = "".join(f.readlines()[-15:])[-3500:]
            return await show(query, f"📜 آخرین لاگ‌ها:\n<pre>{html.escape(tail)}</pre>")
        except OSError:
            return await show(query, "فایل لاگ یافت نشد.")
    if key == "antispam":
        cur = get_global("global_antispam", "1")
        set_global("global_antispam", "0" if cur == "1" else "1")
        return await show(query, f"🛡️ ضدفحش: {'خاموش' if cur == '1' else 'روشن'}")
    if key == "glocks":
        return await show(query, "🔒 قفل سراسری (روی همه گروه‌ها اعمال می‌شود):", glocks_kb())
    if key == "gl" and len(parts) == 3 and parts[2] in LOCKS:
        k = "glock_" + parts[2]
        set_global(k, "0" if get_global(k) == "1" else "1")
        return await show(query, "🔒 قفل سراسری (روی همه گروه‌ها اعمال می‌شود):", glocks_kb())
    if key == "welcometoggle":
        cur = get_global("welcome_enabled", "1")
        set_global("welcome_enabled", "0" if cur == "1" else "1")
        return await show(query, f"🔛 خوش‌آمد: {'خاموش' if cur == '1' else 'روشن'}")
    if key == "captcha":
        cur = get_global("captcha_enabled", "0")
        set_global("captcha_enabled", "0" if cur == "1" else "1")
        return await show(query, f"🤖 کپچا: {'غیرفعال' if cur == '1' else 'فعال'}\n"
                                 "(ربات باید دسترسی محدود کردن کاربران داشته باشد)")
    if key == "restart":
        set_global("restart_update_id", update.update_id)
        await show(query, "🔄 در حال ری‌استارت...")
        os.execv(sys.executable, [sys.executable] + sys.argv)


async def owner_private(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg, user = update.effective_message, update.effective_user
    if not msg or not user or user.id != OWNER_ID:
        return
    st = STATE.get(OWNER_ID)
    if not st:
        return await msg.reply_text("از /panel استفاده کنید.")
    mode, data = st
    text = (msg.text or "").strip()

    if mode == "support_reply":
        try:
            await support_send_reply(int(data), text, context)
            STATE.pop(OWNER_ID, None)
            return await msg.reply_text(f"✅ پاسخ تیکت #{int(data)} ارسال شد.")
        except Exception as e:
            return await msg.reply_text(f"❌ ارسال پاسخ ناموفق بود: {E(e)}\nدوباره تلاش کن یا /cancel بزن.")

    if mode == "bc":
        STATE.pop(OWNER_ID)
        status = await msg.reply_text(f"⏳ ارسال به {len(get_all_chats())} گروه...")
        ok, fail = await broadcast(context, msg.chat_id, msg.message_id)
        return await status.edit_text(f"✅ ارسال کامل شد\nموفق: {ok}\nناموفق: {fail}")

    if mode in ("sched_when", "sch_off", "sch_on"):
        try:
            dt = parse_when(text)
        except ValueError as e:
            return await msg.reply_text(f"❌ {E(e)}\nدوباره بفرستید یا /cancel")
        if mode == "sched_when":
            STATE[OWNER_ID] = ("sched_msg", dt)
            return await msg.reply_text(f"زمان: {fmt_jalali(dt)} (تهران)\nحالا پیامی که باید ارسال شود را بفرستید:")
        STATE.pop(OWNER_ID)
        sid = add_schedule(dt.timestamp(), "off" if mode == "sch_off" else "on")
        return await msg.reply_text(f"✅ زمان‌بندی #{sid}: {'🔴 خاموش' if mode == 'sch_off' else '🟢 روشن'} شدن ربات "
                                    f"در {fmt_jalali(dt)} (به وقت تهران)")

    if mode == "sched_msg":
        STATE.pop(OWNER_ID)
        sid = add_schedule(data.timestamp(), "broadcast",
                           json.dumps({"from_chat": msg.chat_id, "message_id": msg.message_id}))
        return await msg.reply_text(f"⏰ ارسال سراسری #{sid} برای {fmt_jalali(data)} (به وقت تهران) زمان‌بندی شد.\n"
                                    "⚠️ پیام را از همین چت پاک نکنید.")

    if mode == "autohours":
        try:
            rng = parse_range(text)
        except ValueError as e:
            return await msg.reply_text(f"❌ {E(e)}\nدوباره بفرستید یا /cancel")
        STATE.pop(OWNER_ID)
        set_global("auto_hours", rng)
        set_global("auto_state", "")
        return await msg.reply_text("✅ بازه‌ی روزانه غیرفعال شد." if rng == "off" else
                                    f"✅ ربات هر روز فقط در بازه‌ی {fa(rng)} (به وقت تهران) روشن است.")

    if mode == "schdel":
        STATE.pop(OWNER_ID)
        if text.lower() == "all":
            q("UPDATE schedules SET done=1 WHERE done=0")
            return await msg.reply_text("✅ همه‌ی زمان‌بندی‌های در انتظار لغو شد.")
        if text.isdigit():
            q("UPDATE schedules SET done=1 WHERE id=? AND done=0", (int(text),))
            return await msg.reply_text(f"✅ زمان‌بندی #{text} لغو شد.")
        return await msg.reply_text("شماره نامعتبر.")

    if mode == "msgchat_id":
        try:
            chat = await resolve_chat(context, text)
        except Exception as e:
            return await msg.reply_text(f"❌ پیدا نشد (ربات باید عضو باشد): {E(e)}\nدوباره بفرستید یا /cancel")
        STATE[OWNER_ID] = ("msgchat_msg", (chat.id, chat.title or chat.username or str(chat.id)))
        return await msg.reply_text(f"مقصد: «{E(chat.title or chat.username)}»\nحالا پیام را بفرستید:")

    if mode == "msgchat_msg":
        STATE.pop(OWNER_ID)
        try:
            await context.bot.copy_message(data[0], msg.chat_id, msg.message_id)
            return await msg.reply_text(f"✅ به «{E(data[1])}» ارسال شد.")
        except Exception as e:
            return await msg.reply_text(f"❌ خطا (برای کانال، ربات باید ادمین با دسترسی ارسال پیام باشد): {E(e)}")

    if mode == "restore":
        if not msg.document:
            return await msg.reply_text("فایل .db را به‌صورت سند بفرستید یا /cancel")
        STATE.pop(OWNER_ID)
        tmp = os.path.join(tempfile.gettempdir(), "restore_candidate.db")
        try:
            await (await msg.document.get_file()).download_to_drive(tmp)
            conn = sqlite3.connect(tmp)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            ok = conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            conn.close()
            if not ok or not {"chats", "roles", "warnings", "settings"} <= tables:
                return await msg.reply_text("❌ فایل دیتابیس معتبر نیست.")
            shutil.copy(tmp, DB_PATH)
            KNOWN_CHATS.clear()
            invalidate_words()
            init_db()
            return await msg.reply_text("✅ دیتابیس بازیابی شد.")
        except Exception as e:
            return await msg.reply_text(f"❌ خطا: {E(e)}")

    if mode in ("rmchat", "clrgroup", "clruser", "userwarns", "locks", "leave"):
        if not re.fullmatch(r"-?\d+", text):
            return await msg.reply_text("یک عدد معتبر بفرستید یا /cancel")
        STATE.pop(OWNER_ID)
        try:
            return await msg.reply_text(await run_chat_action(mode, int(text), context))
        except Exception as e:
            return await msg.reply_text(f"❌ خطا: {E(e)}")

    # حالت‌های متنی
    STATE.pop(OWNER_ID)
    if mode == "warn":
        if text.isdigit() and int(text) >= 1:
            set_global("warn_limit", int(text))
            return await msg.reply_text(f"✅ محدودیت اخطار: {text}")
        return await msg.reply_text("عدد نامعتبر.")
    if mode == "welcome":
        set_global("welcome_text", text)
        return await msg.reply_text("✅ متن خوش‌آمد ذخیره شد.")
    if mode == "rules":
        set_global("rules_text", text)
        return await msg.reply_text("✅ قوانین ذخیره شد.")
    if mode == "night":
        try:
            rng = parse_range(text)
        except ValueError as e:
            STATE[OWNER_ID] = (mode, data)
            return await msg.reply_text(f"❌ {E(e)}\nدوباره بفرستید یا /cancel")
        set_global("night_range", rng)
        return await msg.reply_text("✅ سکوت شبانه غیرفعال شد." if rng == "off" else
                                    f"✅ سکوت شبانه: {fa(rng)} (به وقت تهران)")
    if mode == "rename":
        try:
            await context.bot.set_my_name(text[:64])
            set_global("bot_name", text[:64])
            return await msg.reply_text(f"✅ نام ربات به «{E(text[:64])}» تغییر کرد.")
        except Exception as e:
            return await msg.reply_text(f"❌ خطا: {E(e)}")
    if mode == "botinfo":
        if "|" not in text:
            return await msg.reply_text("فرمت باید «نام|توضیحات» باشد.")
        name, desc = (x.strip() for x in text.split("|", 1))
        try:
            await context.bot.set_my_name(name[:64])
            await context.bot.set_my_description(desc[:512])
            set_global("bot_name", name[:64])
            return await msg.reply_text(f"✅ نام و توضیحات ربات به‌روز شد.")
        except Exception as e:
            return await msg.reply_text(f"❌ خطا: {E(e)}")


# ============================ کامندهای فارسی ============================
ALIASES = {
    "بن": "ban", "آنبن": "unban", "حذف_بن": "unban",
    "سکوت": "mute", "حذف_سکوت": "unmute",
    "اخطار": "warn", "حذف_اخطار": "unwarn",
    "معاف": "exempt", "حذف_معاف": "unexempt",
    "عزل": "demote", "مدیر": "admin", "مدیر_ویژه": "special",
    "لیست_ویژه": "specials", "قوانین": "rules",
    "قفل": "lock", "بازکردن": "unlock", "قفل‌ها": "locks",
    "نجوا": "whisper", "اسپم": "spam", "ساعت": "time", "راهنما": "help", "ضداسپم": "antispam", "حد_اسپم": "spamlimit",
}
COMMANDS = {}


_CHAT_TOGGLE = re.compile(r"^گپ\s*(باز|بسته)\s*((?:[+\d۰-۹].*)?)$")
_STATS_RE = re.compile(r"^(?:آمار|امار)(\s+(?:گپ|گروه))?$")


async def persian_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """دستورات فارسی بدون اسلش: «بن»، «سکوت 30m»، «گپ بسته 23:45»، «گپ باز»، «آمار»، «آمار گپ»."""
    msg, chat, user = update.effective_message, update.effective_chat, update.effective_user
    if not msg or not msg.text or not user:
        return
    if get_global("maintenance_mode", "0") == "1" and user.id != OWNER_ID:
        return
    text = msg.text.strip()
    m = _CHAT_TOGGLE.match(text)
    if m:
        if await user_level(context, chat.id, user.id) < 1:
            return
        context.args = m.group(2).split()
        return await (closechat_cmd if m.group(1) == "بسته" else openchat_cmd)(update, context)
    m = _STATS_RE.match(text)
    if m:
        context.args = []
        return await (chatstats_cmd if m.group(1) else stats_cmd)(update, context)
    parts = text.split()
    name = ALIASES.get(parts[0])
    if not name:
        return
    if name != "rules" and await user_level(context, chat.id, user.id) < 1:
        return  # برای کاربران عادی بی‌صدا نادیده گرفته می‌شود
    context.args = parts[1:]
    await COMMANDS[name](update, context)


# ============================ راه‌اندازی ============================
async def on_error(update, context: ContextTypes.DEFAULT_TYPE):
    log.error("Unhandled error", exc_info=context.error)


async def post_shutdown(app: Application):
    flush_stats()


async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("panel", "پنل مالک"),
        BotCommand("support", "ثبت درخواست پشتیبانی خصوصی"),
        BotCommand("supp", "درخواست پشتیبانی با ریپلای به پیام ربات"),
        BotCommand("cancel_support", "لغو درخواست پشتیبانی"),
        BotCommand("ban", "بن کاربر (ریپلای)"),
        BotCommand("unban", "رفع بن (user_id)"),
        BotCommand("mute", "سکوت کاربر (ریپلای)"),
        BotCommand("unmute", "رفع سکوت"),
        BotCommand("warn", "اخطار به کاربر"),
        BotCommand("unwarn", "پاک کردن اخطارها"),
        BotCommand("exempt", "معاف کردن کاربر"),
        BotCommand("unexempt", "حذف از معاف‌ها"),
        BotCommand("special", "مدیر ویژه کردن"),
        BotCommand("admin", "مدیر کردن"),
        BotCommand("demote", "عزل مدیر"),
        BotCommand("specials", "لیست مدیران"),
        BotCommand("lock", "قفل کردن (link/photo/...)"),
        BotCommand("unlock", "باز کردن قفل"),
        BotCommand("locks", "وضعیت قفل‌ها"),
        BotCommand("spam", "اسپم (ریپلای): حذف پیام + سکوت + اخطار"),
        BotCommand("antispam", "ضد اسپم روشن/خاموش"),
        BotCommand("spamlimit", "تنظیم حد اسپم"),
        BotCommand("whisper", "پیام فقط برای یک کاربر (ریپلای)"),
        BotCommand("rules", "قوانین گروه"),
        BotCommand("help", "راهنمای کامل مدیر"),
        BotCommand("stats", "آمار کاربر (ریپلای = آمار او)"),
        BotCommand("chatstats", "آمار گپ"),
        BotCommand("closechat", "بستن گپ (با ساعت اختیاری)"),
        BotCommand("openchat", "باز کردن گپ (با ساعت اختیاری)"),
        BotCommand("schedules", "زمان‌بندی‌های این گروه"),
        BotCommand("time", "ساعت و تاریخ شمسی تهران"),
        BotCommand("send", "ارسال پیام به گروه/کانال (مالک)"),
        BotCommand("cancel", "لغو عملیات"),
    ])


def main():
    if BOT_TOKEN.startswith("PUT_") or not OWNER_ID:
        sys.exit("❌ متغیرهای محیطی BOT_TOKEN و OWNER_ID را تنظیم کنید (راهنما بالای فایل).")
    init_db()

    builder = (Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown)
               .defaults(Defaults(parse_mode=ParseMode.HTML)))
    if PROXY_URL:
        builder = builder.proxy(PROXY_URL).get_updates_proxy(PROXY_URL)
    app = builder.build()

    COMMANDS.update({
        "ban": ban_cmd, "unban": unban_cmd, "mute": mute_cmd, "unmute": unmute_cmd,
        "warn": warn_cmd, "unwarn": unwarn_cmd, "exempt": exempt_cmd, "unexempt": unexempt_cmd,
        "special": special_cmd, "admin": admin_cmd, "demote": demote_cmd, "specials": specials_cmd,
        "rules": rules_cmd, "lock": lock_cmd, "unlock": unlock_cmd, "locks": locks_cmd,
        "whisper": whisper_cmd, "spam": spam_cmd, "time": time_cmd, "help": help_cmd,
        "stats": stats_cmd, "chatstats": chatstats_cmd, "closechat": closechat_cmd, "openchat": openchat_cmd,
        "schedules": schedules_cmd, "unschedule": unschedule_cmd, "antispam": antispam_cmd, "spamlimit": spamlimit_cmd,
    })
    for name, fn in COMMANDS.items():
        app.add_handler(CommandHandler(name, fn))

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("support", support_cmd))
    app.add_handler(CommandHandler("supp", supp_cmd))
    app.add_handler(CommandHandler("cancel_support", cancel_support_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("panel", owner_only(lambda u, c: u.effective_message.reply_text(
        panel_text(), reply_markup=main_kb(), parse_mode="HTML"))))
    app.add_handler(CommandHandler("addword", addword_cmd))
    app.add_handler(CommandHandler("delword", delword_cmd))
    app.add_handler(CommandHandler("listwords", listwords_cmd))
    app.add_handler(CommandHandler("schedule_broadcast", schedule_cmd))
    app.add_handler(CommandHandler("send", send_cmd))
    app.add_handler(CommandHandler("remove_chat", owner_chat_cmd("rmchat", "/remove_chat chat_id")))
    app.add_handler(CommandHandler("clear_warns", owner_chat_cmd("clrgroup", "/clear_warns chat_id")))
    app.add_handler(CommandHandler("clear_user_warns", owner_chat_cmd("clruser", "/clear_user_warns user_id")))
    app.add_handler(CommandHandler("user_warns", owner_chat_cmd("userwarns", "/user_warns user_id")))
    app.add_handler(CommandHandler("leave", owner_chat_cmd("leave", "/leave chat_id")))

    app.add_handler(CallbackQueryHandler(owner_button, pattern=r"^p:"))
    app.add_handler(CallbackQueryHandler(support_button, pattern=r"^s:"))
    app.add_handler(CallbackQueryHandler(support_admin_button, pattern=r"^sup:"))
    app.add_handler(CallbackQueryHandler(captcha_button, pattern=r"^cap:\d+$"))
    app.add_handler(CallbackQueryHandler(help_button, pattern=r"^help:\d+$"))
    app.add_handler(CallbackQueryHandler(chatstats_button, pattern=r"^cs:\w+:-?\d+$"))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS & filters.ChatType.GROUPS, on_new_members))

    # پیام‌های خصوصی مالک (broadcast / تنظیمات / بازیابی)
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & ~filters.COMMAND, owner_private))
    # دریافت متن درخواست‌های پشتیبانی؛ گروه و پی‌وی، بدون نیاز به مکالمه قبلی با ربات در پی‌وی
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, support_capture_message), group=1)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, support_capture_message), group=0)

    # کامندهای فارسی (گروه 1) و نظارت (گروه 2) هر دو اجرا می‌شوند
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.GROUPS, persian_router), group=1)
    app.add_handler(MessageHandler(filters.ALL & ~filters.COMMAND & ~filters.StatusUpdate.ALL & filters.ChatType.GROUPS,
                                   message_watcher), group=2)
    app.add_error_handler(on_error)
    app.add_handler(TypeHandler(Update, clock_sync), group=-1)  # همگام‌سازی ساعت با سرور تلگرام

    if app.job_queue:
        app.job_queue.run_repeating(timer_job, interval=20, first=10)
    else:
        log.warning("JobQueue نصب نیست؛ زمان‌بندی‌ها، روشن/خاموش خودکار، سکوت شبانه و کپچا-تایم‌اوت کار نمی‌کنند.")

    log.info("ربات شروع شد.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
