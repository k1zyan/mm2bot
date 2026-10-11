# -*- coding: utf-8 -*-
"""
RobloxUpgrader v4 — Telegram Mini App. Flask + sqlite.

КАРТИНКИ: автоматическая скачка с вики убрана — она физически не могла
быть надёжной (сторонние сайты блокируют "роботов" или не отдают картинку
при встраивании, и это не лечится кодом). Вместо этого картинки — обычные
ссылки, которые ОДИН РАЗ вставляет админ в разделе "Картинки": открываешь
страницу предмета на вики или любом сайте в своём браузере, жмёшь правой
кнопкой по картинке -> "Копировать адрес картинки" -> вставляешь в поле.
Так админ сам видит, что картинка настоящая и точно открывается, прежде
чем сохранить — это надёжнее любого автоматического скрейпинга.
"""
import os, json, time, sqlite3, hmac, hashlib, random, string, logging, mimetypes, asyncio, threading
import urllib.request, urllib.parse, urllib.error
from urllib.parse import parse_qsl
from flask import Flask, request, jsonify, Response, send_from_directory
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (Message, LabeledPrice, PreCheckoutQuery,
                            InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo)

logging.basicConfig(level=logging.INFO)
BOT_TOKEN = os.getenv("BOT_TOKEN", "8733906402:AAHPU4AGTstQkFybAnJ-FlhqELBY-rr_Mh4")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Kitikzyan")
ADMIN_NICK = os.getenv("ADMIN_NICK", "RobloxUpgrader1")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://robloxupgrader-k1zyan.onrender.com")
START_BONUS = 1000
UPGRADE_EDGE = 0.95
MAX_STAKE_ITEMS = 6
APP_VERSION = "v10"
ADMIN_ID_NUM = int(os.getenv("ADMIN_ID_NUM", "2092773964"))  # для настоящих личных сообщений админу
STARS_RATE = int(os.getenv("STARS_RATE", "20"))  # сколько 💎 даём за 1 Telegram Star

app = Flask(__name__)
# ---------- НАСТОЯЩИЙ TELEGRAM (не вебхук-костыль — реальные запросы к API,
# разрешённые на этом хостинге). bot_poll слушает апдейты, notify()/send_stars_invoice()
# шлют сообщения из Flask-обработчиков одноразовыми короткоживущими сессиями. ----------
bot_poll = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()
router = Router()
dp.include_router(router)

def notify(chat_id, text, markup=None):
    async def _go():
        b = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
        try:
            await b.send_message(chat_id, text, reply_markup=markup)
        finally:
            await b.session.close()
    try:
        asyncio.run(_go())
    except Exception as e:
        logging.warning("notify(%s) failed: %s", chat_id, e)

def send_stars_invoice(chat_id, coins, stars):
    async def _go():
        b = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode="HTML"))
        try:
            await b.send_invoice(chat_id=chat_id, title=f"{coins} 💎 в RobloxUpgrader",
                description=f"Пополнение баланса на {coins} монет", payload=f"topup:{coins}:{stars}",
                currency="XTR", prices=[LabeledPrice(label=f"{coins} 💎", amount=stars)], provider_token="")
        finally:
            await b.session.close()
    try:
        asyncio.run(_go()); return True
    except Exception as e:
        logging.warning("send_stars_invoice(%s) failed: %s", chat_id, e); return False


DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.db")
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static_img")
os.makedirs(STATIC_DIR, exist_ok=True)
WIKI_API = "https://murder-mystery-2.fandom.com/api.php"

def _safe_fname(key, ext):
    base = "".join(ch if ch.isalnum() else "_" for ch in key)
    return f"{base}{ext}"

def download_one(key, title):
    """Скачивает картинку предмета/кейса С СЕРВЕРА (не из браузера игрока) и
    сохраняет файл локально. fandom.com разрешён для исходящих запросов на
    бесплатном PythonAnywhere. Возвращает имя файла или None."""
    try:
        q = urllib.parse.quote(title)
        url = f"{WIKI_API}?action=query&titles={q}&prop=pageimages&pithumbsize=300&redirects=1&format=json"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 RobloxUpgraderBot/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            d = json.loads(resp.read().decode())
        pages = d.get("query", {}).get("pages", {})
        thumb = None
        for pg in pages.values():
            thumb = (pg.get("thumbnail") or {}).get("source")
            if thumb: break
        if not thumb: return None
        ireq = urllib.request.Request(thumb, headers={"User-Agent": "Mozilla/5.0 RobloxUpgraderBot/1.0"})
        with urllib.request.urlopen(ireq, timeout=15) as iresp:
            data = iresp.read()
            ctype = iresp.headers.get("Content-Type", "image/png").split(";")[0]
        ext = mimetypes.guess_extension(ctype) or ".png"
        if ext == ".jpe": ext = ".jpg"
        fname = _safe_fname(key, ext)
        with open(os.path.join(STATIC_DIR, fname), "wb") as f:
            f.write(data)
        return fname
    except Exception as e:
        logging.warning("download failed for %s (%s): %s", key, title, e)
        return None

def download_all(c, keys=None):
    """Записывает в local_images КАЖДУЮ попытку — и успешную (с именем файла),
    и неудачную (с пустой строкой вместо имени). Так неудачные попытки не
    блокируют очередь навсегда: они просто помечаются как "не вышло" и
    пропускаются при обычном скачивании, а не зацикливают процесс на себе."""
    keys = list(WIKI_TITLES.keys()) if keys is None else keys
    ok, fail = [], []
    for key in keys:
        fname = download_one(key, WIKI_TITLES[key]) or ""
        c.execute("INSERT INTO local_images(name, filename) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET filename=?",
                  (key, fname, fname))
        (ok if fname else fail).append(key)
    c.commit()
    return ok, fail


SKINS = [
    ("Seer", 5, "godly"), ("Luger", 6, "godly"), ("Sugar", 6, "godly"), ("Candleflame", 7, "godly"),
    ("Eternalcane", 8, "godly"), ("Flowerwood", 7, "godly"), ("Flowerwood Gun", 8, "godly"),
    ("Vampire's Edge", 9, "godly"), ("Alien", 10, "godly"), ("Axe", 9, "godly"), ("Slasher", 11, "godly"),
    ("Waves", 10, "godly"), ("Borealis", 11, "godly"), ("Australis", 11, "godly"), ("Iceflake", 12, "godly"),
    ("Bioblade", 13, "godly"), ("Gemstone", 14, "godly"), ("Cookieblade", 13, "godly"),
    ("Amerilaser", 15, "godly"), ("Antique", 15, "godly"), ("Heartblade", 16, "godly"),
    ("Aurora", 17, "godly"), ("Asylum", 17, "godly"), ("Lightbringer", 20, "godly"),
    ("Darkbringer", 20, "godly"), ("Iceblaster", 22, "godly"), ("Red Luger", 24, "godly"),
    ("Snowflake", 26, "godly"), ("Saw", 27, "godly"), ("Fang", 28, "godly"), ("Web", 30, "godly"),
    ("Ice Dragon", 32, "godly"), ("Elderwood Revolver", 30, "godly"), ("Elderwood Blade", 34, "godly"),
    ("Hallow's Edge", 38, "godly"), ("Icebreaker", 40, "godly"),
    ("Logchopper", 45, "ancient"), ("Batwing", 55, "ancient"), ("Elderwood Scythe", 70, "ancient"),
    ("Icewing", 90, "ancient"), ("Gingerscythe", 120, "ancient"), ("Nik's Scythe", 180, "ancient"),
    ("Chroma Lightbringer", 260, "ancient"), ("Harvester", 350, "ancient"),
]
SKINS.sort(key=lambda s: s[1])
N = len(SKINS)
def cheap(i): return SKINS[i][1]
def sname(i): return SKINS[i][0]
def srar(i): return SKINS[i][2]

TIERS = [
    ("Деревянный кейс", "Knife Box 1", "#8a6a4f"), ("Бронзовый кейс", "Knife Box 2", "#cd7f32"),
    ("Серебряный кейс", "Gun Box 1", "#b8c0cc"), ("Золотой кейс", "Gun Box 2", "#ffd24b"),
    ("Платиновый кейс", "Knife Box 3", "#e8f0ff"), ("Алмазный кейс", "Gun Box 3", "#7ff0ff"),
    ("Рубиновый кейс", "Rainbow Box", "#ff4d6d"), ("Изумрудный кейс", "Knife Box 4", "#3ddc84"),
    ("Сапфировый кейс", "Chroma Box", "#4b9bff"), ("Легендарный кейс", "Knife Box 5", "#b24bff"),
    ("Мифический кейс", "Legendary Box", "#ff5c9a"),
]

def build_cases():
    band = max(1, N // len(TIERS))
    cases = {}
    for idx, (title, icon_title, color) in enumerate(TIERS):
        lo = idx * band
        hi = N if idx == len(TIERS) - 1 else (idx + 1) * band
        pool = list(range(lo, min(hi, N)))
        if not pool: continue
        weights = [1 / cheap(i) for i in pool]
        hm = len(pool) / sum(weights)
        cases[f"c{idx}"] = {"title": title, "icon_title": icon_title, "color": color, "pool": pool,
                             "w": weights, "price": max(3, round(hm * 1.2))}
    return cases
CASES = build_cases()
IMAGE_KEYS = [sname(i) for i in range(N)] + [f"case:{k}" for k in CASES]  # что можно задать картинкой вручную
# ключ -> название страницы вики, которое пробуем скачать автоматически
WIKI_TITLES = {sname(i): sname(i) for i in range(N)}
WIKI_TITLES.update({f"case:{k}": v["icon_title"] for k, v in CASES.items()})

def chance(value_a, price_b): return max(0.000001, min(0.99, value_a / price_b * UPGRADE_EDGE))

# ---------- БАЗА ДАННЫХ ----------
def _has_col(c, table, col):
    try:
        return any(r[1] == col for r in c.execute(f"PRAGMA table_info({table})").fetchall())
    except Exception:
        return True  # на новой базе колонка уже создаётся в CREATE TABLE, доп. ALTER не нужен

def db():
    c = sqlite3.connect(DB_PATH)
    c.execute("CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT, balance INTEGER DEFAULT 0, photo_url TEXT DEFAULT '')")
    c.execute("ALTER TABLE users ADD COLUMN photo_url TEXT DEFAULT ''") if not _has_col(c, "users", "photo_url") else None
    c.execute("CREATE TABLE IF NOT EXISTS inv(user_id INTEGER, skin INTEGER, qty INTEGER, PRIMARY KEY(user_id, skin))")
    c.execute("""CREATE TABLE IF NOT EXISTS deposits(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                 username TEXT, skin INTEGER, status TEXT DEFAULT 'pending', ts DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    c.execute("""CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, coins INTEGER DEFAULT 0,
                 case_key TEXT, skin INTEGER, max_uses INTEGER DEFAULT 1, used INTEGER DEFAULT 0)""")
    c.execute("CREATE TABLE IF NOT EXISTS promo_claims(code TEXT, user_id INTEGER, PRIMARY KEY(code, user_id))")
    c.execute("CREATE TABLE IF NOT EXISTS images(name TEXT PRIMARY KEY, url TEXT)")
    c.execute("CREATE TABLE IF NOT EXISTS local_images(name TEXT PRIMARY KEY, filename TEXT)")
    c.execute("""CREATE TABLE IF NOT EXISTS drops(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                 username TEXT, skin INTEGER, price INTEGER, source TEXT, ts DATETIME DEFAULT CURRENT_TIMESTAMP)""")
    return c

def log_drop(c, uid, username, skin, source):
    c.execute("INSERT INTO drops(user_id, username, skin, price, source) VALUES(?,?,?,?,?)",
              (uid, username or "", skin, cheap(skin), source))
    c.commit()

def recent_drops(c, imgs, limit=12):
    rows = c.execute("""SELECT d.username, d.skin, d.price, u.photo_url FROM drops d
                         LEFT JOIN users u ON u.id = d.user_id ORDER BY d.id DESC LIMIT ?""", (limit,)).fetchall()
    return [{"username": un or "Игрок", "name": sname(s), "rarity": srar(s), "price": p,
              "img": imgs.get(sname(s), ""), "avatar": av or ""} for un, s, p, av in rows]

def top_drop_24h(c, imgs):
    row = c.execute("""SELECT d.username, d.skin, d.price, u.photo_url FROM drops d
                        LEFT JOIN users u ON u.id = d.user_id
                        WHERE d.ts >= datetime('now','-1 day') ORDER BY d.price DESC LIMIT 1""").fetchone()
    if not row: return None
    un, s, p, av = row
    return {"username": un or "Игрок", "name": sname(s), "rarity": srar(s), "price": p,
            "img": imgs.get(sname(s), ""), "avatar": av or ""}

def ensure_user(c, uid, username, photo_url=""):
    row = c.execute("SELECT username, photo_url FROM users WHERE id=?", (uid,)).fetchone()
    if not row:
        c.execute("INSERT INTO users(id, username, balance, photo_url) VALUES(?,?,?,?)", (uid, username or "", START_BONUS, photo_url or "")); c.commit()
    else:
        if (username and row[0] != username) or (photo_url and row[1] != photo_url):
            c.execute("UPDATE users SET username=?, photo_url=? WHERE id=?",
                      (username or row[0], photo_url or row[1], uid)); c.commit()

def balance(c, uid): return c.execute("SELECT balance FROM users WHERE id=?", (uid,)).fetchone()[0]
def add_balance(c, uid, d): c.execute("UPDATE users SET balance=balance+? WHERE id=?", (d, uid)); c.commit()
def qty(c, uid, s):
    r = c.execute("SELECT qty FROM inv WHERE user_id=? AND skin=?", (uid, s)).fetchone()
    return r[0] if r else 0
def add_skin(c, uid, s, d=1):
    c.execute("INSERT INTO inv VALUES(?,?,?) ON CONFLICT(user_id,skin) DO UPDATE SET qty=qty+?", (uid, s, d, d))
    c.execute("DELETE FROM inv WHERE qty<=0"); c.commit()
def inventory(c, uid): return c.execute("SELECT skin, qty FROM inv WHERE user_id=? AND qty>0 ORDER BY skin DESC", (uid,)).fetchall()

def get_images(c):
    """Приоритет: своя ссылка админа (если он её вставил) важнее автоскачанной —
    так можно подменить картинку, если автоматическая оказалась не той.
    Пустое имя файла в local_images значит "пробовали скачать — не вышло",
    это НЕ картинка, её пропускаем."""
    manual = dict(c.execute("SELECT name, url FROM images").fetchall())
    local = dict(c.execute("SELECT name, filename FROM local_images").fetchall())
    out = {}
    for k, fname in local.items():
        if fname: out[k] = f"/img/{fname}"
    for k, url in manual.items():
        if url: out[k] = url
    return out

# ---------- ПРОВЕРКА TELEGRAM WEBAPP initData ----------
def check_init_data(init_data: str):
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        recv_hash = pairs.pop("hash", None)
        if not recv_hash: return None
        check_str = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        good = hmac.new(secret, check_str.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(good, recv_hash): return None
        if time.time() - int(pairs.get("auth_date", 0)) > 86400 * 3: return None
        return json.loads(pairs.get("user", "{}"))
    except Exception:
        return None

def is_admin_user(user): return (user.get("username") or "").lower() == ADMIN_USERNAME.lower()

def auth():
    init_data = request.headers.get("X-Init-Data") or (request.json or {}).get("initData", "")
    user = check_init_data(init_data or "")
    if not user:
        return None, (jsonify({"error": "auth_failed"}), 401)
    return user, None

def admin_auth():
    user, err = auth()
    if err: return None, err
    if not is_admin_user(user): return None, (jsonify({"error": "forbidden"}), 403)
    return user, None

# ---------- API ----------
@app.post("/api/state")
def api_state():
    user, err = auth()
    if err: return err
    c = db(); ensure_user(c, user["id"], user.get("username", ""), user.get("photo_url", ""))
    imgs = get_images(c)
    items = [{"id": s, "name": sname(s), "rarity": srar(s), "price": cheap(s), "qty": q, "img": imgs.get(sname(s), "")} for s, q in inventory(c, user["id"])]
    cases = [{"key": k, "title": v["title"], "color": v["color"], "price": v["price"], "img": imgs.get(f"case:{k}", ""),
              "pool": [{"id": i, "name": sname(i), "rarity": srar(i), "price": cheap(i), "img": imgs.get(sname(i), "")} for i in v["pool"]]} for k, v in CASES.items()]
    all_skins = [{"id": i, "name": sname(i), "rarity": srar(i), "price": cheap(i), "img": imgs.get(sname(i), "")} for i in range(N)]
    deposits = c.execute("SELECT id, skin, status FROM deposits WHERE user_id=? ORDER BY id DESC LIMIT 10", (user["id"],)).fetchall()
    return jsonify({
        "balance": balance(c, user["id"]), "inventory": items, "cases": cases, "skins": all_skins,
        "admin_nick": ADMIN_NICK, "is_admin": is_admin_user(user), "max_stake": MAX_STAKE_ITEMS,
        "deposits": [{"id": d, "skin": sname(s), "status": st} for d, s, st in deposits],
        "recent_drops": recent_drops(c, imgs), "top_drop_24h": top_drop_24h(c, imgs),
        "stars_rate": STARS_RATE,
    })

@app.post("/api/drops")
def api_drops():
    user, err = auth()
    if err: return err
    c = db(); imgs = get_images(c)
    return jsonify({"recent_drops": recent_drops(c, imgs), "top_drop_24h": top_drop_24h(c, imgs)})

@app.post("/api/case_open")
def api_case_open():
    user, err = auth()
    if err: return err
    key = (request.json or {}).get("case")
    if key not in CASES: return jsonify({"error": "bad_case"}), 400
    c = db(); ensure_user(c, user["id"], user.get("username", ""), user.get("photo_url", "")); cs = CASES[key]
    if balance(c, user["id"]) < cs["price"]: return jsonify({"error": "no_money"}), 400
    add_balance(c, user["id"], -cs["price"])
    win = random.choices(cs["pool"], cs["w"])[0]
    add_skin(c, user["id"], win)
    log_drop(c, user["id"], user.get("username",""), win, "case")
    imgs = get_images(c)
    return jsonify({"win": {"id": win, "name": sname(win), "rarity": srar(win), "price": cheap(win), "img": imgs.get(sname(win), "")},
                     "reel": [random.choice(cs["pool"]) for _ in range(26)] + [win],
                     "balance": balance(c, user["id"])})

@app.post("/api/upgrade")
def api_upgrade():
    user, err = auth()
    if err: return err
    data = request.json or {}
    stake = data.get("from") or []
    to = data.get("to")
    if not isinstance(stake, list) or not (1 <= len(stake) <= MAX_STAKE_ITEMS) or to is None or not (0 <= to < N):
        return jsonify({"error": "bad_pair"}), 400
    need = {}
    for s in stake:
        if not (0 <= s < N): return jsonify({"error": "bad_pair"}), 400
        need[s] = need.get(s, 0) + 1
    c = db(); ensure_user(c, user["id"], user.get("username", ""), user.get("photo_url", ""))
    for s, cnt in need.items():
        if qty(c, user["id"], s) < cnt: return jsonify({"error": "no_skin"}), 400
    total_value = sum(cheap(s) for s in stake)
    ch = chance(total_value, cheap(to)); win = random.random() < ch
    for s, cnt in need.items(): add_skin(c, user["id"], s, -cnt)
    if win:
        add_skin(c, user["id"], to)
        log_drop(c, user["id"], user.get("username",""), to, "upgrade")
    imgs = get_images(c)
    return jsonify({"win": win, "chance": ch, "balance": balance(c, user["id"]),
                     "result": {"id": to, "name": sname(to), "rarity": srar(to), "price": cheap(to), "img": imgs.get(sname(to), "")} if win else None})

@app.post("/api/sell")
def api_sell():
    user, err = auth()
    if err: return err
    s = (request.json or {}).get("skin")
    c = db(); ensure_user(c, user["id"], user.get("username", ""), user.get("photo_url", ""))
    if s is None or qty(c, user["id"], s) < 1: return jsonify({"error": "no_skin"}), 400
    add_skin(c, user["id"], s, -1); add_balance(c, user["id"], max(1, round(cheap(s) * 0.8)))
    return jsonify({"balance": balance(c, user["id"])})

@app.post("/api/promo")
def api_promo():
    user, err = auth()
    if err: return err
    code = ((request.json or {}).get("code") or "").strip().upper()
    c = db(); ensure_user(c, user["id"], user.get("username", ""), user.get("photo_url", ""))
    row = c.execute("SELECT coins, case_key, skin, max_uses, used FROM promos WHERE code=?", (code,)).fetchone()
    if not row: return jsonify({"error": "not_found"}), 404
    coins, case_key, skin, max_uses, used = row
    if used >= max_uses: return jsonify({"error": "exhausted"}), 400
    if c.execute("SELECT 1 FROM promo_claims WHERE code=? AND user_id=?", (code, user["id"])).fetchone():
        return jsonify({"error": "already_used"}), 400
    if coins: add_balance(c, user["id"], coins)
    if case_key and case_key in CASES:
        win = random.choices(CASES[case_key]["pool"], CASES[case_key]["w"])[0]; add_skin(c, user["id"], win)
    if skin is not None and 0 <= skin < N: add_s
