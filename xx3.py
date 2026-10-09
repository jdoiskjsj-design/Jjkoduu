# -*- coding: utf-8 -*-
"""
بوت مراقبة المحادثات المرئية في المجموعات والقنوات.

البوت (Bot API) هو الواجهة وقناة الإشعارات، والحساب المساعد (Telethon + PyTgCalls)
هو الذي يدخل المحادثة المرئية ويراقب الصاعدين والخارجين.

التشغيل:  python uu.py            (يثبّت الناقص من المكتبات تلقائيًا)
التحديث:  python uu.py --upgrade  (يحدّث المكتبات)

الإعدادات تُقرأ من متغيرات البيئة، أو ضع قيمك في الأسطر التالية مباشرة.
"""
import importlib.util
import os
import subprocess
import sys

# ------------------------------------------------------------------ الإعدادات
BOT_TOKEN = "8950311526:AAFI3dLKj7uDJPS3A5y8i1Wg9nZS_KjFs3w"
API_ID = "31540159"
API_HASH = "fd3ff16d17e43a5d50b1135ae8437408"
OWNER_ID = 8706836075
DEV_URL = os.getenv("DEV_URL", "https://t.me/x_7_v3")

NORMAL_LIMIT = 1         # الأماكن المسموحة للمشترك العادي
VIP_LIMIT = 3            # الأماكن المسموحة لمشترك VIP
PAGE_SIZE = 8            # عدد الأماكن في كل صفحة من لوحة المطور
SCAN_MIN = 3             # أقل فاصل بين دورتين من الفحص الاحتياطي (الدخول الفوري يتم عبر الأحداث)
SCAN_PER_CHAT = 0.12     # يزيد الفاصل بحسب عدد الأماكن حتى لا يحظرنا تيليجرام (FloodWait)
ACTIVE_RECHECK = 30      # كل كم ثانية نتأكد من الأماكن التي فيها محادثة قائمة
NOTIFY_PACE = 0.5        # أقل فاصل بين رسالتين إلى نفس الوجهة
BACKUP_EVERY = 6 * 3600  # نسخة احتياطية من قاعدة البيانات كل 6 ساعات
BACKUP_KEEP = 10
FALLBACK_UTC_HOURS = 3   # إذا لم تتوفر منطقة دمشق الزمنية

REQUIRED = {"telethon": "telethon", "pytgcalls": "py-tgcalls", "aiohttp": "aiohttp",
            "tzdata": "tzdata", "yt_dlp": "yt-dlp[default]", "static_ffmpeg": "static-ffmpeg",
            "PIL": "pillow"}


def ensure_packages(upgrade=False):
    for module, package in REQUIRED.items():
        if not upgrade and importlib.util.find_spec(module):
            continue
        cmd = [sys.executable, "-m", "pip", "install", "-q", "-U", package]
        if subprocess.run(cmd, capture_output=True).returncode != 0:
            subprocess.run(cmd + ["--break-system-packages"], capture_output=True)


ensure_packages("--upgrade" in sys.argv)

import asyncio
import copy
import html
import io
import json
import logging
import math
import random
import re
import shutil
import time
import wave
from datetime import datetime, timedelta, timezone

import aiohttp
from telethon import TelegramClient, events, functions, types, utils
from telethon.errors import FloodWaitError, SessionPasswordNeededError
from telethon.sessions import StringSession
from pytgcalls import PyTgCalls
from pytgcalls.types import AudioQuality, MediaStream

try:
    from pytgcalls.types import VideoQuality
except Exception:
    VideoQuality = None

try:
    from pytgcalls import filters as call_filters
except Exception:
    call_filters = None

try:
    from zoneinfo import ZoneInfo
    DAMASCUS = ZoneInfo("Asia/Damascus")
except Exception:
    DAMASCUS = timezone(timedelta(hours=FALLBACK_UTC_HOURS))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vcbot")

_token_id = BOT_TOKEN.split(":")[0]
BOT_ID = int(_token_id) if _token_id.isdigit() else 0

# ------------------------------------------------------------------ التخزين
DB_FILE = "db.json"
BACKUP_DIR = "backups"
SILENCE_FILE = "silence.wav"

DEFAULT_DB = {
    "subs": {},        # {user_id: {"exp": timestamp, "vip": bool}}
    "chats": [],       # الأماكن المراقبة
    "unotify": {},     # {user_id: chat_id} وجهة إشعارات كل مستخدم
    "devs": [],
    "known": [],       # كل من فتح البوت (للإذاعة)
    "emoji": {},       # الايموجي المميز للأزرار والنصوص
    "stats": {"joins": 0, "leaves": 0, "bans": 0},
    "session": "",
    "photo": "",
    "enabled": True,
    "people": {},      # {user_id: {name, un, plays, dls, first, last, bot}} إحصائيات المستخدمين
    "music": {
        "enabled": True, "access": "mixed", "max_min": 15, "queue_limit": 20, "cache_mb": 1024,
        "volume": 100, "video": True, "yt": True, "panel": "auto", "updates_url": "",
        "off": [], "plays": 0, "downloads": 0, "fallback": True, "proxy": "",
        "ipv4": True,          # يفرض IPv4 (نطاقات IPv6 تُحجب أكثر)
        "auto_update": True, "last_update": 0,    # تحديث yt-dlp تلقائيًا كل 24 ساعة
        "user_off": [],        # مستخدمون ألغوا الميوزك في أماكنهم
        "progress_sec": 10,    # كل كم ثانية يتحدث شريط التقدم
        "blur": 8,             # درجة تشويش صورة اللوحة (0 = بدون)
        "panel_photo": "",     # file_id للصورة المعالجة
        "stickers": [],        # ملصقات الانتظار
    },
}


def load_db():
    data = copy.deepcopy(DEFAULT_DB)
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, encoding="utf-8") as f:
                data.update(json.load(f))
        except (OSError, ValueError) as e:
            # لا نكتب فوق ملف تالف قبل أن نحتفظ بنسخة منه
            log.error("ملف البيانات تالف (%s)، نسخة احتياطية في %s.corrupt", e, DB_FILE)
            shutil.copy(DB_FILE, DB_FILE + ".corrupt")
    data["stats"] = {**DEFAULT_DB["stats"], **data.get("stats", {})}
    data["music"] = {**DEFAULT_DB["music"], **data.get("music", {})}
    return data


db = load_db()


def save():
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(db, f, ensure_ascii=False, indent=1)
    os.replace(tmp, DB_FILE)


_dirty = False


def save_soon():
    """للتغييرات المتكررة (الإحصائيات): نؤجل الكتابة إلى الدورة التالية بدل كتابة الملف عند كل حدث."""
    global _dirty
    _dirty = True


def flush():
    global _dirty
    if _dirty:
        _dirty = False
        save()


def backup_db():
    os.makedirs(BACKUP_DIR, exist_ok=True)
    name = datetime.now(timezone.utc).strftime("db-%Y%m%d-%H%M.json")
    shutil.copy(DB_FILE, os.path.join(BACKUP_DIR, name))
    old = sorted(os.listdir(BACKUP_DIR))[:-BACKUP_KEEP]
    for f in old:
        os.remove(os.path.join(BACKUP_DIR, f))


def migrate():
    """ترحيل بيانات الإصدارات القديمة إلى البنية الحالية."""
    trial_end = datetime.now(timezone.utc).timestamp() + 30 * 86400
    for uid in db.pop("users", []):
        db["subs"].setdefault(str(uid), {"exp": trial_end, "vip": False})
    for ch in db["chats"]:
        ch.setdefault("owner", OWNER_ID)
        ch.setdefault("mic", False)
        ch.setdefault("novideo", False)
    old_notify = db.pop("notify", 0)
    if old_notify:
        db["unotify"].setdefault(str(OWNER_ID), old_notify)
    for key, sub in db["subs"].items():
        target = sub.pop("notify", None)
        if target:
            db["unotify"].setdefault(key, target)
    save()


migrate()

# ------------------------------------------------------------------ الحالة أثناء التشغيل
HTTP = None            # جلسة aiohttp
UB = None              # الحساب المساعد (Telethon)
CALLS = None           # PyTgCalls
ME_ID = 0              # ايدي الحساب المساعد

KNOWN = set(db["known"])
states = {}            # user_id -> خطوة الإدخال الحالية
logins = {}            # user_id -> عملية تسجيل دخول جارية
active = {}            # chat_id -> call_id
call_chat = {}         # call_id -> chat_id
live_calls = {}        # call_id -> بيانات الجلسة الجارية (للتقرير)
locks = {}             # chat_id -> قفل يمنع الدخول المزدوج
video_warned = {}      # (chat_id, user_id) -> وقت آخر تنبيه فيديو
broadcasting = False


# ------------------------------------------------------------------ الاشتراكات والصلاحيات
def now_ts():
    return datetime.now(timezone.utc).timestamp()


def sub_of(uid):
    sub = db["subs"].get(str(uid))
    return sub if sub and sub["exp"] > now_ts() else None


def is_vip(uid):
    sub = sub_of(uid)
    return bool(sub and sub.get("vip"))


def is_dev(uid):
    return uid == OWNER_ID or uid in db["devs"]


def is_auth(uid):
    return is_dev(uid) or sub_of(uid) is not None


def limit_of(uid):
    if is_dev(uid):
        return 10 ** 6
    return VIP_LIMIT if is_vip(uid) else NORMAL_LIMIT


def days_left(sub):
    return max(0, math.ceil((sub["exp"] - now_ts()) / 86400))


def chat_of(cid):
    return next((c for c in db["chats"] if c["id"] == cid), None)


def my_chats(uid):
    return [c for c in db["chats"] if c.get("owner") == uid]


def can_manage(uid, ch):
    return is_dev(uid) or ch.get("owner") == uid


async def can_ban(uid, ch):
    """الحظر لمشرف في المجموعة نفسها بصلاحية حظر الأعضاء، أو لمنشئها. لا استثناء لأحد."""
    d = await api("getChatMember", chat_id=ch["id"], user_id=uid)
    res = d.get("result") or {}
    if d.get("ok"):
        if res.get("status") == "creator":
            return True
        return res.get("status") == "administrator" and bool(res.get("can_restrict_members"))
    if UB:  # البوت ليس في المجموعة: نسأل عبر الحساب المساعد
        try:
            perms = await UB.get_permissions(ch["id"], uid)
            return bool(perms and (perms.is_creator or (perms.is_admin and perms.ban_users)))
        except Exception as e:
            log.warning("فحص الصلاحيات: %s", e)
    return False


def tg_name(user):
    """اسم عرض من كائن مستخدم قادم من Bot API."""
    full = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
    return full or user.get("username") or str(user.get("id", ""))


def record_person(uid, name=None, username=None, bump=None, bot=False, touch=True):
    """يسجل المستخدم ويزيد عدّاده (plays / dls). username: None = لا تغيّر، "" = بدون يوزر."""
    if not uid or uid < 0:
        return
    rec = db["people"].setdefault(str(uid), {"first": now_ts(), "plays": 0, "dls": 0})
    if name:
        rec["name"] = name
    if username is not None:
        rec["un"] = username
    if bot:
        rec["bot"] = True
    if touch:
        rec["last"] = now_ts()
    if bump:
        rec[bump] = rec.get(bump, 0) + 1
    save_soon()


def person_tag(uid):
    un = db["people"].get(str(uid), {}).get("un")
    return f"@{esc(un)} " if un else ""


def remember_user(uid):
    if uid and uid not in KNOWN:
        KNOWN.add(uid)
        db["known"].append(uid)
        save()


# ------------------------------------------------------------------ Bot API
async def _post(method, params, retry=True):
    try:
        async with HTTP.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", json=params) as r:
            data = await r.json()
        wait = (data.get("parameters") or {}).get("retry_after")
        if not data.get("ok") and wait and retry:
            log.warning("%s: flood، ننتظر %ss", method, wait)
            await asyncio.sleep(wait + 1)
            return await _post(method, params, retry=False)
        if not data.get("ok"):
            log.warning("%s: %s", method, data.get("description"))
        return data
    except Exception as e:
        log.warning("api %s: %s", method, e)
        return {"ok": False}


def has_icons(markup):
    try:
        return any("icon_custom_emoji_id" in btn for row in markup["inline_keyboard"] for btn in row)
    except (TypeError, KeyError):
        return False


def strip_icons(markup):
    rows = [[{k: v for k, v in btn.items() if k != "icon_custom_emoji_id"} for btn in row]
            for row in markup["inline_keyboard"]]
    return {"inline_keyboard": rows}


TG_EMOJI = re.compile(r"<tg-emoji[^>]*>.*?</tg-emoji>\s?", re.S)


async def api(method, **params):
    """نداء Bot API. إذا رُفضت الرسالة بسبب ايموجي مميز غير صالح نعيدها بدونه."""
    data = await _post(method, params)
    if data.get("ok"):
        return data
    changed = False
    if has_icons(params.get("reply_markup")):
        params["reply_markup"] = strip_icons(params["reply_markup"])
        changed = True
    for field_name in ("text", "caption"):
        if isinstance(params.get(field_name), str) and "<tg-emoji" in params[field_name]:
            params[field_name] = TG_EMOJI.sub("", params[field_name])
            changed = True
    return await _post(method, params) if changed else data


# ------------------------------------------------------------------ تنسيق النصوص
def esc(value):
    return html.escape(str(value))


def mention(uid, name=None):
    if isinstance(uid, int) and uid < 0:      # قناة: لا يوجد لها رابط tg://user
        return f"<b>{esc(name or uid)}</b>"
    return f'<a href="tg://user?id={uid}">{esc(name or uid)}</a>'


def local(dt=None):
    dt = dt or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(DAMASCUS)


def fmt(dt):
    if dt is None:
        return "غير معروف"
    return local(dt).strftime("%Y-%m-%d %I:%M:%S %p")


def dur(seconds):
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return (f"{hours} ساعة " if hours else "") + (f"{minutes} دقيقة " if minutes else "") + f"{secs} ثانية"


# ------------------------------------------------------------------ الايموجي المميز
FALLBACK_MARK = "&#8226;"


def custom_emoji(eid):
    return f'<tg-emoji emoji-id="{eid}">{FALLBACK_MARK}</tg-emoji>'


def deco(text, kind=None):
    """يضع الايموجي المميز في بداية الرسالة: الخاص بنوعها، وإلا العام."""
    eid = (kind and db["emoji"].get("t:" + kind)) or db["emoji"].get("t:*")
    return f"{custom_emoji(eid)} {text}" if eid else text


def field(slot, label, value):
    """سطر «العنوان: القيمة» يسبقه ملصق مميز إن حدده المطور لهذا الحقل."""
    eid = db["emoji"].get("f:" + slot) or db["emoji"].get("f:*")
    mark = custom_emoji(eid) + " " if eid else ""
    return f"{mark}{label}: {value}"


BUTTON_SLOTS = {
    "*": "كل الأزرار", "home": "رجوع", "dev": "مراسلة المطور", "me": "اشتراكي",
    "guide": "طريقة الاستخدام", "chats": "الأماكن المراقبة", "chat": "عنصر مكان",
    "addchat": "إضافة كروب أو قناة", "delchat": "حذف مكان", "open": "فتح المكان",
    "live": "المتصلون الآن", "vcstart": "تشغيل المحادثة المرئية", "vcend": "إنهاء المحادثة المرئية",
    "chmic": "الميكروفون", "chvid": "حظر الفيديو", "ban": "حظر المستخدم", "unban": "فك الحظر",
    "notify": "كروب الإشعارات", "setnotify": "تحديد كروب الإشعارات", "notifyreset": "إلغاء كروب الإشعارات",
    "devpanel": "لوحة المطور", "allchats": "كل الأماكن", "login": "تسجيل الدخول",
    "acc": "حالة الحساب المساعد", "logout": "تسجيل خروج", "photo": "صورة الرد",
    "mu_pause": "الميوزك: إيقاف مؤقت", "mu_resume": "الميوزك: استكمال", "mu_back": "الميوزك: عودة",
    "mu_fwd": "الميوزك: تقديم", "mu_skip": "الميوزك: تخطي", "mu_stop": "الميوزك: إنهاء",
    "mu_loop": "الميوزك: تكرار", "mu_queue": "الميوزك: الطابور", "mu_dev": "الميوزك: المطور",
    "mu_updates": "الميوزك: قناة التحديثات", "dm": "أزرار لوحة الميوزك",
    "mutog": "تفعيل وإلغاء الميوزك", "chban": "حظر القنوات الصاعدة",
    "ust": "إحصائيات المستخدمين", "ustfill": "تحديث اليوزرات",
    "stats": "الإحصائيات", "toggle": "تشغيل وإيقاف المراقبة", "adduser": "اشتراك مستخدم",
    "deluser": "حذف مشترك", "bcast": "الإذاعة الشاملة", "adddev": "إضافة مطور",
    "deldev": "حذف مطور", "emoji": "ايموجي الأزرار", "txtem": "ملصقات النصوص", "preview": "معاينة",
}

TEXT_SLOTS = {
    "t:*": "كل الرسائل", "t:join": "إشعار الصعود", "t:leave": "إشعار الخروج",
    "t:ban": "إشعار الحظر", "t:report": "تقرير المحادثة", "t:video": "إيقاف الفيديو",
    "t:call": "دخول الحساب المساعد", "t:panel": "لوحة المستخدم", "t:dev": "لوحة المطور",
    "t:guide": "طريقة الاستخدام", "t:music": "رسائل الموسيقى",
}

FIELD_SLOTS = {
    "f:*": "كل عناوين الحقول", "f:place": "حقل المكان", "f:name": "حقل الاسم",
    "f:user": "حقل اليوزر", "f:id": "حقل الايدي", "f:time": "حقل الوقت",
    "f:duration": "حقل المدة", "f:owner": "حقل المالك", "f:song": "حقل عنوان الأغنية",
    "f:artist": "حقل القناة", "f:by": "حقل الطالب",
}

SLOT_NAMES = {**BUTTON_SLOTS, **TEXT_SLOTS, **FIELD_SLOTS}

# أزرار لا يراها إلا المطور، والتي يحتكرها المطور الأساسي
DEV_ONLY = {"login", "acc", "logout", "photo", "stats", "toggle", "adduser", "deluser",
            "emoji", "txtem", "emk", "emojiclear", "txtclear", "preview", "bcast", "bct", "bcgo",
            "devpanel", "allchats", "adddev", "deldev", "dm", "ust", "ustfill"}
PRIMARY_ONLY = {"adddev", "deldev"}


def b(text, data=None, url=None, style=None, k=None):
    """زر inline. style: primary أزرق | success أخضر | danger أحمر."""
    btn = {"text": text}
    if url:
        btn["url"] = url
    else:
        btn["callback_data"] = data
    if style:
        btn["style"] = style
    slot = k or (data.split(":")[0] if data else "dev")
    eid = db["emoji"].get(slot) or db["emoji"].get("*")
    if eid:
        btn["icon_custom_emoji_id"] = str(eid)
    return btn


def kb(rows):
    return {"inline_keyboard": rows}


def back_kb(extra=None, to="home"):
    return kb((extra or []) + [[b("رجوع", to, style="primary")]])


async def show(chat, text, markup=None, old=None, photo=True, kind=None, editable=True):
    """يعرض شاشة. نعدّل الرسالة القديمة في مكانها إن أمكن (editable)، وإلا نرسل جديدة ثم نحذف القديمة."""
    text = deco(text, kind)
    page = dict(chat_id=chat, parse_mode="HTML", reply_markup=markup)
    use_photo = photo and db["photo"] and len(text) < 1000
    if old and editable and not use_photo:
        d = await api("editMessageText", message_id=old, text=text,
                      disable_web_page_preview=True, **page)
        if d.get("ok") or "not modified" in str(d.get("description", "")):
            return
    sent = {"ok": False}
    if use_photo:
        sent = await api("sendPhoto", photo=db["photo"], caption=text, **page)
    if not sent.get("ok"):
        sent = await api("sendMessage", text=text, disable_web_page_preview=True, **page)
    if sent.get("ok") and old:
        await api("deleteMessage", chat_id=chat, message_id=old)


# ------------------------------------------------------------------ لوحة المستخدم
def home_kb(uid):
    rows = [[b("الأماكن المراقبة", "chats", style="primary"),
             b("إضافة كروب أو قناة", "addchat", style="success")],
            [b("كروب الإشعارات", "notify", style="primary"),
             b("طريقة الاستخدام", "guide", style="success")]]
    music_on = uid not in db["music"]["user_off"]
    rows.append([b("الميوزك: مفعّل" if music_on else "الميوزك: ملغى", "mutog",
                   style="success" if music_on else "danger")])
    last = [b("اشتراكي", "me", style="primary")]
    if is_dev(uid):
        last.append(b("لوحة المطور", "devpanel", style="success"))
    else:
        last.append(b("مراسلة المطور", url=DEV_URL, style="success"))
    return kb(rows + [last])


def home_text(uid):
    sub = sub_of(uid)
    notify_to = db["unotify"].get(str(uid))
    notify_line = f"<code>{notify_to}</code>" if notify_to else "غير محدد (لن تصلك أي إشعارات)"
    if is_dev(uid):
        kind, remaining, limit = "مطور", "غير محدود", "غير محدود"
    else:
        kind = "VIP" if is_vip(uid) else "عادي"
        remaining, limit = f"{days_left(sub)} يوم", str(limit_of(uid))
    if uid in db["music"]["user_off"]:
        music_line = "ملغى"
    else:
        music_line = "مفعّل" if db["music"]["enabled"] else "متوقف من المطور"
    return (f"<b>لوحة المستخدم</b>\n\n"
            f"نوع الحساب: <b>{kind}</b>\n"
            f"المتبقي: <b>{remaining}</b>\n"
            f"الأماكن المضافة: <b>{len(my_chats(uid))}</b> من <b>{limit}</b>\n"
            f"كروب الإشعارات: {notify_line}\n"
            f"الميوزك: <b>{music_line}</b>")


async def panel(chat, uid, old=None):
    await show(chat, home_text(uid), home_kb(uid), old, kind="panel")


# ------------------------------------------------------------------ لوحة المطور
def vip_count():
    return sum(1 for k, s in db["subs"].items() if sub_of(int(k)) and s.get("vip"))


def dev_kb(uid):
    if db["session"]:
        rows = [[b("حالة الحساب المساعد", "acc", style="primary"),
                 b("تسجيل خروج", "logout", style="danger")]]
    else:
        rows = [[b("تسجيل الدخول للحساب المساعد", "login", style="success")]]
    rows += [
        [b(f"الأماكن المراقبة ({len(db['chats'])})", "allchats", style="primary"),
         b("طريقة الاستخدام", "guide:dev", style="success")],
        [b("تغيير صورة الرد", "photo", style="success"), b("الإحصائيات", "stats", style="primary")],
        [b("إحصائيات المستخدمين", "ust:0", style="primary")],
        [b("اشتراك مستخدم", "adduser", style="success"), b("حذف مشترك", "deluser", style="danger")],
        [b("إذاعة شاملة", "bcast", style="success"), b("قسم الميوزك", "dm", style="primary")],
        [b("ايموجي الأزرار", "emoji", style="primary"), b("ملصقات النصوص", "txtem", style="primary")],
    ]
    if uid == OWNER_ID:
        rows.append([b("إضافة مطور", "adddev", style="success"), b("حذف مطور", "deldev", style="danger")])
    rows.append([b("إيقاف المراقبة" if db["enabled"] else "تشغيل المراقبة", "toggle",
                   style="danger" if db["enabled"] else "success")])
    rows.append([b("رجوع", "home", style="primary")])
    return kb(rows)


def dev_text():
    return (f"<b>لوحة تحكم المطور</b>\n\n"
            f"الحساب المساعد: {'مسجّل' if db['session'] else 'غير مسجّل'}\n"
            f"الأماكن المراقبة: <b>{len(db['chats'])}</b> (فيها محادثة مرئية الآن: <b>{len(active)}</b>)\n"
            f"المشتركون: <b>{len(db['subs'])}</b> (VIP: {vip_count()})\n"
            f"المطورون: <b>{len(db['devs']) + 1}</b>\n"
            f"المراقبة: {'شغّالة' if db['enabled'] else 'متوقفة'}\n"
            f"مستخدمو البوت: <b>{len(KNOWN)}</b>")


async def dev_panel(chat, uid, old=None):
    await show(chat, dev_text(), dev_kb(uid), old, kind="dev")


def dev_lines():
    lines = [f"- <code>{OWNER_ID}</code> (المطور الأساسي، لا يمكن حذفه)"]
    return "\n".join(lines + [f"- <code>{d}</code>" for d in db["devs"]])


def sub_lines():
    lines = [f"- <code>{k}</code> {person_tag(k)}| {'VIP' if s.get('vip') else 'عادي'} | {days_left(s)} يوم"
             for k, s in db["subs"].items() if sub_of(int(k))]
    if len(lines) > 40:
        lines = lines[:40] + [f"... و{len(lines) - 40} مشتركًا آخر"]
    return "\n".join(lines) or "لا يوجد مشتركون"


# ------------------------------------------------------------------ الأماكن
# النطاق (scope) يحدد من أين جاء المستخدم ليعرف زر الرجوع وجهته:
#   m = قائمته الشخصية | a<رقم> = كل الأماكن (للمطور) | l<رقم> = النشطة الآن فقط
def parse_scope(scope):
    mode = scope[:1] or "m"
    num = scope[1:]
    return mode, int(num) if num.isdigit() else 0


def back_target(scope):
    return "chats" if scope.startswith("m") else f"allchats:{scope}"


def cb_target(data):
    """يفصل «action:chat_id:scope» إلى (chat_id, scope)."""
    parts = data.split(":")
    return int(parts[1]), (parts[2] if len(parts) > 2 else "m")


def place_link(ch):
    if ch.get("username"):
        return f"https://t.me/{ch['username']}"
    raw = str(ch["id"])
    if raw.startswith("-100"):
        return f"https://t.me/c/{raw[4:]}/1"
    return None


def online_members(cid):
    session = live_calls.get(active.get(cid))
    if not session:
        return []
    return sorted(((k, p) for k, p in session["parts"].items() if p["cur"]),
                  key=lambda kv: kv[1]["cur"])


def chats_view(uid):
    rows = [[b(c["title"][:32] or str(c["id"]), f"chat:{c['id']}:m",
               style="success" if c["id"] in active else "primary")] for c in my_chats(uid)]
    rows.append([b("إضافة كروب أو قناة", "addchat", style="success")])
    count = "غير محدود" if is_dev(uid) else f"{len(my_chats(uid))} من {limit_of(uid)}"
    text = (f"<b>الأماكن المراقبة</b>\nالمضاف: {count}\n"
            "اضغط على المكان لإدارته. الأخضر فيه محادثة مرئية قائمة الآن.\n"
            "يجب أن يكون الحساب المساعد عضوًا فيه.")
    return text, back_kb(rows, "home")


def places_view(scope):
    """قائمة كل الأماكن للمطور: صفحات، وفلتر للنشطة الآن."""
    mode, page = parse_scope(scope)
    items = sorted(db["chats"], key=lambda c: (c["id"] not in active, c["title"].lower()))
    if mode == "l":
        items = [c for c in items if c["id"] in active]
    pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = min(page, pages - 1)
    chunk = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    rows = [[b(c["title"][:32] or str(c["id"]), f"chat:{c['id']}:{mode}{page}",
               style="success" if c["id"] in active else "primary")] for c in chunk]
    nav = []
    if page > 0:
        nav.append(b("السابق", f"allchats:{mode}{page - 1}", style="primary"))
    if page < pages - 1:
        nav.append(b("التالي", f"allchats:{mode}{page + 1}", style="primary"))
    if nav:
        rows.append(nav)
    if mode == "l":
        rows.append([b("عرض كل الأماكن", "allchats:a0", style="success")])
    else:
        rows.append([b("النشطة الآن فقط", "allchats:l0", style="success")])

    head = "الأماكن النشطة الآن" if mode == "l" else "كل الأماكن المراقبة"
    text = (f"<b>{head}</b>\n"
            f"العدد: <b>{len(items)}</b> | الصفحة {page + 1} من {pages}\n"
            f"فيها محادثة مرئية الآن: <b>{len(active)}</b> من <b>{len(db['chats'])}</b>\n\n"
            "الأخضر فيه محادثة قائمة الآن. اضغط على أي مكان لتدخل عليه وتشوف تفاصيله والمتصلين فيه.")
    if not chunk:
        text += "\n\nلا توجد أماكن لعرضها."
    return text, back_kb(rows, "devpanel")


def chat_view(ch, scope="m"):
    cid = ch["id"]
    live = cid in active
    lines = [f"<b>{esc(ch['title'])}</b>", "",
             field("id", "الايدي", f"<code>{cid}</code>"),
             field("owner", "المالك", f"<code>{ch.get('owner')}</code>"),
             f"المحادثة المرئية: {'قائمة الآن' if live else 'لا توجد'}"]
    if live:
        lines.append(f"المتصلون الآن: <b>{len(online_members(cid))}</b>")
    lines += [f"الميكروفون للصاعدين: {'مسموح' if ch.get('mic') else 'حسب إعداد تيليجرام'}",
              f"الفيديو: {'محظور' if ch.get('novideo') else 'مسموح'}",
              f"حظر القنوات الصاعدة للمحادثة: {'مفعّل' if ch.get('ban_channels', True) else 'متوقف'}"]
    if ch.get("added"):
        lines.append(field("time", "أُضيف", fmt(datetime.fromtimestamp(ch["added"], timezone.utc))))

    tail = f"{cid}:{scope}"
    rows = [[b("تشغيل المحادثة المرئية", f"vcstart:{tail}", style="success"),
             b("إنهاء المحادثة المرئية", f"vcend:{tail}", style="danger")],
            [b("الميكروفون: مسموح" if ch.get("mic") else "السماح بالميكروفون", f"chmic:{tail}",
               style="success" if ch.get("mic") else "primary"),
             b("الفيديو: محظور" if ch.get("novideo") else "حظر الفيديو", f"chvid:{tail}",
               style="danger" if ch.get("novideo") else "primary")],
            [b("حظر القنوات: مفعّل" if ch.get("ban_channels", True) else "حظر القنوات: متوقف", f"chban:{tail}",
               style="success" if ch.get("ban_channels", True) else "danger")]]
    row = [b("المتصلون الآن", f"live:{tail}", style="primary")]
    link = place_link(ch)
    if link:
        row.append(b("فتح المكان", url=link, style="success", k="open"))
    rows += [row, [b("حذف هذا المكان", f"delchat:{tail}", style="danger")],
             [b("رجوع", back_target(scope), style="primary")]]
    return "\n".join(lines), kb(rows)


def live_view(ch, scope):
    members = online_members(ch["id"])
    now = datetime.now(timezone.utc)
    if ch["id"] not in active:
        body = "لا توجد محادثة مرئية قائمة الآن."
    elif not members:
        body = "لا أحد في المحادثة غير الحساب المساعد."
    else:
        shown = []
        for i, (key, p) in enumerate(members[:40], 1):
            who_ = mention(key, p["name"]) if isinstance(key, int) else esc(p["name"])
            shown.append(f"{i}. {who_} - منذ {dur((now - p['cur']).total_seconds())}")
        if len(members) > 40:
            shown.append(f"... و{len(members) - 40} آخرون")
        body = "\n".join(shown)
    text = f"<b>المتصلون الآن</b>\n{esc(ch['title'])}\n\n{body}"
    tail = f"{ch['id']}:{scope}"
    return text, kb([[b("تحديث", f"live:{tail}", style="success")],
                     [b("رجوع", f"chat:{tail}", style="primary")]])


# ------------------------------------------------------------------ طريقة الاستخدام
USER_GUIDE = (
    "<b>طريقة الاستخدام</b>\n\n"
    "1. اضغط «إضافة كروب أو قناة» وأرسل يوزر المكان أو ايديه أو رابط الدعوة.\n"
    "2. تأكد أن الحساب المساعد عضو في المكان. ولكي يتحكم بالمحادثة المرئية اجعله مشرفًا بصلاحية إدارة المحادثات المرئية.\n"
    "3. اضغط «كروب الإشعارات» وحدد الكروب أو القناة التي تصلك فيها الإشعارات. هذه الخطوة إلزامية، بدونها لا يصل شيء. "
    "في القناة يجب أن يكون البوت مشرفًا بصلاحية النشر. وفي الكروب يكفي أن تضيف البوت وترسل فيه /setgroup.\n"
    "4. عند فتح محادثة مرئية يدخل الحساب المساعد فورًا ويبدأ الرصد.\n"
    "5. يصلك إشعار عند كل صعود وخروج، وعند انتهاء المحادثة يصلك تقرير كامل.\n"
    "6. زر «حظر المستخدم» تحت الإشعار يعمل فقط لمشرفي المجموعة أصحاب صلاحية الحظر.\n"
    "7. من «الأماكن المراقبة» اضغط على أي مكان لتشغيل المحادثة المرئية أو إنهائها، "
    "والسماح بالميكروفون، وحظر الفيديو، ورؤية المتصلين الآن، وحذف المكان.\n"
    "8. من «اشتراكي» تعرف نوع اشتراكك وأيامه. مشترك VIP يضيف حتى 3 أماكن.\n\n"
    "<b>الموسيقى</b> (اكتبها في المجموعة نفسها):\n"
    "- <code>شغل اسم الأغنية</code>: تُشغَّل في المحادثة المرئية، وتُفتح تلقائيًا إن لم تكن قائمة.\n"
    "- <code>شغل اسم الأغنية مع الفيديو</code>: بالصورة والصوت.\n"
    "- <code>يوت اسم الأغنية</code>: يحمّلها ويرسلها كملف صوتي.\n"
    "- التحكم: تخطي، وقف (إيقاف مؤقت)، استكمال، ايقاف (إيقاف الموسيقى)، تكرار، قائمة، الان، تقديم 30، رجوع 30، صوت 80.\n"
    "- عند بدء التشغيل تظهر لوحة أزرار للتحكم مع شريط تقدم يتحدث تلقائيًا.\n"
    "- من لوحتك زر «الميوزك» لتفعيل الميوزك أو إلغائه في أماكنك.\n\n"
    "<b>حظر القنوات</b>\n"
    "أي قناة تصعد إلى المحادثة المرئية في مجموعتك تُحظر تلقائيًا (بلا استثناء إلا المكان نفسه) ويصلك إشعار يظهر فيه مالك المجموعة. "
    "يلزم أن يكون الحساب المساعد مشرفًا بصلاحية حظر الأعضاء. يمكنك إيقاف ذلك لكل مكان من صفحته."
)

DEV_GUIDE = (
    "<b>دليل المطور</b>\n\n"
    "1. تسجيل الدخول: اضغط الزر وأرسل رقم الحساب المساعد، ثم الكود بينه شرطات مثل 1-2-3-4-5، ثم كلمة المرور إن وجدت.\n"
    "2. الأماكن المراقبة: قائمة بكل الأماكن في صفحات. الأخضر فيه محادثة مرئية الآن. "
    "اضغط على المكان لتدخل عليه: تفاصيله، المتصلون الآن، تشغيل وإنهاء المحادثة، الميكروفون والفيديو، "
    "وزر «فتح المكان» ينقلك إليه مباشرة.\n"
    "3. اشتراك مستخدم: أرسل «الايدي عدد_الأيام»، وأضف v في آخره ليصبح VIP.\n"
    "4. حذف مشترك: يحذف اشتراكه وكل أماكنه.\n"
    "5. الإذاعة الشاملة: اختر الفئة ثم أرسل الرسالة بأي تنسيق وأكّد الإرسال.\n"
    "6. ايموجي الأزرار: اختر الزر ثم أرسل الايموجي المميز نفسه، أو أرسل 0 لإزالته.\n"
    "7. ملصقات النصوص: اختر نوع الرسالة ليظهر الملصق في أولها، أو اختر حقلًا مثل الاسم أو الايدي "
    "ليظهر قبل عنوانه في كل الإشعارات، ثم أرسل الايموجي المميز. زر المعاينة يريك النتيجة فورًا. "
    "إذا رفض تيليجرام الملصق يرسل البوت الرسالة بدونه تلقائيًا.\n"
    "8. إضافة وحذف مطور: للمطور الأساسي فقط.\n"
    "9. إيقاف المراقبة: يوقف الرصد ويخرج الحساب المساعد من كل المحادثات.\n"
    "10. قسم الميوزك: تشغيل وإيقاف القسم، الصلاحيات (الجميع، الطلب للجميع والتحكم للمشرفين، المشرفون فقط)، "
    "أقصى مدة للمقطع، طول الطابور، حجم الكاش، الصوت، الفيديو، أمر يوت، موقع اللوحة، قناة التحديثات، "
    "تشغيل القسم في أماكن محددة فقط، مسح الكاش، تحديث yt-dlp. "
    "إذا حجب يوتيوب السيرفر يتوقف البوت عن طلبه (15 دقيقة ثم أكثر إن تكرر) ويستخدم ساوندكلاود تلقائيًا، "
    "ويظهر حال يوتيوب في لوحة الميوزك مع زر فحص وزر إعادة المحاولة. ويمكنك إضافة بروكسيات أو كوكيز اختيارية. وفيه أيضًا: صورة لوحة الميوزك مع درجة التشويش، "
    "ملصق الانتظار الذي يردّ به البوت أثناء البحث والتحميل، وسرعة تحديث شريط التقدم.\n"
    "11. إحصائيات المستخدمين: كل من استخدم البوت أو طلب موسيقى، مع يوزراتهم وعدد طلباتهم."
)


# ------------------------------------------------------------------ لوحات الايموجي المميز
def slot_pairs(slots):
    items = list(slots.items())
    return [[b(label, f"emk:{key}", k=key, style="primary") for key, label in items[i:i + 2]]
            for i in range(0, len(items), 2)]


def applied_lines(keys):
    lines = [f"- {SLOT_NAMES.get(k, k)}: <code>{db['emoji'][k]}</code>" for k in keys]
    return "\n".join(lines) or "لا يوجد"


def emoji_view():
    rows = slot_pairs(BUTTON_SLOTS)
    rows.append([b("حذف ايموجي الأزرار", "emojiclear", style="danger")])
    button_keys = [k for k in db["emoji"] if not k.startswith(("t:", "f:"))]
    text = ("<b>ايموجي الأزرار</b>\n\n"
            "اختر الزر ثم أرسل الايموجي المميز نفسه ويظهر قبل نص الزر فورًا.\n"
            "أرسل 0 لإزالته. «كل الأزرار» يطبَّق على أي زر ليس له ايموجي خاص.\n\n"
            f"المطبّق حاليًا:\n{applied_lines(button_keys)}")
    return text, back_kb(rows, "devpanel")


def text_stickers_view():
    rows = slot_pairs(TEXT_SLOTS) + slot_pairs(FIELD_SLOTS)
    text_keys = [k for k in db["emoji"] if k.startswith(("t:", "f:"))]
    rows.append([b("معاينة", "preview", style="success"),
                 b("حذف ملصقات النصوص", "txtclear", style="danger")])
    text = ("<b>ملصقات النصوص</b>\n\n"
            "ملصقات مميزة تظهر داخل رسائل البوت:\n"
            "- أنواع الرسائل: يظهر الملصق في أول الرسالة (إشعار الصعود، الخروج، الحظر، التقرير...).\n"
            "- الحقول: يظهر الملصق قبل عنوان الحقل (الاسم، اليوزر، الايدي، الوقت...).\n"
            "«كل الرسائل» و«كل عناوين الحقول» يطبَّقان على ما ليس له ملصق خاص.\n\n"
            "اختر الخانة ثم أرسل الايموجي المميز نفسه، أو 0 لإزالته.\n\n"
            f"المطبّق حاليًا:\n{applied_lines(text_keys)}")
    return text, back_kb(rows, "devpanel")


# ------------------------------------------------------------------ نصوص الإشعارات
def new_part(name, username, joined_at):
    return {"name": name, "un": username, "joins": 0, "leaves": 0, "total": 0.0,
            "first": joined_at, "last": None, "cur": joined_at}


def update_peak(session):
    session["peak"] = max(session["peak"], sum(1 for p in session["parts"].values() if p["cur"]))


def user_head(title, link, uname, uid):
    return "\n".join([field("place", "المكان", f"<b>{esc(title)}</b>"),
                      field("name", "الاسم", link),
                      field("user", "اليوزر", esc(uname)),
                      field("id", "الايدي", f"<code>{uid}</code>")])


def join_text(head, joined_at):
    return ("<b>صعد إلى المحادثة المرئية</b>\n" + head + "\n"
            + field("time", "وقت الصعود", fmt(joined_at)))


def leave_text(head, left_at, joined_at=None):
    lines = ["<b>خرج من المحادثة المرئية</b>", head, field("time", "وقت الخروج", fmt(left_at))]
    if joined_at:
        lines += [field("time", "وقت الصعود", fmt(joined_at)),
                  field("duration", "المدة", dur((left_at - joined_at).total_seconds()))]
    return "\n".join(lines)


def ban_text(title, tags, actor, target, unban=False):
    """actor و target: (id, name)"""
    verb = "فك حظر" if unban else "حظر"
    top = "\n".join(tags) + "\n\n" if tags else ""
    return (top + f"<b>إشعار {verb}</b>\n\n"
            f"قام المشرف {mention(*actor)} بـ{verb} {mention(*target)}\n"
            + field("id", "ايدي المنفّذ", f"<code>{actor[0]}</code>") + "\n"
            + field("id", "ايدي المستهدف", f"<code>{target[0]}</code>") + "\n"
            + field("place", "المكان", f"<b>{esc(title)}</b>") + "\n"
            + field("time", "الوقت", f"{fmt(None)} (توقيت دمشق)"))


def sample_notices():
    """نماذج للمعاينة بنفس الدوال الحقيقية، فيظهر الملصق كما سيظهر فعلًا."""
    now = datetime.now(timezone.utc)
    head = user_head("مجموعة تجريبية", mention(1, "اسم تجريبي"), "@username", 123456789)
    return [
        ("join", join_text(head, now - timedelta(minutes=5))),
        ("leave", leave_text(head, now, now - timedelta(minutes=5))),
        ("ban", ban_text("مجموعة تجريبية", [f"مالك المجموعة: {mention(2, 'المالك')}"],
                         (3, "المشرف"), (123456789, "اسم تجريبي"))),
    ]


# ------------------------------------------------------------------ الإشعارات
def dest_of(ch):
    """وجهة الإشعارات: ما حدده مالك المكان. إن لم يحدد شيئًا لا يصل شيء."""
    return db["unotify"].get(str(ch.get("owner", OWNER_ID)))


def title_of(cid):
    ch = chat_of(cid)
    return ch["title"] if ch else str(cid)


_pace_locks = {}
_pace_last = {}


async def send_paced(dst, **params):
    """يرسل إلى dst بالترتيب وبفاصل صغير، فلا تضيع الإشعارات عند ازدحام الصاعدين."""
    lock = _pace_locks.setdefault(dst, asyncio.Lock())
    async with lock:
        wait = NOTIFY_PACE - (time.monotonic() - _pace_last.get(dst, 0))
        if wait > 0:
            await asyncio.sleep(wait)
        data = await api("sendMessage", chat_id=dst, **params)
        _pace_last[dst] = time.monotonic()
        return data


async def notify(cid, text, uid=None, kind=None):
    ch = chat_of(cid)
    if not ch or not dest_of(ch):
        return
    markup = kb([[b("حظر المستخدم", f"ban:{cid}:{uid}", style="danger")]]) if uid else None
    await send_paced(dest_of(ch), text=deco(text, kind), parse_mode="HTML",
                     reply_markup=markup, disable_web_page_preview=True)


async def name_of(uid):
    if UB:
        try:
            return utils.get_display_name(await UB.get_entity(uid))
        except Exception:
            pass
    return str(uid)


async def group_creator(cid):
    """منشئ المجموعة الحقيقي (مالكها في تيليجرام) أو None."""
    d = await api("getChatAdministrators", chat_id=cid)
    for admin in d.get("result") or []:
        user = admin.get("user")
        if admin.get("status") == "creator" and user and not admin.get("is_anonymous"):
            name = " ".join(x for x in (user.get("first_name"), user.get("last_name")) if x)
            return user["id"], name or str(user["id"])
    if UB:
        try:
            async for u in UB.iter_participants(cid, filter=types.ChannelParticipantsAdmins):
                if isinstance(getattr(u, "participant", None), types.ChannelParticipantCreator):
                    return u.id, utils.get_display_name(u)
        except Exception:
            pass
    return None


async def announce_ban(ch, actor_id, actor_name, target_id, target_name, reply=None, unban=False):
    owner = ch.get("owner", OWNER_ID)
    tags = []
    creator = await group_creator(ch["id"])
    if creator:
        tags.append(f"مالك المجموعة: {mention(*creator)}")
    if not creator or creator[0] != owner:
        tags.append(f"مسؤول المكان: {mention(owner, await name_of(owner))}")
    text = deco(ban_text(ch["title"], tags, (actor_id, actor_name), (target_id, target_name), unban), "ban")

    targets = [dest_of(ch)] if dest_of(ch) else []
    if owner != OWNER_ID and OWNER_ID not in targets:
        targets.append(OWNER_ID)       # نسخة للمطور الأساسي
    for dst in targets:
        params = dict(text=text, parse_mode="HTML", disable_web_page_preview=True)
        if reply and reply[0] == dst:
            params.update(reply_to_message_id=reply[1], allow_sending_without_reply=True)
        await send_paced(dst, **params)


# ------------------------------------------------------------------ مراقبة المشاركين
def peer_uid(peer):
    """(ايدي، هل هي قناة). المستخدم موجب، والقناة بصيغة -100... مثل بقية ايديات تيليجرام."""
    user_id = getattr(peer, "user_id", None)
    if user_id:
        return user_id, False
    channel_id = getattr(peer, "channel_id", None)
    if channel_id:
        return -1000000000000 - channel_id, True
    return None, False


async def who(peer, call=None):
    """(الاسم، اليوزر، الايدي، قناة؟) لصاحب peer، مع محاولة ثانية من قائمة المشاركين."""
    uid, is_channel = peer_uid(peer)
    entity = None
    try:
        entity = await UB.get_entity(peer)
    except Exception:
        if call and uid:
            try:
                res = await UB(functions.phone.GetGroupParticipantsRequest(
                    call=call, ids=[], sources=[], offset="", limit=200))
                pool, want = (res.chats, peer.channel_id) if is_channel else (res.users, uid)
                entity = next((e for e in pool if e.id == want), None)
            except Exception:
                pass
    if entity:
        return utils.get_display_name(entity), getattr(entity, "username", None), uid, is_channel, entity
    return str(uid or "مجهول"), None, uid, is_channel, None


async def announce_channel_ban(ch, name, username, chan_id, ok, err=None):
    owner = ch.get("owner", OWNER_ID)
    tags = []
    creator = await group_creator(ch["id"])
    if creator:
        tags.append(f"مالك المجموعة: {mention(*creator)}")
    if not creator or creator[0] != owner:
        tags.append(f"مسؤول المكان: {mention(owner, await name_of(owner))}")
    link = f'<a href="https://t.me/{esc(username)}">{esc(name)}</a>' if username else f"<b>{esc(name)}</b>"
    lines = tags + ["", "<b>حظر قناة صعدت إلى المحادثة المرئية</b>" if ok
                    else "<b>قناة صعدت إلى المحادثة المرئية وتعذر حظرها</b>",
                    field("name", "القناة", link),
                    field("user", "اليوزر", esc(f"@{username}") if username else "لا يوجد"),
                    field("id", "الايدي", f"<code>{chan_id}</code>"),
                    field("place", "المكان", f"<b>{esc(ch['title'])}</b>"),
                    field("time", "الوقت", f"{fmt(None)} (توقيت دمشق)")]
    if not ok:
        lines.append("السبب: الحساب المساعد يحتاج أن يكون مشرفًا بصلاحية حظر الأعضاء في المجموعة."
                     + (f"\n<code>{esc(err)}</code>" if err else ""))
    text = deco("\n".join(lines).strip(), "ban")
    markup = kb([[b("فك الحظر", f"unban:{ch['id']}:{chan_id}", style="success")]]) if ok else None

    targets = [dest_of(ch)] if dest_of(ch) else []
    if owner != OWNER_ID and OWNER_ID not in targets:
        targets.append(OWNER_ID)
    for dst in targets:
        await send_paced(dst, text=text, parse_mode="HTML", disable_web_page_preview=True,
                         **({"reply_markup": markup} if markup and dst == dest_of(ch) else {}))
    if creator and creator[0] not in targets:     # محاولة إخبار مالك المجموعة مباشرة (تنجح إن فتح البوت)
        await api("sendMessage", chat_id=creator[0], text=text, parse_mode="HTML",
                  disable_web_page_preview=True)


async def channel_input_peer(peer, entity=None):
    """InputPeer للقناة: من ذاكرة Telethon، وإلا من الكيان الذي جلبناه من قائمة المشاركين."""
    try:
        return await UB.get_input_entity(peer)
    except Exception:
        if entity is not None:
            return utils.get_input_peer(entity)
        raise


async def handle_channel_join(ch, call, p, name, username, chan_id, session, entity=None):
    """أي قناة تصعد للمحادثة المرئية تُحظر فورًا ويُخبَر المالك. يرجع True إن عولجت فلا تُسجَّل كمشارك.
    الاستثناء الوحيد: المكان نفسه (المشرف المجهول يظهر باسم المجموعة)."""
    cid = ch["id"]
    if not ch.get("ban_channels", True) or chan_id == cid:
        return False
    if chan_id in session["chan_seen"]:
        return True
    now = time.monotonic()
    if now - session["chan_retry"].get(chan_id, 0) < 60:
        return True                      # فشلت قبل قليل: لا نكرر المحاولة كل ثانية
    session["chan_retry"][chan_id] = now
    ok, err = True, None
    try:
        peer = await channel_input_peer(p.peer, entity)
        await UB(functions.channels.EditBannedRequest(
            channel=await UB.get_input_entity(cid), participant=peer,
            banned_rights=types.ChatBannedRights(until_date=None, view_messages=True)))
        db["stats"]["bans"] += 1
        save_soon()
        try:    # نكتمها من المحادثة المرئية أيضًا
            await UB(functions.phone.EditGroupCallParticipantRequest(call=call, participant=peer, muted=True))
        except Exception:
            pass
    except Exception as e:
        ok, err = False, short(e)
        log.warning("ban channel %s: %s", chan_id, e)
    if ok:
        session["chan_seen"].add(chan_id)
    if ok or chan_id not in session["chan_notified"]:       # الفشل يُبلَّغ عنه مرة واحدة فقط
        session["chan_notified"].add(chan_id)
        await announce_channel_ban(ch, name, username, chan_id, ok, err)
    return True


async def sweep_channels(ch):
    """فحص احتياطي كل دورة: يحظر أي قناة موجودة في المحادثة فاتنا حدث صعودها."""
    cid = ch["id"]
    if not ch.get("ban_channels", True) or cid not in active:
        return
    session = live_calls.get(active[cid])
    call = await get_call(cid) if session is not None else None
    if not call:
        return
    res = await UB(functions.phone.GetGroupParticipantsRequest(
        call=call, ids=[], sources=[], offset="", limit=200))
    chats = {c.id: c for c in getattr(res, "chats", [])}
    for p in res.participants:
        raw = getattr(p.peer, "channel_id", None)
        chan_id = -1000000000000 - raw if raw else None
        if not raw or getattr(p, "left", False) or chan_id in session["chan_seen"]:
            continue
        ent = chats.get(raw)
        await handle_channel_join(ch, call, p, utils.get_display_name(ent) if ent else str(raw),
                                  getattr(ent, "username", None), chan_id, session, ent)


async def on_parts(ev):
    try:
        cid = call_chat.get(ev.call.id)
        session = live_calls.get(ev.call.id)
        if cid is None or session is None or not db["enabled"]:
            return
        now = datetime.now(timezone.utc)
        for p in ev.participants:
            name, username, uid, is_channel, entity = await who(p.peer, ev.call)
            if uid == ME_ID:
                continue
            if is_channel:
                if uid in session["chan_seen"]:
                    continue                       # قناة حظرناها: لا إشعارات صعود وخروج لها
                place = chat_of(cid)
                if not p.left and place and await handle_channel_join(place, ev.call, p, name, username, uid, session, entity):
                    continue
            key = uid if uid else str(p.peer)
            part = session["parts"].get(key)
            link = mention(uid, name) if uid else esc(name)
            head = user_head(title_of(cid), link, f"@{username}" if username else "لا يوجد", uid)

            if p.left:
                if part is None:
                    part = session["parts"][key] = new_part(name, username, None)
                    part["cur"] = None
                joined_at = part["cur"]
                if joined_at:
                    part["total"] += max(0, (now - joined_at).total_seconds())
                part["cur"] = None
                part["leaves"] += 1
                part["last"] = now
                db["stats"]["leaves"] += 1
                save_soon()
                await notify(cid, leave_text(head, now, joined_at), uid, kind="leave")
            else:
                chat = chat_of(cid)
                if chat:
                    await apply_policy(ev.call, p, chat, uid, name)
                if part is not None and part["cur"] is not None:
                    continue    # تحديث بسيط (كتم أو فتح مايك) وليس صعودًا
                joined_at = p.date or now
                if part is None:
                    part = session["parts"][key] = new_part(name, username, joined_at)
                else:
                    part["cur"] = joined_at
                if p.just_joined:
                    part["joins"] += 1
                    db["stats"]["joins"] += 1
                    save_soon()
                    await notify(cid, join_text(head, joined_at), uid, kind="join")
            update_peak(session)
    except Exception as e:
        log.exception("on_parts: %s", e)


async def on_participant(ev):
    """يكشف من حظر عضوًا مباشرة من المجموعة أو القناة (يلزم أن يكون الحساب المساعد مشرفًا)."""
    try:
        if not db["enabled"]:
            return
        new, old = ev.new_participant, ev.prev_participant
        if not (isinstance(new, types.ChannelParticipantBanned) and new.banned_rights.view_messages):
            return
        if isinstance(old, types.ChannelParticipantBanned) and old.banned_rights.view_messages:
            return
        actor = getattr(new, "kicked_by", None) or ev.actor_id
        if not actor or actor in (ME_ID, BOT_ID):
            return
        cid = utils.get_peer_id(types.PeerChannel(ev.channel_id))
        ch = chat_of(cid)
        if not ch:
            return
        db["stats"]["bans"] += 1
        save_soon()
        await announce_ban(ch, actor, await name_of(actor), ev.user_id, await name_of(ev.user_id))
    except Exception as e:
        log.exception("on_participant: %s", e)


# ------------------------------------------------------------------ التقرير النهائي
def split_blocks(head, blocks, limit=3500):
    """يجمع الكتل في رسائل لا يتجاوز طول الواحدة limit حرفًا."""
    chunks, current = [], head
    for block in blocks:
        if len(current) + len(block) + 2 > limit:
            chunks.append(current)
            current = block
        else:
            current += "\n\n" + block
    chunks.append(current)
    return chunks


async def send_report(session):
    end = datetime.now(timezone.utc)
    for p in session["parts"].values():
        if p["cur"]:
            p["total"] += max(0, (end - p["cur"]).total_seconds())
            p["last"] = end
            p["cur"] = None
    parts = sorted(session["parts"].items(), key=lambda kv: -kv[1]["total"])
    joins = sum(p["joins"] for _, p in parts)
    leaves = sum(p["leaves"] for _, p in parts)
    average = sum(p["total"] for _, p in parts) / len(parts) if parts else 0

    head = "\n".join([
        "<b>تقرير المحادثة المرئية</b>", "",
        field("place", "المكان", f"<b>{esc(session['title'])}</b>"),
        field("time", "بداية الرصد", fmt(session["start"])),
        field("time", "النهاية", fmt(end)),
        field("duration", "المدة الكلية", dur((end - session["start"]).total_seconds())),
        f"عدد المشاركين: <b>{len(parts)}</b>",
        f"أعلى عدد متزامن: <b>{session['peak']}</b>",
        f"مرات الصعود: <b>{joins}</b>",
        f"مرات الخروج: <b>{leaves}</b>",
        f"متوسط بقاء المشارك: {dur(average)}",
        "التوقيت: دمشق",
    ])
    blocks = ["لم يشارك أحد غير الحساب المساعد."] if not parts else []
    for i, (key, p) in enumerate(parts, 1):
        link = mention(key, p["name"]) if isinstance(key, int) else esc(p["name"])
        username = f"@{p['un']}" if p["un"] else "لا يوجد"
        blocks.append(f"<b>{i}.</b> {link}\n"
                      f"اليوزر: {esc(username)}\nالايدي: <code>{key}</code>\n"
                      f"مرات الدخول: {p['joins']} | مرات الخروج: {p['leaves']}\n"
                      f"إجمالي البقاء: {dur(p['total'])}\n"
                      f"أول ظهور: {fmt(p['first'])}\n"
                      f"آخر خروج: {fmt(p['last'])}")

    ch = chat_of(session["chat"])
    if not ch or not dest_of(ch):
        return
    for n, chunk in enumerate(split_blocks(head, blocks)):
        await api("sendMessage", chat_id=dest_of(ch), text=deco(chunk, "report") if n == 0 else chunk,
                  parse_mode="HTML", disable_web_page_preview=True)
        await asyncio.sleep(0.4)


# ------------------------------------------------------------------ سياسة الميكروفون والفيديو
async def apply_policy(call, p, ch, uid, name):
    """يفتح المايك للصاعدين الجدد ويوقف الفيديو والمشاركة حسب إعدادات المكان."""
    if not uid or uid in (ME_ID, ch.get("owner")) or is_dev(uid):
        return
    cid = ch["id"]
    try:
        if ch.get("novideo") and (getattr(p, "video", None) or getattr(p, "presentation", None)):
            await UB(functions.phone.EditGroupCallParticipantRequest(
                call=call, participant=await UB.get_input_entity(p.peer),
                video_stopped=True, presentation_paused=True))
            key, now = (cid, uid), now_ts()
            if now - video_warned.get(key, 0) > 30:
                video_warned[key] = now
                text = "\n".join([
                    "<b>تم إيقاف الفيديو</b>",
                    field("place", "المكان", f"<b>{esc(ch['title'])}</b>"),
                    field("name", "المستخدم", mention(uid, name)),
                    field("id", "الايدي", f"<code>{uid}</code>"),
                    field("time", "الوقت", fmt(None)),
                ])
                await notify(cid, text, uid, kind="video")
        if (ch.get("mic") and getattr(p, "just_joined", False) and getattr(p, "muted", False)
                and not getattr(p, "can_self_unmute", False)):
            await UB(functions.phone.EditGroupCallParticipantRequest(
                call=call, participant=await UB.get_input_entity(p.peer), muted=False))
    except Exception as e:
        log.warning("policy %s: %s", cid, e)


async def sweep_video(ch):
    """يوقف أي فيديو شغّال حاليًا (عند تفعيل حظر الفيديو)."""
    call = await get_call(ch["id"])
    if not call:
        return
    res = await UB(functions.phone.GetGroupParticipantsRequest(
        call=call, ids=[], sources=[], offset="", limit=200))
    users = {u.id: u for u in res.users}
    for p in res.participants:
        uid = getattr(p.peer, "user_id", None)
        user = users.get(uid)
        await apply_policy(call, p, ch, uid, utils.get_display_name(user) if user else str(uid))


# ------------------------------------------------------------------ المحادثات المرئية
def make_silence():
    """ملف صمت مدته 10 دقائق، يبقي الحساب المساعد داخل المحادثة دون صوت."""
    if os.path.exists(SILENCE_FILE):
        return
    with wave.open(SILENCE_FILE, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(1)
        w.setframerate(8000)
        w.writeframes(bytes([128]) * 8000 * 600)


async def join_call(chat_id):
    await CALLS.play(chat_id, MediaStream(SILENCE_FILE, AudioQuality.LOW,
                                          video_flags=MediaStream.Flags.IGNORE))


async def get_call(chat_id):
    entity = await UB.get_entity(chat_id)
    if isinstance(entity, types.Channel):
        full = await UB(functions.channels.GetFullChannelRequest(entity))
    else:
        full = await UB(functions.messages.GetFullChatRequest(chat_id=entity.id))
    return full.full_chat.call


async def leave(chat_id, report=False):
    call_id = active.pop(chat_id, None)
    session = None
    if call_id:
        call_chat.pop(call_id, None)
        session = live_calls.pop(call_id, None)
    await music_cleanup(chat_id)
    try:
        await CALLS.leave_call(chat_id)
    except Exception:
        pass
    if session and report:
        await send_report(session)


async def ensure_joined(ch, call=None):
    cid = ch["id"]
    async with locks.setdefault(cid, asyncio.Lock()):
        if call is None:
            call = await get_call(cid)
        if not call:
            if cid in active:
                await leave(cid, report=True)      # انتهت المحادثة
            return
        if active.get(cid) == call.id:
            return
        if cid in active:                           # محادثة جديدة بدل القديمة
            await leave(cid, report=True)

        now = datetime.now(timezone.utc)
        active[cid] = call.id
        call_chat[call.id] = cid
        session = {"chat": cid, "title": ch["title"], "start": now, "parts": {}, "peak": 0,
                   "chan_seen": set(), "chan_retry": {}, "chan_notified": set()}
        live_calls[call.id] = session
        try:
            await join_call(cid)
        except Exception:
            active.pop(cid, None)
            call_chat.pop(call.id, None)
            live_calls.pop(call.id, None)
            raise

        spawn(after_join(ch, call, session, now))


async def after_join(ch, call, session, joined_at):
    """ما بعد الدخول: تسجيل الموجودين وضبط المايك والإشعار. خارج القفل حتى لا يؤخر أي دخول."""
    cid = ch["id"]
    try:    # نسجل الموجودين مسبقًا بصمت (دون أن نكتب فوق من سبقتنا إليهم الأحداث)
        res = await UB(functions.phone.GetGroupParticipantsRequest(
            call=call, ids=[], sources=[], offset="", limit=200))
        users = {u.id: u for u in res.users}
        channels = {c.id: c for c in getattr(res, "chats", [])}
        for p in res.participants:
            raw_chan = getattr(p.peer, "channel_id", None)
            if raw_chan:                       # قناة كانت في المحادثة قبل دخولنا
                ent = channels.get(raw_chan)
                cname = utils.get_display_name(ent) if ent else str(raw_chan)
                cuser = getattr(ent, "username", None)
                cuid = -1000000000000 - raw_chan
                if not await handle_channel_join(ch, call, p, cname, cuser, cuid, session, ent) \
                        and cuid not in session["parts"]:
                    session["parts"][cuid] = new_part(cname, cuser, p.date or joined_at)   # مستثناة: تُسجَّل عادية
                continue
            uid = getattr(p.peer, "user_id", None)
            if uid and uid != ME_ID:
                user = users.get(uid)
                name = utils.get_display_name(user) if user else str(uid)
                if uid not in session["parts"]:
                    session["parts"][uid] = new_part(name, getattr(user, "username", None),
                                                     p.date or joined_at)
                await apply_policy(call, p, ch, uid, name)
        update_peak(session)
    except Exception as e:
        log.warning("participants: %s", e)

    if ch.get("mic"):
        try:
            await UB(functions.phone.ToggleGroupCallSettingsRequest(call=call, join_muted=False))
        except Exception as e:
            log.warning("join_muted: %s", e)

    text = "\n".join([
        "<b>الحساب المساعد دخل المحادثة المرئية</b>",
        field("place", "المكان", f"<b>{esc(ch['title'])}</b>"),
        f"الموجودون الآن: <b>{len(session['parts'])}</b>",
        field("time", "الوقت", fmt(joined_at)),
    ])
    await notify(cid, text, kind="call")


async def join_soon(ch):
    """دخول فوري بعد فتح المحادثة، مع إعادة المحاولة حتى تظهر في تيليجرام."""
    for _ in range(4):
        try:
            await ensure_joined(ch)
        except Exception as e:
            log.warning("join_soon %s: %s", ch["id"], e)
        if ch["id"] in active:
            return
        await asyncio.sleep(1.5)


async def on_group_call(ev):
    """يصل فور فتح أو إغلاق محادثة مرئية: دخول فوري وتقرير فوري."""
    try:
        call = ev.call
        if isinstance(call, types.GroupCallDiscarded):
            cid = call_chat.get(call.id)
            if cid:
                async with locks.setdefault(cid, asyncio.Lock()):
                    if active.get(cid) == call.id:
                        await leave(cid, report=True)
            return
        raw = getattr(ev, "chat_id", None)
        if raw is not None:
            candidates = (-1000000000000 - raw, -raw)
        elif getattr(ev, "peer", None) is not None:
            candidates = (utils.get_peer_id(ev.peer),)
        else:
            return
        ch = next((c for c in db["chats"] if c["id"] in candidates), None)
        if ch and db["enabled"] and UB and CALLS:
            try:    # دخول فوري بالبيانات القادمة مع التحديث دون طلب إضافي
                await ensure_joined(ch, types.InputGroupCall(id=call.id, access_hash=call.access_hash))
            except Exception as e:
                log.warning("fast join: %s", e)
                await join_soon(ch)
    except Exception as e:
        log.exception("on_group_call: %s", e)


async def vc_start(cid):
    peer = await UB.get_input_entity(cid)
    await UB(functions.phone.CreateGroupCallRequest(peer=peer, random_id=random.randint(1, 2 ** 31 - 1)))


async def vc_end(cid):
    call = await get_call(cid)
    if not call:
        raise Exception("لا توجد محادثة مرئية قائمة")
    await UB(functions.phone.DiscardGroupCallRequest(call=call))
    async with locks.setdefault(cid, asyncio.Lock()):
        if cid in active:
            await leave(cid, report=True)


# ------------------------------------------------------------------ الاشتراكات المنتهية وال��حص الدوري
async def check_expired():
    now = now_ts()
    for key, sub in list(db["subs"].items()):
        if sub["exp"] > now:
            continue
        uid = int(key)
        db["subs"].pop(key, None)
        gone = my_chats(uid)
        db["chats"] = [c for c in db["chats"] if c.get("owner") != uid]
        save()
        for ch in gone:
            await leave(ch["id"])
        await api("sendMessage", chat_id=uid, parse_mode="HTML",
                  text=deco("<b>انتهى اشتراكك</b>\nتم إيقاف مراقبة أماكنك. تواصل مع المطور للتجديد."),
                  reply_markup=kb([[b("مراسلة المطور", url=DEV_URL, style="success")]]))


async def scan_chat(ch, gate):
    async with gate:
        try:
            await ensure_joined(ch)
            await sweep_channels(ch)
        except FloodWaitError as e:
            log.warning("flood wait %ss", e.seconds)
            await asyncio.sleep(min(e.seconds, 60))
        except Exception as e:
            log.warning("chat %s: %s", ch["id"], e)


async def watchdog():
    """إذا انقطع الحساب المساعد أو فشل تشغيله نعيد تشغيله، ونخبر المطور عند العودة."""
    if not db["session"]:
        return
    if UB is not None and UB.is_connected():
        return
    log.warning("الحساب المساعد غير متصل، نعيد تشغيله")
    if await start_userbot():
        await api("sendMessage", chat_id=OWNER_ID, text="انقطع الحساب المساعد وتمت إعادة تشغيله تلقائيًا.")


async def monitor_loop():
    """فحص احتياطي: الدخول الفوري يتم عبر الأحداث، وهذه الحلقة تلتقط ما فاته.
    الأماكن الفارغة نفحصها كل دورة (لنلتقط محادثة جديدة بسرعة)، والتي فيها محادثة قائمة كل ACTIVE_RECHECK ثانية."""
    gate = asyncio.Semaphore(5)
    last_active_check = last_backup = last_watchdog = time.monotonic()
    while True:
        delay = SCAN_MIN
        try:
            await check_expired()
            flush()
            mono = time.monotonic()
            if mono - last_backup > BACKUP_EVERY:
                last_backup = mono
                backup_db()
            if mono - last_watchdog > 60:
                last_watchdog = mono
                await watchdog()
            if UB and CALLS:
                if db["enabled"]:
                    check_active = mono - last_active_check > ACTIVE_RECHECK
                    if check_active:
                        last_active_check = mono
                    todo = [ch for ch in list(db["chats"]) if check_active or ch["id"] not in active]
                    delay = max(SCAN_MIN, len(todo) * SCAN_PER_CHAT)
                    await asyncio.gather(*[scan_chat(ch, gate) for ch in todo])
                else:
                    for cid in list(active):
                        await leave(cid)
        except Exception as e:
            log.warning("loop: %s", e)
        await asyncio.sleep(delay)


# ------------------------------------------------------------------ الإذاعة الشاملة
BC_NAMES = {"all": "كل مستخدمي البوت", "subs": "المشتركون فقط", "vip": "مشتركو VIP فقط"}


def bc_targets(kind):
    if kind == "vip":
        ids = {int(k) for k, v in db["subs"].items() if v.get("vip") and sub_of(int(k))}
    elif kind == "subs":
        ids = {int(k) for k in db["subs"] if sub_of(int(k))}
    else:
        ids = KNOWN | {int(k) for k in db["subs"]} | {c.get("owner") for c in db["chats"]}
    ids.discard(OWNER_ID)
    ids.discard(None)
    return sorted(ids)


async def broadcast(targets, src_chat, src_mid, report_chat):
    global broadcasting
    broadcasting = True
    started = datetime.now(timezone.utc)
    sent = failed = 0
    try:
        for target in targets:
            args = dict(chat_id=target, from_chat_id=src_chat, message_id=src_mid)
            d = await api("copyMessage", **args)
            if not d.get("ok"):
                wait = (d.get("parameters") or {}).get("retry_after")
                if wait:
                    await asyncio.sleep(wait + 1)
                    d = await api("copyMessage", **args)
            if d.get("ok"):
                sent += 1
            else:
                failed += 1
            await asyncio.sleep(0.05)
    finally:
        broadcasting = False
    took = (datetime.now(timezone.utc) - started).total_seconds()
    await api("sendMessage", chat_id=report_chat, parse_mode="HTML",
              text=("<b>تقرير الإذاعة</b>\n\n"
                    f"المستهدفون: <b>{len(targets)}</b>\n"
                    f"وصلت: <b>{sent}</b>\n"
                    f"فشلت (حظروا البوت أو حذفوه): <b>{failed}</b>\n"
                    f"المدة: {dur(took)}"))


# ------------------------------------------------------------------ الحساب المساعد
async def stop_userbot():
    global UB, CALLS
    for cid in list(active):
        await leave(cid)
    if UB:
        try:
            await UB.disconnect()
        except Exception:
            pass
    UB = CALLS = None


async def warm_up(client):
    """يجلب المحادثات مرة واحدة ليحفظ Telethon كيانات الأماكن، فلا نضيع وقتًا في البحث عنها عند أول حدث."""
    try:
        async for _ in client.iter_dialogs(limit=500):
            pass
    except Exception as e:
        log.warning("warm_up: %s", e)


async def start_userbot():
    global UB, CALLS, ME_ID
    await stop_userbot()
    if not db["session"]:
        return False
    try:
        UB = TelegramClient(StringSession(db["session"]), API_ID, API_HASH)
        await UB.connect()
        if not await UB.is_user_authorized():
            db["session"] = ""
            save()
            UB = None
            return False
        ME_ID = (await UB.get_me()).id
        spawn(warm_up(UB))
        UB.add_event_handler(on_parts, events.Raw(types.UpdateGroupCallParticipants))
        UB.add_event_handler(on_group_call, events.Raw(types.UpdateGroupCall))
        UB.add_event_handler(on_participant, events.Raw(types.UpdateChannelParticipant))
        UB.add_event_handler(on_music_message, events.NewMessage(incoming=True))
        CALLS = PyTgCalls(UB)
        if call_filters:
            try:
                @CALLS.on_update(call_filters.stream_end)
                async def _stream_ended(_, update):
                    try:
                        await on_stream_end(update.chat_id)
                    except Exception as e:
                        log.warning("stream_end: %s", e)
            except Exception as e:
                log.warning("stream_end: %s", e)
        await CALLS.start()
        return True
    except Exception as e:
        log.exception("start_userbot: %s", e)
        UB = CALLS = None
        return False


# ------------------------------------------------------------------ الموسيقى
MUSIC_DIR = "music_cache"
COOKIES_FILE = "cookies.txt"
REQUEST_GAP = 3            # ثواني بين طلبين من نفس المستخدم

ACCESS_MODES = {"all": "الجميع", "mixed": "الطلب للجميع والتحكم للمشرفين", "admins": "المشرفون فقط"}
PANEL_MODES = {"auto": "تلقائي (المكان ثم كروب الإشعارات)", "place": "في المكان",
               "notify": "في كروب الإشعارات", "both": "الاثنان معًا"}
PRESETS = {"max_min": [5, 10, 15, 30, 60, 120], "queue_limit": [5, 10, 20, 50],
           "cache_mb": [256, 512, 1024, 2048, 5120], "volume": [50, 100, 150, 200],
           "progress_sec": [5, 10, 15, 30], "blur": [0, 4, 8, 14, 22]}

# كلمة الأمر (بعد توحيد الهمزات) -> الإجراء
COMMANDS = {
    "شغل": "play", "play": "play", "يوت": "yt", "yt": "yt",
    "تخطي": "skip", "skip": "skip", "وقف": "pause", "pause": "pause",
    "استكمال": "resume", "كمل": "resume", "resume": "resume",
    "انهاء": "stop", "ايقاف": "stop", "stop": "stop",
    "تكرار": "loop", "loop": "loop", "قائمة": "queue", "الطابور": "queue", "queue": "queue",
    "الان": "now", "now": "now", "تقديم": "fwd", "رجوع": "back", "عودة": "back",
    "صوت": "volume", "volume": "volume",
}
VIDEO_SUFFIXES = ("مع الفيديو", "مع فيديو", "بالفيديو", "فيديو", "video")

players = {}               # chat_id -> Player
_inflight = {}             # (id, نوع) -> مهمة تحميل جارية
_dl_gate = None
_bot_cache = {}            # chat_id -> (هل يقدر البوت الكتابة، وقت الفحص)
_admin_cache = {}          # (chat_id, user_id) -> (هل مشرف، وقت الفحص)
_last_request = {}         # (chat_id, user_id) -> وقت آخر طلب

_AR_MAP = str.maketrans({"أ": "ا", "إ": "ا", "آ": "ا", "ى": "ي", "ؤ": "و", "ئ": "ي"})


def norm_ar(text):
    return re.sub(r"[\u064b-\u0652\u0640]", "", text).translate(_AR_MAP)


def short(err):
    """نص خطأ قصير نظيف (yt-dlp يضيف ألوان ANSI)."""
    return re.sub(r"\x1b\[[0-9;]*m", "", str(err)).strip()[:200]


def fmt_secs(n):
    n = int(n or 0)
    h, rest = divmod(n, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def progress_bar(pos, total, width=16):
    if not total:
        return fmt_secs(pos)
    ratio = min(1.0, pos / total)
    i = min(width - 1, int(width * ratio))
    return ("▬" * i + "●" + "▬" * (width - 1 - i)
            + f"\n{fmt_secs(pos)} / {fmt_secs(total)}  ({int(ratio * 100)}%)")


def next_preset(options, current):
    return options[(options.index(current) + 1) % len(options)] if current in options else options[0]


async def run_blocking(fn, *args):
    return await asyncio.get_running_loop().run_in_executor(None, fn, *args)


# ------------------------------------------------------------------ تجهيز ffmpeg و yt-dlp
def ensure_ffmpeg():
    """يجهز ffmpeg و ffprobe تلقائيًا (حزمة static-ffmpeg) إن لم يكونا مثبتين في النظام."""
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return True
    try:
        import static_ffmpeg
        static_ffmpeg.add_paths()
    except Exception as e:
        log.warning("تعذر تجهيز ffmpeg تلقائيًا (%s). ثبّته يدويًا: apt install ffmpeg", e)
    return bool(shutil.which("ffmpeg"))


def ensure_js_runtime():
    """يوتيوب يحتاج بيئة جافاسكربت (deno أو node) مع yt-dlp الحديث. نحاول تثبيت deno إن لم نجد شيئًا."""
    import site
    import sysconfig
    extra = [sysconfig.get_path("scripts"), os.path.join(site.USER_BASE, "bin")]
    os.environ["PATH"] = os.pathsep.join([p for p in extra if p]) + os.pathsep + os.environ.get("PATH", "")
    if any(shutil.which(x) for x in ("deno", "node", "bun", "qjs")):
        return True
    marker = ".js_runtime_tried"
    if os.path.exists(marker):
        return bool(shutil.which("deno"))
    cmd = [sys.executable, "-m", "pip", "install", "-q", "-U", "deno"]
    if subprocess.run(cmd, capture_output=True).returncode != 0:
        subprocess.run(cmd + ["--break-system-packages"], capture_output=True)
    open(marker, "w").close()
    ok = bool(shutil.which("deno"))
    if not ok:
        log.warning("لم أجد deno أو node. إن فشل التحميل من يوتيوب ثبّت أحدهما: https://deno.com")
    return ok


def ytdlp_version():
    try:
        import yt_dlp
        return yt_dlp.version.__version__
    except Exception:
        return None


def _pip_upgrade_ytdlp():
    cmd = [sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp[default]"]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        res = subprocess.run(cmd + ["--break-system-packages"], capture_output=True, text=True)
    for name in [k for k in sys.modules if k == "yt_dlp" or k.startswith("yt_dlp.")]:
        del sys.modules[name]      # ليُحمَّل الإصدار الجديد عند أول استخدام
    return res.returncode == 0, (res.stderr or res.stdout)[-200:]


# ------------------------------------------------------------------ البحث والتحميل
def _ydl_opts(**extra):
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20,
            "retries": 3, "fragment_retries": 3, "concurrent_fragment_downloads": 4}
    if os.path.exists(COOKIES_FILE):
        opts["cookiefile"] = COOKIES_FILE
    proxies = proxy_list()
    if proxies:
        opts["proxy"] = random.choice(proxies)          # عند وجود أكثر من بروكسي نبدّل بينها
    elif db["music"]["ipv4"]:
        opts["source_address"] = "0.0.0.0"              # IPv4 فقط
    opts.update(extra)
    return opts


def proxy_list():
    return [x for x in re.split(r"[\s,]+", db["music"]["proxy"].strip()) if x]


class YtBlocked(Exception):
    """يوتيوب يحجب السيرفر مؤقتًا: لا نكرر الطلبات حتى لا يزيد الحجب."""


_yt = {"until": 0.0, "count": 0, "reason": ""}
BLOCK_HINTS = ("sign in to confirm", "not a bot", "429", "too many requests", "po token",
               "http error 403", "forbidden")


def is_block_error(err):
    msg = short(err).lower()
    return any(h in msg for h in BLOCK_HINTS)


def yt_blocked():
    return time.time() < _yt["until"]


def yt_mark_blocked(err):
    _yt["count"] += 1
    minutes = min(120, 15 * 2 ** (_yt["count"] - 1))      # 15 ثم 30 ثم 60 ثم 120 دقيقة
    _yt["until"] = time.time() + minutes * 60
    _yt["reason"] = short(err)[:90]
    log.warning("يوتيوب يحجب السيرفر، نتوقف عنه %s دقيقة: %s", minutes, _yt["reason"])


def yt_mark_ok():
    _yt.update(until=0.0, count=0, reason="")


def yt_status_line():
    if not yt_blocked():
        return "سليم"
    until = fmt(datetime.fromtimestamp(_yt["until"], timezone.utc))
    return f"محجوب مؤقتًا حتى {until} ({esc(_yt['reason'])})"


def _yt_probe():
    """اختبار سريع: هل يعطينا يوتيوب صيغ فيديو معروف؟"""
    import yt_dlp
    with yt_dlp.YoutubeDL(_ydl_opts(skip_download=True)) as y:
        info = y.extract_info("https://www.youtube.com/watch?v=jNQXAC9IVRw", download=False)
    count = len((info or {}).get("formats") or [])
    if not count:
        raise RuntimeError("لم يرجع يوتيوب أي صيغة")
    return count


def safe_id(info):
    ie = str(info.get("ie_key") or info.get("extractor_key") or "yt").lower()
    return re.sub(r"[^\w.-]", "_", f"{ie}_{info['id']}")[:90]


def norm_info(entry):
    if not entry or not entry.get("id"):
        return None
    vid = str(entry["id"])
    url = entry.get("webpage_url") or entry.get("url") or ""
    if not url.startswith("http"):
        url = f"https://www.youtube.com/watch?v={vid}"
    return {"id": safe_id(entry), "title": entry.get("title") or "بدون عنوان",
            "dur": int(entry.get("duration") or 0), "url": url,
            "uploader": entry.get("uploader") or entry.get("channel") or "",
            "live": bool(entry.get("is_live")) or entry.get("live_status") == "is_live"}


def _flat_search(prefix, query):
    import yt_dlp
    with yt_dlp.YoutubeDL(_ydl_opts(extract_flat="in_playlist", skip_download=True)) as y:
        res = y.extract_info(f"{prefix}{query}", download=False)
    return [x for x in (norm_info(e) for e in (res or {}).get("entries") or []) if x]


def _search_blocking(query):
    import yt_dlp
    if re.match(r"https?://", query):
        with yt_dlp.YoutubeDL(_ydl_opts(skip_download=True)) as y:
            info = y.extract_info(query, download=False)
        if info and info.get("entries"):
            info = next(iter(info["entries"]), None)
        return [x for x in [norm_info(info)] if x]
    found, err = [], None
    if yt_blocked():
        err = YtBlocked("يوتيوب يحجب السيرفر مؤقتًا")
    else:
        try:
            found = _flat_search("ytsearch4:", query)
        except Exception as e:
            err = e
            if is_block_error(e):
                yt_mark_blocked(e)
    if not found and db["music"]["fallback"]:       # يوتيوب لا يرد: نبحث في ساوندكلاود (لا يحتاج كوكيز)
        found = _flat_search("scsearch4:", query)
    if not found and err:
        raise err
    return found


def _soundcloud_alt(title, dur):
    """بديل للمقطع من ساوندكلاود بنفس العنوان تقريبًا وبمدة قريبة."""
    for info in _flat_search("scsearch5:", title):
        if not dur or not info["dur"] or abs(info["dur"] - dur) <= 90:
            return info
    return None


_search_cache = {}


async def search(query):
    """بحث مع ذاكرة قصيرة: تكرار نفس الطلب خلال 10 دقائق فوري."""
    key = norm_ar(query).lower().strip()
    hit = _search_cache.get(key)
    if hit and time.monotonic() - hit[0] < 600:
        return hit[1]
    results = await run_blocking(_search_blocking, query)
    if len(_search_cache) > 200:
        _search_cache.clear()
    _search_cache[key] = (time.monotonic(), results)
    return results


def pick_result(results, max_seconds):
    """أول نتيجة صالحة: ليست بثًا مباشرًا ولا أطول من الحد المسموح."""
    if not results:
        raise ValueError("لم أجد نتائج")
    for r in results:
        if not r["live"] and (r["dur"] == 0 or r["dur"] <= max_seconds):
            return r
    if all(r["live"] for r in results):
        raise ValueError("النتائج بثوث مباشرة ولا يمكن تشغيلها")
    raise ValueError(f"كل النتائج أطول من الحد المسموح ({max_seconds // 60} دقيقة)")


def find_cached(sid, kind):
    prefix = f"{sid}.{kind}."
    best = None
    try:
        for name in os.listdir(MUSIC_DIR):
            rest = name[len(prefix):]
            if not name.startswith(prefix) or re.match(r"^f\d+[.-]", rest):
                continue
            if name.endswith((".part", ".ytdl", ".temp")):
                continue
            path = os.path.join(MUSIC_DIR, name)
            if os.path.getsize(path) > 0 and (best is None or os.path.getmtime(path) > os.path.getmtime(best)):
                best = path
    except OSError:
        pass
    return best


# عملاء يوتيوب نجربهم بالترتيب عند الفشل بدون كوكيز (الأسماء تتغير مع تحديثات yt-dlp)
YT_CLIENT_TRIES = (None, ["mweb", "android_vr", "web_safari"])


def _download_once(url, sid, kind, clients):
    import yt_dlp
    out = os.path.join(MUSIC_DIR, f"{sid}.{kind}.%(ext)s")
    if kind == "v":
        opts = _ydl_opts(format="bv*[height<=480]+ba/b[height<=480]/b", outtmpl=out,
                         merge_output_format="mp4")
    else:
        opts = _ydl_opts(format="bestaudio[ext=m4a][abr<=?130]/bestaudio[abr<=?130]/bestaudio/best", outtmpl=out)
    if clients:
        opts["extractor_args"] = {"youtube": {"player_client": clients}}
    with yt_dlp.YoutubeDL(opts) as y:
        info = y.extract_info(url, download=True)
    done = ((info or {}).get("requested_downloads") or [{}])[0].get("filepath")
    if not done or not os.path.exists(done):
        done = find_cached(sid, kind)
    if not done:
        raise RuntimeError("اكتمل التحميل لكن الملف غير موجود")
    return done


def _download_blocking(url, sid, kind):
    youtube = "youtube.com" in url or "youtu.be" in url
    if youtube and yt_blocked():
        raise YtBlocked("يوتيوب يحجب السيرفر مؤقتًا، جرّب بعد قليل")
    tries = YT_CLIENT_TRIES if youtube else (None,)
    last, blocks = None, 0
    for clients in tries:
        try:
            path = _download_once(url, sid, kind, clients)
            if youtube:
                yt_mark_ok()
            return path
        except Exception as e:
            last = e
            blocks += is_block_error(e)
    if youtube and blocks == len(tries):
        yt_mark_blocked(last)
    raise last


def protected_paths():
    keep = set()
    for p in players.values():
        for t in ([p.current] if p.current else []) + p.queue:
            if t.path:
                keep.add(t.path)
    return keep


def trim_cache():
    """يحذف الأقدم استخدامًا حتى يرجع حجم الكاش تحت الحد الذي حدده المطور."""
    limit = db["music"]["cache_mb"] * 1024 * 1024
    keep = protected_paths()
    files = []
    for name in os.listdir(MUSIC_DIR):
        path = os.path.join(MUSIC_DIR, name)
        if os.path.isfile(path):
            st = os.stat(path)
            files.append((st.st_mtime, st.st_size, path))
    total = sum(f[1] for f in files)
    for _, size, path in sorted(files):
        if total <= limit:
            break
        if path not in keep:
            try:
                os.remove(path)
                total -= size
            except OSError:
                pass


def cache_size():
    try:
        return sum(os.path.getsize(os.path.join(MUSIC_DIR, f)) for f in os.listdir(MUSIC_DIR))
    except OSError:
        return 0


def is_youtube(url):
    return "youtube.com" in url or "youtu.be" in url


async def _fallback_download(track, err):
    """يوتيوب فشل (غالبًا حجب السيرفر بدون كوكيز): الصوت فقط، ثم نسخة من ساوندكلاود."""
    if not is_youtube(track.url):
        raise err
    log.info("فشل تحميل يوتيوب (%s) نجرب البدائل", short(err))
    if track.video:
        try:
            path = await run_blocking(_download_blocking, track.url, track.id, "a")
            track.video, track.fallback = False, "audio"
            return path
        except Exception:
            pass
    alt = await run_blocking(_soundcloud_alt, track.title, track.dur)
    if not alt:
        raise err
    path = await run_blocking(_download_blocking, alt["url"], alt["id"], "a")
    track.id, track.url, track.video, track.fallback = alt["id"], alt["url"], False, "soundcloud"
    return path


async def _download_task(track, kind):
    global _dl_gate
    if _dl_gate is None:
        _dl_gate = asyncio.Semaphore(3)
    async with _dl_gate:
        try:
            path = await run_blocking(_download_blocking, track.url, track.id, kind)
        except Exception as err:
            if not db["music"]["fallback"]:
                raise
            path = await _fallback_download(track, err)
    await run_blocking(trim_cache)
    return path


async def fetch(track):
    """مسار الملف من الكاش أو بعد تحميله. الطلبات المتزامنة لنفس المقطع تنتظر تحميلًا واحدًا."""
    kind = "v" if track.video else "a"
    cached = find_cached(track.id, kind)
    if cached:
        return cached
    key = (track.id, kind)
    task = _inflight.get(key)
    if task is None:
        task = asyncio.ensure_future(_download_task(track, kind))
        _inflight[key] = task
        task.add_done_callback(lambda _t, k=key: _inflight.pop(k, None))
    return await asyncio.shield(task)


async def prefetch(track):
    try:
        track.path = await fetch(track)
    except Exception as e:
        log.info("prefetch: %s", short(e))


# ------------------------------------------------------------------ المقطع والمشغّل
FALLBACK_NOTES = {"audio": "تعذر تحميل الفيديو من يوتيوب فشغّلت الصوت فقط.",
                  "soundcloud": "يوتيوب حجب التحميل من السيرفر فشغّلت نسخة من ساوندكلاود."}


class Track:
    def __init__(self, info, video, by):
        self.id = info["id"]
        self.title = info["title"]
        self.dur = info["dur"]
        self.url = info["url"]
        self.uploader = info["uploader"]
        self.video = video
        self.by = by             # (ايدي، اسم) من طلبه
        self.path = None
        self.fallback = None     # "audio" أو "soundcloud" إذا استُخدم بديل


class Player:
    def __init__(self, cid):
        self.cid = cid
        self.queue = []
        self.current = None
        self.loop = False
        self.paused = False
        self.busy = False        # جاري تحضير مقطع أو تبديله
        self.lock = asyncio.Lock()
        self.base = 0.0          # الموضع بالثواني عند آخر بداية أو استئناف
        self.t0 = None
        self.started_at = 0.0
        self.panels = []         # [(chat_id, message_id, هل هي صورة)]
        self.last_edit = 0.0     # آخر مرة حدّثنا فيها اللوحة (لشريط التقدم)
        self.volume = db["music"]["volume"]

    def position(self):
        if self.current is None:
            return 0
        run = time.monotonic() - self.t0 if (self.t0 is not None and not self.paused) else 0
        pos = self.base + run
        return min(pos, self.current.dur) if self.current.dur else pos


def player_of(cid):
    if cid not in players:
        players[cid] = Player(cid)
    return players[cid]


async def pytg(names, *args):
    """ينادي أول دالة موجودة في PyTgCalls من بين الأسماء (تختلف أسماؤها بين الإصدارات)."""
    for name in names:
        fn = getattr(CALLS, name, None)
        if fn:
            return await fn(*args)
    raise RuntimeError("هذه الميزة غير متوفرة في إصدار المكتبة المثبّت")


async def start_stream(p, seek=0):
    t = p.current
    extra = {"ffmpeg_parameters": f"-ss {int(seek)}"} if seek else {}
    quality = getattr(VideoQuality, "SD_480p", None) if VideoQuality else None
    if t.video and quality:
        stream = MediaStream(t.path, audio_parameters=AudioQuality.HIGH, video_parameters=quality, **extra)
    else:
        stream = MediaStream(t.path, audio_parameters=AudioQuality.HIGH,
                             video_flags=MediaStream.Flags.IGNORE, **extra)
    await CALLS.play(p.cid, stream)
    p.base, p.t0, p.paused = float(seek), time.monotonic(), False
    p.started_at = time.monotonic()
    try:
        os.utime(t.path, None)       # لتعرف سياسة الكاش أنه استُخدم حديثًا
    except OSError:
        pass
    if p.volume != 100:
        try:
            await pytg(("change_volume_call", "change_volume"), p.cid, p.volume)
        except Exception as e:
            log.info("volume: %s", e)


async def to_silence(p):
    """نرجع إلى ملف الصمت ليبقى الحساب المساعد داخل المحادثة ويستمر الرصد."""
    p.base, p.t0, p.paused = 0.0, None, False
    if p.cid not in active:
        return
    for attempt in range(3):          # الحساب المساعد لا يغادر أبدًا بسبب الموسيقى
        try:
            await join_call(p.cid)
            return
        except Exception as e:
            log.warning("silence %s (محاولة %s): %s", p.cid, attempt + 1, e)
            await asyncio.sleep(1)


async def play_next(p, skip=False):
    """يبدأ المقطع التالي من الطابور (أو يعيد الحالي إن كان التكرار مفعّلًا). True إن بدأ تشغيل شيء."""
    async with p.lock:
        p.busy = True
        try:
            while True:
                prev = p.current
                if prev is not None and p.loop and not skip:
                    nxt = prev
                elif p.queue:
                    nxt = p.queue.pop(0)
                else:
                    nxt = None
                skip = False
                if nxt is None:
                    p.current = None
                    await to_silence(p)
                    await panel_finish(p, "انتهى التشغيل، والحساب المساعد باقٍ في المحادثة للمراقبة")
                    return False
                p.current = nxt
                try:
                    if not nxt.path or not os.path.exists(nxt.path):
                        nxt.path = await fetch(nxt)
                    await start_stream(p, 0)
                except Exception as e:
                    log.warning("play %s: %s", p.cid, e)
                    p.current = None
                    await say(p.cid, f"تعذر تشغيل <b>{esc(nxt.title)}</b>:\n<code>{esc(short(e))}</code>")
                    continue
                db["music"]["plays"] += 1
                save_soon()
                if nxt.fallback:
                    await say(p.cid, FALLBACK_NOTES[nxt.fallback])
                    nxt.fallback = None
                await panel_show(p, fresh=True)
                if p.queue:
                    spawn(prefetch(p.queue[0]))
                return True
        finally:
            p.busy = False


async def music_stop(cid, note="تم إيقاف الموسيقى، والحساب المساعد باقٍ في المحادثة للمراقبة"):
    p = players.get(cid)
    if not p:
        return
    async with p.lock:
        p.queue.clear()
        p.current = None
        p.loop = False
        await to_silence(p)
        await panel_finish(p, note)


async def stop_all_music():
    count = 0
    for cid, p in list(players.items()):
        if p.current or p.queue:
            await music_stop(cid)
            count += 1
    return count


async def music_cleanup(cid):
    """تُنادى عند انتهاء المحادثة المرئية: نمسح المشغّل دون العودة للصمت."""
    p = players.pop(cid, None)
    if p and (p.current or p.queue):
        p.queue.clear()
        p.current = None
        await panel_finish(p, "انتهت المحادثة المرئية")


async def on_stream_end(cid):
    p = players.get(cid)
    if p is None or p.current is None:
        if cid in active:
            await join_call(cid)          # انتهى ملف الصمت: نعيده
        return
    if p.busy or time.monotonic() - p.started_at < 2:
        return                            # حدث مكرر (صوت وصورة) أو ناتج عن تبديلنا للمقطع
    await play_next(p)


# ------------------------------------------------------------------ الرسائل واللوحة
class Sent:
    def __init__(self, via, chat, mid, sticker=False, reply_to=None):
        self.via, self.chat, self.mid = via, chat, mid
        self.sticker, self.reply_to = sticker, reply_to

    async def edit(self, text):
        if self.sticker:                       # الملصق لا يُعدَّل: نحذفه ونرسل النص مكانه
            await self.delete()
            new = await say(self.chat, text, self.reply_to)
            self.via, self.mid, self.sticker = new.via, new.mid, False
            return
        if self.via == "bot":
            await api("editMessageText", chat_id=self.chat, message_id=self.mid, text=deco(text, "music"),
                      parse_mode="HTML", disable_web_page_preview=True)
        elif self.via == "user" and UB:
            try:
                await UB.edit_message(self.chat, self.mid, text, parse_mode="html")
            except Exception as e:
                log.info("edit: %s", e)

    async def progress(self, text):
        """تحديث حالة أثناء العمل: الملصق يبقى ظاهرًا ولا يُستبدل بنص."""
        if not self.sticker:
            await self.edit(text)

    async def delete(self):
        if self.via == "bot":
            await api("deleteMessage", chat_id=self.chat, message_id=self.mid)
        elif self.via == "user" and UB:
            try:
                await UB.delete_messages(self.chat, self.mid)
            except Exception as e:
                log.info("delete: %s", e)


async def say(cid, text, reply_to=None):
    """رسالة في المكان: بالبوت إن كان يقدر الكتابة هناك، وإلا بالحساب المساعد."""
    hit = _bot_cache.get(cid)
    bot_blocked = hit and hit[0] is False and time.monotonic() - hit[1] < 600
    if not bot_blocked:
        params = dict(chat_id=cid, text=deco(text, "music"), parse_mode="HTML", disable_web_page_preview=True)
        if reply_to:
            params.update(reply_to_message_id=reply_to, allow_sending_without_reply=True)
        d = await api("sendMessage", **params)
        _bot_cache[cid] = (bool(d.get("ok")), time.monotonic())
        if d.get("ok"):
            return Sent("bot", cid, d["result"]["message_id"])
    if UB:
        try:
            msg = await UB.send_message(cid, text, parse_mode="html", reply_to=reply_to, link_preview=False)
            return Sent("user", cid, msg.id)
        except Exception as e:
            log.warning("say %s: %s", cid, e)
    return Sent(None, cid, 0)


async def say_status(cid, text, reply_to=None):
    """رد أثناء البحث والتحميل: ملصق ميوزك إن حدده المطور، وإلا رسالة نصية."""
    stickers = db["music"]["stickers"]
    hit = _bot_cache.get(cid)
    bot_blocked = hit and hit[0] is False and time.monotonic() - hit[1] < 600
    if stickers and not bot_blocked:
        params = dict(chat_id=cid, sticker=random.choice(stickers))
        if reply_to:
            params.update(reply_to_message_id=reply_to, allow_sending_without_reply=True)
        d = await api("sendSticker", **params)
        if d.get("ok"):
            return Sent("bot", cid, d["result"]["message_id"], sticker=True, reply_to=reply_to)
    return await say(cid, text, reply_to)


def panel_text(p):
    t = p.current
    lines = [f"<b>{'تشغيل فيديو' if t.video else 'تشغيل موسيقى'}</b>", "",
             field("song", "العنوان", f"<b>{esc(t.title)}</b>")]
    if t.uploader:
        lines.append(field("artist", "القناة", esc(t.uploader)))
    lines += [field("duration", "المدة", fmt_secs(t.dur) if t.dur else "غير معروفة"),
              field("by", "الطالب", mention(*t.by)),
              field("place", "المكان", esc(title_of(p.cid))),
              f"الحالة: {'متوقف مؤقتًا' if p.paused else 'يعمل الآن'}",
              progress_bar(p.position(), t.dur)]
    if p.queue:
        lines.append(f"في الانتظار: <b>{len(p.queue)}</b>")
    if p.loop:
        lines.append("التكرار: مفعّل")
    return "\n".join(lines)


def panel_kb(p):
    def mb(label, act, style, slot):
        return b(label, f"mu:{act}:{p.cid}", style=style, k=slot)
    rows = [
        [mb("إيقاف مؤقتًا", "pause", "danger", "mu_pause"), mb("استكمال البث", "resume", "success", "mu_resume")],
        [mb("عودة 10 ثواني", "back", "primary", "mu_back"), mb("تقديم 10 ثواني", "fwd", "primary", "mu_fwd")],
        [mb("تخطي", "skip", "primary", "mu_skip"), mb("إيقاف الموسيقى", "stop", "danger", "mu_stop")],
        [mb("التكرار: مفعّل" if p.loop else "تكرار", "loop", "success" if p.loop else "primary", "mu_loop"),
         mb("الطابور", "queue", "primary", "mu_queue")],
    ]
    last = [b("المطور", url=DEV_URL, style="success", k="mu_dev")]
    if db["music"]["updates_url"]:
        last.append(b("قناة التحديثات", url=db["music"]["updates_url"], style="primary", k="mu_updates"))
    return kb(rows + [last])


def panel_targets(p):
    ch = chat_of(p.cid)
    dest = dest_of(ch) if ch else None
    mode = db["music"]["panel"]
    targets = []
    if mode in ("auto", "place", "both"):
        targets.append(p.cid)
    if mode in ("notify", "both") and dest:
        targets.append(dest)
    return list(dict.fromkeys(targets)), dest


async def panel_send(target, text, markup):
    """يرسل اللوحة كصورة (إن حدد المطور صورة) أو كنص. يرجع (الدردشة، الرسالة، هل صورة) أو None."""
    photo = db["music"]["panel_photo"]
    if photo and len(text) <= 1000:
        d = await api("sendPhoto", chat_id=target, photo=photo, caption=text, parse_mode="HTML",
                      reply_markup=markup)
        if d.get("ok"):
            return target, d["result"]["message_id"], True
    d = await api("sendMessage", chat_id=target, text=text, parse_mode="HTML", reply_markup=markup,
                  disable_web_page_preview=True)
    return (target, d["result"]["message_id"], False) if d.get("ok") else None


async def panel_edit(entry, text, markup):
    chat, mid, is_photo = entry
    if is_photo:
        d = await api("editMessageCaption", chat_id=chat, message_id=mid, caption=text, parse_mode="HTML",
                      reply_markup=markup)
    else:
        d = await api("editMessageText", chat_id=chat, message_id=mid, text=text, parse_mode="HTML",
                      reply_markup=markup, disable_web_page_preview=True)
    return bool(d.get("ok")) or "not modified" in str(d.get("description", ""))


async def panel_show(p, fresh=False, resend=True):
    """fresh: لوحة جديدة (عند بدء مقطع). وإلا نعدّل الموجودة في مكانها.
    resend=False: تعديل فقط (يستعمله شريط التقدم حتى لا يعيد إرسال لوحة حذفها أحد)."""
    if p.current is None:
        return
    text, markup = deco(panel_text(p), "music"), panel_kb(p)
    if fresh:
        for chat, mid, _ in p.panels:
            await api("deleteMessage", chat_id=chat, message_id=mid)
        p.panels = []
    alive = []
    for entry in p.panels:
        if await panel_edit(entry, text, markup):
            alive.append(entry)
    p.panels = alive
    p.last_edit = time.monotonic()
    if p.panels or not resend:
        return
    targets, dest = panel_targets(p)
    for target in targets:
        entry = await panel_send(target, text, markup)
        if entry:
            p.panels.append(entry)
    if not p.panels and dest and dest not in targets:    # البوت لا يكتب في المكان: نرسلها لكروب الإشعارات
        entry = await panel_send(dest, text, markup)
        if entry:
            p.panels.append(entry)


async def panel_finish(p, note):
    text = deco(f"<b>{esc(note)}</b>", "music")
    for chat, mid, is_photo in p.panels:
        if is_photo:
            await api("editMessageCaption", chat_id=chat, message_id=mid, caption=text, parse_mode="HTML",
                      reply_markup=kb([]))
        else:
            await api("editMessageText", chat_id=chat, message_id=mid, text=text, parse_mode="HTML",
                      reply_markup=kb([]))
    p.panels = []


def queue_text(p, limit=10):
    lines = [f"<b>الآن:</b> {esc(p.current.title)}"]
    for i, t in enumerate(p.queue[:limit], 1):
        lines.append(f"{i}. {esc(t.title)} ({fmt_secs(t.dur)})")
    if len(p.queue) > limit:
        lines.append(f"... و{len(p.queue) - limit} أخرى")
    if len(lines) == 1:
        lines.append("الطابور فارغ")
    return "\n".join(lines)


# ------------------------------------------------------------------ الصلاحيات والإجراءات
async def is_chat_admin(uid, cid):
    key, now = (cid, uid), time.monotonic()
    hit = _admin_cache.get(key)
    if hit and now - hit[1] < 60:
        return hit[0]
    ok = False
    d = await api("getChatMember", chat_id=cid, user_id=uid)
    if d.get("ok"):
        ok = d["result"].get("status") in ("creator", "administrator")
    elif UB:
        try:
            perms = await UB.get_permissions(cid, uid)
            ok = bool(perms and (perms.is_admin or perms.is_creator))
        except Exception:
            ok = False
    _admin_cache[key] = (ok, now)
    return ok


async def music_can_control(uid, ch):
    if db["music"]["access"] == "all" or is_dev(uid):
        return True
    if not ch:
        return False
    return ch.get("owner") == uid or await is_chat_admin(uid, ch["id"])


async def music_can_request(uid, ch):
    if db["music"]["access"] in ("all", "mixed"):
        return True
    return await music_can_control(uid, ch)


async def seek_to(p, target):
    t = p.current
    target = max(0, min(target, t.dur - 2)) if t.dur else max(0, target)
    await start_stream(p, target)
    return target


async def music_action(p, act, arg=None):
    """ينفّذ إجراء تحكم ويرجع نص النتيجة (يظهر كتنبيه للزر أو كرد على الأمر)."""
    if p.current is None:
        return "لا يوجد تشغيل الآن"
    if p.busy:
        return "جاري تحضير المقطع، حاول بعد لحظات"
    try:
        if act == "pause":
            if p.paused:
                return "التشغيل متوقف مؤقتًا بالفعل"
            await pytg(("pause", "pause_stream"), p.cid)
            p.base, p.t0, p.paused = p.position(), None, True
            await panel_show(p)
            return "تم الإيقاف المؤقت"
        if act == "resume":
            if not p.paused:
                return "التشغيل يعمل بالفعل"
            await pytg(("resume", "resume_stream"), p.cid)
            p.t0, p.paused = time.monotonic(), False
            await panel_show(p)
            return "تم استكمال البث"
        if act in ("fwd", "back"):
            step = arg if arg else 10
            target = p.position() + (step if act == "fwd" else -step)
            await seek_to(p, target)
            await panel_show(p)
            unit = "ثواني" if 3 <= step <= 10 else "ثانية"
            return f"{'تقديم' if act == 'fwd' else 'عودة'} {step} {unit}"
        if act == "skip":
            more = bool(p.queue)
            await play_next(p, skip=True)
            return "تم التخطي" if more else "لا يوجد مقطع تالٍ، انتهى التشغيل"
        if act == "stop":
            await music_stop(p.cid)
            return "تم إيقاف الموسيقى وبقي الحساب المساعد يراقب"
        if act == "loop":
            p.loop = not p.loop
            await panel_show(p)
            return "التكرار مفعّل" if p.loop else "التكرار متوقف"
        if act == "queue":
            return html.unescape(re.sub(r"<[^>]+>", "", queue_text(p, 5)))[:190]
        if act == "now":
            await panel_show(p, fresh=True)
            return ""
        if act == "volume":
            if arg is None:
                return f"الصوت الحالي {p.volume}%"
            p.volume = max(1, min(200, arg))
            await pytg(("change_volume_call", "change_volume"), p.cid, p.volume)
            return f"تم ضبط الصوت على {p.volume}%"
    except Exception as e:
        log.warning("music_action %s: %s", act, e)
        return f"تعذر التنفيذ: {short(e)}"[:190]
    return ""


async def cb_music(c):
    """أزرار لوحة التشغيل. تصل من أي شخص يضغط، فنتحقق من صلاحيته هنا."""
    try:
        act, cid = c.parts[1], int(c.parts[2])
    except (IndexError, ValueError):
        await c.toast()
        return
    p, ch = players.get(cid), chat_of(cid)
    if p is None or p.current is None:
        await c.toast("انتهى التشغيل", alert=True)
        return
    if not await music_can_control(c.uid, ch):
        await c.toast("التحكم للمشرفين فقط", alert=True)
        return
    result = await music_action(p, act)
    await c.toast(result[:190] or None, alert=(act == "queue"))


# ------------------------------------------------------------------ أوامر المجموعة
async def sender_info(ev):
    try:
        sender = await ev.get_sender()
        return utils.get_display_name(sender), getattr(sender, "username", None) or ""
    except Exception:
        return str(ev.sender_id), ""


async def quick_join(ch):
    """إن كانت هناك محادثة مرئية قائمة ندخلها فورًا. يرجع True إن وجدت."""
    call = await get_call(ch["id"])
    if call:
        await ensure_joined(ch, call)
    return bool(call)


async def music_request(ch, ev, query, video, by_name):
    cid, uid = ch["id"], ev.sender_id
    mu = db["music"]
    # البحث والدخول والملصق يبدؤون معًا بدل الانتظار واحدًا بعد الآخر
    status_task = asyncio.ensure_future(say_status(cid, f"جاري البحث عن: <b>{esc(query[:80])}</b>", ev.id))
    search_task = asyncio.ensure_future(search(query))
    join_task = asyncio.ensure_future(quick_join(ch)) if cid not in active else None
    status = await status_task
    try:
        info, err = pick_result(await search_task, mu["max_min"] * 60), None
    except Exception as e:
        info, err = None, e
    if join_task:
        await asyncio.gather(join_task, return_exceptions=True)
    if err:
        await status.edit(f"تعذر العثور على نتيجة:\n<code>{esc(short(err))}</code>")
        return
    p = player_of(cid)
    if len(p.queue) >= mu["queue_limit"]:
        await status.edit(f"الطابور ممتلئ ({mu['queue_limit']} مقطع)")
        return
    if cid not in active:
        try:
            await vc_start(cid)           # لا توجد محادثة مرئية: نفتحها
        except Exception as e:
            log.info("auto vc_start %s: %s", cid, e)
        await join_soon(ch)
    if cid not in active:
        await status.edit("لا توجد محادثة مرئية، وتعذر فتحها.\n"
                          "تأكد أن الحساب المساعد مشرف وله صلاحية إدارة المحادثات المرئية.")
        return
    track = Track(info, video, (uid, by_name))
    idle = p.current is None and not p.busy and not p.queue    # بدون await بين الفحص والإضافة
    p.queue.append(track)
    if not idle:
        await status.edit(f"أُضيف إلى الطابور (رقم {len(p.queue)}): <b>{esc(track.title)}</b>")
        spawn(prefetch(track))
        if p.current:
            await panel_show(p)
        return
    await status.progress(f"جاري التحميل: <b>{esc(track.title)}</b>")
    await play_next(p)
    await status.delete()


async def music_download(ch, ev, query):
    cid = ch["id"]
    mu = db["music"]
    status = await say_status(cid, f"جاري البحث والتحميل: <b>{esc(query[:80])}</b>", ev.id)
    try:
        info = pick_result(await search(query), mu["max_min"] * 60)
        track = Track(info, False, (ev.sender_id, ""))
        path = await fetch(track)
        caption = f"<b>{esc(info['title'])}</b>\nالمدة: {fmt_secs(info['dur'])}"
        attrs = [types.DocumentAttributeAudio(duration=info["dur"], title=info["title"][:60],
                                              performer=info["uploader"][:60])]
        await UB.send_file(cid, path, caption=caption, parse_mode="html", reply_to=ev.id,
                           attributes=attrs, force_document=False)
        mu["downloads"] += 1
        save_soon()
        await status.delete()
    except Exception as e:
        log.warning("yt %s: %s", cid, e)
        await status.edit(f"تعذر التحميل:\n<code>{esc(short(e))}</code>")


def parse_video_flag(rest):
    """يفصل كلمة «مع الفيديو» عن اسم الأغنية. يرجع (الاسم، فيديو؟)."""
    low = norm_ar(rest)
    for word in VIDEO_SUFFIXES:
        if low.endswith(" " + word) or low == word:
            cut = low[:len(low) - len(word)].strip()
            return (rest[:len(cut)].strip() if len(low) == len(rest) else cut), True
        if low.startswith(word + " "):
            cut = low[len(word):].strip()
            return (rest[len(rest) - len(cut):].strip() if len(low) == len(rest) else cut), True
    return rest, False


async def on_music_message(ev):
    try:
        mu = db["music"]
        if not (mu["enabled"] and db["enabled"] and UB and CALLS) or ev.out:
            return
        text = (ev.raw_text or "").strip()
        if not text or len(text) > 300:
            return
        first, _, rest = text.partition(" ")
        word = norm_ar(first.lstrip("/!.#")).lower().split("@")[0]
        action = COMMANDS.get(word)
        if action is None:
            return
        ch = chat_of(ev.chat_id)
        uid = ev.sender_id
        if not ch or not uid or ch["id"] in mu["off"] or ch.get("owner") in mu["user_off"]:
            return
        rest = rest.strip()
        cid = ch["id"]

        if action in ("play", "yt"):
            if not rest or (action == "yt" and not mu["yt"]):
                return
            if not await music_can_request(uid, ch):
                return
            now = time.monotonic()
            if now - _last_request.get((cid, uid), 0) < REQUEST_GAP:
                return
            _last_request[(cid, uid)] = now
            name, username = await sender_info(ev)
            record_person(uid, name, username, bump="dls" if action == "yt" else "plays")
            if action == "yt":
                await music_download(ch, ev, rest)
                return
            query, video = parse_video_flag(rest)
            if video and not mu["video"]:
                video = False
            if query:
                await music_request(ch, ev, query, video, name)
            return

        p = players.get(cid)
        if p is None or p.current is None:
            return                                   # كلمات مثل «تخطي» تُتجاهل إن لم يكن هناك تشغيل
        if not await music_can_control(uid, ch):
            now = time.monotonic()
            if now - _last_request.get((cid, uid, "deny"), 0) > 15:     # نخبره مرة بدل تجاهله بصمت
                _last_request[(cid, uid, "deny")] = now
                await say(cid, "التحكم بالموسيقى للمشرفين فقط", reply_to=ev.id)
            return
        number = int(re.sub(r"\D", "", rest) or 0) or None
        result = await music_action(p, action, number)
        if result:
            await say(cid, esc(result), reply_to=ev.id)
    except Exception as e:
        log.exception("on_music_message: %s", e)


# ------------------------------------------------------------------ لوحة المطور: قسم الميوزك
def on_off(flag):
    return "شغّال" if flag else "متوقف"


def music_view():
    mu = db["music"]
    playing = [p for p in players.values() if p.current]
    version = ytdlp_version()
    lines = [
        "<b>قسم الميوزك</b>", "",
        f"الحالة: <b>{on_off(mu['enabled'])}</b>",
        f"يشتغل الآن في: <b>{len(playing)}</b> مكان",
        f"إجمالي التشغيلات: {mu['plays']} | تحميلات يوت: {mu['downloads']}",
        f"الصلاحيات: {ACCESS_MODES[mu['access']]}",
        f"أقصى مدة للمقطع: {mu['max_min']} دقيقة | الطابور: {mu['queue_limit']} | الصوت الافتراضي: {mu['volume']}%",
        f"الفيديو: {on_off(mu['video'])} | أمر يوت: {on_off(mu['yt'])}",
        f"يوتيوب: {yt_status_line()}",
        f"بديل ساوندكلاود: {on_off(mu['fallback'])} | بروكسي: {len(proxy_list()) or 'لا'} | "
        f"IPv4 فقط: {on_off(mu['ipv4'])} | تحديث yt-dlp تلقائي: {on_off(mu['auto_update'])}",
        f"موقع اللوحة: {PANEL_MODES[mu['panel']]}",
        f"تحديث شريط التقدم كل {mu['progress_sec']} ثانية | تشويش الصورة: {mu['blur']}",
        f"صورة اللوحة: {'محددة' if mu['panel_photo'] else 'لا'} | ملصقات الانتظار: {len(mu['stickers'])}",
        f"قناة التحديثات: {esc(mu['updates_url']) if mu['updates_url'] else 'غير محددة'}",
        f"الكاش: {cache_size() // (1024 * 1024)} من {mu['cache_mb']} ميجا",
        f"ffmpeg: {'جاهز' if shutil.which('ffmpeg') else 'غير موجود'} | "
        f"yt-dlp: {version or 'غير مثبت'} | الكوكيز: {'مستخدمة' if os.path.exists(COOKIES_FILE) else 'غير مستخدمة (اختياري)'}",
    ]
    for p in playing[:8]:
        lines.append(f"- {esc(title_of(p.cid))}: {esc(p.current.title)[:40]}")
    return "\n".join(lines)


def music_kb():
    mu = db["music"]
    rows = [
        [b("إيقاف القسم" if mu["enabled"] else "تشغيل القسم", "dm:tog", style="danger" if mu["enabled"] else "success"),
         b("الصلاحيات", "dm:acc", style="primary")],
        [b(f"أقصى مدة: {mu['max_min']} د", "dm:cycle:max_min", style="primary"),
         b(f"الطابور: {mu['queue_limit']}", "dm:cycle:queue_limit", style="primary")],
        [b(f"الكاش: {mu['cache_mb']} ميجا", "dm:cycle:cache_mb", style="primary"),
         b(f"الصوت: {mu['volume']}%", "dm:cycle:volume", style="primary")],
        [b("الفيديو: شغّال" if mu["video"] else "الفيديو: متوقف", "dm:flag:video",
           style="success" if mu["video"] else "danger"),
         b("أمر يوت: شغّال" if mu["yt"] else "أمر يوت: متوقف", "dm:flag:yt",
           style="success" if mu["yt"] else "danger")],
        [b("موقع اللوحة", "dm:panel", style="primary"), b("قناة التحديثات", "dm:updates", style="success")],
        [b("أماكن الميوزك", "dm:places:0", style="primary"), b("إيقاف كل التشغيل", "dm:stopall", style="danger")],
        [b("مسح الكاش", "dm:clear", style="danger"), b("تحديث yt-dlp", "dm:upd", style="success")],
        [b("فحص يوتيوب", "dm:ytcheck", style="primary"), b("إعادة المحاولة الآن", "dm:ytreset", style="success")],
        [b("بديل ساوندكلاود: شغّال" if mu["fallback"] else "بديل ساوندكلاود: متوقف", "dm:flag:fallback",
           style="success" if mu["fallback"] else "danger"),
         b("بروكسي", "dm:proxy", style="primary")],
        [b("IPv4 فقط: شغّال" if mu["ipv4"] else "IPv4 فقط: متوقف", "dm:flag:ipv4",
           style="success" if mu["ipv4"] else "danger"),
         b("تحديث تلقائي: شغّال" if mu["auto_update"] else "تحديث تلقائي: متوقف", "dm:flag:auto_update",
           style="success" if mu["auto_update"] else "danger")],
        [b(f"تحديث التقدم: {mu['progress_sec']} ث", "dm:cycle:progress_sec", style="primary"),
         b(f"تشويش الصورة: {mu['blur']}", "dm:cycle:blur", style="primary")],
        [b("صورة لوحة الميوزك", "dm:pphoto", style="success"), b("ملصق الانتظار", "dm:sticker", style="success")],
        [b("رفع الكوكيز (اختياري)", "dm:cookies", style="primary")],
        [b("رجوع", "devpanel", style="primary")],
    ]
    return kb(rows)


def music_places_view(page):
    off = db["music"]["off"]
    items = sorted(db["chats"], key=lambda ch: ch["title"].lower())
    pages = max(1, math.ceil(len(items) / PAGE_SIZE))
    page = max(0, min(page, pages - 1))
    chunk = items[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    rows = [[b(("مطفأ: " if ch["id"] in off else "شغّال: ") + ch["title"][:26], f"dm:pt:{ch['id']}:{page}",
               style="danger" if ch["id"] in off else "success")] for ch in chunk]
    nav = []
    if page > 0:
        nav.append(b("السابق", f"dm:places:{page - 1}", style="primary"))
    if page < pages - 1:
        nav.append(b("التالي", f"dm:places:{page + 1}", style="primary"))
    if nav:
        rows.append(nav)
    rows.append([b("رجوع", "dm", style="primary")])
    text = (f"<b>أماكن الميوزك</b>\nالصفحة {page + 1} من {pages}\n"
            "اضغط على المكان لتشغيل أو إيقاف أوامر الميوزك فيه.")
    return text if chunk else text + "\n\nلا توجد أماكن.", kb(rows)


async def ytdlp_autoupdate():
    """yt-dlp يتعطل كلما غيّر يوتيوب شيئًا، فنحدّثه كل 24 ساعة (يمكن إيقافه من لوحة المطور)."""
    await asyncio.sleep(120)
    while True:
        mu = db["music"]
        try:
            if mu["auto_update"] and time.time() - mu["last_update"] > 24 * 3600:
                ok, tail = await run_blocking(_pip_upgrade_ytdlp)
                mu["last_update"] = time.time()
                save()
                if ok:
                    yt_mark_ok()
                    log.info("تم تحديث yt-dlp إلى %s", ytdlp_version())
                else:
                    log.warning("فشل تحديث yt-dlp: %s", tail)
        except Exception as e:
            log.warning("autoupdate: %s", e)
        await asyncio.sleep(3600)


async def cb_dm(c):
    await reset_state(c.uid)
    sub = c.parts[1] if len(c.parts) > 1 else "main"
    arg = c.parts[2] if len(c.parts) > 2 else None
    mu = db["music"]
    note = ""
    if sub == "tog":
        mu["enabled"] = not mu["enabled"]
        if not mu["enabled"]:
            await stop_all_music()
    elif sub == "acc":
        keys = list(ACCESS_MODES)
        mu["access"] = keys[(keys.index(mu["access"]) + 1) % len(keys)]
    elif sub == "cycle" and arg in PRESETS:
        mu[arg] = next_preset(PRESETS[arg], mu[arg])
        if arg == "blur" and os.path.exists(PANEL_SRC):
            done = await rebuild_panel_photo(c.chat)
            note = "تم تطبيق التشويش الجديد على الصورة.\n\n" if done else "تعذر تطبيق التشويش على الصورة.\n\n"
    elif sub == "ytreset":
        yt_mark_ok()
        note = "أُعيد فتح يوتيوب، وسيُجرَّب عند الطلب القادم.\n\n"
    elif sub == "ytcheck":
        await c.screen("جاري فحص يوتيوب...", None)
        try:
            count = await run_blocking(_yt_probe)
            yt_mark_ok()
            note = f"يوتيوب يعمل من هذا السيرفر ({count} صيغة).\n\n"
        except Exception as e:
            if is_block_error(e):
                yt_mark_blocked(e)
            note = f"يوتيوب لا يعمل الآن:\n<code>{esc(short(e))}</code>\n\n"
    elif sub == "flag" and arg in ("video", "yt", "fallback", "ipv4", "auto_update"):
        mu[arg] = not mu[arg]
    elif sub == "panel":
        keys = list(PANEL_MODES)
        mu["panel"] = keys[(keys.index(mu["panel"]) + 1) % len(keys)]
    elif sub == "stopall":
        note = f"تم إيقاف التشغيل في {await stop_all_music()} مكان.\n\n"
    elif sub == "clear":
        keep = protected_paths()
        for name in os.listdir(MUSIC_DIR):
            path = os.path.join(MUSIC_DIR, name)
            if os.path.isfile(path) and path not in keep:
                os.remove(path)
        note = "تم مسح الكاش.\n\n"
    elif sub == "upd":
        await c.screen("جاري تحديث yt-dlp...", None)
        ok, tail = await run_blocking(_pip_upgrade_ytdlp)
        note = (f"تم التحديث. الإصدار الآن: {ytdlp_version()}\n\n" if ok
                else f"فشل التحديث:\n<code>{esc(tail)}</code>\n\n")
    elif sub == "pphoto":
        states[c.uid] = {"step": "mu_pphoto"}
        await c.screen("أرسل الصورة التي تظهر في لوحة الميوزك أثناء التشغيل.\n"
                       f"تُطبَّق عليها درجة التشويش الحالية ({mu['blur']}) ويمكنك تغييرها من زر «تشويش الصورة» "
                       "(0 = بدون تشويش).\nأرسل 0 لإزالة الصورة.", back_kb(None, "dm"))
        return
    elif sub == "sticker":
        states[c.uid] = {"step": "mu_sticker"}
        await c.screen("أرسل ملصق الميوزك الذي يردّ به البوت أثناء البحث والتحميل بدل رسالة «جاري البحث».\n"
                       "يمكنك إرسال عدة ملصقات (حتى 10) ويختار البوت منها عشوائيًا.\n"
                       "أرسل 0 لمسح الملصقات.", back_kb(None, "dm"))
        return
    elif sub == "updates":
        states[c.uid] = {"step": "mu_updates"}
        await c.screen("أرسل رابط قناة التحديثات (مثل <code>https://t.me/name</code> أو <code>@name</code>)\n"
                       "وأرسل 0 لإزالتها:", back_kb(None, "dm"))
        return
    elif sub == "proxy":
        states[c.uid] = {"step": "mu_proxy"}
        await c.screen("أرسل عنوان البروكسي مثل <code>http://user:pass@host:port</code> "
                       "أو <code>socks5://host:port</code>.\n"
                       "يمكنك إرسال عدة بروكسيات (بينها مسافة أو سطر جديد) ويبدّل البوت بينها.\n"
                       "يفيد إذا كان يوتيوب يحجب ايبي السيرفر (الأفضل بروكسي سكني). أرسل 0 لإزالته.",
                       back_kb(None, "dm"))
        return
    elif sub == "cookies":
        states[c.uid] = {"step": "mu_cookies"}
        await c.screen("أرسل ملف <code>cookies.txt</code> (صيغة Netscape) المصدَّر من متصفح مسجّل الدخول في يوتيوب.\n"
                       "يفيد عندما يحجب يوتيوب سيرفرك. احذف رسالتك بعد الإرسال لأن الكوكيز حساسة.\n"
                       "أرسل 0 لحذف الكوكيز الحالية.", back_kb(None, "dm"))
        return
    elif sub == "places":
        text, markup = music_places_view(int(arg or 0))
        await c.screen(text, markup)
        return
    elif sub == "pt":
        cid, page = int(c.parts[2]), int(c.parts[3])
        if cid in mu["off"]:
            mu["off"].remove(cid)
        else:
            mu["off"].append(cid)
            await music_stop(cid)
        save()
        text, markup = music_places_view(page)
        await c.screen(text, markup)
        return
    save()
    await c.screen(note + music_view(), music_kb())


async def step_mu_updates(m, chat, uid, text, st):
    states.pop(uid, None)
    link = text.strip()
    if link == "0":
        db["music"]["updates_url"] = ""
        msg = "تمت إزالة قناة التحديثات."
    else:
        link = "https://t.me/" + link.lstrip("@") if not link.startswith("http") else link
        if not link.startswith("https://t.me/"):
            await show(chat, "الرابط يجب أن يبدأ بـ https://t.me/", back_kb(None, "dm"), photo=False)
            return
        db["music"]["updates_url"] = link
        msg = "تم حفظ قناة التحديثات."
    save()
    await show(chat, msg + "\n\n" + music_view(), music_kb(), photo=False)


async def step_mu_proxy(m, chat, uid, text, st):
    items = [x for x in re.split(r"[\s,]+", text.strip()) if x]
    valid = all(re.match(r"^(https?|socks[45]h?)://\S+$", x) for x in items)
    if text.strip() != "0" and (not items or not valid):
        await show(chat, "الصيغة غير صحيحة. مثال: <code>http://user:pass@host:port</code>\n"
                         "ويمكنك إرسال عدة بروكسيات تفصل بينها مسافة أو سطر جديد.",
                   back_kb(None, "dm"), photo=False)
        return
    states.pop(uid, None)
    clear = text.strip() == "0"
    db["music"]["proxy"] = "" if clear else " ".join(items)
    save()
    await api("deleteMessage", chat_id=chat, message_id=m["message_id"])    # فيه بيانات دخول
    await show(chat, ("تمت إزالة البروكسي." if clear else f"تم حفظ {len(items)} بروكسي وحذفت رسالتك.")
               + "\n\n" + music_view(), music_kb(), photo=False)


async def step_mu_cookies(m, chat, uid, text, st):
    back = back_kb(None, "dm")
    if text == "0":
        if os.path.exists(COOKIES_FILE):
            os.remove(COOKIES_FILE)
        states.pop(uid, None)
        await show(chat, "تم حذف الكوكيز.\n\n" + music_view(), music_kb(), photo=False)
        return
    doc = m.get("document")
    if not doc:
        await show(chat, "أرسل الملف كمستند، أو 0 للحذف.", back, photo=False)
        return
    info = await api("getFile", file_id=doc["file_id"])
    if not info.get("ok"):
        await show(chat, "تعذر جلب الملف من تيليجرام.", back, photo=False)
        return
    url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['result']['file_path']}"
    async with HTTP.get(url) as r:
        raw = await r.read()
    body = raw.decode("utf-8", "ignore")
    if "\t" not in body:
        await show(chat, "الملف لا يبدو بصيغة Netscape (أعمدة مفصولة بـ Tab).", back, photo=False)
        return
    with open(COOKIES_FILE, "w", encoding="utf-8") as f:
        f.write(body)
    states.pop(uid, None)
    await api("deleteMessage", chat_id=chat, message_id=m["message_id"])
    await show(chat, "تم حفظ الكوكيز وحذفت رسالتك.\n\n" + music_view(), music_kb(), photo=False)


# ------------------------------------------------------------------ صورة اللوحة وشريط التقدم والمستخدمون
PANEL_SRC = "music_panel_src.jpg"


def _blur_image(path, radius):
    from PIL import Image, ImageFilter
    img = Image.open(path).convert("RGB")
    img.thumbnail((1280, 1280))
    if radius:
        img = img.filter(ImageFilter.GaussianBlur(radius))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue()


async def rebuild_panel_photo(chat):
    """يطبّق التشويش على الصورة الأصلية ويرفعها مرة واحدة ليأخذ file_id نعيد استعماله مع كل لوحة."""
    if not os.path.exists(PANEL_SRC):
        return False
    try:
        data = await run_blocking(_blur_image, PANEL_SRC, db["music"]["blur"])
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat))
        form.add_field("photo", data, filename="panel.jpg", content_type="image/jpeg")
        async with HTTP.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto", data=form) as r:
            d = await r.json()
    except Exception as e:
        log.warning("panel photo: %s", e)
        return False
    if not d.get("ok"):
        log.warning("panel photo: %s", d.get("description"))
        return False
    db["music"]["panel_photo"] = d["result"]["photo"][-1]["file_id"]
    save()
    await api("deleteMessage", chat_id=chat, message_id=d["result"]["message_id"])
    return True


async def step_mu_pphoto(m, chat, uid, text, st):
    back = back_kb(None, "dm")
    if text == "0":
        if os.path.exists(PANEL_SRC):
            os.remove(PANEL_SRC)
        db["music"]["panel_photo"] = ""
        save()
        states.pop(uid, None)
        await show(chat, "تمت إزالة صورة اللوحة.\n\n" + music_view(), music_kb(), photo=False)
        return
    if not m.get("photo"):
        await show(chat, "أرسل صورة (وليس ملفًا)، أو 0 للإزالة.", back, photo=False)
        return
    info = await api("getFile", file_id=m["photo"][-1]["file_id"])
    if not info.get("ok"):
        await show(chat, "تعذر جلب الصورة من تيليجرام.", back, photo=False)
        return
    async with HTTP.get(f"https://api.telegram.org/file/bot{BOT_TOKEN}/{info['result']['file_path']}") as r:
        raw = await r.read()
    with open(PANEL_SRC, "wb") as f:
        f.write(raw)
    states.pop(uid, None)
    done = await rebuild_panel_photo(chat)
    msg = ("تم حفظ صورة اللوحة وتطبيق التشويش عليها." if done
           else "تعذر معالجة الصورة (تأكد أن مكتبة pillow مثبتة).")
    await show(chat, msg + "\n\n" + music_view(), music_kb(), photo=False)


async def step_mu_sticker(m, chat, uid, text, st):
    back = back_kb(None, "dm")
    stickers = db["music"]["stickers"]
    if text == "0":
        stickers.clear()
        save()
        states.pop(uid, None)
        await show(chat, "تم مسح الملصقات، وسيعود البوت لرسائل «جاري البحث» النصية.\n\n" + music_view(),
                   music_kb(), photo=False)
        return
    sticker = m.get("sticker")
    if not sticker:
        await show(chat, "أرسل ملصقًا، أو 0 لمسح الملصقات.", back, photo=False)
        return
    if sticker["file_id"] not in stickers:
        if len(stickers) >= 10:
            await show(chat, "وصلت للحد الأقصى (10 ملصقات). أرسل 0 لمسحها أولًا.", back, photo=False)
            return
        stickers.append(sticker["file_id"])
        save()
    await show(chat, f"تمت الإضافة. عدد الملصقات: <b>{len(stickers)}</b>\nأرسل ملصقًا آخر أو ارجع.",
               back, photo=False)


async def music_ticker():
    """يحدّث شريط التقدم في لوحات التشغيل الجارية كل progress_sec ثانية (تعديل فقط، دون إعادة إرسال)."""
    while True:
        await asyncio.sleep(2)
        try:
            step = db["music"]["progress_sec"]
            now = time.monotonic()
            for p in list(players.values()):
                if p.current is None or p.paused or p.busy or not p.panels:
                    continue
                if now - p.last_edit >= step:
                    await panel_show(p, resend=False)
        except Exception as e:
            log.warning("ticker: %s", e)


async def cb_mutog(c):
    """زر المستخدم: تفعيل الميوزك أو إلغاؤه في أماكنه."""
    off = db["music"]["user_off"]
    if c.uid in off:
        off.remove(c.uid)
    else:
        off.append(c.uid)
        for ch in my_chats(c.uid):
            await music_stop(ch["id"])
    save()
    await panel(c.chat, c.uid, c.mid)


async def userbot_set_ban(cid, target, banned):
    try:
        await UB(functions.channels.EditBannedRequest(
            await UB.get_input_entity(cid), await UB.get_input_entity(target),
            types.ChatBannedRights(until_date=None, view_messages=banned)))
        return True
    except Exception as e:
        log.warning("ban via userbot: %s", e)
        return False


# ---- إحصائيات المستخدمين
PEOPLE_PAGE = 8
_fill_failed = set()


async def fill_people(limit=80):
    """يجلب اسم ويوزر كل من فتح البوت قبل أن نبدأ التسجيل (يعمل لمن راسل البوت فقط)."""
    ids = [uid for uid in (set(KNOWN) | {int(k) for k in db["subs"]})
           if "name" not in db["people"].get(str(uid), {}) and uid not in _fill_failed]
    done = 0
    for uid in ids[:limit]:
        d = await api("getChat", chat_id=uid)
        res = d.get("result") or {}
        if d.get("ok"):
            record_person(uid, tg_name({**res, "id": uid}), res.get("username") or "", bot=True, touch=False)
            done += 1
        else:
            _fill_failed.add(uid)
        await asyncio.sleep(0.1)
    return done


def people_view(page):
    items = sorted(db["people"].items(),
                   key=lambda kv: (-(kv[1].get("plays", 0) + kv[1].get("dls", 0)), -kv[1].get("last", 0)))
    total = len(items)
    pages = max(1, math.ceil(total / PEOPLE_PAGE))
    page = max(0, min(page, pages - 1))
    head = ["<b>إحصائيات المستخدمين</b>", "",
            f"المسجلون: <b>{total}</b> | فتحوا البوت: <b>{sum(1 for _, r in items if r.get('bot'))}</b>",
            f"طلبات التشغيل: <b>{sum(r.get('plays', 0) for _, r in items)}</b> | "
            f"تحميلات يوت: <b>{sum(r.get('dls', 0) for _, r in items)}</b>",
            f"الصفحة {page + 1} من {pages}"]
    blocks = []
    start = page * PEOPLE_PAGE
    for i, (uid, r) in enumerate(items[start:start + PEOPLE_PAGE], start + 1):
        username = f"@{r['un']}" if r.get("un") else "بدون يوزر"
        sub, tag = sub_of(int(uid)), ""
        if is_dev(int(uid)):
            tag = " | مطور"
        elif sub:
            tag = " | VIP" if sub.get("vip") else " | مشترك"
        seen = fmt(datetime.fromtimestamp(r["last"], timezone.utc)) if r.get("last") else "غير معروف"
        blocks.append(f"<b>{i}.</b> {mention(int(uid), r.get('name'))} {esc(username)}{tag}\n"
                      f"الايدي: <code>{uid}</code> | طلبات: {r.get('plays', 0)} | تحميل: {r.get('dls', 0)}\n"
                      f"آخر نشاط: {seen}")
    text = "\n".join(head) + "\n\n" + ("\n\n".join(blocks) if blocks else "لا يوجد مستخدمون بعد.")
    rows, nav = [], []
    if page > 0:
        nav.append(b("السابق", f"ust:{page - 1}", style="primary"))
    if page < pages - 1:
        nav.append(b("التالي", f"ust:{page + 1}", style="primary"))
    if nav:
        rows.append(nav)
    rows.append([b("تحديث اليوزرات", "ustfill", style="success")])
    return text, back_kb(rows, "devpanel")


async def cb_ust(c):
    page = int(c.parts[1]) if len(c.parts) > 1 and c.parts[1].isdigit() else 0
    text, markup = people_view(page)
    await c.screen(text, markup)


async def cb_ustfill(c):
    count = await fill_people()
    text, markup = people_view(0)
    await c.screen(f"تم جلب بيانات {count} مستخدم.\n\n" + text, markup)


# ------------------------------------------------------------------ مهام الخلفية
_background = set()


def spawn(coro):
    """يشغّل مهمة في الخلفية ويحتفظ بمرجعها حتى لا يجمعها garbage collector."""
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)
    return task


async def reset_state(uid):
    """يلغي أي خطوة إدخال معلّقة وأي عملية تسجيل دخول لم تكتمل."""
    states.pop(uid, None)
    pending = logins.pop(uid, None)
    if pending:
        await pending["c"].disconnect()


# ------------------------------------------------------------------ قراءة مدخلات المطور
def forwarded_user(m):
    return ((m.get("forward_from") or {}).get("id")
            or ((m.get("forward_origin") or {}).get("sender_user") or {}).get("id"))


def target_from(m, text):
    """ايدي مستخدم من رسالة محوّلة أو من أرقام مكتوبة."""
    digits = re.sub(r"\D", "", text)
    return forwarded_user(m) or (int(digits) if digits else None)


def parse_sub(m, text):
    """«الايدي أيام [v]» أو رسالة محوّلة ثم «أيام [v]». يرجع (الايدي، الأيام، vip)."""
    numbers = re.findall(r"\d+", text)
    vip = bool(re.search(r"(?<![A-Za-z])[vV](?![A-Za-z])", text))
    fwd = forwarded_user(m)
    if fwd:
        return fwd, (int(numbers[0]) if numbers else None), vip
    target = int(numbers[0]) if numbers else None
    days = int(numbers[1]) if len(numbers) > 1 else None
    return target, days, vip


# ------------------------------------------------------------------ خطوات الإدخال (رسائل)
async def step_phone(m, chat, uid, text, st):
    old = logins.pop(uid, None)
    if old:
        await old["c"].disconnect()
    phone = text.replace(" ", "")
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
    except Exception as e:
        await client.disconnect()
        states.pop(uid, None)
        await show(chat, f"فشل إرسال الكود: <code>{esc(e)}</code>", back_kb(None, "devpanel"), photo=False)
        return
    logins[uid] = {"c": client, "phone": phone, "hash": sent.phone_code_hash}
    states[uid] = {"step": "code"}
    await show(chat, "وصلك كود التحقق على الحساب.\n\n"
                     "أرسله <b>بينه فواصل أو شرطات</b> مثل: <code>1-2-3-4-5</code>\n"
                     "(تيليجرام يلغي الكود إذا أرسلته متصلًا)", back_kb(None, "devpanel"), photo=False)


async def step_login(m, chat, uid, text, st):
    login = logins.get(uid)
    if not login:
        states.pop(uid, None)
        return
    back = back_kb(None, "devpanel")
    try:
        if st["step"] == "code":
            await login["c"].sign_in(login["phone"], re.sub(r"\D", "", text),
                                     phone_code_hash=login["hash"])
        else:
            await login["c"].sign_in(password=text)
    except SessionPasswordNeededError:
        states[uid] = {"step": "password"}
        await show(chat, "الحساب عليه تحقق بخطوتين، أرسل كلمة المرور:", back, photo=False)
        return
    except Exception as e:
        await show(chat, f"خطأ: <code>{esc(e)}</code>\nحاول مرة أخرى.", back, photo=False)
        return
    db["session"] = login["c"].session.save()
    save()
    await login["c"].disconnect()
    logins.pop(uid, None)
    states.pop(uid, None)
    started = await start_userbot()
    await show(chat, "تم تسجيل الدخول وتشغيل الحساب المساعد." if started else "تم الحفظ لكن تعذر التشغيل.")
    await dev_panel(chat, uid)


async def resolve_place(raw):
    """يرجع كيان المكان من يوزر أو ايدي أو رابط، وينضم إليه إن لزم."""
    text = raw.strip()
    invite = re.search(r"(?:\+|joinchat/)([\w-]+)$", text)
    if invite:
        try:
            res = await UB(functions.messages.ImportChatInviteRequest(invite.group(1)))
            return res.chats[0]
        except Exception:
            info = await UB(functions.messages.CheckChatInviteRequest(invite.group(1)))
            return info.chat
    text = text.replace("https://t.me/", "").replace("@", "")
    entity = await UB.get_entity(int(text) if re.fullmatch(r"-?\d+", text) else text)
    if isinstance(entity, types.Channel) and getattr(entity, "left", False):
        try:
            await UB(functions.channels.JoinChannelRequest(entity))
        except Exception:
            pass
    return entity


async def step_addchat(m, chat, uid, text, st):
    back = back_kb(None, "home")
    if not UB:
        states.pop(uid, None)
        await show(chat, "الحساب المساعد غير جاهز حاليًا، تواصل مع المطور.", back, photo=False)
        return
    if len(my_chats(uid)) >= limit_of(uid):
        states.pop(uid, None)
        extra = "" if is_vip(uid) or is_dev(uid) else "\nاشتراك VIP يتيح 3 أماكن."
        await show(chat, f"وصلت للحد المسموح ({limit_of(uid)}).{extra}", back, photo=False)
        return
    try:
        entity = await resolve_place(text)
        cid = utils.get_peer_id(entity)
        if chat_of(cid):
            await show(chat, "هذا المكان مضاف مسبقًا.", back, photo=False)
        else:
            ch = {"id": cid, "title": utils.get_display_name(entity), "owner": uid,
                  "username": getattr(entity, "username", None), "mic": False, "novideo": False,
                  "added": now_ts()}
            db["chats"].append(ch)
            save()
            await show(chat, f"تمت إضافة: <b>{esc(ch['title'])}</b>\n"
                             "سيدخل الحساب المساعد فور فتح محادثة مرئية.",
                       back_kb([[b("إضافة آخر", "addchat", style="success")]]), photo=False)
            spawn(join_soon(ch))
        states.pop(uid, None)
    except Exception as e:
        await show(chat, f"تعذر الوصول: <code>{esc(e)}</code>\nتأكد أن الحساب المساعد عضو فيه.",
                   back, photo=False)


async def step_setnotify(m, chat, uid, text, st):
    fwd_chat = m.get("forward_from_chat") or (m.get("forward_origin") or {}).get("chat") or {}
    name = text.replace("@", "").replace("https://t.me/", "").strip()
    try:
        if fwd_chat.get("id"):
            cid = fwd_chat["id"]
        elif re.fullmatch(r"-?\d+", name):
            cid = int(name)
        else:
            res = await api("getChat", chat_id="@" + name)
            if not res.get("ok"):
                raise Exception("لم أجد هذا المكان")
            cid = res["result"]["id"]
        test = await api("sendMessage", chat_id=cid, text="تم تعيين هذا المكان لاستقبال الإشعارات")
        if not test.get("ok"):
            raise Exception("البوت ليس عضوًا أو مشرفًا هناك")
        db["unotify"][str(uid)] = cid
        save()
        states.pop(uid, None)
        await panel(chat, uid)
    except Exception as e:
        await show(chat, f"تعذر التعيين: <code>{esc(e)}</code>\n"
                         "تأكد أن البوت مشرف في القناة أو عضو في الكروب.",
                   back_kb(None, "notify"), photo=False)


async def step_photo(m, chat, uid, text, st):
    if not m.get("photo"):
        await show(chat, "أرسل صورة فقط", back_kb(None, "devpanel"), photo=False)
        return
    db["photo"] = m["photo"][-1]["file_id"]
    save()
    states.pop(uid, None)
    await show(chat, "تم تغيير صورة الرد.")
    await dev_panel(chat, uid)


async def step_bcast(m, chat, uid, text, st):
    kind = st["to"]
    states[uid] = {"step": "bcconfirm", "to": kind, "mid": m["message_id"], "chat": chat}
    await show(chat, f"<b>تأكيد الإذاعة</b>\n\nالفئة: {BC_NAMES[kind]}\n"
                     f"عدد المستلمين: <b>{len(bc_targets(kind))}</b>\n\n"
                     "سيُرسل لهم نفس رسالتك أعلاه كما هي. هل تؤكد؟",
               back_kb([[b("تأكيد الإرسال", "bcgo", style="success")]], "devpanel"), photo=False)


async def step_emoji(m, chat, uid, text, st):
    key = st["key"]
    is_text_slot = key.startswith(("t:", "f:"))
    back = "txtem" if is_text_slot else "emoji"
    entities = [e.get("custom_emoji_id") for e in (m.get("entities") or [])
                if e.get("type") == "custom_emoji"]
    if text == "0":
        eid = "0"
    elif entities:
        eid = entities[0]                    # الايموجي المميز نفسه كما أرسله المطور
    else:
        eid = re.sub(r"\D", "", text)        # أو رقمه مباشرة
    if not eid:
        await show(chat, "أرسل الايموجي المميز نفسه (أو رقمه)، أو 0 لإزالته.",
                   back_kb(None, back), photo=False)
        return
    if int(eid) == 0:
        db["emoji"].pop(key, None)
    else:
        db["emoji"][key] = str(eid)
    save()
    states.pop(uid, None)
    body, markup = text_stickers_view() if is_text_slot else emoji_view()
    await show(chat, "تم التطبيق فورًا.\n\n" + body, markup, photo=False)


async def step_adduser(m, chat, uid, text, st):
    target, days, vip = parse_sub(m, text)
    if not target or not days or not (0 < days <= 3650):
        await show(chat, "الصيغة غير صحيحة.\nأرسل: <code>الايدي عدد_الأيام</code>\n"
                         "ولرفعه VIP: <code>الايدي عدد_الأيام v</code>",
                   back_kb(None, "devpanel"), photo=False)
        return
    old = sub_of(target)
    sub = dict(old) if old else {}
    sub["exp"] = (max(now_ts(), old["exp"]) if old else now_ts()) + days * 86400
    sub["vip"] = True if vip else bool(old and old.get("vip"))
    db["subs"][str(target)] = sub
    save()
    states.pop(uid, None)
    kind = "VIP" if sub["vip"] else "عادي"
    await show(chat, f"تم تفعيل اشتراك <code>{target}</code>\nالنوع: <b>{kind}</b>\n"
                     f"أضيف: {days} يوم\nالمتبقي: {days_left(sub)} يوم\n"
                     f"ينتهي: {fmt(datetime.fromtimestamp(sub['exp'], timezone.utc))}",
               back_kb([[b("اشتراك آخر", "adduser", style="success")]], "devpanel"), photo=False)
    await api("sendMessage", chat_id=target, parse_mode="HTML",
              text=f"<b>تم تفعيل اشتراكك</b>\nالنوع: <b>{kind}</b>\nالمتبقي: {days_left(sub)} يوم\n"
                   "أرسل /start لفتح لوحتك.")


async def step_deluser(m, chat, uid, text, st):
    target = target_from(m, text)
    if not target:
        await show(chat, "أرسل الايدي (أرقام) أو حوّل رسالة منه.", back_kb(None, "devpanel"), photo=False)
        return
    if db["subs"].pop(str(target), None) is not None:
        gone = my_chats(target)
        db["chats"] = [c for c in db["chats"] if c.get("owner") != target]
        for ch in gone:
            await leave(ch["id"])
        msg = f"تم حذف المشترك <code>{target}</code> وأماكنه ({len(gone)})"
    else:
        msg = "هذا الايدي ليس مشتركًا."
    save()
    states.pop(uid, None)
    await show(chat, msg, back_kb(None, "devpanel"), photo=False)


async def step_adddev(m, chat, uid, text, st):
    target = target_from(m, text)
    if not target:
        await show(chat, "أرسل الايدي (أرقام) أو حوّل رسالة منه.", back_kb(None, "devpanel"), photo=False)
        return
    states.pop(uid, None)
    if target == OWNER_ID or target in db["devs"]:
        msg = "هذا الشخص مطور بالفعل."
    else:
        db["devs"].append(target)
        save()
        msg = f"تمت إضافة المطور <code>{target}</code>"
        await api("sendMessage", chat_id=target, text="تمت إضافتك كمطور في البوت. أرسل /start لفتح لوحتك.")
    await show(chat, msg + "\n\nالمطورون:\n" + dev_lines(), back_kb(None, "devpanel"), photo=False)


async def step_deldev(m, chat, uid, text, st):
    target = target_from(m, text)
    if not target:
        await show(chat, "أرسل الايدي (أرقام) أو حوّل رسالة منه.", back_kb(None, "devpanel"), photo=False)
        return
    states.pop(uid, None)
    if target == OWNER_ID:
        msg = "المطور الأساسي لا يمكن حذفه."
    elif target in db["devs"]:
        db["devs"].remove(target)
        save()
        msg = f"تم حذف المطور <code>{target}</code>"
        await api("sendMessage", chat_id=target, text="تم إنهاء صلاحية المطور الخاصة بك.")
    else:
        msg = "هذا الايدي ليس مطورًا."
    await show(chat, msg + "\n\nالمطورون:\n" + dev_lines(), back_kb(None, "devpanel"), photo=False)


# الخطوة -> (الدالة، من يحق له: None للجميع | "dev" | "owner")
STEPS = {
    "phone": (step_phone, "dev"), "code": (step_login, "dev"), "password": (step_login, "dev"),
    "addchat": (step_addchat, None), "setnotify": (step_setnotify, None),
    "photo": (step_photo, "dev"), "bcast": (step_bcast, "dev"), "emoji": (step_emoji, "dev"),
    "adduser": (step_adduser, "dev"), "deluser": (step_deluser, "dev"),
    "adddev": (step_adddev, "owner"), "deldev": (step_deldev, "owner"),
    "mu_updates": (step_mu_updates, "dev"), "mu_cookies": (step_mu_cookies, "dev"),
    "mu_proxy": (step_mu_proxy, "dev"),
    "mu_pphoto": (step_mu_pphoto, "dev"), "mu_sticker": (step_mu_sticker, "dev"),
}


async def handle_message(m):
    chat = m["chat"]["id"]
    uid = (m.get("from") or {}).get("id")
    text = (m.get("text") or "").strip()

    if m["chat"]["type"] != "private":      # داخل مجموعة: فقط تحديد كروب الإشعارات
        if text.startswith("/setgroup") and is_auth(uid):
            db["unotify"][str(uid)] = chat
            save()
            await api("sendMessage", chat_id=chat, text="تم تعيين هذا الكروب لاستقبال إشعاراتك")
        return

    remember_user(uid)
    if m.get("from"):
        record_person(uid, tg_name(m["from"]), m["from"].get("username") or "", bot=True)
    if not is_auth(uid):
        await api("sendMessage", chat_id=chat, parse_mode="HTML",
                  text=deco("<b>لا تملك اشتراكًا فعّالًا في هذا البوت.</b>\nتواصل مع المطور للاشتراك."),
                  reply_markup=kb([[b("مراسلة المطور", url=DEV_URL, style="success")]]))
        return

    if text in ("/start", "/panel"):
        await reset_state(uid)
        await panel(chat, uid)
        return

    st = states.get(uid)
    if not st:
        await panel(chat, uid)
        return
    entry = STEPS.get(st["step"])
    if not entry:                           # خطوة لا تنتظر نصًا (مثل تأكيد الإذاعة)
        states.pop(uid, None)
        await panel(chat, uid)
        return
    handler, need = entry
    if (need == "dev" and not is_dev(uid)) or (need == "owner" and uid != OWNER_ID):
        return
    await handler(m, chat, uid, text, st)


# ------------------------------------------------------------------ معالجة الأزرار
class Ctx:
    """سياق الضغطة: من ضغط، وأين، وعلى ماذا."""

    def __init__(self, q):
        msg = q["message"]
        self.qid = q["id"]
        self.user = q["from"]
        self.uid = self.user["id"]
        self.chat = msg["chat"]["id"]
        self.mid = msg["message_id"]
        self.editable = "photo" not in msg
        self.data = q["data"]
        self.parts = self.data.split(":")
        self.act = self.parts[0]

    async def screen(self, text, markup=None, kind=None):
        await show(self.chat, text, markup, self.mid, photo=False, kind=kind, editable=self.editable)

    async def toast(self, text=None, alert=False):
        params = {"callback_query_id": self.qid}
        if text:
            params.update(text=text, show_alert=alert)
        await api("answerCallbackQuery", **params)


def managed_place(c):
    """المكان الذي ضغط عليه المستخدم إن كان يملك صلاحية إدارته."""
    cid, scope = cb_target(c.data)
    ch = chat_of(cid)
    return (ch if ch and can_manage(c.uid, ch) else None), scope


def list_for(uid, scope):
    return chats_view(uid) if scope.startswith("m") else places_view(scope)


async def cb_ban(c):
    """الحظر وفكه: متاح لمشرفي المجموعة أصحاب الصلاحية وليس للمشتركين فقط."""
    await api("answerCallbackQuery", callback_query_id=c.qid)
    cid, target = int(c.parts[1]), int(c.parts[2])
    ch = chat_of(cid)
    if not ch or not await can_ban(c.uid, ch):
        await api("sendMessage", chat_id=c.chat, reply_to_message_id=c.mid,
                  text="الحظر متاح فقط للمشرفين الذين لديهم صلاحية حظر الأعضاء في هذه المجموعة")
        return
    unban = c.act == "unban"
    is_chan = target < 0                      # القنوات ايديها سالب وتُحظر كـ sender chat
    if unban:
        if is_chan:
            ok = (await api("unbanChatSenderChat", chat_id=cid, sender_chat_id=target)).get("ok")
        else:
            ok = (await api("unbanChatMember", chat_id=cid, user_id=target, only_if_banned=True)).get("ok")
        if not ok and UB:
            ok = await userbot_set_ban(cid, target, False)
        next_btn = b("حظر المستخدم", f"ban:{cid}:{target}", style="danger")
    else:
        if is_chan:
            ok = (await api("banChatSenderChat", chat_id=cid, sender_chat_id=target)).get("ok")
        else:
            ok = (await api("banChatMember", chat_id=cid, user_id=target)).get("ok")
        if not ok and UB:
            ok = await userbot_set_ban(cid, target, True)
        next_btn = b("فك الحظر", f"unban:{cid}:{target}", style="success")
    if not ok:
        await api("sendMessage", chat_id=c.chat, reply_to_message_id=c.mid,
                  text="تعذر التنفيذ (تأكد من صلاحيات الإشراف)")
        return
    if not unban:
        db["stats"]["bans"] += 1
        save()
    await api("editMessageReplyMarkup", chat_id=c.chat, message_id=c.mid, reply_markup=kb([[next_btn]]))
    actor = " ".join(x for x in (c.user.get("first_name"), c.user.get("last_name")) if x) or str(c.uid)
    await announce_ban(ch, c.uid, actor, target, await name_of(target),
                       reply=(c.chat, c.mid), unban=unban)


async def cb_home(c):
    await reset_state(c.uid)
    await panel(c.chat, c.uid, c.mid)


async def cb_devpanel(c):
    await reset_state(c.uid)
    await dev_panel(c.chat, c.uid, c.mid)


async def cb_guide(c):
    dev_guide = len(c.parts) > 1 and c.parts[1] == "dev" and is_dev(c.uid)
    if dev_guide:
        await c.screen(DEV_GUIDE, back_kb(None, "devpanel"), kind="guide")
        return
    extra = [[b("دليل المطور", "guide:dev", style="success")]] if is_dev(c.uid) else None
    await c.screen(USER_GUIDE, back_kb(extra, "home"), kind="guide")


async def cb_me(c):
    if is_dev(c.uid):
        text = "أنت المطور، لا يوجد حد للاشتراك أو للأماكن."
    else:
        sub = sub_of(c.uid)
        text = (f"<b>اشتراكي</b>\n\nالنوع: <b>{'VIP' if sub.get('vip') else 'عادي'}</b>\n"
                f"المتبقي: <b>{days_left(sub)}</b> يوم\n"
                f"ينتهي: {fmt(datetime.fromtimestamp(sub['exp'], timezone.utc))}\n"
                f"الأماكن المسموحة: {limit_of(c.uid)}")
    await c.screen(text, back_kb())


async def cb_login(c):
    if db["session"]:
        await c.screen("الحساب مسجّل مسبقًا.", back_kb(None, "devpanel"))
        return
    states[c.uid] = {"step": "phone"}
    await c.screen("أرسل رقم الحساب المساعد مع رمز الدولة:\nمثال: <code>+9647700000000</code>",
                   back_kb(None, "devpanel"))


async def cb_acc(c):
    if UB:
        me = await UB.get_me()
        text = (f"<b>{esc(utils.get_display_name(me))}</b>\nالايدي: <code>{me.id}</code>\n"
                f"محادثات مرئية نشطة الآن: <b>{len(active)}</b>")
    else:
        text = "الحساب غير متصل."
    await c.screen(text, back_kb(None, "devpanel"))


async def cb_logout(c):
    await stop_userbot()
    db["session"] = ""
    save()
    await dev_panel(c.chat, c.uid, c.mid)


async def cb_chats(c):
    text, markup = chats_view(c.uid)
    await c.screen(text, markup)


async def cb_places(c):
    text, markup = places_view(c.parts[1] if len(c.parts) > 1 else "a0")
    await c.screen(text, markup)


async def cb_chat(c):
    ch, scope = managed_place(c)
    if ch:
        text, markup = chat_view(ch, scope)
        await c.screen(text, markup)


async def cb_live(c):
    ch, scope = managed_place(c)
    if ch:
        text, markup = live_view(ch, scope)
        await c.screen(text, markup)


async def cb_policy(c):
    """الميكروفون وحظر الفيديو."""
    ch, scope = managed_place(c)
    if not ch:
        return
    key = {"chmic": "mic", "chvid": "novideo", "chban": "ban_channels"}[c.act]
    ch[key] = not ch.get(key, key == "ban_channels")
    save()
    note = ""
    if UB and ch["id"] in active and key != "ban_channels":
        try:
            if key == "mic":
                call = await get_call(ch["id"])
                if call:
                    await UB(functions.phone.ToggleGroupCallSettingsRequest(
                        call=call, join_muted=not ch["mic"]))
            elif ch["novideo"]:
                await sweep_video(ch)
        except Exception as e:
            note = (f"تم الحفظ لكن تعذر التطبيق الآن: <code>{esc(e)}</code>\n"
                    "تأكد أن الحساب المساعد مشرف وله صلاحية إدارة المحادثات المرئية.\n\n")
    text, markup = chat_view(ch, scope)
    await c.screen(note + text, markup)


async def cb_vc(c):
    """تشغيل وإنهاء المحادثة المرئية."""
    ch, scope = managed_place(c)
    if not ch:
        return
    if not UB:
        msg = "الحساب المساعد غير جاهز."
    else:
        try:
            if c.act == "vcstart":
                if ch["id"] in active:
                    msg = "المحادثة المرئية قائمة بالفعل."
                else:
                    await vc_start(ch["id"])
                    spawn(join_soon(ch))
                    msg = "تم تشغيل المحادثة المرئية وسيدخل الحساب المساعد فورًا."
            else:
                await vc_end(ch["id"])
                msg = "تم إنهاء المحادثة المرئية، وسيصلك التقرير الكامل."
        except Exception as e:
            msg = (f"تعذر التنفيذ: <code>{esc(e)}</code>\n"
                   "تأكد أن الحساب المساعد مشرف وله صلاحية إدارة المحادثات المرئية.")
    text, markup = chat_view(ch, scope)
    await c.screen(msg + "\n\n" + text, markup)


async def cb_addchat(c):
    if len(my_chats(c.uid)) >= limit_of(c.uid):
        extra = "" if is_vip(c.uid) or is_dev(c.uid) else "\nاشتراك VIP يتيح 3 أماكن."
        await c.screen(f"وصلت للحد المسموح ({limit_of(c.uid)}).{extra}", back_kb())
        return
    states[c.uid] = {"step": "addchat"}
    await c.screen("أرسل يوزر أو ايدي أو رابط الكروب/القناة:", back_kb())


async def cb_delchat(c):
    """يطلب تأكيدًا قبل الحذف."""
    ch, scope = managed_place(c)
    if not ch:
        return
    tail = f"{ch['id']}:{scope}"
    await c.screen(f"حذف <b>{esc(ch['title'])}</b>؟\nسيخرج الحساب المساعد منه وتتوقف مراقبته.",
                   kb([[b("تأكيد الحذف", f"delok:{tail}", style="danger"),
                        b("إلغاء", f"chat:{tail}", style="primary")]]))


async def cb_delok(c):
    ch, scope = managed_place(c)
    if ch:
        await leave(ch["id"])
        db["chats"] = [x for x in db["chats"] if x["id"] != ch["id"]]
        save()
    text, markup = list_for(c.uid, scope)
    await c.screen("تم الحذف.\n\n" + text, markup)


async def cb_notify(c):
    current = db["unotify"].get(str(c.uid))
    text = ("<b>كروب الإشعارات</b>\n\n"
            f"الحالي: <code>{current or 'غير محدد (لن تصلك أي إشعارات حتى تحدده)'}</code>\n\n"
            "إشعارات الصعود والخروج والحظر تصل إلى القناة أو الكروب الذي تحدده.\n"
            "الطريقة الأولى: اضغط تحديد ثم أرسل ايدي أو يوزر القناة أو الكروب، أو حوّل رسالة منه.\n"
            "الطريقة الثانية: أضف البوت للكروب وأرسل فيه <code>/setgroup</code>.\n"
            "في القناة يجب أن يكون البوت مشرفًا بصلاحية النشر.")
    await c.screen(text, back_kb([[b("تحديد كروب الإشعارات", "setnotify", style="success"),
                                   b("إلغاء الكروب", "notifyreset", style="danger")]]))


async def cb_setnotify(c):
    states[c.uid] = {"step": "setnotify"}
    await c.screen("أرسل ايدي القناة أو الكروب (مثل <code>-100123...</code>) أو يوزره،\n"
                   "أو حوّل رسالة منه:", back_kb(None, "notify"))


async def cb_notifyreset(c):
    db["unotify"].pop(str(c.uid), None)
    save()
    await panel(c.chat, c.uid, c.mid)


async def cb_photo(c):
    states[c.uid] = {"step": "photo"}
    await c.screen("أرسل الصورة الجديدة:", back_kb(None, "devpanel"))


async def cb_stats(c):
    s = db["stats"]
    await c.screen(f"<b>الإحصائيات</b>\n\nصعود: <b>{s['joins']}</b>\nخروج: <b>{s['leaves']}</b>\n"
                   f"حظر: <b>{s['bans']}</b>\nأماكن: <b>{len(db['chats'])}</b>\n"
                   f"محادثات مرئية نشطة: <b>{len(active)}</b>\n"
                   f"المشتركون: <b>{len(db['subs'])}</b> (VIP: {vip_count()})\n"
                   f"المستخدمون المسجلون: <b>{len(db['people'])}</b>\n"
                   f"تشغيلات الموسيقى: <b>{db['music']['plays']}</b> | تحميلات يوت: <b>{db['music']['downloads']}</b>",
                   back_kb([[b("إحصائيات المستخدمين", "ust:0", style="primary")]], "devpanel"))


async def cb_toggle(c):
    db["enabled"] = not db["enabled"]
    save()
    await dev_panel(c.chat, c.uid, c.mid)


async def cb_adduser(c):
    states[c.uid] = {"step": "adduser"}
    await c.screen("<b>اشتراك مستخدم</b>\n\nأرسل الايدي وعدد الأيام:\n<code>123456789 30</code>\n\n"
                   "ولرفعه VIP أضف v:\n<code>123456789 30 v</code>\n\n"
                   f"المشتركون الحاليون:\n{sub_lines()}", back_kb(None, "devpanel"))


async def cb_deluser(c):
    states[c.uid] = {"step": "deluser"}
    await c.screen(f"أرسل ايدي المشترك لحذفه (سيحذف أماكنه أيضًا):\n\n{sub_lines()}",
                   back_kb(None, "devpanel"))


async def cb_adddev(c):
    states[c.uid] = {"step": "adddev"}
    await c.screen("<b>إضافة مطور</b>\n\nأرسل ايدي الشخص أو حوّل رسالة منه.\n\n"
                   f"المطورون الحاليون:\n{dev_lines()}", back_kb(None, "devpanel"))


async def cb_deldev(c):
    states[c.uid] = {"step": "deldev"}
    await c.screen("<b>حذف مطور</b>\n\nأرسل ايدي المطور المراد حذفه.\n"
                   "المطور الأساسي لا يمكن حذفه.\n\n"
                   f"المطورون الحاليون:\n{dev_lines()}", back_kb(None, "devpanel"))


async def cb_bcast(c):
    rows = [[b(f"{name} ({len(bc_targets(kind))})", f"bct:{kind}", style="primary")]
            for kind, name in BC_NAMES.items()]
    await c.screen("<b>الإذاعة الشاملة</b>\n\nاختر الفئة التي تريد الإرسال لها:",
                   back_kb(rows, "devpanel"))


async def cb_bct(c):
    kind = c.parts[1]
    if kind not in BC_NAMES:
        return
    states[c.uid] = {"step": "bcast", "to": kind}
    await c.screen(f"الفئة: <b>{BC_NAMES[kind]}</b>\n\nأرسل الآن الرسالة التي تريد إذاعتها "
                   "(نص أو صورة أو فيديو أو ملف، بأي تنسيق).", back_kb(None, "bcast"))


async def cb_bcgo(c):
    st = states.get(c.uid)
    if not st or st.get("step") != "bcconfirm":
        return
    if broadcasting:
        await c.screen("هناك إذاعة قيد الإرسال الآن، انتظر انتهاءها.", back_kb(None, "devpanel"))
        return
    targets = bc_targets(st["to"])
    states.pop(c.uid, None)
    await c.screen(f"بدأت الإذاعة إلى <b>{len(targets)}</b> مستخدم، وسيصلك تقرير عند الانتهاء.",
                   back_kb(None, "devpanel"))
    spawn(broadcast(targets, st["chat"], st["mid"], c.chat))


async def cb_emoji(c):
    text, markup = emoji_view()
    await c.screen(text, markup)


async def cb_txtem(c):
    text, markup = text_stickers_view()
    await c.screen(text, markup)


async def cb_emk(c):
    key = c.data.split(":", 1)[1]
    states[c.uid] = {"step": "emoji", "key": key}
    back = "txtem" if key.startswith(("t:", "f:")) else "emoji"
    await c.screen(f"أرسل الآن الايموجي المميز نفسه لـ: <b>{esc(SLOT_NAMES.get(key, key))}</b>\n"
                   "(أو رقمه)، وأرسل 0 لإزالته.", back_kb(None, back))


async def cb_emojiclear(c):
    db["emoji"] = {k: v for k, v in db["emoji"].items() if k.startswith(("t:", "f:"))}
    save()
    text, markup = emoji_view()
    await c.screen(text, markup)


async def cb_txtclear(c):
    db["emoji"] = {k: v for k, v in db["emoji"].items() if not k.startswith(("t:", "f:"))}
    save()
    text, markup = text_stickers_view()
    await c.screen(text, markup)


async def cb_preview(c):
    for kind, text in sample_notices():
        await api("sendMessage", chat_id=c.chat, text=deco(text, kind), parse_mode="HTML",
                  disable_web_page_preview=True)


ROUTES = {
    "home": cb_home, "devpanel": cb_devpanel, "guide": cb_guide, "me": cb_me,
    "login": cb_login, "acc": cb_acc, "logout": cb_logout,
    "chats": cb_chats, "allchats": cb_places, "chat": cb_chat, "live": cb_live,
    "chmic": cb_policy, "chvid": cb_policy, "vcstart": cb_vc, "vcend": cb_vc,
    "addchat": cb_addchat, "delchat": cb_delchat, "delok": cb_delok,
    "notify": cb_notify, "setnotify": cb_setnotify, "notifyreset": cb_notifyreset,
    "photo": cb_photo, "stats": cb_stats, "toggle": cb_toggle,
    "adduser": cb_adduser, "deluser": cb_deluser, "adddev": cb_adddev, "deldev": cb_deldev,
    "bcast": cb_bcast, "bct": cb_bct, "bcgo": cb_bcgo,
    "emoji": cb_emoji, "txtem": cb_txtem, "emk": cb_emk,
    "emojiclear": cb_emojiclear, "txtclear": cb_txtclear, "preview": cb_preview,
    "dm": cb_dm, "mutog": cb_mutog, "chban": cb_policy, "ust": cb_ust, "ustfill": cb_ustfill,
}


async def handle_callback(q):
    c = Ctx(q)
    record_person(c.uid, tg_name(c.user), c.user.get("username") or "", bot=c.chat > 0)
    if c.act in ("ban", "unban"):
        await cb_ban(c)
        return
    if c.act == "mu":              # أزرار لوحة التشغيل: الصلاحية تُفحص داخلها
        await cb_music(c)
        return
    if not is_auth(c.uid):
        await c.toast("ليس لديك اشتراك فعّال", alert=True)
        return
    if c.act in DEV_ONLY and not is_dev(c.uid):
        await c.toast("هذه الميزة للمطور فقط", alert=True)
        return
    if c.act in PRIMARY_ONLY and c.uid != OWNER_ID:
        await c.toast("للمطور الأساسي فقط", alert=True)
        return
    await c.toast()
    route = ROUTES.get(c.act)
    if route:
        await route(c)


async def handle(update):
    try:
        if "message" in update:
            await handle_message(update["message"])
        elif "callback_query" in update:
            await handle_callback(update["callback_query"])
    except Exception as e:
        log.exception("handle: %s", e)


# ------------------------------------------------------------------ التشغيل
async def main():
    global HTTP
    if not (BOT_TOKEN and API_ID and API_HASH):
        sys.exit("ضع BOT_TOKEN و API_ID و API_HASH في أعلى الملف أو في متغيرات البيئة ثم أعد التشغيل.")
    HTTP = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=70))
    os.makedirs(MUSIC_DIR, exist_ok=True)
    await run_blocking(ensure_ffmpeg)
    await run_blocking(ensure_js_runtime)
    make_silence()
    await api("deleteWebhook")
    await start_userbot()
    spawn(monitor_loop())
    spawn(music_ticker())
    spawn(ytdlp_autoupdate())
    spawn(fill_people())
    log.info("Bot started")

    offset = 0
    while True:
        try:
            d = await api("getUpdates", offset=offset, timeout=50,
                          allowed_updates=["message", "callback_query"])
            if not d.get("ok"):
                await asyncio.sleep(3)
                continue
            for update in d.get("result", []):
                offset = update["update_id"] + 1
                spawn(handle(update))
        except Exception as e:
            log.warning("poll: %s", e)
            await asyncio.sleep(3)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    finally:
        flush()