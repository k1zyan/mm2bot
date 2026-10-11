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
    if skin is not None and 0 <= skin < N: add_skin(c, user["id"], skin)
    c.execute("INSERT INTO promo_claims VALUES(?,?)", (code, user["id"]))
    c.execute("UPDATE promos SET used=used+1 WHERE code=?", (code,)); c.commit()
    return jsonify({"ok": True, "balance": balance(c, user["id"])})

# ---------- ПОПОЛНЕНИЕ ----------
@app.post("/api/deposit")
def api_deposit():
    user, err = auth()
    if err: return err
    s = (request.json or {}).get("skin")
    if s is None or not (0 <= s < N): return jsonify({"error": "bad_skin"}), 400
    c = db(); ensure_user(c, user["id"], user.get("username", ""), user.get("photo_url", ""))
    cur = c.execute("INSERT INTO deposits(user_id, username, skin) VALUES(?,?,?)",
                     (user["id"], user.get("username", ""), s)); c.commit()
    did = cur.lastrowid
    notify(ADMIN_ID_NUM, f"💎 Заявка #{did}\n@{user.get('username') or user['id']} хочет внести <b>{sname(s)}</b>\nОткрой приложение, раздел «Админ».")
    return jsonify({"id": did, "admin_nick": ADMIN_NICK})

@app.post("/api/deposit_given")
def api_deposit_given():
    user, err = auth()
    if err: return err
    did = (request.json or {}).get("id")
    c = db()
    row = c.execute("SELECT user_id, status, skin FROM deposits WHERE id=?", (did,)).fetchone()
    if not row or row[0] != user["id"] or row[1] != "pending": return jsonify({"error": "bad_state"}), 400
    c.execute("UPDATE deposits SET status='given' WHERE id=?", (did,)); c.commit()
    notify(ADMIN_ID_NUM, f"✅ @{user.get('username') or user['id']} ОТДАЛ <b>{sname(row[2])}</b> по заявке #{did} — подтверди в приложении.")
    return jsonify({"ok": True})

# ---------- АДМИНКА (только ADMIN_USERNAME) ----------
@app.post("/api/admin/deposits")
def api_admin_deposits():
    user, err = admin_auth()
    if err: return err
    c = db()
    rows = c.execute("SELECT id, user_id, username, skin, status FROM deposits WHERE status IN ('pending','given') ORDER BY id").fetchall()
    return jsonify({"items": [{"id": i, "user_id": u, "username": un, "skin": sname(s), "status": st} for i, u, un, s, st in rows]})

@app.post("/api/admin/decide")
def api_admin_decide():
    user, err = admin_auth()
    if err: return err
    data = request.json or {}; did, ok = data.get("id"), data.get("ok")
    c = db()
    row = c.execute("SELECT user_id, skin, status FROM deposits WHERE id=?", (did,)).fetchone()
    if not row or row[2] != "given": return jsonify({"error": "not_found"}), 404
    uid, s, _ = row
    if ok:
        add_balance(c, uid, cheap(s)); c.execute("UPDATE deposits SET status='ok' WHERE id=?", (did,))
        c.commit()
        notify(uid, f"✅ Пополнение принято: <b>{sname(s)}</b> зачислено как {cheap(s)} 💎 на баланс!")
    else:
        c.execute("UPDATE deposits SET status='rejected' WHERE id=?", (did,))
        c.commit()
        notify(uid, f"❌ Заявка на внесение <b>{sname(s)}</b> отклонена.")
    return jsonify({"ok": True})

@app.post("/api/admin/give_self")
def api_admin_give_self():
    user, err = admin_auth()
    if err: return err
    amount = int((request.json or {}).get("amount") or 0)
    c = db(); ensure_user(c, user["id"], user.get("username", ""), user.get("photo_url", ""))
    add_balance(c, user["id"], amount)
    return jsonify({"balance": balance(c, user["id"])})

@app.post("/api/admin/promo_create")
def api_admin_promo_create():
    user, err = admin_auth()
    if err: return err
    d = request.json or {}
    code = (d.get("code") or "").strip().upper() or "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
    coins = int(d.get("coins") or 0); case_key = d.get("case_key") or None
    skin = d.get("skin"); skin = int(skin) if skin not in (None, "", "null") else None
    max_uses = max(1, int(d.get("max_uses") or 1))
    c = db()
    c.execute("INSERT OR REPLACE INTO promos(code, coins, case_key, skin, max_uses, used) VALUES(?,?,?,?,?,0)",
              (code, coins, case_key, skin, max_uses)); c.commit()
    return jsonify({"ok": True, "code": code})

@app.post("/api/admin/promo_list")
def api_admin_promo_list():
    user, err = admin_auth()
    if err: return err
    c = db()
    rows = c.execute("SELECT code, coins, case_key, skin, max_uses, used FROM promos ORDER BY rowid DESC").fetchall()
    return jsonify({"items": [{"code": a, "coins": b, "case_key": cc, "skin": (sname(sk) if sk is not None else None),
                                "max_uses": m, "used": u} for a, b, cc, sk, m, u in rows]})

@app.post("/api/admin/download_images")
def api_admin_download_images():
    """Качает МАЛЕНЬКИМИ ПОРЦИЯМИ (по умолчанию 5 штук за вызов), а не все 55
    разом — иначе один HTTP-запрос может не уложиться в тайм-аут бесплатного
    PythonAnywhere и просто оборваться с ошибкой на полпути. Клиент сам
    вызывает этот эндпоинт в цикле, пока not done."""
    user, err = admin_auth()
    if err: return err
    d = request.json or {}
    batch = max(1, min(10, int(d.get("batch") or 5)))
    retry_failed = bool(d.get("retry_failed"))
    c = db()
    if retry_failed:
        c.execute("DELETE FROM local_images WHERE filename=''")  # забыть прошлые неудачи, попробовать снова
        c.commit()
    attempted = {row[0] for row in c.execute("SELECT name FROM local_images").fetchall()}
    missing = [k for k in WIKI_TITLES.keys() if k not in attempted]
    chunk = missing[:batch]
    ok, fail = download_all(c, chunk)
    remaining = len(missing) - len(chunk)
    return jsonify({"ok": len(ok), "fail": len(fail), "failed_names": fail,
                     "remaining": remaining, "done": remaining <= 0, "total_missing_before": len(missing)})

@app.post("/api/admin/images_list")
def api_admin_images_list():
    user, err = admin_auth()
    if err: return err
    c = db(); imgs = get_images(c)
    rows = [{"key": sname(i), "label": sname(i), "url": imgs.get(sname(i), "")} for i in range(N)]
    rows += [{"key": f"case:{k}", "label": v["title"], "url": imgs.get(f"case:{k}", "")} for k, v in CASES.items()]
    return jsonify({"items": rows})

@app.post("/api/admin/set_image")
def api_admin_set_image():
    user, err = admin_auth()
    if err: return err
    d = request.json or {}
    key = (d.get("key") or "").strip(); url = (d.get("url") or "").strip()
    if key not in IMAGE_KEYS: return jsonify({"error": "bad_key"}), 400
    c = db()
    if url:
        c.execute("INSERT INTO images(name, url) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET url=?", (key, url, url))
    else:
        c.execute("DELETE FROM images WHERE name=?", (key,))
    c.commit()
    return jsonify({"ok": True})

# ---------- ПОПОЛНЕНИЕ ЗВЁЗДАМИ (настоящая оплата Telegram Stars) ----------
@app.post("/api/stars_topup")
def api_stars_topup():
    user, err = auth()
    if err: return err
    stars = int((request.json or {}).get("stars") or 0)
    if stars < 1 or stars > 2500: return jsonify({"error": "bad_amount"}), 400
    coins = stars * STARS_RATE
    ok = send_stars_invoice(user["id"], coins, stars)
    if not ok: return jsonify({"error": "send_failed"}), 500
    return jsonify({"ok": True, "coins": coins})

# ---------- ХЭНДЛЕРЫ AIOGRAM (настоящий поллинг, без вебхуков) ----------
@router.message(CommandStart())
async def on_start(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🚀 Открыть", web_app=WebAppInfo(url=WEBAPP_URL))]])
    await m.answer("👋 Добро пожаловать в <b>Роблокс Апгрейдер</b>!\n\n"
                    "Открывай кейсы, апгрейдь скины MM2 и пополняй баланс прямо тут.", reply_markup=kb)

@router.pre_checkout_query()
async def on_pre_checkout(pcq: PreCheckoutQuery):
    await pcq.answer(ok=True)

@router.message(F.successful_payment)
async def on_payment(m: Message):
    sp = m.successful_payment
    try:
        coins = int(sp.invoice_payload.split(":")[1])
    except Exception:
        coins = sp.total_amount * STARS_RATE
    c = db(); ensure_user(c, m.from_user.id, m.from_user.username or "")
    add_balance(c, m.from_user.id, coins)
    await m.answer(f"✅ Баланс пополнен на <b>{coins} 💎</b> (оплата {sp.total_amount}⭐). Спасибо!")

def _run_bot_polling():
    asyncio.run(dp.start_polling(bot_poll))



@app.get("/img/<path:fname>")
def serve_img(fname):
    return send_from_directory(STATIC_DIR, fname, max_age=86400 * 30)

@app.get("/")
def index():
    """no-store — обязательно, иначе Telegram WebView может годами показывать
    закэшированную старую версию страницы и НЕ подтянет новый файл сам."""
    resp = Response(INDEX_HTML.replace("__APP_VERSION__", APP_VERSION), mimetype="text/html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp

INDEX_HTML = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Roblox Upgrader</title>
<script src="https://telegram.org/js/telegram-web-app.js"></script>
<style>
:root{--bg:#0b0d14;--card:#141826;--card2:#1b2033;--accent:#7c5cff;--accent2:#ff5c9a;--good:#3ddc84;--bad:#ff4d6d;
--gold:#ffc24b;--anc:#ff6b6b;--text:#f1f2f6;--sub:#8891a7;}
*{box-sizing:border-box}
html{background:var(--bg)}
body{margin:0;min-height:100vh;background:radial-gradient(1200px 600px at 50% -10%,#1d1440,var(--bg) 60%);color:var(--text);
font-family:-apple-system,Segoe UI,Roboto,sans-serif;padding-bottom:90px;padding-top:env(safe-area-inset-top,0)}
.topbar{position:sticky;top:0;z-index:5;background:linear-gradient(180deg,#0b0d14 75%,transparent);padding:14px 16px 6px;
padding-top:calc(14px + env(safe-area-inset-top,0))}
.brand{font-weight:900;font-size:22px;letter-spacing:.3px;background:linear-gradient(90deg,var(--accent),var(--accent2));
-webkit-background-clip:text;background-clip:text;color:transparent}
.balance{display:flex;justify-content:space-between;align-items:center;margin-top:8px;background:var(--card);
border:1px solid #262c42;border-radius:16px;padding:12px 16px}
.balance b{font-size:20px;color:var(--gold)}
.ver{font-size:10px;font-weight:600;color:var(--sub);background:var(--card2);padding:2px 6px;border-radius:6px;margin-left:6px;vertical-align:middle}
.wrap{padding:14px 16px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.case-card{border-radius:18px;padding:14px;text-align:center;border:1px solid #262c42;cursor:pointer;
transition:transform .18s cubic-bezier(.2,.8,.3,1)}
.case-card:active{transform:scale(.94)}
.case-ic{height:54px;display:flex;align-items:center;justify-content:center}
.case-ic img{max-height:54px;max-width:80%;object-fit:contain;filter:drop-shadow(0 6px 10px rgba(0,0,0,.5))}
.case-ic .ph{font-size:34px}
.case-title{font-weight:700;margin-top:4px;font-size:13px}
.case-price{color:var(--gold);font-weight:700;margin-top:2px;font-size:13px}
.skin{background:var(--card2);border-radius:14px;padding:10px;text-align:center;position:relative;overflow:hidden;
transition:transform .15s,box-shadow .15s}
.skin.selectable{cursor:pointer}
.skin.selectable:active{transform:scale(.95)}
.skin.selected{box-shadow:0 0 0 2px var(--accent2),0 0 16px rgba(255,92,154,.5)}
.skin img{width:48px;height:48px;object-fit:contain}
.skin .ph{width:48px;height:48px;border-radius:10px;display:flex;align-items:center;justify-content:center;
font-weight:800;font-size:16px;margin:0 auto;background:linear-gradient(135deg,var(--accent),var(--accent2))}
.skin .name{font-size:11px;margin-top:6px;line-height:1.2}
.skin .price{font-size:11px;color:var(--gold)}
.rar-godly{box-shadow:inset 0 0 0 1.5px var(--gold)}
.rar-ancient{box-shadow:inset 0 0 0 1.5px var(--anc)}
.skin.selected.rar-godly, .skin.selected.rar-ancient{box-shadow:inset 0 0 0 1.5px var(--accent2),0 0 16px rgba(255,92,154,.5)}
.btn{background:linear-gradient(90deg,var(--accent),var(--accent2));border:none;color:#fff;font-weight:800;
padding:12px 18px;border-radius:14px;width:100%;font-size:15px;transition:transform .15s,opacity .15s}
.btn.secondary{background:var(--card2);color:var(--text)}
.btn:disabled{opacity:.4}
.btn:active:not(:disabled){transform:scale(.97);opacity:.9}
.tap-ripple{position:relative;overflow:hidden;-webkit-tap-highlight-color:transparent}
.tap-ripple:active{transform:scale(.96)}
.skin{transition:transform .15s,box-shadow .15s}
.skin:active{transform:scale(.95)}
.case-card{transition:transform .15s,box-shadow .15s}
::-webkit-scrollbar{display:none}
*{scroll-behavior:smooth}
.navbar{position:fixed;bottom:0;left:0;right:0;background:#10121c;border-top:1px solid #262c42;
display:flex;padding:8px calc(10px + env(safe-area-inset-left,0)) calc(8px + env(safe-area-inset-bottom,0)) 10px;overflow-x:auto}
.nav-btn{flex:1;text-align:center;padding:8px 2px;color:var(--sub);font-size:10px;min-width:56px;transition:color .15s,transform .15s}
.nav-btn.active{color:var(--text)}
.nav-btn:active{transform:scale(.92)}
.nav-ic{font-size:16px;opacity:.85}
h2{font-size:17px;margin:4px 0 12px}
.reel-wrap{overflow:hidden;background:var(--card);border-radius:16px;padding:0;position:relative;margin-bottom:14px;height:98px}
.reel-wrap:before,.reel-wrap:after{content:"";position:absolute;top:0;bottom:0;width:36px;z-index:2}
.reel-wrap:before{left:0;background:linear-gradient(90deg,var(--card),transparent)}
.reel-wrap:after{right:0;background:linear-gradient(270deg,var(--card),transparent)}
.reel-ptr{position:absolute;left:50%;top:0;bottom:0;width:3px;background:var(--accent2);z-index:3;transform:translateX(-1px);
box-shadow:0 0 10px var(--accent2)}
.reel{display:flex;gap:10px;position:absolute;left:50%;top:14px;will-change:transform;backface-visibility:hidden}
.reel .skin{min-width:74px;flex:0 0 auto}
.result-box{text-align:center;padding:16px;border-radius:18px;margin-bottom:14px;opacity:0;transform:scale(.85);
transition:opacity .4s,transform .4s cubic-bezier(.2,1.4,.4,1)}
.result-box.show{opacity:1;transform:scale(1)}
.result-box.win{background:linear-gradient(180deg,#1c2b1f,var(--card))}
.result-box.lose{background:linear-gradient(180deg,#2b1c23,var(--card))}
.pick-list .skin{display:flex;align-items:center;gap:10px;text-align:left;margin-bottom:8px;padding:10px}
.pick-list img, .pick-list .ph{width:38px;height:38px;flex-shrink:0}
.badge{font-size:10px;padding:2px 8px;border-radius:20px;background:#262c42;color:var(--sub)}
.badge.pending{color:#ffc24b}.badge.given{color:#7c5cff}.badge.ok{color:var(--good)}.badge.rejected{color:var(--bad)}
.gauge{width:220px;height:220px;margin:6px auto 10px;position:relative}
.gauge svg{width:100%;height:100%;transform:rotate(-90deg)}
.gauge-track{fill:none;stroke:#241633;stroke-width:14}
.gauge-fill{fill:none;stroke:var(--gold);stroke-width:14;stroke-linecap:round;
transition:stroke-dasharray .6s ease, stroke .3s}
.gauge-mid{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;flex-direction:column}
.gauge-pct{font-size:32px;font-weight:900}
.gauge-label{font-size:11px;font-weight:700;letter-spacing:.5px;margin-top:2px}
.gauge-needle{position:absolute;left:50%;top:50%;width:6px;height:94px;background:linear-gradient(180deg,#fff,#fff 70%,transparent);
border-radius:6px;transform-origin:50% 0;transform:translate(-50%,0) rotate(0deg);box-shadow:0 0 10px #fff;
transition:transform 3.6s cubic-bezier(.08,.85,.1,1)}
.gauge-needle:after{content:"";position:absolute;left:50%;top:-5px;width:14px;height:14px;background:#fff;
border-radius:50%;transform:translateX(-50%);box-shadow:0 0 10px #fff}
@keyframes popIn{0%{transform:scale(.85);opacity:0}100%{transform:scale(1);opacity:1}}
@keyframes burst{0%{box-shadow:0 0 0 0 rgba(61,220,132,.35)}100%{box-shadow:0 0 0 24px rgba(61,220,132,0)}}
.win-burst{position:absolute;inset:0;border-radius:18px;animation:burst 1.1s ease-out}
.tap-ripple{position:relative;overflow:hidden}
.ticker{overflow:hidden;white-space:nowrap;background:var(--card);border-radius:14px;padding:10px 0;margin-bottom:12px;border:1px solid #262c42}
.ticker-track{display:inline-flex;gap:22px;padding-left:100%;animation:ticker 22s linear infinite}
@keyframes ticker{from{transform:translateX(0)}to{transform:translateX(-100%)}}
.ticker-item{display:inline-flex;align-items:center;gap:6px;font-size:12px;color:var(--sub);white-space:nowrap}
.ticker-item b{color:var(--text)}
.ticker-item img, .ticker-item .ph{width:18px;height:18px;border-radius:4px}
.avatar{width:18px;height:18px;border-radius:50%;object-fit:cover}
.avatar-ph{width:18px;height:18px;border-radius:50%;background:var(--accent);color:#fff;font-size:10px;font-weight:800;
display:inline-flex;align-items:center;justify-content:center}
.top-drop{background:linear-gradient(135deg,#2a1f08,var(--card));border:1px solid #4a3a10;border-radius:16px;
padding:12px 14px;display:flex;align-items:center;gap:12px;margin-bottom:14px}
.top-drop img,.top-drop .ph{width:42px;height:42px}
.top-drop .cap{font-size:10px;color:var(--gold);font-weight:800;letter-spacing:.5px}
.stake-chip{display:inline-flex;align-items:center;gap:4px;background:var(--card2);border-radius:10px;padding:4px 8px;
margin:2px;font-size:11px;animation:popIn .3s}
.stake-chip img,.stake-chip .ph{width:20px;height:20px}
.stake-chip .x{color:var(--bad);font-weight:900;margin-left:2px;cursor:pointer}
.stake-row{display:flex;gap:10px;margin-bottom:14px}
.stake-box{flex:1;background:var(--card);border:1px solid #262c42;border-radius:16px;padding:12px;text-align:center;cursor:pointer;
transition:transform .15s}
.stake-box:active{transform:scale(.97)}
.stake-box .cap{font-size:11px;color:var(--sub);font-weight:700}
.stake-box .cnt{font-size:11px;color:var(--gold);margin-top:2px}
.stake-box .visual{height:50px;display:flex;align-items:center;justify-content:center;gap:4px;margin-top:6px}
.stake-box .visual img{max-height:44px;max-width:44px;object-fit:contain}
.stake-box .sum{font-size:13px;font-weight:800;margin-top:4px;color:var(--text)}
input.inp{width:100%;padding:12px;border-radius:12px;border:1px solid #262c42;background:var(--card2);color:var(--text);margin-bottom:8px;
transition:border-color .15s}
input.inp:focus{outline:none;border-color:var(--accent)}
.hidden{display:none}
.empty{color:var(--sub);text-align:center;padding:30px 10px}
.row{display:flex;gap:8px}
.row .btn{width:auto;flex:1}
.img-row{display:flex;align-items:center;gap:8px;background:var(--card2);border-radius:12px;padding:8px;margin-bottom:6px}
.img-row .prev{width:36px;height:36px;border-radius:8px;object-fit:contain;background:#0b0d14;flex-shrink:0}
.img-row .lbl{font-size:11px;width:92px;flex-shrink:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.img-row input{flex:1;padding:8px;border-radius:8px;border:1px solid #262c42;background:#0b0d14;color:var(--text);font-size:11px}
.img-row button{flex-shrink:0;background:var(--accent);border:none;color:#fff;border-radius:8px;padding:8px 10px;font-size:11px}
</style></head>
<body>
<div class="topbar">
  <div class="brand">ROBLOX UPGRADER <span class="ver">__APP_VERSION__</span></div>
  <div class="balance"><span>💎 Баланс</span><b id="balVal">0</b></div>
</div>
<div class="wrap" id="app"></div>
<div class="navbar">
  <div class="nav-btn" data-tab="cases"><div class="nav-ic">🎁</div>Кейсы</div>
  <div class="nav-btn" data-tab="up"><div class="nav-ic">⚡</div>Апгрейд</div>
  <div class="nav-btn" data-tab="inv"><div class="nav-ic">🎒</div>Инвентарь</div>
  <div class="nav-btn" data-tab="dep"><div class="nav-ic">💎</div>Пополнить</div>
  <div class="nav-btn" data-tab="promo"><div class="nav-ic">🎟</div>Промо</div>
  <div class="nav-btn hidden" id="adminTab" data-tab="admin"><div class="nav-ic">🛠</div>Админ</div>
</div>
<script>
const tg = window.Telegram.WebApp; tg.ready(); tg.expand();
const initData = tg.initData || "";
let STATE = null;
let stakeSel = [];
let targetSel = null;
async function api(path, body){
  const r = await fetch(path, {method:"POST", headers:{"Content-Type":"application/json","X-Init-Data":initData},
    body: JSON.stringify(body||{})});
  return r.json();
}
function rarCls(r){ return r==="ancient" ? "rar-ancient" : r==="godly" ? "rar-godly" : ""; }
function skinVisual(s){ return s.img ? `<img src="${s.img}" loading="lazy" onerror="this.outerHTML='<div class=ph>${s.name.slice(0,2).toUpperCase()}</div>'">` : `<div class="ph">${s.name.slice(0,2).toUpperCase()}</div>`; }
function avatarVisual(d){
  const init = (d.username||"?").slice(0,1).toUpperCase();
  return d.avatar ? `<img class="avatar" src="${d.avatar}" loading="lazy" onerror="this.outerHTML='<span class=avatar-ph>${init}</span>'">`
                   : `<span class="avatar-ph">${init}</span>`;
}
function skinCard(s){
  return `<div class="skin ${rarCls(s.rarity)}">${skinVisual(s)}
    <div class="name">${s.name}</div><div class="price">${s.price} 💎</div></div>`;
}
async function load(){
  STATE = await api("/api/state");
  document.getElementById("balVal").textContent = STATE.balance;
  if(STATE.is_admin) document.getElementById("adminTab").classList.remove("hidden");
}
function setTab(t){
  document.querySelectorAll(".nav-btn").forEach(b=>b.classList.toggle("active", b.dataset.tab===t));
  if(t==="up"){ stakeSel=[]; targetSel=null; }
  ({cases:renderCases, up:renderUp, inv:renderInv, dep:renderDep, promo:renderPromo, admin:renderAdmin})[t]();
}
document.querySelectorAll(".nav-btn").forEach(b=>b.onclick=()=>setTab(b.dataset.tab));

function renderCases(){
  const app = document.getElementById("app");
  const td = STATE.top_drop_24h;
  app.innerHTML = (td ? `<div class="top-drop"></div>` : "") +
    `<h2>Кейсы</h2><div class='grid' id='caseGrid'></div>`;
  if(td){
    const el = app.querySelector(".top-drop");
    el.innerHTML = `${skinVisual(td)}<div><div class="cap">ЛУЧШИЙ ДРОП ЗА 24 ЧАСА</div>
      <div style="font-weight:800">${td.name}</div>
      <div style="font-size:11px;color:var(--sub);display:flex;align-items:center;gap:4px">${avatarVisual(td)}@${td.username} · ${td.price} 💎</div></div>`;
  }
  if(STATE.recent_drops && STATE.recent_drops.length){
    const t = document.createElement("div"); t.className = "ticker";
    t.innerHTML = `<div class="ticker-track">${STATE.recent_drops.map(d=>
      `<span class="ticker-item">${avatarVisual(d)}<b>@${d.username}</b> выбил ${skinVisual(d)}<b>${d.name}</b> (${d.price}💎)</span>`).join("")}</div>`;
    app.insertBefore(t, app.querySelector("h2"));
  }
  document.getElementById("caseGrid").innerHTML = STATE.cases.map(c=>
    `<div class="case-card tap-ripple" style="background:linear-gradient(180deg,${c.color}22,var(--card))" onclick="openCaseView('${c.key}')">
    <div class="case-ic">${c.img ? `<img src="${c.img}" onerror="this.outerHTML='<div class=ph>🎁</div>'">` : `<div class="ph">🎁</div>`}</div>
    <div class="case-title">${c.title}</div><div class="case-price">${c.price} 💎</div></div>`).join("");
}
function openCaseView(key){
  const c = STATE.cases.find(x=>x.key===key);
  document.getElementById("app").innerHTML = `<h2>${c.title}</h2>
    <div class="pick-list">${c.pool.map(s=>skinCard(s)).join("")}</div>
    <button class="btn" id="openBtn" onclick="doOpen('${key}')">Открыть за ${c.price} 💎</button>
    <button class="btn secondary" style="margin-top:8px" onclick="renderCases()">Назад</button>`;
}
async function doOpen(key){
  const btn = document.getElementById("openBtn"); if(btn) btn.disabled = true;
  const r = await api("/api/case_open", {case:key});
  if(r.error){ tg.showAlert(r.error==="no_money" ? "Не хватает монет 💎" : "Ошибка"); if(btn) btn.disabled=false; return; }
  STATE.balance = r.balance; document.getElementById("balVal").textContent = r.balance;
  const items = r.reel.map(id=>skinById(id));
  document.getElementById("app").innerHTML = `<h2>🎰 Открываем...</h2>
    <div class="reel-wrap"><div class="reel-ptr"></div><div class="reel" id="reel">
      ${items.map(s=>skinCard(s)).join("")}
    </div></div><div id="resultBox" class="result-box"></div>`;
  const reel = document.getElementById("reel");
  requestAnimationFrame(()=>{
    const card = reel.children[0];
    const style = getComputedStyle(reel);
    const gap = parseFloat(style.gap || style.columnGap || "10");
    const itemW = card.offsetWidth + gap;
    const stopIndex = items.length - 4;
    const offset = itemW * stopIndex + (Math.random()*itemW*0.5 - itemW*0.25);
    requestAnimationFrame(()=>{
      reel.style.transition = "transform 4.8s cubic-bezier(.09,.82,.09,1)";
      reel.style.transform = `translate3d(-${offset}px,0,0)`;
    });
  });
  setTimeout(()=>{
    const box = document.getElementById("resultBox");
    box.className = "result-box win";
    box.style.position = "relative";
    box.innerHTML = `<div class="win-burst"></div>
      <div style="display:flex;justify-content:center;margin:8px 0">${skinVisual(r.win)}</div>
      <div style="font-weight:800">${r.win.name}</div><div class="price">${r.win.price} 💎</div>
      <button class="btn" style="margin-top:10px" onclick="renderCases()">Ещё кейс</button>`;
    requestAnimationFrame(()=>box.classList.add("show"));
    load();
  }, 5000);
}
function skinById(id){ return STATE.skins.find(s=>s.id===id); }

function stakeSum(){ return stakeSel.reduce((s,id)=>s+skinById(id).price,0); }
function gaugeSvg(pct, idSuffix){
  const r = 100, c = 2*Math.PI*r, dash = Math.max(0.001, pct/100)*c;
  return `<svg viewBox="0 0 220 220"><circle class="gauge-track" cx="110" cy="110" r="${r}"></circle>
    <circle class="gauge-fill" id="gfill${idSuffix}" cx="110" cy="110" r="${r}"
      stroke-dasharray="${dash} ${c}"></circle></svg>`;
}
function gaugeHtml(pct, spinning, idSuffix){
  const safe = pct>=50;
  const pctTxt = pct<1 ? pct.toFixed(3) : pct.toFixed(1);
  return `<div class="gauge">
    ${gaugeSvg(pct, idSuffix)}
    <div class="gauge-mid"><div class="gauge-pct">${pctTxt}%</div>
      <div class="gauge-label" style="color:${safe?'var(--good)':'var(--bad)'}">${safe?'БЕЗОПАСНЫЙ ШАНС':'РИСКОВАННЫЙ ШАНС'}</div></div>
    <div class="gauge-needle" id="needle${idSuffix}" style="transform:translate(-50%,0) rotate(0deg)"></div>
  </div>`;
}
function renderUp(){
  if(!STATE.inventory.length){ document.getElementById("app").innerHTML = "<h2>⚡ Апгрейдер</h2><div class='empty'>Нужен хотя бы один скин в инвентаре.</div>"; return; }
  drawUpScreen();
}
function drawUpScreen(){
  const app = document.getElementById("app");
  const sum = stakeSum();
  const tgt = targetSel!==null ? skinById(targetSel) : null;
  const pct = tgt && sum>0 ? Math.min(99, sum/tgt.price*95) : 0;
  app.innerHTML = `<h2>⚡ Апгрейдер</h2>
    ${gaugeHtml(pct, false, "0")}
    <div class="stake-row">
      <div>
        <div style="font-size:11px;color:var(--sub);font-weight:700;margin-bottom:6px">СТАВКА (${stakeSel.length} ИЗ ${STATE.max_stake}) · ${sum} 💎</div>
        <div id="chips">${stakeSel.length ? [...new Set(stakeSel)].map(id=>{
          const n = stakeSel.filter(x=>x===id).length; const s=skinById(id);
          return `<span class="stake-chip">${skinVisual(s)}${s.name}${n>1?(' ×'+n):''}<span class="x" onclick="stakeRemove(${id})">✕</span></span>`;
        }).join("") : "<span style='color:var(--sub);font-size:12px'>тапни скин ниже, чтобы поставить</span>"}</div>
      </div>
    </div>
    <div class="stake-box" style="margin-bottom:12px;cursor:pointer" onclick="openTargetPicker()">
      <div class="cap">ВЫБЕРИТЕ ЦЕЛЬ</div>
      <div class="visual">${tgt ? skinVisual(tgt) : "<span style='color:var(--sub);font-size:12px'>не выбрано — нажми, чтобы выбрать</span>"}</div>
      <div class="sum">${tgt ? tgt.name+" · "+tgt.price+" 💎" : "—"}</div>
    </div>
    <button class="btn tap-ripple" id="goBtn" ${(sum>0 && tgt) ? "" : "disabled"} onclick="doUpgrade()">⚡ Заключить сделку</button>
    <h2 style="margin-top:18px">Инвентарь — тапни, чтобы поставить</h2>
    <div class="pick-list" id="invPick"></div>`;
  document.getElementById("invPick").innerHTML = STATE.inventory.map(s=>{
    const used = stakeSel.filter(x=>x===s.id).length;
    return `<div class="skin selectable tap-ripple ${rarCls(s.rarity)} ${used?'selected':''}" onclick="stakeTap(${s.id})" style="display:flex;align-items:center;gap:10px;text-align:left">
      ${skinVisual(s)}<div style="flex:1"><div class="name">${s.name} ${used?`(в ставке ${used}/${s.qty})`:`×${s.qty}`}</div><div class="price">${s.price} 💎</div></div></div>`;
  }).join("");
}
function stakeTap(id){
  const s = skinById(id); const used = stakeSel.filter(x=>x===id).length;
  if(used >= s.qty || stakeSel.length >= STATE.max_stake){
    // уже всё поставлено с этого скина (или лимит ставки) — повторный тап снимает его целиком
    stakeSel = stakeSel.filter(x=>x!==id);
  } else {
    stakeSel.push(id);
  }
  drawUpScreen();
}
function stakeRemove(id){ stakeSel = stakeSel.filter(x=>x!==id); drawUpScreen(); }
function openTargetPicker(){
  const list = [...STATE.skins].sort((a,b)=>a.price-b.price);
  const app = document.getElementById("app");
  app.innerHTML = `<h2>Выбери цель — любая, без ограничений</h2><div class="pick-list">${list.map(s=>
    `<div class="skin selectable tap-ripple ${rarCls(s.rarity)}" onclick="setTarget(${s.id})" style="display:flex;align-items:center;gap:10px;text-align:left">
      ${skinVisual(s)}<div style="flex:1"><div class="name">${s.name}</div><div class="price">${s.price} 💎</div></div></div>`).join("")}</div>
    <button class="btn secondary" style="margin-top:10px" onclick="drawUpScreen()">Назад</button>`;
}
function setTarget(id){ targetSel = id; drawUpScreen(); }
async function doUpgrade(){
  const btn = document.getElementById("goBtn"); btn.disabled = true;
  const r = await api("/api/upgrade", {from: stakeSel, to: targetSel});
  if(r.error){ tg.showAlert("Ошибка"); btn.disabled=false; return; }
  STATE.balance = r.balance; document.getElementById("balVal").textContent = r.balance;
  const pct = r.chance*100;
  document.getElementById("app").innerHTML = `<h2>⚡ Крутим колесо...</h2>${gaugeHtml(pct, true, "1")}<div id="resultBox" class="result-box"></div>`;
  const needle = document.getElementById("needle1");
  // финальный угол: внутри "безопасной" дуги для победы, вне неё для проигрыша; несколько полных оборотов для красоты
  const zoneDeg = Math.max(pct/100*360, 2);
  const stopDeg = r.win ? Math.random()*zoneDeg : zoneDeg + Math.random()*(360-zoneDeg);
  const spins = 4*360;
  requestAnimationFrame(()=>{ needle.style.transform = `translate(-50%,0) rotate(${spins+stopDeg}deg)`; });
  setTimeout(()=>{
    const box = document.getElementById("resultBox");
    box.className = "result-box " + (r.win?"win":"lose");
    box.style.position = "relative";
    box.innerHTML = r.win
      ? `<div class="win-burst"></div>
         <div style="display:flex;justify-content:center;margin:8px 0">${skinVisual(r.result)}</div>
         <div style="font-weight:800">Успех! Получен ${r.result.name}</div>
         <button class="btn" style="margin-top:10px" onclick="setTab('up')">Ещё апгрейд</button>`
      : `<div style="font-size:22px;color:var(--bad);font-weight:800;animation:popIn .3s">Неудача</div><div style="font-weight:800">Не повезло, ставка сгорела</div>
         <button class="btn secondary" style="margin-top:10px" onclick="setTab('up')">Назад</button>`;
    requestAnimationFrame(()=>box.classList.add("show"));
    load();
  }, 3800);
}

function renderInv(){
  if(!STATE.inventory.length){ document.getElementById("app").innerHTML = "<h2>🎒 Инвентарь</h2><div class='empty'>Пусто. Открой кейс!</div>"; return; }
  document.getElementById("app").innerHTML = "<h2>🎒 Инвентарь</h2><div class='pick-list' id='invList'></div>";
  document.getElementById("invList").innerHTML = STATE.inventory.map(s=>
    `<div class="skin tap-ripple ${rarCls(s.rarity)}" style="display:flex;align-items:center;gap:10px;text-align:left">
     ${skinVisual(s)}<div style="flex:1"><div class="name">${s.name} ×${s.qty}</div><div class="price">${s.price} 💎</div></div>
     <button class="btn secondary" style="width:auto;padding:8px 12px" onclick="doSell(${s.id})">Продать за ${Math.max(1,Math.round(s.price*0.8))}</button>
     </div>`).join("");
}
async function doSell(id){ const r = await api("/api/sell", {skin:id}); if(r.error) return tg.showAlert("Ошибка"); await load(); renderInv(); }

function renderDep(){
  const app = document.getElementById("app");
  app.innerHTML = `<h2>⭐ Пополнение звёздами Telegram</h2>
    <div style="color:var(--sub);margin-bottom:10px">1⭐ = ${STATE.stars_rate} 💎. Бот пришлёт тебе счёт в личные сообщения — оплата проходит прямо в Telegram.</div>
    <div class="row" style="margin-bottom:18px">
      <input class="inp" id="starsAmt" type="number" placeholder="Сколько звёзд" style="margin-bottom:0">
      <button class="btn tap-ripple" style="width:auto" onclick="buyStars()">Выставить счёт</button>
    </div>
    <h2>💎 Пополнение скином</h2>
    <div style="color:var(--sub);margin-bottom:10px">Выбери скин, который хочешь внести.</div>
    <div class="pick-list" id="depList"></div>
    <h2 style="margin-top:18px">Мои заявки</h2>
    <div id="depHist">${STATE.deposits.length ? STATE.deposits.map(d=>
      `<div class="skin" style="display:flex;justify-content:space-between;align-items:center;text-align:left">
      <span>#${d.id} · ${d.skin}</span>
      <span style="display:flex;gap:6px;align-items:center">
      <span class="badge ${d.status}">${({pending:'ожидает',given:'передано',ok:'принято',rejected:'отклонено'})[d.status]}</span>
      ${d.status==='pending' ? `<button class="btn secondary" style="width:auto;padding:6px 10px;font-size:12px" onclick="markGiven(${d.id})">Я отдал предмет</button>` : ""}
      </span></div>`
    ).join("") : "<div class='empty'>Пока нет заявок</div>"}</div>`;
  document.getElementById("depList").innerHTML = STATE.skins.map(s=>
    `<div class="skin selectable tap-ripple ${rarCls(s.rarity)}" style="display:flex;align-items:center;gap:10px;text-align:left" onclick="doDeposit(${s.id})">
     ${skinVisual(s)}<div style="flex:1"><div class="name">${s.name}</div><div class="price">${s.price} 💎</div></div></div>`).join("");
}
async function buyStars(){
  const stars = parseInt(document.getElementById("starsAmt").value||"0");
  if(stars < 1) return;
  const r = await api("/api/stars_topup", {stars});
  if(r.error){ tg.showAlert("Не получилось выставить счёт, попробуй позже"); return; }
  tg.showAlert(`Счёт на ${stars}⭐ отправлен тебе в личные сообщения бота — открой чат с ботом и оплати.`);
}
async function doDeposit(id){
  const r = await api("/api/deposit", {skin:id});
  tg.showAlert(`Ожидайте пополнения, в личку бота вам придёт ссылка: вы должны перейти по ней и отдать предмет нашему боту, ник будет указан возле ссылки (${r.admin_nick}).`);
  await load(); renderDep();
}
async function markGiven(id){ await api("/api/deposit_given", {id}); await load(); renderDep(); }

function renderPromo(){
  document.getElementById("app").innerHTML = `<h2>🎟 Промокод</h2>
    <input class="inp" id="promoInp" placeholder="Введи промокод">
    <button class="btn" onclick="activatePromo()">Активировать</button>`;
}
async function activatePromo(){
  const code = document.getElementById("promoInp").value.trim();
  if(!code) return;
  const r = await api("/api/promo", {code});
  if(r.error){ tg.showAlert({not_found:"Код не найден", exhausted:"Лимит активаций исчерпан", already_used:"Ты уже использовал этот код"}[r.error] || "Ошибка"); return; }
  STATE.balance = r.balance; document.getElementById("balVal").textContent = r.balance;
  tg.showAlert("Промокод активирован! 🎉"); renderPromo();
}

async function renderAdmin(){
  const app = document.getElementById("app");
  app.innerHTML = `<h2>🛠 Админ-панель</h2>
    <div class="row" style="margin-bottom:14px">
      <input class="inp" id="giveAmt" placeholder="Сколько монет себе" type="number" style="margin-bottom:0">
      <button class="btn" style="width:auto" onclick="giveSelf()">Выдать</button>
    </div>
    <h2>Заявки на пополнение</h2><div id="adminDep">Загрузка...</div>
    <h2 style="margin-top:18px">Создать промокод</h2>
    <input class="inp" id="pCode" placeholder="Код (пусто = случайный)">
    <input class="inp" id="pCoins" placeholder="Монет" type="number">
    <input class="inp" id="pCaseKey" placeholder="Ключ кейса (напр. c0, c10) — необязательно">
    <input class="inp" id="pUses" placeholder="Макс. активаций" type="number" value="1">
    <button class="btn" onclick="createPromo()">Создать</button>
    <h2 style="margin-top:18px">Промокоды</h2><div id="promoList">Загрузка...</div>
    <h2 style="margin-top:18px">🖼 Картинки скинов и кейсов</h2>
    <div style="color:var(--sub);font-size:12px;margin-bottom:8px">Кнопка ниже один раз качает картинки с вики MM2 прямо
    на сервер (без твоего участия) — дальше они отдаются с нашего домена и ни от чего постороннего не зависят.
    Если для чего-то картинка не скачалась или оказалась не той — вставь свою ссылку вручную, она в приоритете.</div>
    <div class="row" style="margin-bottom:10px">
      <button class="btn secondary" onclick="downloadImages(this,false)">📥 Скачать недостающие</button>
      <button class="btn secondary" onclick="downloadImages(this,true)">🔁 Повторить неудавшиеся</button>
    </div>
    <input class="inp" id="imgSearch" placeholder="Поиск по названию..." oninput="filterImages()">
    <div id="imgList">Загрузка...</div>
    <div style="text-align:center;color:var(--sub);font-size:11px;margin-top:24px">Версия: __APP_VERSION__</div>`;
  const dep = await api("/api/admin/deposits");
  document.getElementById("adminDep").innerHTML = (!dep.items || !dep.items.length) ? "<div class='empty'>Нет заявок</div>" :
    dep.items.map(p=>`<div class="skin" style="text-align:left">
     <div>#${p.id} · @${p.username||p.user_id} → <b>${p.skin}</b> <span class="badge ${p.status}">${p.status==='given'?'передано':'ожидает'}</span></div>
     <div class="row" style="margin-top:8px">
       ${p.username ? `<button class="btn secondary" onclick="window.open('https://t.me/${p.username}')">Открыть чат и выдать ник ${STATE.admin_nick}</button>` : ""}
       ${p.status==='given' ? `<button class="btn" onclick="decide(${p.id},true)">✅ Принять</button><button class="btn secondary" onclick="decide(${p.id},false)">❌ Отказ</button>` : ""}
     </div></div>`).join("");
  const pl = await api("/api/admin/promo_list");
  document.getElementById("promoList").innerHTML = (!pl.items || !pl.items.length) ? "<div class='empty'>Нет промокодов</div>" :
    pl.items.map(p=>`<div class="skin" style="text-align:left"><b>${p.code}</b> — ${p.coins}💎 ${p.case_key?('· кейс '+p.case_key):''} ${p.skin?('· '+p.skin):''} · ${p.used}/${p.max_uses}</div>`).join("");
  window._imgItems = (await api("/api/admin/images_list")).items || [];
  filterImages();
}
function filterImages(){
  const q = (document.getElementById("imgSearch").value||"").toLowerCase();
  const items = (window._imgItems||[]).filter(it=>it.label.toLowerCase().includes(q));
  document.getElementById("imgList").innerHTML = items.map(it=>`
    <div class="img-row">
      <img class="prev" src="${it.url}" onerror="this.style.visibility='hidden'">
      <div class="lbl">${it.label}</div>
      <input id="iu_${btoa(unescape(encodeURIComponent(it.key)))}" value="${(it.url||'').replace(/"/g,'&quot;')}" placeholder="вставь ссылку на картинку">
      <button onclick="saveImage('${it.key.replace(/'/g,"\\'")}')">✓</button>
    </div>`).join("");
}
async function downloadImages(btnEl, retryFailed){
  const btn = btnEl; if(btn) btn.disabled = true;
  let totalOk = 0, totalFail = 0, allFailed = [], guard = 0;
  while(true){
    guard++; if(guard > 30) break; // защита от зацикливания
    let r;
    try{ r = await api("/api/admin/download_images", {batch:5, retry_failed: guard===1 ? !!retryFailed : false}); }
    catch(e){ totalFail += 1; break; }
    if(r.error){ tg.showAlert("Ошибка доступа"); break; }
    totalOk += r.ok; totalFail += r.fail; allFailed = allFailed.concat(r.failed_names||[]);
    if(btn) btn.textContent = "📥 Качаю... осталось " + r.remaining;
    window._imgItems = (await api("/api/admin/images_list")).items || [];
    filterImages();
    if(r.done) break;
  }
  if(btn){ btn.disabled = false; btn.textContent = "📥 Скачать картинки с вики (недостающие)"; }
  tg.showAlert("Готово: скачано " + totalOk + ", не удалось " + totalFail + (allFailed.length ? ("\n Не скачались: "+allFailed.join(", ")) : ""));
}
async function saveImage(key){
  const id = "iu_"+btoa(unescape(encodeURIComponent(key)));
  const url = document.getElementById(id).value.trim();
  await api("/api/admin/set_image", {key, url});
  const it = (window._imgItems||[]).find(x=>x.key===key); if(it) it.url = url;
  filterImages();
}
async function giveSelf(){
  const amt = parseInt(document.getElementById("giveAmt").value||"0");
  const r = await api("/api/admin/give_self", {amount:amt});
  STATE.balance = r.balance; document.getElementById("balVal").textContent = r.balance; renderAdmin();
}
async function createPromo(){
  await api("/api/admin/promo_create", {
    code: document.getElementById("pCode").value, coins: parseInt(document.getElementById("pCoins").value||"0"),
    case_key: document.getElementById("pCaseKey").value || null, max_uses: parseInt(document.getElementById("pUses").value||"1")});
  renderAdmin();
}
async function decide(id, ok){ await api("/api/admin/decide", {id, ok}); renderAdmin(); }

(async()=>{ await load(); setTab("cases"); })();
</script>
</body></html>"""

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    threading.Thread(target=_run_bot_polling, daemon=True).start()
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
