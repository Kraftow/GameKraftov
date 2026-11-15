import os
import re
import tempfile
import html
import hmac
import json
import time
import random
import asyncio
import secrets
import sqlite3
import hashlib
import threading
from decimal import Decimal
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl

import httpx
from fastapi import FastAPI, Request, HTTPException, Header
from fastapi.responses import FileResponse, JSONResponse

# читаем настройки из файла .env (работает и на Windows)
_env = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env):
    for _line in open(_env, encoding="utf-8"):
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ.get("OWNER_ID") or 7406050921)          # твой Telegram ID — без лимита на кручение
_rw = os.environ.get("RAILWAY_PUBLIC_DOMAIN")  # Railway подставляет адрес сам
BASE_URL = (os.environ.get("BASE_URL") or os.environ.get("RENDER_EXTERNAL_URL")
            or (f"https://{_rw}" if _rw else "")).rstrip("/")
if not BASE_URL:
    raise RuntimeError("Не задан адрес сайта: добавьте переменную BASE_URL (https://ваш-домен)")    # https://твой-домен
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "change-me-please")
DB_PATH = os.environ.get("DB_PATH") or ("/data/bot.db" if os.path.isdir("/data") else "bot.db")  # /data = подключённый диск
DB_PERSISTENT = bool(os.environ.get("DB_PATH")) or DB_PATH.startswith("/data/")  # False -> база сотрётся при перезапуске

COOLDOWN = 12 * 60 * 60  # 12 часов
API = f"https://api.telegram.org/bot{BOT_TOKEN}"
MENU_BTN = "🏠 Главное меню"
OLD_FEEDBACK_BTN = "✉️ Обратная связь"   # старая кнопка у прежних игроков — заменяем на меню

# ---- мини-игра «Пельмень или Тапок» (вместо орла и решки) ----
COIN_SIDES = {"pelmen": ("Пельмень", "🥟"), "tapok": ("Тапок", "🩴")}
MAX_BET = 1_000_000_000
CURRENCY = "🪙"

# ---- курс: сколько монет даём за 1 USDT (меняется переменной COINS_PER_USDT; НЕ меняйте после появления балансов) ----
COINS_PER_USDT = max(1, int(os.environ.get("COINS_PER_USDT") or 100))
DEPOSIT_NETWORK = "TRC20"   # сеть для вывода (вывод обрабатывается вручную)

# ---- приём оплаты через Crypto Pay (@CryptoBot) ----
# Токен берём ТОЛЬКО из переменной окружения CRYPTOBOT_TOKEN (Railway -> Variables), в коде его не храним.
CRYPTO_TOKEN = (os.environ.get("CRYPTOBOT_TOKEN") or "").strip()
CRYPTO_API = ("https://testnet-pay.crypt.bot/api" if os.environ.get("CRYPTOBOT_TESTNET") == "1"
              else "https://pay.crypt.bot/api")
INVOICE_TTL = 3600           # сколько секунд счёт можно оплатить
# Счёт выставляется в долларах (1 USDT = 1 USD), а платить игрок может любой из этих валют — выбирает в Crypto Bot.
CRYPTO_ASSETS = (os.environ.get("CRYPTO_ASSETS") or "USDT,TON,BTC,ETH,LTC,BNB,TRX,USDC").replace(" ", "")
MIN_DEPOSIT, MAX_DEPOSIT = COINS_PER_USDT, 1_000_000   # в монетах (минимум = 1 USDT)


def usdt_str(coins: int) -> str:
    cents = coins * 100 // COINS_PER_USDT   # округляем вниз до сотых
    return f"{cents // 100}.{cents % 100:02d}"

# ---- вейджер: бонусные монеты нужно отыграть (сумма ставок) в N раз больше бонуса, прежде чем выводить ----
WAGER_MULT = int(os.environ.get("WAGER_MULT") or 15)

# ---- вывод ----
MIN_WITHDRAW = int(os.environ.get("MIN_WITHDRAW_USDT") or 5) * COINS_PER_USDT   # минимум на вывод (по умолчанию 5 USDT)
WALLET_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")  # адрес TRC20

# ---- крестики-нолики и наперстки ----
TTT_BLOCK_CHANCE = 75        # % — как часто бот замечает вашу выигрышную линию и блокирует её
SHELL_MULT = (27, 10)        # выплата при угадывании: ставка x2.7 (шанс 1 из 3)
POPUP_TTL = 3 * 24 * 3600    # сколько дней непросмотренное окно ждёт игрока

# ---- Колесо фортуны ----
# Сегменты идут по часовой стрелке от верха. «Ничего» стоит дважды, на противоположных сторонах (0 и 3).
# weight — относительный шанс выпадения: меняйте веса, чтобы регулировать щедрость колеса.
WHEEL_FS_BET = 10            # ставка одного фриспина с колеса (в монетах)
FREE_MAX_WIN = 50_000        # потолок выплаты за ОДИН бесплатный спин / фрибет (в монетах)
WHEEL_PRIZES = [
    {"kind": "nothing",   "value": 0,    "name": "Ничего",        "emoji": "😢", "big": "",     "small": "НИЧЕГО",     "weight": 37, "color": "#23233a"},
    {"kind": "freebet",   "value": 1000, "name": "Фрибет 1000",   "emoji": "🎟", "big": "1000", "small": "ФРИБЕТ",     "weight": 2,  "color": "#e0a81e", "dark": True},
    {"kind": "freebet",   "value": 500,  "name": "Фрибет 500",    "emoji": "🎟", "big": "500",  "small": "ФРИБЕТ",     "weight": 6,  "color": "#b3122e"},
    {"kind": "nothing",   "value": 0,    "name": "Ничего",        "emoji": "😢", "big": "",     "small": "НИЧЕГО",     "weight": 37, "color": "#23233a"},
    {"kind": "freespins", "value": 50,   "name": "50 фриспинов",  "emoji": "💎", "big": "50",   "small": "ФРИСПИНОВ",  "weight": 3,  "color": "#6a3cff", "rare": True},
    {"kind": "freespins", "value": 30,   "name": "30 фриспинов",  "emoji": "🎰", "big": "30",   "small": "ФРИСПИНОВ",  "weight": 15, "color": "#119a68"},
]
WHEEL_EMOJI = {p["name"]: p["emoji"] for p in WHEEL_PRIZES}

# ---- реферальная программа ----
REF_MIN_USDT = 5                                   # минимальный депозит друга
REF_BONUS_USDT = int(os.environ.get("REF_BONUS_USDT") or 50)   # бонус пригласившему
REF_MIN_COINS = REF_MIN_USDT * COINS_PER_USDT
REF_BONUS_COINS = REF_BONUS_USDT * COINS_PER_USDT

# ---- промокоды ----
PROMO_MIN_BET = 10                                 # минимальная ставка фриспина (подходит ко всем слотам)
PROMO_RE = re.compile(r"^[A-Z0-9_-]{3,20}$")
BOT_USERNAME = (os.environ.get("BOT_USERNAME") or "").lstrip("@")

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db_lock = threading.Lock()
with db_lock:
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS spins(user_id INTEGER, ts REAL, prize TEXT);
        CREATE TABLE IF NOT EXISTS fb_wait(user_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS fb_map(owner_msg_id INTEGER PRIMARY KEY, user_id INTEGER);
        CREATE TABLE IF NOT EXISTS users(user_id INTEGER PRIMARY KEY, first_name TEXT, username TEXT, last_seen REAL, blocked INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS balances(user_id INTEGER PRIMARY KEY, coins INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS tx(user_id INTEGER, ts REAL, delta INTEGER, reason TEXT);
        CREATE TABLE IF NOT EXISTS banned(user_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS cd_skip(user_id INTEGER PRIMARY KEY, ts REAL);
        CREATE TABLE IF NOT EXISTS bj(user_id INTEGER PRIMARY KEY, deck TEXT, player TEXT, dealer TEXT, bet INTEGER);
        CREATE TABLE IF NOT EXISTS ttt(user_id INTEGER PRIMARY KEY, board TEXT, bet INTEGER);
        CREATE TABLE IF NOT EXISTS deposits(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, ts REAL, amount INTEGER, status TEXT);
        CREATE TABLE IF NOT EXISTS popups(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, icon TEXT, title TEXT, text TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS popup_seen(user_id INTEGER, popup_id INTEGER, PRIMARY KEY(user_id, popup_id));
        CREATE TABLE IF NOT EXISTS withdrawals(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, ts REAL, coins INTEGER, usdt TEXT, wallet TEXT, status TEXT, reason TEXT);
        CREATE TABLE IF NOT EXISTS admin_wait(owner_id INTEGER PRIMARY KEY, wd_id INTEGER, card_msg_id INTEGER);
        CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS luck(scope TEXT PRIMARY KEY, level INTEGER NOT NULL, until REAL);
        CREATE TABLE IF NOT EXISTS freespins(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, kind TEXT, spins_left INTEGER NOT NULL, bet INTEGER NOT NULL, source TEXT, ts REAL);
        CREATE TABLE IF NOT EXISTS promos(code TEXT PRIMARY KEY, coins INTEGER NOT NULL DEFAULT 0, spins INTEGER NOT NULL DEFAULT 0, bet INTEGER NOT NULL DEFAULT 0, max_uses INTEGER NOT NULL, used INTEGER NOT NULL DEFAULT 0, active INTEGER NOT NULL DEFAULT 1, created_by INTEGER, ts REAL);
        CREATE TABLE IF NOT EXISTS promo_uses(code TEXT, user_id INTEGER, ts REAL, PRIMARY KEY(code, user_id));
        CREATE TABLE IF NOT EXISTS wager(user_id INTEGER PRIMARY KEY, left INTEGER NOT NULL DEFAULT 0, bonus INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS referrals(user_id INTEGER PRIMARY KEY, referrer_id INTEGER NOT NULL, ts REAL, rewarded INTEGER NOT NULL DEFAULT 0, reward_ts REAL);
        """
    )
    try:
        db.execute("ALTER TABLE wager ADD COLUMN bonus INTEGER NOT NULL DEFAULT 0")
    except sqlite3.OperationalError:
        pass
    for _col in ("invoice_id INTEGER", "pay_url TEXT"):     # миграция: поля для счетов Crypto Pay
        try:
            db.execute(f"ALTER TABLE deposits ADD COLUMN {_col}")
        except sqlite3.OperationalError:
            pass
    db.execute("CREATE INDEX IF NOT EXISTS idx_dep_invoice ON deposits(invoice_id)")
    db.commit()


async def tg(method: str, **data):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{API}/{method}", json=data)
        return r.json()


async def ensure_bot_username() -> str:
    """Имя бота нужно для реферальных ссылок (t.me/<бот>?start=ref_ID)."""
    global BOT_USERNAME
    if not BOT_USERNAME:
        try:
            BOT_USERNAME = ((await tg("getMe")).get("result") or {}).get("username") or ""
        except Exception:
            pass
    return BOT_USERNAME


@asynccontextmanager
async def lifespan(app: FastAPI):
    await tg(
        "setWebhook",
        url=f"{BASE_URL}/webhook",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message", "callback_query"],
    )
    await tg(
        "setChatMenuButton",
        menu_button={"type": "web_app", "text": "Открыть", "web_app": {"url": BASE_URL}},
    )
    if not DB_PERSISTENT:
        await tg("sendMessage", chat_id=OWNER_ID,
                 text="⚠️ База данных лежит НЕ на постоянном диске — балансы и заявки сотрутся при перезапуске.\n"
                      "Подключите в Railway Volume с путём /data и перезапустите сервис.")
    await ensure_bot_username()
    if not CRYPTO_TOKEN:
        await tg("sendMessage", chat_id=OWNER_ID,
                 text="⚠️ Не задана переменная CRYPTOBOT_TOKEN — пополнения через Crypto Bot не работают.\n"
                      "Добавьте её в Railway → Variables и перезапустите сервис.")
    poller = asyncio.create_task(deposit_poller())
    yield
    poller.cancel()


app = FastAPI(lifespan=lifespan)


# ---------- проверка initData от Telegram ----------
def check_init_data(init_data: str) -> dict:
    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    their_hash = pairs.pop("hash", None)
    if not their_hash:
        raise HTTPException(401, "no hash")
    data_check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    mine = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mine, their_hash):
        raise HTTPException(401, "bad hash")
    if time.time() - int(pairs.get("auth_date", 0)) > 86400:
        raise HTTPException(401, "expired")
    return json.loads(pairs["user"])


def _cd_nolock(uid: int) -> int:
    if uid == OWNER_ID:
        return 0
    last = db.execute("SELECT MAX(ts) FROM spins WHERE user_id=?", (uid,)).fetchone()[0]
    if not last:
        return 0
    skip = db.execute("SELECT ts FROM cd_skip WHERE user_id=?", (uid,)).fetchone()
    if skip and skip[0] >= last:  # админ сбросил кулдаун после последней прокрутки
        return 0
    return max(0, int(COOLDOWN - (time.time() - last)))


def seconds_left(uid: int) -> int:
    with db_lock:
        return _cd_nolock(uid)


# ---------- пользователи и баланс ----------
def touch_user(u: dict):
    with db_lock:
        db.execute(
            "INSERT INTO users(user_id, first_name, username, last_seen, blocked) VALUES(?,?,?,?,0) "
            "ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name, "
            "username=excluded.username, last_seen=excluded.last_seen, blocked=0",
            (u["id"], u.get("first_name", ""), (u.get("username") or "").lower(), time.time()),
        )
        db.commit()


def get_balance(uid: int) -> int:
    row = db.execute("SELECT coins FROM balances WHERE user_id=?", (uid,)).fetchone()
    return row[0] if row else 0


def add_balance(uid: int, delta: int, reason: str) -> int:
    """Вызывать только под db_lock. Возвращает новый баланс."""
    db.execute("INSERT OR IGNORE INTO balances(user_id, coins) VALUES(?,0)", (uid,))
    db.execute("UPDATE balances SET coins = coins + ? WHERE user_id=?", (delta, uid))
    db.execute("INSERT INTO tx VALUES(?,?,?,?)", (uid, time.time(), delta, reason))
    if delta > 0 and (reason.startswith(("referral bonus", "promo ", "free "))):   # бонусные начисления -> вейджер
        db.execute("INSERT INTO wager(user_id, left, bonus) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET "
                   "left=left+excluded.left, bonus=bonus+excluded.bonus", (uid, delta * WAGER_MULT, delta))
    db.commit()
    return get_balance(uid)


def wager_play(uid: int, bet: int):
    """Реальная ставка (не фриспин) засчитывается в отыгрыш. Вызывать под db_lock."""
    db.execute("UPDATE wager SET left=MAX(0, left-?) WHERE user_id=? AND left>0", (bet, uid))
    db.execute("UPDATE wager SET bonus=0 WHERE user_id=? AND left=0 AND bonus>0", (uid,))   # отыграли — бонус стал обычными деньгами


def wager_left(uid: int) -> int:
    row = db.execute("SELECT left FROM wager WHERE user_id=?", (uid,)).fetchone()
    return row[0] if row else 0


def resolve_target(target: str):
    """Вызывать под db_lock. ID или @username -> (user_id, first_name, username) или None."""
    t = str(target).strip()
    if not t:
        return None
    if t.lstrip("-").isdigit():
        return db.execute("SELECT user_id, first_name, username FROM users WHERE user_id=?", (int(t),)).fetchone()
    return db.execute(
        "SELECT user_id, first_name, username FROM users WHERE username=?", (t.lstrip("@").lower(),)
    ).fetchone()


def is_banned(uid: int) -> bool:
    return db.execute("SELECT 1 FROM banned WHERE user_id=?", (uid,)).fetchone() is not None


def is_admin(uid: int) -> bool:
    """Вызывать под db_lock."""
    return uid == OWNER_ID or db.execute("SELECT 1 FROM admins WHERE user_id=?", (uid,)).fetchone() is not None


# ---------- регулятор удачи («щедрый режим») ----------
# Уровень задаёт, сколько раз игра «бросает кубик» и отдаёт игроку лучший исход.
# 0 — обычные шансы, 1 — лучший из 2 попыток, 2 — лучший из 3. Режим только повышает шансы игрока.
LUCK_NAMES = ["Обычная", "Щедрая (×2)", "Очень щедрая (×3)"]
LUCK_TRIES = [1, 2, 3]


def luck_tries(uid: int) -> int:
    """Сколько попыток получит игрок. Вызывать под db_lock."""
    rows = db.execute(
        "SELECT level FROM luck WHERE scope IN ('global', ?) AND (until IS NULL OR until>?)",
        (f"u:{uid}", time.time()),
    ).fetchall()
    lvl = max([r[0] for r in rows] + [0])
    return LUCK_TRIES[max(0, min(lvl, len(LUCK_TRIES) - 1))]


BANNED_RESP = lambda: JSONResponse({"error": "banned"}, status_code=403)


async def body_user(req: Request):
    body = await req.json()
    user = check_init_data(body["initData"])
    return body, user


async def body_admin(req: Request):
    body, user = await body_user(req)
    with db_lock:
        ok = is_admin(user["id"])
    if not ok:  # проверка на сервере, а не в интерфейсе
        raise HTTPException(403, "admin only")
    return body, user

# ---------- API для мини-приложения ----------
@app.get("/")
async def index():
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "index.html"))


# ---------- фриспины и фрибеты ----------
def packs_nolock(uid: int) -> list:
    """Бесплатные вращения игрока. Вызывать под db_lock."""
    rows = db.execute(
        "SELECT id, kind, spins_left, bet FROM freespins WHERE user_id=? AND spins_left>0 ORDER BY kind DESC, bet, id",
        (uid,),
    ).fetchall()
    return [{"id": a, "kind": k, "left": n, "bet": b} for a, k, n, b in rows]


def grant_pack(uid: int, kind: str, n: int, bet: int, source: str):
    """kind: 'freespins' | 'freebet'. Одинаковые наборы складываются. Вызывать под db_lock."""
    cur = db.execute(
        "UPDATE freespins SET spins_left=spins_left+? WHERE user_id=? AND kind=? AND bet=? AND spins_left>0",
        (n, uid, kind, bet),
    )
    if cur.rowcount == 0:
        db.execute("INSERT INTO freespins(user_id, kind, spins_left, bet, source, ts) VALUES(?,?,?,?,?,?)",
                   (uid, kind, n, bet, source, time.time()))
    db.commit()


@app.post("/api/status")
async def status(req: Request):
    user = check_init_data((await req.json())["initData"])
    touch_user(user)
    with db_lock:
        bal = get_balance(user["id"])
        adm = is_admin(user["id"])
        packs = packs_nolock(user["id"])
    return {
        "balance": bal,
        "is_admin": adm,
        "is_owner": user["id"] == OWNER_ID,
        "left": seconds_left(user["id"]),
        "packs": packs,
        "wager_left": wager_left(user["id"]), "rate": COINS_PER_USDT, "min_deposit": MIN_DEPOSIT, "min_withdraw": MIN_WITHDRAW,
        "ref_bonus_usdt": REF_BONUS_USDT, "ref_min_usdt": REF_MIN_USDT,
        "prizes": [{k: p.get(k) for k in ("kind", "name", "emoji", "big", "small", "color", "dark", "rare")}
                   for p in WHEEL_PRIZES],
    }


@app.post("/api/history")
async def history(req: Request):
    user = check_init_data((await req.json())["initData"])
    with db_lock:
        rows = db.execute(
            "SELECT prize, ts FROM spins WHERE user_id=? ORDER BY ts DESC LIMIT 50", (user["id"],)
        ).fetchall()
    return {"items": [{"prize": p, "ts": t, "emoji": WHEEL_EMOJI.get(p, "🎁")} for p, t in rows]}


@app.post("/api/spin")
async def spin(req: Request):
    user = check_init_data((await req.json())["initData"])
    uid = user["id"]
    with db_lock:  # защита от двойного нажатия
        if is_banned(uid):
            return BANNED_RESP()
        left = seconds_left_nolock(uid)
        if left > 0:
            return JSONResponse({"error": "cooldown", "left": left}, status_code=429)
        idx = random.SystemRandom().choices(range(len(WHEEL_PRIZES)), weights=[p["weight"] for p in WHEEL_PRIZES])[0]
        p = WHEEL_PRIZES[idx]
        db.execute("INSERT INTO spins VALUES(?,?,?)", (uid, time.time(), p["name"]))
        if p["kind"] == "freebet":
            grant_pack(uid, "freebet", 1, p["value"], "wheel")
        elif p["kind"] == "freespins":
            grant_pack(uid, "freespins", p["value"], WHEEL_FS_BET, "wheel")
        db.commit()
        packs = packs_nolock(uid)
    if p["kind"] == "freebet":
        text = f"🎟 Фрибет {p['value']} {CURRENCY}! Используйте его в слотах — выигрыш ваш"
    elif p["kind"] == "freespins":
        text = f"{p['emoji']} {p['value']} фриспинов по {WHEEL_FS_BET} {CURRENCY}! Крутите их в слотах"
    else:
        text = "😢 В этот раз ничего — повезёт в другой раз"
    await tg("sendMessage", chat_id=uid, text=f"🎡 Колесо фортуны: {text}" if p["kind"] != "nothing" else f"🎡 {text}")
    return {"index": idx, "name": p["name"], "kind": p["kind"], "text": text, "packs": packs,
            "left": 0 if uid == OWNER_ID else COOLDOWN}


seconds_left_nolock = _cd_nolock


# ---------- мини-игры ----------
@app.post("/api/coin")
async def coin(req: Request):
    body, user = await body_user(req)
    uid = user["id"]
    side = body.get("side")
    try:
        bet = int(body.get("bet"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad_bet"}, status_code=400)
    if side not in COIN_SIDES or bet < 1 or bet > MAX_BET:
        return JSONResponse({"error": "bad_bet"}, status_code=400)
    with db_lock:  # атомарно: проверка баланса + списание/начисление
        if is_banned(uid):
            return BANNED_RESP()
        bal = get_balance(uid)
        if bet > bal:
            return JSONResponse({"error": "no_money", "balance": bal}, status_code=400)
        for _ in range(luck_tries(uid)):          # режим удачи: даём несколько попыток, берём выигрышную
            result = secrets.choice(list(COIN_SIDES))
            if result == side:
                break
        won = result == side
        wager_play(uid, bet)
        bal = add_balance(uid, bet if won else -bet, f"coin {'win' if won else 'lose'}")
    return {"result": result, "won": won, "balance": bal}


# ---------- админка (только OWNER_ID) ----------
@app.post("/api/admin/stats")
async def admin_stats(req: Request):
    await body_admin(req)
    with db_lock:
        users = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        blocked = db.execute("SELECT COUNT(*) FROM users WHERE blocked=1").fetchone()[0]
        total = db.execute("SELECT COALESCE(SUM(coins),0) FROM balances").fetchone()[0]
        top = db.execute(
            "SELECT b.user_id, u.first_name, u.username, b.coins FROM balances b "
            "LEFT JOIN users u ON u.user_id=b.user_id WHERE b.coins<>0 ORDER BY b.coins DESC LIMIT 10"
        ).fetchall()
    return {
        "users": users, "blocked": blocked, "total": total, "persistent": DB_PERSISTENT,
        "top": [{"id": i, "name": n or "", "username": un or "", "coins": c} for i, n, un, c in top],
    }


@app.post("/api/admin/give")
async def admin_give(req: Request):
    body, _ = await body_admin(req)
    target = str(body.get("target", "")).strip()
    try:
        amount = int(body.get("amount"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Некорректная сумма"}, status_code=400)
    if amount == 0 or abs(amount) > MAX_BET:
        return JSONResponse({"error": "Некорректная сумма"}, status_code=400)
    with db_lock:
        row = resolve_target(target)
        if not row:
            return JSONResponse(
                {"error": "Пользователь не найден (он должен хотя бы раз открыть бота)"}, status_code=404
            )
        uid, name = row[0], row[1]
        if amount < 0 and get_balance(uid) + amount < 0:
            amount = -get_balance(uid)  # не уходим в минус
            if amount == 0:
                return JSONResponse({"error": "У пользователя уже 0"}, status_code=400)
        new_bal = add_balance(uid, amount, "admin")
    if amount > 0:
        await tg("sendMessage", chat_id=uid, text=f"💰 Вам начислено {amount} {CURRENCY}\nБаланс: {new_bal} {CURRENCY}")
    return {"ok": True, "user_id": uid, "name": name, "balance": new_bal, "applied": amount}


_broadcast_running = False


async def run_broadcast(text: str):
    global _broadcast_running
    ok = fail = 0
    try:
        with db_lock:
            ids = [r[0] for r in db.execute("SELECT user_id FROM users WHERE blocked=0").fetchall()]
        async with httpx.AsyncClient(timeout=15) as c:
            for uid in ids:
                try:
                    r = (await c.post(f"{API}/sendMessage", json={"chat_id": uid, "text": text})).json()
                except Exception:
                    r = {"ok": False}
                if r.get("ok"):
                    ok += 1
                else:
                    fail += 1
                    if r.get("error_code") == 403:  # пользователь заблокировал бота
                        with db_lock:
                            db.execute("UPDATE users SET blocked=1 WHERE user_id=?", (uid,))
                            db.commit()
                    elif r.get("error_code") == 429:
                        await asyncio.sleep(r.get("parameters", {}).get("retry_after", 3))
                await asyncio.sleep(0.05)  # ~20 сообщений/сек, в рамках лимитов Telegram
        await tg("sendMessage", chat_id=OWNER_ID, text=f"📣 Рассылка завершена\n✅ Доставлено: {ok}\n❌ Не доставлено: {fail}")
    finally:
        _broadcast_running = False


@app.post("/api/admin/broadcast")
async def admin_broadcast(req: Request):
    global _broadcast_running
    body, _ = await body_admin(req)
    text = str(body.get("text", "")).strip()
    if not text or len(text) > 4000:
        return JSONResponse({"error": "Текст пустой или длиннее 4000 символов"}, status_code=400)
    if _broadcast_running:
        return JSONResponse({"error": "Предыдущая рассылка ещё идёт"}, status_code=409)
    with db_lock:
        n = db.execute("SELECT COUNT(*) FROM users WHERE blocked=0").fetchone()[0]
    _broadcast_running = True
    asyncio.create_task(run_broadcast(text))
    return {"ok": True, "recipients": n}


# ---------- Блэкджек ----------
SUITS = "♠♥♦♣"
RANKS = ["2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K", "A"]


def card_val(c: str) -> int:
    r = c[:-1]
    return 11 if r == "A" else 10 if r in ("J", "Q", "K") else int(r)


def hand_value(cards) -> int:
    total = sum(card_val(c) for c in cards)
    aces = sum(1 for c in cards if c[:-1] == "A")
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total


def bj_load(uid: int):
    row = db.execute("SELECT deck, player, dealer, bet FROM bj WHERE user_id=?", (uid,)).fetchone()
    if not row:
        return None
    return {"deck": json.loads(row[0]), "player": json.loads(row[1]), "dealer": json.loads(row[2]), "bet": row[3]}


def bj_save(uid: int, g: dict):
    db.execute(
        "INSERT OR REPLACE INTO bj VALUES(?,?,?,?,?)",
        (uid, json.dumps(g["deck"]), json.dumps(g["player"]), json.dumps(g["dealer"]), g["bet"]),
    )
    db.commit()


def bj_view(uid: int, g: dict, outcome=None, payout=0) -> dict:
    finished = outcome is not None
    dealer = g["dealer"] if finished else [g["dealer"][0], "??"]
    bal = get_balance(uid)
    return {
        "active": not finished,
        "player": g["player"], "player_value": hand_value(g["player"]),
        "dealer": dealer, "dealer_value": hand_value(g["dealer"]) if finished else hand_value(g["dealer"][:1]),
        "bet": g["bet"], "outcome": outcome, "payout": payout, "balance": bal,
        "can_double": (not finished) and len(g["player"]) == 2 and bal >= g["bet"],
    }


def bj_settle(uid: int, g: dict, outcome: str) -> dict:
    bet = g["bet"]
    payout = {"blackjack": bet + bet * 3 // 2, "win": bet * 2, "push": bet, "lose": 0}[outcome]
    if payout:
        add_balance(uid, payout, f"bj {outcome}")
    db.execute("DELETE FROM bj WHERE user_id=?", (uid,))
    db.commit()
    return bj_view(uid, g, outcome, payout)


def bj_stand(uid: int, g: dict) -> dict:
    while hand_value(g["dealer"]) < 17:  # дилер берёт до 17
        g["dealer"].append(g["deck"].pop())
    pv, dv = hand_value(g["player"]), hand_value(g["dealer"])
    outcome = "win" if dv > 21 or pv > dv else "push" if pv == dv else "lose"
    return bj_settle(uid, g, outcome)


@app.post("/api/bj/state")
async def bj_state(req: Request):
    _, user = await body_user(req)
    with db_lock:
        g = bj_load(user["id"])
        return bj_view(user["id"], g) if g else {"active": False, "balance": get_balance(user["id"])}


@app.post("/api/bj/start")
async def bj_start(req: Request):
    body, user = await body_user(req)
    uid = user["id"]
    try:
        bet = int(body.get("bet"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad_bet"}, status_code=400)
    if bet < 1 or bet > MAX_BET:
        return JSONResponse({"error": "bad_bet"}, status_code=400)
    with db_lock:
        if is_banned(uid):
            return BANNED_RESP()
        if bj_load(uid):
            return JSONResponse({"error": "already_active"}, status_code=409)
        bal = get_balance(uid)
        if bet > bal:
            return JSONResponse({"error": "no_money", "balance": bal}, status_code=400)
        wager_play(uid, bet)
        add_balance(uid, -bet, "bj bet")
        deck = [r + s_ for r in RANKS for s_ in SUITS]
        secrets.SystemRandom().shuffle(deck)
        g = {"deck": deck, "player": [deck.pop(), deck.pop()], "dealer": [], "bet": bet}
        g["dealer"] = [g["deck"].pop(), g["deck"].pop()]
        p_bj, d_bj = hand_value(g["player"]) == 21, hand_value(g["dealer"]) == 21
        if p_bj and d_bj:
            return bj_settle(uid, g, "push")
        if p_bj:
            return bj_settle(uid, g, "blackjack")
        if d_bj:
            return bj_settle(uid, g, "lose")
        bj_save(uid, g)
        return bj_view(uid, g)


@app.post("/api/bj/action")
async def bj_action(req: Request):
    body, user = await body_user(req)
    uid, act = user["id"], body.get("action")
    with db_lock:
        g = bj_load(uid)
        if not g:
            return JSONResponse({"error": "no_game"}, status_code=409)
        if act == "hit":
            g["player"].append(g["deck"].pop())
            v = hand_value(g["player"])
            if v > 21:
                return bj_settle(uid, g, "lose")
            if v == 21:
                return bj_stand(uid, g)
            bj_save(uid, g)
            return bj_view(uid, g)
        if act == "stand":
            return bj_stand(uid, g)
        if act == "double":
            if len(g["player"]) != 2 or get_balance(uid) < g["bet"]:
                return JSONResponse({"error": "cant_double"}, status_code=400)
            wager_play(uid, g["bet"])
            add_balance(uid, -g["bet"], "bj double")
            g["bet"] *= 2
            g["player"].append(g["deck"].pop())
            if hand_value(g["player"]) > 21:
                return bj_settle(uid, g, "lose")
            return bj_stand(uid, g)
        return JSONResponse({"error": "bad_action"}, status_code=400)


# ---------- ещё функции админа ----------
@app.post("/api/admin/find")
async def admin_find(req: Request):
    body, _ = await body_admin(req)
    with db_lock:
        row = resolve_target(body.get("target", ""))
        if not row:
            return JSONResponse({"error": "Пользователь не найден"}, status_code=404)
        uid = row[0]
        info = db.execute("SELECT last_seen, blocked FROM users WHERE user_id=?", (uid,)).fetchone()
        spins = db.execute("SELECT COUNT(*) FROM spins WHERE user_id=?", (uid,)).fetchone()[0]
        return {
            "id": uid, "name": row[1], "username": row[2], "balance": get_balance(uid),
            "last_seen": info[0], "blocked_bot": bool(info[1]), "banned": is_banned(uid),
            "spins": spins, "cooldown_left": _cd_nolock(uid),
        }


@app.post("/api/admin/setbal")
async def admin_setbal(req: Request):
    body, _ = await body_admin(req)
    try:
        amount = int(body.get("amount"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Некорректная сумма"}, status_code=400)
    if amount < 0 or amount > MAX_BET:
        return JSONResponse({"error": "Сумма от 0 до 1 000 000 000"}, status_code=400)
    with db_lock:
        row = resolve_target(body.get("target", ""))
        if not row:
            return JSONResponse({"error": "Пользователь не найден"}, status_code=404)
        delta = amount - get_balance(row[0])
        bal = add_balance(row[0], delta, "admin set") if delta else amount
    return {"ok": True, "name": row[1], "balance": bal}


@app.post("/api/admin/giveall")
async def admin_giveall(req: Request):
    global _broadcast_running
    body, _ = await body_admin(req)
    try:
        amount = int(body.get("amount"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Некорректная сумма"}, status_code=400)
    notify = bool(body.get("notify"))
    if amount < 1 or amount > MAX_BET:
        return JSONResponse({"error": "Сумма от 1 до 1 000 000 000"}, status_code=400)
    if notify and _broadcast_running:
        return JSONResponse({"error": "Сейчас идёт рассылка, подождите"}, status_code=409)
    with db_lock:
        ids = [r[0] for r in db.execute("SELECT user_id FROM users").fetchall()]
        for uid in ids:
            add_balance(uid, amount, "admin give all")
    if notify:
        _broadcast_running = True
        asyncio.create_task(run_broadcast(f"🎁 Всем игрокам начислено {amount} {CURRENCY}! Заходите играть."))
    return {"ok": True, "count": len(ids)}


@app.post("/api/admin/resetcd")
async def admin_resetcd(req: Request):
    body, _ = await body_admin(req)
    with db_lock:
        row = resolve_target(body.get("target", ""))
        if not row:
            return JSONResponse({"error": "Пользователь не найден"}, status_code=404)
        db.execute("INSERT OR REPLACE INTO cd_skip VALUES(?,?)", (row[0], time.time()))
        db.commit()
    return {"ok": True, "name": row[1]}


@app.post("/api/admin/ban")
async def admin_ban(req: Request):
    body, _ = await body_admin(req)
    ban = bool(body.get("ban"))
    with db_lock:
        row = resolve_target(body.get("target", ""))
        if not row:
            return JSONResponse({"error": "Пользователь не найден"}, status_code=404)
        if is_admin(row[0]):
            return JSONResponse({"error": "Админов банить нельзя 🙂"}, status_code=400)
        if ban:
            db.execute("INSERT OR IGNORE INTO banned VALUES(?)", (row[0],))
        else:
            db.execute("DELETE FROM banned WHERE user_id=?", (row[0],))
        db.commit()
    return {"ok": True, "name": row[1], "banned": ban}


@app.post("/api/admin/dm")
async def admin_dm(req: Request):
    body, _ = await body_admin(req)
    text = str(body.get("text", "")).strip()
    if not text or len(text) > 4000:
        return JSONResponse({"error": "Текст пустой или длиннее 4000 символов"}, status_code=400)
    with db_lock:
        row = resolve_target(body.get("target", ""))
    if not row:
        return JSONResponse({"error": "Пользователь не найден"}, status_code=404)
    r = await tg("sendMessage", chat_id=row[0], text=f"💬 Сообщение от администратора:\n\n{text}")
    if not r.get("ok"):
        return JSONResponse({"error": "Не доставлено (возможно, пользователь заблокировал бота)"}, status_code=502)
    return {"ok": True, "name": row[1]}


@app.post("/api/admin/log")
async def admin_log(req: Request):
    await body_admin(req)
    with db_lock:
        rows = db.execute(
            "SELECT t.ts, t.user_id, u.first_name, u.username, t.delta, t.reason FROM tx t "
            "LEFT JOIN users u ON u.user_id=t.user_id ORDER BY t.ts DESC LIMIT 25"
        ).fetchall()
    return {"items": [{"ts": a, "id": b, "name": c or "", "username": d or "", "delta": e, "reason": f}
                      for a, b, c, d, e, f in rows]}


_IMAGES = {
    "clover.svg": "image/svg+xml", "ace.svg": "image/svg+xml", "money.svg": "image/svg+xml", "sad.svg": "image/svg+xml",
    "pug.png": "image/png", "beag.png": "image/png", "rott.png": "image/png", "lep.png": "image/png",
    "bonus.jpg": "image/jpeg",
}


@app.get("/static/{name}")
async def static_img(name: str):
    if name not in _IMAGES:
        raise HTTPException(404)
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), media_type=_IMAGES[name],
                        headers={"Cache-Control": "public, max-age=86400"})


# ---------- Крестики-нолики ----------
WIN_LINES = [(0, 1, 2), (3, 4, 5), (6, 7, 8), (0, 3, 6), (1, 4, 7), (2, 5, 8), (0, 4, 8), (2, 4, 6)]


def ttt_winner(b: str):
    for a, c, d in WIN_LINES:
        if b[a] != "-" and b[a] == b[c] == b[d]:
            return b[a]
    return None


def ttt_bot_move(b: str) -> int:
    empty = [i for i, ch in enumerate(b) if ch == "-"]
    for mark, chance in (("O", 100), ("X", TTT_BLOCK_CHANCE)):  # сначала выиграть, потом (не всегда) заблокировать
        if secrets.randbelow(100) < chance:
            for i in empty:
                if ttt_winner(b[:i] + mark + b[i + 1:]) == mark:
                    return i
    if 4 in empty and secrets.randbelow(100) < 70:
        return 4
    return secrets.choice(empty)


def ttt_view(uid: int, b: str, bet: int, outcome=None, payout=0) -> dict:
    return {"active": outcome is None, "board": list(b), "bet": bet, "outcome": outcome,
            "payout": payout, "balance": get_balance(uid)}


def ttt_settle(uid: int, b: str, bet: int, outcome: str) -> dict:
    payout = {"win": bet * 2, "push": bet, "lose": 0}[outcome]
    if payout:
        add_balance(uid, payout, f"ttt {outcome}")
    db.execute("DELETE FROM ttt WHERE user_id=?", (uid,))
    db.commit()
    return ttt_view(uid, b, bet, outcome, payout)


@app.post("/api/ttt/state")
async def ttt_state(req: Request):
    _, user = await body_user(req)
    uid = user["id"]
    with db_lock:
        row = db.execute("SELECT board, bet FROM ttt WHERE user_id=?", (uid,)).fetchone()
        return ttt_view(uid, row[0], row[1]) if row else {"active": False, "balance": get_balance(uid)}


@app.post("/api/ttt/start")
async def ttt_start(req: Request):
    body, user = await body_user(req)
    uid = user["id"]
    try:
        bet = int(body.get("bet"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad_bet"}, status_code=400)
    if bet < 1 or bet > MAX_BET:
        return JSONResponse({"error": "bad_bet"}, status_code=400)
    with db_lock:
        if is_banned(uid):
            return BANNED_RESP()
        if db.execute("SELECT 1 FROM ttt WHERE user_id=?", (uid,)).fetchone():
            return JSONResponse({"error": "already_active"}, status_code=409)
        bal = get_balance(uid)
        if bet > bal:
            return JSONResponse({"error": "no_money", "balance": bal}, status_code=400)
        wager_play(uid, bet)
        add_balance(uid, -bet, "ttt bet")
        b = "-" * 9
        db.execute("INSERT OR REPLACE INTO ttt VALUES(?,?,?)", (uid, b, bet))
        db.commit()
        return ttt_view(uid, b, bet)


@app.post("/api/ttt/move")
async def ttt_move(req: Request):
    body, user = await body_user(req)
    uid = user["id"]
    try:
        cell = int(body.get("cell"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad_cell"}, status_code=400)
    if not 0 <= cell <= 8:
        return JSONResponse({"error": "bad_cell"}, status_code=400)
    with db_lock:
        row = db.execute("SELECT board, bet FROM ttt WHERE user_id=?", (uid,)).fetchone()
        if not row:
            return JSONResponse({"error": "no_game"}, status_code=409)
        b, bet = row
        if b[cell] != "-":
            return JSONResponse({"error": "bad_cell"}, status_code=400)
        b = b[:cell] + "X" + b[cell + 1:]          # ход игрока
        if ttt_winner(b) == "X":
            return ttt_settle(uid, b, bet, "win")
        if "-" not in b:
            return ttt_settle(uid, b, bet, "push")
        j = ttt_bot_move(b)                         # ход бота
        b = b[:j] + "O" + b[j + 1:]
        if ttt_winner(b) == "O":
            return ttt_settle(uid, b, bet, "lose")
        if "-" not in b:
            return ttt_settle(uid, b, bet, "push")
        db.execute("UPDATE ttt SET board=? WHERE user_id=?", (b, uid))
        db.commit()
        return ttt_view(uid, b, bet)


# ---------- Наперстки ----------
@app.post("/api/shell")
async def shell(req: Request):
    body, user = await body_user(req)
    uid = user["id"]
    try:
        bet, cup = int(body.get("bet")), int(body.get("cup"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad_bet"}, status_code=400)
    if bet < 1 or bet > MAX_BET or cup not in (0, 1, 2):
        return JSONResponse({"error": "bad_bet"}, status_code=400)
    with db_lock:
        if is_banned(uid):
            return BANNED_RESP()
        bal = get_balance(uid)
        if bet > bal:
            return JSONResponse({"error": "no_money", "balance": bal}, status_code=400)
        for _ in range(luck_tries(uid)):            # режим удачи: несколько попыток, берём угаданную
            ball = secrets.randbelow(3)
            if ball == cup:
                break
        won = ball == cup
        payout = bet * SHELL_MULT[0] // SHELL_MULT[1] if won else 0
        wager_play(uid, bet)
        bal = add_balance(uid, payout - bet, f"shell {'win' if won else 'lose'}")
    return {"ball": ball, "won": won, "payout": payout, "balance": bal}


# ---------- Слоты ----------
def _strip(counts: dict, seed: int) -> list:
    """Лента барабана: символы в фиксированном (перемешанном) порядке."""
    s = [k for k, n in counts.items() for _ in range(n)]
    random.Random(seed).shuffle(s)
    return s


# pay[символ] = {сколько_подряд_слева: выплата в «ставках на линию»}
# Общая ставка делится поровну между линиями: выплата = ставка * сумма_выплат / число_линий
SLOTS = {
    # 1) классика 3 барабана × 3 ряда, 5 линий (3 горизонтали + 2 диагонали)
    "fruit": {
        "name": "Фруктовый", "emoji": "🍒", "reels": 3, "rows": 3,
        "lines": [[0, 0, 0], [1, 1, 1], [2, 2, 2], [0, 1, 2], [2, 1, 0]],
        "strip": _strip({"🍒": 7, "🍋": 6, "🍉": 4, "🔔": 3, "⭐": 2, "7️⃣": 1}, 11),
        "wild": None,
        "pay": {"🍒": {3: 11}, "🍋": {3: 15}, "🍉": {3: 25}, "🔔": {3: 48}, "⭐": {3: 115}, "7️⃣": {3: 375}},
        "info": "3 барабана × 3 ряда, 5 линий: 3 горизонтали и 2 диагонали. Три одинаковых на линии — выигрыш.",
    },
    # 2) 5 барабанов × 3 ряда, 10 линий, дикий символ, выигрыш от 3 слева направо
    "pelmen": {
        "name": "Пельмень-Мания", "emoji": "🥟", "reels": 5, "rows": 3,
        "lines": [[1, 1, 1, 1, 1], [0, 0, 0, 0, 0], [2, 2, 2, 2, 2], [0, 1, 2, 1, 0], [2, 1, 0, 1, 2],
                  [0, 0, 1, 2, 2], [2, 2, 1, 0, 0], [1, 0, 0, 0, 1], [1, 2, 2, 2, 1], [0, 1, 1, 1, 0]],
        "strip": _strip({"🧅": 8, "🥔": 8, "🍅": 6, "🧄": 5, "🩴": 3, "🥟": 2}, 22),
        "wild": "🥟",
        "pay": {"🧅": {3: 4, 4: 10, 5: 30}, "🥔": {3: 4, 4: 10, 5: 30}, "🍅": {3: 6, 4: 15, 5: 50},
                "🧄": {3: 10, 4: 30, 5: 100}, "🩴": {3: 20, 4: 80, 5: 300}, "🥟": {3: 40, 4: 160, 5: 800}},
        "info": "5 барабанов × 3 ряда, 10 линий. Комбинация считается слева направо от 3 одинаковых. 🥟 — дикий, заменяет любой символ.",
    },
    # 3) старый добрый «одноручный бандит»: 3 барабана × 1 ряд, 1 линия
    "classic": {
        "name": "Классика 777", "emoji": "7️⃣", "reels": 3, "rows": 1,
        "lines": [[0, 0, 0]],
        "strip": _strip({"🍒": 7, "🍋": 6, "🔔": 4, "💎": 3, "7️⃣": 2}, 33),
        "wild": None,
        "pay": {"🍒": {2: 2, 3: 8}, "🍋": {3: 9}, "🔔": {3: 17}, "💎": {3: 44}, "7️⃣": {3: 195}},
        "info": "3 барабана, 1 линия. Две 🍒 слева — уже выигрыш, три 7️⃣ — джекпот x195!",
    },
}


def slot_line_pay(cfg: dict, syms: list):
    """Лучшая выплата по одной линии -> (выплата, сколько символов, символ) или None."""
    wild, pay = cfg.get("wild"), cfg["pay"]
    base = next((s for s in syms if s != wild), None)
    best = None
    for target in {t for t in (base, wild) if t}:
        n = 0
        for s in syms:
            if s == target or (wild and s == wild):
                n += 1
            else:
                break
        table = pay.get(target, {})
        ks = [k for k in table if k <= n]
        p = table[max(ks)] if ks else 0
        if p and (best is None or p > best[0]):
            best = (p, n, target)
    return best


def slot_spin(cfg: dict):
    """Один спин. Возвращает (сетка[ряд][барабан], список выигрышных линий, сумма выплат в «ставках на линию»)."""
    strip, L = cfg["strip"], len(cfg["strip"])
    stops = [secrets.randbelow(L) for _ in range(cfg["reels"])]
    grid = [[strip[(stops[c] + r) % L] for c in range(cfg["reels"])] for r in range(cfg["rows"])]
    wins, total = [], 0
    for li, line in enumerate(cfg["lines"]):
        res = slot_line_pay(cfg, [grid[line[c]][c] for c in range(cfg["reels"])])
        if res:
            p, n, sym = res
            wins.append({"line": li, "cells": [[line[c], c] for c in range(n)], "sym": sym, "pay": p})
            total += p
    sc = cfg.get("scatter")   # скаттер: символ платит в любом месте поля, выплата — множитель ОБЩЕЙ ставки
    if sc:
        cells = [[r, c] for r in range(cfg["rows"]) for c in range(cfg["reels"]) if grid[r][c] == sc["sym"]]
        ks = [k for k in sc["pay"] if k <= len(cells)]
        if ks:
            p = sc["pay"][max(ks)]
            wins.append({"line": -1, "cells": cells, "sym": sc["sym"], "pay": p})
            total += p * len(cfg["lines"])
    return grid, wins, total


# ==== DOGS SLOT BEGIN ====
# 4) «Собачья банда»: 5 барабанов × 3 ряда, 10 линий, вайлд-лепрекон.
#    Бонус: по собаке (любой из трёх) на 1-м, 3-м и 5-м барабанах -> 15 бонусных вращений.
#    В бонусе вайлды выпадают часто и несут иксы; иксы вайлдов на линии перемножаются.
_sysrand = random.SystemRandom()
DOG_SYMS = ("@pug", "@beag", "@rott")
BONUS_SPINS = 15
WILD_MULTS = [2, 3, 5, 10]
WILD_MULT_W = [60, 28, 10, 2]
LINE_MULT_CAP = 200      # потолок общего икса на одной линии
MAX_WIN_X = 3000         # потолок выигрыша за один запуск (включая бонус): ставка × 3000

_DOGS_A = {"🎾": 18, "🦴": 16, "🍖": 14, "@beag": 1, "@pug": 1, "@rott": 1, "@lep": 2}   # барабаны 1, 3, 5
_DOGS_B = {"🎾": 14, "🦴": 12, "🍖": 10, "@beag": 2, "@pug": 2, "@rott": 1, "@lep": 3}   # барабаны 2, 4
_DOGS_BONUS = {"🎾": 12, "🦴": 11, "🍖": 10, "@beag": 2, "@pug": 2, "@rott": 2, "@lep": 4}  # бонус: много вайлдов

SLOTS["dogs"] = {
    "name": "Собачья банда", "emoji": "🐶", "reels": 5, "rows": 3, "lines": SLOTS["pelmen"]["lines"],
    "strips": [_strip(_DOGS_A if c % 2 == 0 else _DOGS_B, 40 + c) for c in range(5)],
    "bstrips": [_strip(_DOGS_BONUS, 70 + c) for c in range(5)],
    "wild": "@lep",
    "pay": {"🎾": {3: 2, 4: 5, 5: 15}, "🦴": {3: 2, 4: 6, 5: 20}, "🍖": {3: 4, 4: 10, 5: 30},
            "@beag": {3: 6, 4: 20, 5: 60}, "@pug": {3: 10, 4: 30, 5: 120}, "@rott": {3: 15, 4: 60, 5: 300},
            "@lep": {3: 15, 4: 60, 5: 300}},
    "bonus": True,
    "info": ("5 барабанов × 3 ряда, 10 линий, выигрыш слева направо от 3 одинаковых. Лепрекон — дикий. "
             f"Собака на 1-м, 3-м и 5-м барабанах одновременно — бонус: {BONUS_SPINS} вращений, где лепреконы "
             "падают часто и несут иксы, а иксы на линии перемножаются!"),
}


def dogs_spin(cfg: dict, bonus: bool = False):
    """Одно вращение -> (сетка, сетка иксов, выигрышные линии, сумма выплат в «ставках на линию»)."""
    strips = cfg["bstrips"] if bonus else cfg["strips"]
    R, W, wild = cfg["rows"], cfg["reels"], cfg["wild"]
    grid = [[None] * W for _ in range(R)]
    mults = [[1] * W for _ in range(R)]
    for c in range(W):
        s = strips[c]
        stop = secrets.randbelow(len(s))
        for r in range(R):
            sym = s[(stop + r) % len(s)]
            grid[r][c] = sym
            if bonus and sym == wild:
                mults[r][c] = _sysrand.choices(WILD_MULTS, weights=WILD_MULT_W)[0]
    wins, total = [], 0
    for li, line in enumerate(cfg["lines"]):
        res = slot_line_pay(cfg, [grid[line[c]][c] for c in range(W)])
        if res:
            p, n, sym = res
            m = 1
            for c in range(n):
                if grid[line[c]][c] == wild:
                    m *= mults[line[c]][c]
            m = min(m, LINE_MULT_CAP)
            wins.append({"line": li, "cells": [[line[c], c] for c in range(n)], "sym": sym, "pay": p, "mult": m})
            total += p * m
    return grid, mults, wins, total


def dogs_trigger(grid) -> bool:
    return all(any(grid[r][c] in DOG_SYMS for r in range(len(grid))) for c in (0, 2, 4))


def dogs_pick(cfg: dict, tries: int, bonus: bool = False):
    """Режим удачи: несколько попыток, берём лучшую (старт бонуса ценится высоко)."""
    best = None
    for _ in range(tries):
        sp = dogs_spin(cfg, bonus)
        score = sp[3] + (150 if (not bonus and dogs_trigger(sp[0])) else 0)
        if best is None or score > best[0]:
            best = (score, sp)
    return best[1]
# ==== DOGS SLOT END ====


# ==== NEW SLOTS BEGIN ====
# 5) «Золото Фараона»: 5×3, 10 линий, дикое солнце, высокая волатильность
SLOTS["pharaoh"] = {
    "name": "Золото Фараона", "emoji": "🏺", "reels": 5, "rows": 3,
    "lines": SLOTS["pelmen"]["lines"],
    "strip": _strip({"🪲": 9, "🐍": 8, "🐪": 6, "🏺": 5, "👑": 3, "🔱": 2, "☀️": 2}, 55),
    "wild": "☀️",
    "pay": {"🪲": {3: 3, 4: 10, 5: 30}, "🐍": {3: 4, 4: 12, 5: 42}, "🐪": {3: 7, 4: 21, 5: 68},
            "🏺": {3: 11, 4: 35, 5: 135}, "👑": {3: 22, 4: 90, 5: 340}, "🔱": {3: 45, 4: 170, 5: 900},
            "☀️": {3: 45, 4: 170, 5: 900}},
    "info": "5 барабанов × 3 ряда, 10 линий, выигрыш слева направо от 3 одинаковых. ☀️ — дикий, заменяет любой символ. Три 🔱 или три ☀️ на линии — x45, пять — x900!",
}

# 6) «Космо-Рейс»: 4 барабана × 3 ряда, 8 линий, дикая звезда
SLOTS["space"] = {
    "name": "Космо-Рейс", "emoji": "🚀", "reels": 4, "rows": 3,
    "lines": [[1, 1, 1, 1], [0, 0, 0, 0], [2, 2, 2, 2], [0, 1, 2, 1], [2, 1, 0, 1],
              [1, 0, 0, 1], [1, 2, 2, 1], [0, 0, 1, 2]],
    "strip": _strip({"🌑": 8, "☄️": 7, "🛰️": 6, "👽": 4, "🛸": 3, "🚀": 2, "🌟": 2}, 66),
    "wild": "🌟",
    "pay": {"🌑": {3: 3, 4: 12}, "☄️": {3: 4, 4: 15}, "🛰️": {3: 6, 4: 25},
            "👽": {3: 12, 4: 50}, "🛸": {3: 25, 4: 120}, "🚀": {3: 60, 4: 300},
            "🌟": {3: 60, 4: 300}},
    "info": "4 барабана × 3 ряда, 8 линий, выигрыш слева направо от 3 одинаковых. 🌟 — дикая звезда, заменяет любой символ. Четыре 🚀 — x300!",
}

# 7) «Сладкая жизнь»: 3×3, 9 линий, частые небольшие выигрыши
SLOTS["candy"] = {
    "name": "Сладкая жизнь", "emoji": "🍭", "reels": 3, "rows": 3,
    "lines": [[0, 0, 0], [1, 1, 1], [2, 2, 2], [0, 1, 2], [2, 1, 0], [0, 1, 0], [2, 1, 2], [1, 0, 1], [1, 2, 1]],
    "strip": _strip({"🍬": 8, "🍩": 7, "🧁": 6, "🍰": 4, "🍭": 2, "🍫": 1}, 77),
    "wild": None,
    "pay": {"🍬": {2: 2, 3: 10}, "🍩": {3: 13}, "🧁": {3: 18}, "🍰": {3: 45}, "🍭": {3: 130}, "🍫": {3: 400}},
    "info": "3 барабана × 3 ряда, 9 линий. Две 🍬 слева — уже выигрыш, три 🍫 — джекпот x400!",
}

# 8) «Супер Хот»: 3×3, 27 линий (все возможные комбинации рядов), звезда-скаттер платит где угодно
SLOTS["superhot"] = {
    "name": "Супер Хот", "emoji": "🔥", "reels": 3, "rows": 3,
    "lines": [[a, b, c] for a in range(3) for b in range(3) for c in range(3)],
    "strip": _strip({"🍒": 7, "🍋": 7, "🍊": 6, "🍇": 5, "🍉": 4, "🔔": 3, "7️⃣": 2, "⭐": 2}, 88),
    "wild": None,
    "pay": {"🍒": {3: 16}, "🍋": {3: 21}, "🍊": {3: 32}, "🍇": {3: 48}, "🍉": {3: 84}, "🔔": {3: 210}, "7️⃣": {3: 850}},
    "scatter": {"sym": "⭐", "pay": {3: 3, 4: 8, 5: 25, 6: 100, 7: 300, 8: 700, 9: 1500}},
    "info": "3 барабана × 3 ряда, 27 линий — учитываются все комбинации рядов. ⭐ — скаттер: 3 и более звёзд в любом месте платят множитель общей ставки.",
}

# 9) «Монетный удар»: 3×3, 5 линий + Hold & Win
#    5+ монет 💰 на поле -> респины: монеты фиксируются, на каждую новую монету респины обновляются до 3.
#    У монеты номинал (x ставки) или джекпот MINI/MINOR/MAJOR. Заполнили все 9 клеток — GRAND.
COIN_SYM = "💰"
COIN_TRIGGER = 5
COIN_RESPINS = 3
COIN_LAND_P = 0.07                 # шанс монеты в пустой клетке на каждом респине
COIN_VALS = [1, 2, 3, 5, 8, 10, 15, 25]
COIN_VAL_W = [30, 24, 16, 12, 8, 5, 3, 2]
COIN_JACKS = [("MINI", 20, 18), ("MINOR", 50, 7), ("MAJOR", 200, 2)]   # (имя, x ставки, вес на 1000 монет)
COIN_GRAND = 500
_coin_rand = random.SystemRandom()

SLOTS["coinstrike"] = {
    "name": "Монетный удар", "emoji": "💰", "reels": 3, "rows": 3,
    "lines": [[0, 0, 0], [1, 1, 1], [2, 2, 2], [0, 1, 2], [2, 1, 0]],
    "strip": _strip({"🍒": 7, "🍋": 6, "🍊": 5, "🔔": 3, "⭐": 2, "7️⃣": 1, COIN_SYM: 8}, 99),
    "wild": None,
    "pay": {"🍒": {3: 7}, "🍋": {3: 11}, "🍊": {3: 17}, "🔔": {3: 36}, "⭐": {3: 85}, "7️⃣": {3: 240}},
    "hold": True,
    "info": (f"3 барабана × 3 ряда, 5 линий. {COIN_TRIGGER}+ монет 💰 на поле — бонус Hold & Win: монеты фиксируются, "
             f"у вас {COIN_RESPINS} респина (каждая новая монета их обновляет). Номиналы x1–x25, джекпоты MINI x20, "
             f"MINOR x50, MAJOR x200, а все 9 монет — GRAND x{COIN_GRAND}!"),
}


def coin_roll():
    """Одна монета -> (x ставки, название джекпота или '')."""
    r = _coin_rand.randrange(1000)
    for name, x, w in COIN_JACKS:
        if r < w:
            return x, name
        r -= w
    return _coin_rand.choices(COIN_VALS, weights=COIN_VAL_W)[0], ""


def coin_spin(cfg: dict):
    """Один запуск -> (сетка, выигрышные линии, сумма линий, hold или None). hold.total — в «ставках» (x общей ставки)."""
    grid, wins, total = slot_spin(cfg)
    cells = [(r, c) for r in range(3) for c in range(3) if grid[r][c] == COIN_SYM]
    if len(cells) < COIN_TRIGGER:
        return grid, wins, total, None
    locked = {}
    for r, c in cells:
        locked[(r, c)] = coin_roll()
    initial = [[r, c, v, j] for (r, c), (v, j) in locked.items()]
    steps, left = [], COIN_RESPINS
    while left > 0 and len(locked) < 9:
        new = []
        for r in range(3):
            for c in range(3):
                if (r, c) not in locked and _coin_rand.random() < COIN_LAND_P:
                    locked[(r, c)] = coin_roll()
                    new.append([r, c, *locked[(r, c)]])
        left = COIN_RESPINS if new else left - 1
        steps.append({"new": new, "left": left})
    total_x = sum(v for v, _ in locked.values())
    grand = len(locked) == 9
    if grand:
        total_x += COIN_GRAND
    return grid, wins, total, {"initial": initial, "steps": steps, "total": total_x, "grand": grand}
# ==== NEW SLOTS END ====


def slot_public(key: str) -> dict:
    cfg = SLOTS[key]
    syms = sorted(cfg["pay"], key=lambda s: -max(cfg["pay"][s].values()))
    out = {
        "name": cfg["name"], "emoji": cfg["emoji"], "reels": cfg["reels"], "rows": cfg["rows"],
        "lines": len(cfg["lines"]), "min_bet": len(cfg["lines"]), "wild": cfg["wild"],
        "symbols": syms, "pay": cfg["pay"], "info": cfg["info"], "bonus": bool(cfg.get("bonus")),
    }
    if cfg.get("bonus"):
        out["bonus_spins"] = BONUS_SPINS
        out["mults"] = WILD_MULTS
    if cfg.get("scatter"):
        out["scatter"] = cfg["scatter"]
        out["symbols"] = syms + [cfg["scatter"]["sym"]]
    if cfg.get("hold"):
        out["hold"] = True
        out["symbols"] = syms + [COIN_SYM]
        out["coin"] = COIN_SYM
        out["jackpots"] = [[n, x] for n, x, _ in COIN_JACKS] + [["GRAND", COIN_GRAND]]
    return out


@app.post("/api/slot/config")
async def slot_config(req: Request):
    await body_user(req)
    return {"slots": {k: slot_public(k) for k in SLOTS}}


@app.post("/api/slot/spin")
async def slot_spin_api(req: Request):
    body, user = await body_user(req)
    uid = user["id"]
    key = body.get("slot")
    cfg = SLOTS.get(key)
    if cfg is None:
        return JSONResponse({"error": "bad_bet"}, status_code=400)
    n_lines = len(cfg["lines"])
    pack_id = body.get("pack_id")
    free = pack_id is not None                       # бесплатное вращение: фриспин или фрибет
    bet = 0
    if free:
        try:
            pack_id = int(pack_id)
        except (TypeError, ValueError):
            return JSONResponse({"error": "no_pack"}, status_code=400)
    else:
        try:
            bet = int(body.get("bet"))
        except (TypeError, ValueError):
            return JSONResponse({"error": "bad_bet"}, status_code=400)
        if bet < n_lines or bet > MAX_BET:           # минимум 1 монета на линию
            return JSONResponse({"error": "bad_bet"}, status_code=400)
    with db_lock:  # атомарно: проверка баланса + списание/начисление
        if is_banned(uid):
            return BANNED_RESP()
        if free:
            pk = db.execute("SELECT bet, kind FROM freespins WHERE id=? AND user_id=? AND spins_left>0",
                            (pack_id, uid)).fetchone()
            if not pk:
                return JSONResponse({"error": "no_pack", "packs": packs_nolock(uid)}, status_code=409)
            bet = pk[0]
            if bet < n_lines:
                return JSONResponse({"error": "pack_bet_low", "need": n_lines}, status_code=400)
        else:
            bal = get_balance(uid)
            if bet > bal:
                return JSONResponse({"error": "no_money", "balance": bal}, status_code=400)
        tries = luck_tries(uid)
        if cfg.get("bonus"):
            grid, mults, wins, total = dogs_pick(cfg, tries)          # результат выбирает только сервер
            base = bet * total // n_lines
            bonus = None
            if dogs_trigger(grid):                                    # собаки на 1-м, 3-м и 5-м барабанах
                spins, bonus_pay = [], 0
                for _ in range(BONUS_SPINS):
                    g2, m2, w2, t2 = dogs_pick(cfg, tries, bonus=True)
                    win2 = bet * t2 // n_lines
                    bonus_pay += win2
                    spins.append({"grid": g2, "mults": m2, "wins": w2, "win": win2})
                bonus_pay = min(bonus_pay, bet * MAX_WIN_X)
                bonus = {"spins": spins, "total": bonus_pay}
            payout = min(base + (bonus["total"] if bonus else 0), bet * MAX_WIN_X)
            result = {"grid": grid, "mults": mults, "wins": wins, "base_payout": base, "bonus": bonus}
            tag = " +bonus" if bonus else ""
        elif cfg.get("hold"):                                         # Hold & Win («Монетный удар»)
            best = None
            for _ in range(tries):                                    # режим удачи: лучший из нескольких запусков
                sp = coin_spin(cfg)
                score = sp[2] / n_lines + (sp[3]["total"] if sp[3] else 0)
                if best is None or score > best[0]:
                    best = (score, sp)
            grid, wins, total, hold = best[1]
            base = bet * total // n_lines
            hold_pay = min(bet * hold["total"], bet * MAX_WIN_X) if hold else 0
            if hold:
                hold["pay"] = hold_pay
            payout = min(base + hold_pay, bet * MAX_WIN_X)
            result = {"grid": grid, "wins": wins, "base_payout": base, "hold": hold}
            tag = " +hold" if hold else ""
        else:
            best = None
            for _ in range(tries):                                    # режим удачи: берём лучший из нескольких бросков
                sp = slot_spin(cfg)
                if best is None or sp[2] > best[2]:
                    best = sp
            grid, wins, total = best
            payout = bet * total // n_lines
            result = {"grid": grid, "wins": wins}
            tag = ""
        if free:
            payout = min(payout, FREE_MAX_WIN)                        # потолок выигрыша с бесплатного вращения
            db.execute("UPDATE freespins SET spins_left=spins_left-1 WHERE id=?", (pack_id,))
            db.execute("DELETE FROM freespins WHERE spins_left<=0")
            delta, kind_tag = payout, "free "
        else:
            wager_play(uid, bet)
            delta, kind_tag = payout - bet, ""
        reason = f"{kind_tag}slot {key} {'win' if payout else 'lose'}{tag}"
        if delta:
            bal = add_balance(uid, delta, reason)
        else:
            db.commit()
            bal = get_balance(uid)
        result.update({"payout": payout, "bet": bet, "balance": bal, "free": free, "packs": packs_nolock(uid)})
    return result


# ---------- Пополнение счёта (Crypto Pay) ----------
# Бот ТОЛЬКО принимает деньги: создаёт счёт в @CryptoBot, ждёт оплату (вебхук + опрос) и зачисляет монеты.
# Вывод по-прежнему ручной (см. раздел «Касса: вывод средств»).
async def crypto(method: str, **params):
    if not CRYPTO_TOKEN:
        raise RuntimeError("CRYPTOBOT_TOKEN не задан")
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(f"{CRYPTO_API}/{method}", json=params, headers={"Crypto-Pay-API-Token": CRYPTO_TOKEN})
    data = r.json()
    if not data.get("ok"):
        raise RuntimeError(f"Crypto Pay {method}: {data.get('error')}")
    return data["result"]


def _ref_bonus_nolock(uid: int, coins: int):
    """Первый депозит друга от REF_MIN_USDT -> бонус пригласившему. Под db_lock. -> (id, имя друга, баланс) или None."""
    if coins < REF_MIN_COINS:
        return None
    ref = db.execute("SELECT referrer_id FROM referrals WHERE user_id=? AND rewarded=0", (uid,)).fetchone()
    if not ref:
        return None
    db.execute("UPDATE referrals SET rewarded=1, reward_ts=? WHERE user_id=?", (time.time(), uid))
    ref_bal = add_balance(ref[0], REF_BONUS_COINS, f"referral bonus (friend {uid})")
    fr = db.execute("SELECT first_name FROM users WHERE user_id=?", (uid,)).fetchone()
    return ref[0], (fr[0] if fr and fr[0] else "ваш друг"), ref_bal


async def settle_invoice(inv: dict) -> str:
    """Обрабатывает счёт Crypto Pay (из вебхука или опроса). Идемпотентно: монеты зачисляются один раз.
    Возвращает 'done' | 'expired' | 'skip'."""
    inv_id, st = inv.get("invoice_id"), inv.get("status")
    if inv_id is None or st not in ("paid", "expired"):
        return "skip"
    with db_lock:
        row = db.execute("SELECT id, user_id, amount, status FROM deposits WHERE invoice_id=?", (inv_id,)).fetchone()
        if not row:
            return "skip"
        dep_id, uid, coins, cur = row
        if st == "expired":
            if cur in ("new", "pending"):
                db.execute("UPDATE deposits SET status='expired' WHERE id=?", (dep_id,))
                db.commit()
                return "expired"
            return "skip"
        if cur == "done":
            return "skip"
        try:   # сверяем, что счёт именно на ту сумму: в USD (любая валюта оплаты) или в USDT (старый формат)
            same = Decimal(str(inv.get("amount"))) == Decimal(usdt_str(coins))
            if inv.get("currency_type") == "fiat":
                ok_sum = same and inv.get("fiat") == "USD"
            else:
                ok_sum = same and inv.get("asset") == "USDT"
        except Exception:
            ok_sum = False
        if not ok_sum:
            return "skip"
        db.execute("UPDATE deposits SET status='done' WHERE id=?", (dep_id,))
        new_bal = add_balance(uid, coins, f"deposit #{dep_id}")
        ref_note = _ref_bonus_nolock(uid, coins)
        db.commit()
        who = db.execute("SELECT first_name, username FROM users WHERE user_id=?", (uid,)).fetchone() or ("", "")
    paid_with = f"{inv.get('paid_amount')} {inv.get('paid_asset')}" if inv.get("paid_asset") else "USDT"
    await tg("sendMessage", chat_id=uid, parse_mode="HTML",
             text=f"✅ <b>Оплата получена!</b>\n\nНа счёт зачислено: <b>+{coins} {CURRENCY}</b> (${usdt_str(coins)})\n"
                  f"Ваш баланс: <b>{new_bal} {CURRENCY}</b>\n\nУдачной игры! 🍀",
             reply_markup={"inline_keyboard": [[{"text": "🎰 Играть", "web_app": {"url": BASE_URL}}]]})
    await tg("sendMessage", chat_id=OWNER_ID,
             text=f"💰 Пополнение #{dep_id}: ${usdt_str(coins)} = {coins} {CURRENCY}\nОплачено: {paid_with}\n"
                  f"От: {who[0] or '—'} @{who[1] or '—'} (id {uid})")
    if ref_note:
        await tg("sendMessage", chat_id=ref_note[0],
                 text=f"🎉 {ref_note[1]} пополнил счёт — вам начислен бонус {REF_BONUS_USDT} USDT "
                      f"(+{REF_BONUS_COINS} {CURRENCY})!\nБаланс: {ref_note[2]} {CURRENCY}")
    return "done"


async def poll_invoices(ids: list):
    """Спрашиваем у Crypto Pay статус счетов и обрабатываем оплаченные/просроченные."""
    if not ids or not CRYPTO_TOKEN:
        return
    res = await crypto("getInvoices", invoice_ids=",".join(str(i) for i in ids), count=100)
    for inv in (res.get("items") if isinstance(res, dict) else res) or []:
        await settle_invoice(inv)


async def deposit_poller():
    """Подстраховка на случай, если вебхук не настроен или потерялся: раз в ~10 секунд проверяем ожидающие счета."""
    while True:
        try:
            await asyncio.sleep(10)
            with db_lock:
                ids = [r[0] for r in db.execute(
                    "SELECT invoice_id FROM deposits WHERE status='pending' AND invoice_id IS NOT NULL AND ts>? "
                    "ORDER BY ts DESC LIMIT 100", (time.time() - 2 * 24 * 3600,)).fetchall()]
            await poll_invoices(ids)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print("deposit_poller:", e)


@app.post("/api/deposit/create")
async def deposit_create(req: Request):
    body, user = await body_user(req)
    uid = user["id"]
    try:
        amount = int(body.get("amount"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Введите количество монет — целое число"}, status_code=400)
    if amount < MIN_DEPOSIT or amount > MAX_DEPOSIT:
        return JSONResponse({"error": f"От {MIN_DEPOSIT} до {MAX_DEPOSIT} монет"}, status_code=400)
    if not CRYPTO_TOKEN:
        return JSONResponse({"error": "Оплата временно недоступна, попробуйте позже"}, status_code=503)
    with db_lock:
        if is_banned(uid):
            return BANNED_RESP()
        recent = db.execute(
            "SELECT COUNT(*) FROM deposits WHERE user_id=? AND ts>?", (uid, time.time() - 3600)
        ).fetchone()[0]
        if recent >= 10:
            return JSONResponse({"error": "Слишком много заявок, попробуйте позже"}, status_code=429)
        cur = db.execute(
            "INSERT INTO deposits(user_id, ts, amount, status) VALUES(?,?,?,'new')", (uid, time.time(), amount)
        )
        db.commit()
        dep_id = cur.lastrowid
    usdt = usdt_str(amount)
    params = dict(currency_type="fiat", fiat="USD", accepted_assets=CRYPTO_ASSETS, amount=usdt,
                  description=f"Пополнение: {amount} монет", payload=f"dep:{dep_id}:{uid}", expires_in=INVOICE_TTL,
                  allow_comments=False, allow_anonymous=False)
    bot = await ensure_bot_username()
    if bot:   # после оплаты в Crypto Bot появится кнопка «Вернуться в бота»
        params.update(paid_btn_name="openBot", paid_btn_url=f"https://t.me/{bot}")
    try:
        try:
            inv = await crypto("createInvoice", **params)
        except Exception as e:      # на случай, если какая-то из валют недоступна приложению — выставляем счёт в USDT
            print("createInvoice (multi-asset) failed, fallback to USDT:", e)
            for k in ("currency_type", "fiat", "accepted_assets"):
                params.pop(k)
            inv = await crypto("createInvoice", asset="USDT", **params)
    except Exception as e:
        print("createInvoice:", e)
        with db_lock:
            db.execute("UPDATE deposits SET status='failed' WHERE id=?", (dep_id,))
            db.commit()
        return JSONResponse({"error": "Не удалось создать счёт, попробуйте через минуту"}, status_code=502)
    url = inv.get("mini_app_invoice_url") or inv.get("bot_invoice_url") or inv.get("pay_url") or ""
    with db_lock:
        db.execute("UPDATE deposits SET status='pending', invoice_id=?, pay_url=? WHERE id=?",
                   (inv["invoice_id"], url, dep_id))
        db.commit()
    return {"id": dep_id, "amount": amount, "usdt": usdt, "currency": "USDT", "pay_url": url, "ttl": INVOICE_TTL}


@app.post("/api/deposit/status")
async def deposit_status(req: Request):
    """Приложение спрашивает, оплачен ли счёт. Заодно сами проверяем его в Crypto Pay."""
    body, user = await body_user(req)
    uid = user["id"]
    try:
        dep_id = int(body.get("id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Ошибка"}, status_code=400)
    with db_lock:
        row = db.execute("SELECT status, invoice_id FROM deposits WHERE id=? AND user_id=?", (dep_id, uid)).fetchone()
    if not row:
        return JSONResponse({"error": "Счёт не найден"}, status_code=404)
    if row[0] == "pending" and row[1]:
        try:
            await poll_invoices([row[1]])
        except Exception as e:
            print("deposit_status:", e)
    with db_lock:
        st = db.execute("SELECT status FROM deposits WHERE id=?", (dep_id,)).fetchone()[0]
        bal = get_balance(uid)
    return {"status": st, "balance": bal}


@app.post("/cryptopay/webhook")
async def cryptopay_webhook(req: Request):
    """Вебхук Crypto Pay (необязателен, т.к. есть опрос). Адрес: BASE_URL/cryptopay/webhook"""
    raw = await req.body()
    sig = req.headers.get("crypto-pay-api-signature", "")
    key = hashlib.sha256(CRYPTO_TOKEN.encode()).digest()
    mine = hmac.new(key, raw, hashlib.sha256).hexdigest()
    if not CRYPTO_TOKEN or not hmac.compare_digest(mine, sig):
        raise HTTPException(403)
    try:
        upd = json.loads(raw)
    except ValueError:
        raise HTTPException(400)
    if upd.get("update_type") == "invoice_paid" and isinstance(upd.get("payload"), dict):
        await settle_invoice(upd["payload"])
    return {"ok": True}


@app.post("/api/admin/deposits")
async def admin_deposits(req: Request):
    await body_admin(req)
    with db_lock:
        rows = db.execute(
            "SELECT d.id, d.user_id, u.first_name, u.username, d.amount, d.ts FROM deposits d "
            "LEFT JOIN users u ON u.user_id=d.user_id WHERE d.status='claimed' ORDER BY d.ts DESC LIMIT 30"
        ).fetchall()
    return {"items": [{"id": a, "user_id": b, "name": c or "", "username": d or "", "amount": e, "usdt": usdt_str(e), "ts": f}
                      for a, b, c, d, e, f in rows]}


@app.post("/api/admin/deposit_ok")
async def admin_deposit_ok(req: Request):
    body, _ = await body_admin(req)
    try:
        dep_id = int(body.get("id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Ошибка"}, status_code=400)
    approve = bool(body.get("ok"))
    with db_lock:
        row = db.execute("SELECT user_id, amount FROM deposits WHERE id=? AND status='claimed'", (dep_id,)).fetchone()
        if not row:
            return JSONResponse({"error": "Заявка не найдена или уже обработана"}, status_code=404)
        db.execute("UPDATE deposits SET status=? WHERE id=?", ("done" if approve else "rejected", dep_id))
        new_bal = add_balance(row[0], row[1], f"deposit #{dep_id}") if approve else None
        ref_note = _ref_bonus_nolock(row[0], row[1]) if approve else None
        db.commit()
    if approve:
        await tg("sendMessage", chat_id=row[0],
                 text=f"✅ Пополнение на {usdt_str(row[1])} USDT зачислено: +{row[1]} {CURRENCY}\nБаланс: {new_bal} {CURRENCY}")
    else:
        await tg("sendMessage", chat_id=row[0],
                 text=f"❌ Заявка на пополнение #{dep_id} отклонена. Если вы оплатили — напишите нам прямо в этот чат.")
    if ref_note:
        await tg("sendMessage", chat_id=ref_note[0],
                 text=f"🎉 {ref_note[1]} пополнил счёт — вам начислен бонус {REF_BONUS_USDT} USDT "
                      f"(+{REF_BONUS_COINS} {CURRENCY})!\nБаланс: {ref_note[2]} {CURRENCY}")
    return {"ok": True, "approved": approve}




# ---------- Управление админами (только владелец) ----------
async def body_owner(req: Request):
    body, user = await body_user(req)
    if user["id"] != OWNER_ID:
        raise HTTPException(403, "owner only")
    return body, user


# ---------- Регулятор удачи (только владелец) ----------
def _luck_rows():
    rows = db.execute(
        "SELECT l.scope, l.level, l.until, u.first_name, u.username FROM luck l "
        "LEFT JOIN users u ON ('u:' || u.user_id)=l.scope WHERE l.until IS NULL OR l.until>? ORDER BY l.scope",
        (time.time(),),
    ).fetchall()
    return [{"scope": a, "level": b, "level_name": LUCK_NAMES[b], "until": c,
             "name": (d or "") + (" @" + e if e else "")} for a, b, c, d, e in rows]


@app.post("/api/admin/luck")
async def luck_state(req: Request):
    await body_owner(req)
    with db_lock:
        db.execute("DELETE FROM luck WHERE until IS NOT NULL AND until<=?", (time.time(),))
        db.commit()
        return {"levels": LUCK_NAMES, "items": _luck_rows()}


@app.post("/api/admin/luck/set")
async def luck_set(req: Request):
    body, _ = await body_owner(req)
    try:
        level = int(body.get("level"))
        minutes = int(body.get("minutes") or 0)
    except (TypeError, ValueError):
        return JSONResponse({"error": "Некорректные значения"}, status_code=400)
    if not 0 <= level < len(LUCK_NAMES) or not 0 <= minutes <= 60 * 24 * 30:
        return JSONResponse({"error": "Уровень 0–2, время до 30 суток (0 = пока не отключите)"}, status_code=400)
    target = str(body.get("target", "")).strip()
    with db_lock:
        if target:
            row = resolve_target(target)
            if not row:
                return JSONResponse({"error": "Пользователь не найден"}, status_code=404)
            scope, name = f"u:{row[0]}", row[1] or str(row[0])
        else:
            scope, name = "global", "все игроки"
        if level == 0:
            db.execute("DELETE FROM luck WHERE scope=?", (scope,))
        else:
            until = time.time() + minutes * 60 if minutes else None
            db.execute("INSERT OR REPLACE INTO luck VALUES(?,?,?)", (scope, level, until))
        db.commit()
        return {"ok": True, "name": name, "level": level, "level_name": LUCK_NAMES[level], "items": _luck_rows()}


@app.post("/api/admin/admins")
async def admins_list(req: Request):
    await body_owner(req)
    with db_lock:
        rows = db.execute("SELECT a.user_id, u.first_name, u.username FROM admins a "
                          "LEFT JOIN users u ON u.user_id=a.user_id ORDER BY a.rowid").fetchall()
    return {"items": [{"id": a, "name": b or "", "username": c or ""} for a, b, c in rows]}


@app.post("/api/admin/admins/add")
async def admins_add(req: Request):
    body, _ = await body_owner(req)
    with db_lock:
        row = resolve_target(body.get("target", ""))
        if not row:
            return JSONResponse({"error": "Пользователь не найден (он должен хотя бы раз открыть бота)"}, status_code=404)
        if row[0] == OWNER_ID:
            return JSONResponse({"error": "Это вы 🙂 — вы и так главный админ"}, status_code=400)
        db.execute("INSERT OR IGNORE INTO admins VALUES(?)", (row[0],))
        db.commit()
    await tg("sendMessage", chat_id=row[0],
             text="🛠 Вам выдан доступ к админ-панели. Закройте мини-приложение и откройте заново — появится вкладка «Админ».")
    return {"ok": True, "name": row[1] or str(row[0])}


@app.post("/api/admin/admins/remove")
async def admins_remove(req: Request):
    body, _ = await body_owner(req)
    with db_lock:
        row = resolve_target(body.get("target", ""))
        if not row:
            return JSONResponse({"error": "Пользователь не найден"}, status_code=404)
        db.execute("DELETE FROM admins WHERE user_id=?", (row[0],))
        db.commit()
    await tg("sendMessage", chat_id=row[0], text="Доступ к админ-панели отозван.")
    return {"ok": True, "name": row[1] or str(row[0])}


# ---------- Копия базы (команда /backup, только владелец) ----------
async def send_backup(chat_id: int):
    tmp = os.path.join(tempfile.gettempdir(), "bot-backup.db")
    with db_lock:
        dst = sqlite3.connect(tmp)
        db.backup(dst)
        dst.close()
    with open(tmp, "rb") as f:
        data = f.read()
    async with httpx.AsyncClient(timeout=60) as c:
        await c.post(f"{API}/sendDocument", data={"chat_id": chat_id, "caption": "💾 Копия базы (балансы, заявки, история)"},
                     files={"document": ("bot-backup.db", data)})
    os.remove(tmp)


# ---------- Касса: вывод средств ----------
WD_STATUS = {"pending": "⏳ Ожидает решения", "approved": "✅ Одобрено", "rejected": "❌ Отклонено", "frozen": "🧊 Заморожено"}


def wd_card_nolock(wd_id: int):
    """Текст карточки заявки для админа. Вызывать под db_lock."""
    r = db.execute(
        "SELECT w.user_id, w.coins, w.usdt, w.wallet, w.status, w.reason, u.first_name, u.username "
        "FROM withdrawals w LEFT JOIN users u ON u.user_id=w.user_id WHERE w.id=?", (wd_id,)
    ).fetchone()
    if not r:
        return None
    uid, coins, usdt, wallet, status, reason, fn, un = r
    who = f"{html.escape(fn or '—')} @{html.escape(un or '—')} (id {uid})"
    text = (f"💸 <b>Запрос на вывод #{wd_id}</b>\nИгрок: {who}\n"
            f"Сумма: {coins} {CURRENCY} = <b>{usdt} USDT</b> ({DEPOSIT_NETWORK})\n"
            f"Кошелёк: <code>{html.escape(wallet)}</code>\n\nСтатус: {WD_STATUS.get(status, status)}")
    if reason:
        text += f"\nПричина: {html.escape(reason)}"
    return text, status


def wd_kb(status: str, wd_id: int) -> dict:
    ok = {"text": "✅ Одобрить", "callback_data": f"wd:ok:{wd_id}"}
    no = {"text": "❌ Отклонить", "callback_data": f"wd:no:{wd_id}"}
    fr = {"text": "🧊 Заморозить", "callback_data": f"wd:fr:{wd_id}"}
    if status == "pending":
        return {"inline_keyboard": [[ok, no, fr]]}
    if status == "frozen":
        return {"inline_keyboard": [[ok, no]]}
    return {"inline_keyboard": []}


async def wd_resolve(wd_id: int, action: str, reason: str = "", chat_id=None, msg_id=None):
    """action: approve | reject | freeze. Возвращает (ok, текст для админа)."""
    with db_lock:
        row = db.execute("SELECT user_id, coins, usdt, status FROM withdrawals WHERE id=?", (wd_id,)).fetchone()
        if not row:
            return False, "Заявка не найдена"
        uid, coins, usdt, status = row
        allowed = {"approve": ("pending", "frozen"), "reject": ("pending", "frozen"), "freeze": ("pending",)}[action]
        if status not in allowed:
            return False, f"Заявка #{wd_id} уже обработана: {WD_STATUS.get(status, status)}"
        new = {"approve": "approved", "reject": "rejected", "freeze": "frozen"}[action]
        db.execute("UPDATE withdrawals SET status=?, reason=? WHERE id=?", (new, reason or None, wd_id))
        if action == "reject":  # монеты были заморожены при создании заявки — возвращаем
            add_balance(uid, coins, f"withdraw refund #{wd_id}")
        db.commit()
        card = wd_card_nolock(wd_id)
    if action == "approve":
        note = f"✅ Вывод #{wd_id} одобрен: {usdt} USDT ({coins} {CURRENCY}) отправляются на ваш кошелёк."
    elif action == "reject":
        note = (f"❌ Вывод #{wd_id} отклонён.\nПричина: {reason}\n"
                f"Монеты ({coins} {CURRENCY}) возвращены на ваш баланс.")
    else:
        note = f"🧊 Вывод #{wd_id} заморожен. Напишите нам прямо в этот чат, чтобы уточнить детали."
    await tg("sendMessage", chat_id=uid, text=note)
    if chat_id and msg_id and card:
        await tg("editMessageText", chat_id=chat_id, message_id=msg_id, text=card[0], parse_mode="HTML",
                 reply_markup=wd_kb(card[1], wd_id))
    return True, f"Заявка #{wd_id}: {WD_STATUS[new]}"


@app.post("/api/withdraw/create")
async def withdraw_create(req: Request):
    body, user = await body_user(req)
    uid = user["id"]
    try:
        coins = int(body.get("coins"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Введите количество монет — целое число"}, status_code=400)
    wallet = str(body.get("wallet", "")).strip()
    if coins < MIN_WITHDRAW or coins > MAX_BET:
        return JSONResponse({"error": f"Минимум для вывода — {MIN_WITHDRAW} монет"}, status_code=400)
    if not WALLET_RE.match(wallet):
        return JSONResponse({"error": "Неверный адрес TRC20 (начинается с T, 34 символа)"}, status_code=400)
    with db_lock:
        open_n = db.execute("SELECT COUNT(*) FROM withdrawals WHERE user_id=? AND status IN ('pending','frozen')",
                            (uid,)).fetchone()[0]
        if open_n >= 3:
            return JSONResponse({"error": "У вас уже есть необработанные заявки на вывод"}, status_code=429)
        bal = get_balance(uid)
        if coins > bal:
            return JSONResponse({"error": "Недостаточно монет", "balance": bal}, status_code=400)
        wl = wager_left(uid)
        if wl > 0:
            return JSONResponse({"error": f"Сначала отыграйте бонус: осталось поставить {wl} {CURRENCY} "
                                          f"(вейджер x{WAGER_MULT})", "wager_left": wl}, status_code=400)
        usdt = usdt_str(coins)
        cur = db.execute("INSERT INTO withdrawals(user_id, ts, coins, usdt, wallet, status) VALUES(?,?,?,?,?,'pending')",
                         (uid, time.time(), coins, usdt, wallet))
        wd_id = cur.lastrowid
        new_bal = add_balance(uid, -coins, f"withdraw #{wd_id}")  # монеты замораживаются до решения админа
        card = wd_card_nolock(wd_id)
    await tg("sendMessage", chat_id=OWNER_ID, text=card[0], parse_mode="HTML", reply_markup=wd_kb("pending", wd_id))
    return {"ok": True, "id": wd_id, "coins": coins, "usdt": usdt, "balance": new_bal}


def _wager_bonus(uid: int) -> int:
    """Сколько бонусных монет спишется при отказе (не больше текущего баланса). Вызывать под db_lock."""
    row = db.execute("SELECT bonus FROM wager WHERE user_id=? AND left>0", (uid,)).fetchone()
    return min(row[0], get_balance(uid)) if row else 0


@app.post("/api/wager")
async def wager_info(req: Request):
    _, user = await body_user(req)
    with db_lock:
        return {"left": wager_left(user["id"]), "mult": WAGER_MULT, "bonus": _wager_bonus(user["id"])}


@app.post("/api/wager/cancel")
async def wager_cancel(req: Request):
    """Отказ от бонуса: бонусные монеты списываются, вейджер обнуляется — после этого можно выводить остальное."""
    _, user = await body_user(req)
    uid = user["id"]
    with db_lock:
        if wager_left(uid) <= 0:
            return JSONResponse({"error": "Активного бонуса нет"}, status_code=400)
        removed = _wager_bonus(uid)
        if removed:
            add_balance(uid, -removed, "bonus forfeited")
        db.execute("UPDATE wager SET left=0, bonus=0 WHERE user_id=?", (uid,))
        db.commit()
        bal = get_balance(uid)
    return {"ok": True, "removed": removed, "balance": bal}


@app.post("/api/cashier/history")
async def cashier_history(req: Request):
    _, user = await body_user(req)
    uid = user["id"]
    with db_lock:
        deps = db.execute("SELECT amount, status, ts FROM deposits WHERE user_id=? AND status IN ('claimed','done','rejected','pending','expired') "
                          "ORDER BY ts DESC LIMIT 30", (uid,)).fetchall()
        wds = db.execute("SELECT coins, usdt, status, reason, ts FROM withdrawals WHERE user_id=? ORDER BY ts DESC LIMIT 30",
                         (uid,)).fetchall()
    items = [{"kind": "deposit", "coins": a, "usdt": usdt_str(a), "status": b, "reason": "", "ts": c} for a, b, c in deps]
    items += [{"kind": "withdraw", "coins": a, "usdt": b, "status": c, "reason": d or "", "ts": e} for a, b, c, d, e in wds]
    items.sort(key=lambda x: x["ts"], reverse=True)
    return {"items": items[:40]}


async def handle_callback(cb: dict):
    cid, uid = cb["id"], cb["from"]["id"]
    if uid != OWNER_ID:
        await tg("answerCallbackQuery", callback_query_id=cid)
        return
    parts = (cb.get("data") or "").split(":")
    try:
        assert len(parts) == 3 and parts[0] == "wd"
        act, wd_id = parts[1], int(parts[2])
    except (AssertionError, ValueError):
        await tg("answerCallbackQuery", callback_query_id=cid)
        return
    m = cb.get("message") or {}
    chat_id, msg_id = (m.get("chat") or {}).get("id"), m.get("message_id")
    if act == "no":  # причину вводим отдельным сообщением
        with db_lock:
            row = db.execute("SELECT status FROM withdrawals WHERE id=?", (wd_id,)).fetchone()
            if not row or row[0] not in ("pending", "frozen"):
                busy = True
            else:
                busy = False
                db.execute("INSERT OR REPLACE INTO admin_wait VALUES(?,?,?)", (OWNER_ID, wd_id, msg_id))
                db.commit()
        if busy:
            await tg("answerCallbackQuery", callback_query_id=cid, text="Заявка уже обработана")
            return
        await tg("answerCallbackQuery", callback_query_id=cid)
        await tg("sendMessage", chat_id=chat_id,
                 text=f"✍️ Напишите причину отклонения заявки #{wd_id} — она придёт игроку.\n"
                      f"(Любая команда, например /start, отменит отклонение.)",
                 reply_markup={"force_reply": True, "input_field_placeholder": "Причина отклонения"})
        return
    action = {"ok": "approve", "fr": "freeze"}.get(act)
    if not action:
        await tg("answerCallbackQuery", callback_query_id=cid)
        return
    ok, info = await wd_resolve(wd_id, action, "", chat_id, msg_id)
    await tg("answerCallbackQuery", callback_query_id=cid, text=info[:150])


# ---------- Всплывающие сообщения от админа ----------
@app.post("/api/popups")
async def popups_get(req: Request):
    _, user = await body_user(req)
    uid = user["id"]
    with db_lock:
        rows = db.execute(
            "SELECT id, icon, title, text FROM popups WHERE (user_id IS NULL OR user_id=?) AND ts>? "
            "AND id NOT IN (SELECT popup_id FROM popup_seen WHERE user_id=?) ORDER BY id LIMIT 5",
            (uid, time.time() - POPUP_TTL, uid),
        ).fetchall()
    return {"items": [{"id": a, "icon": b, "title": c, "text": d} for a, b, c, d in rows]}


@app.post("/api/popups/seen")
async def popups_seen(req: Request):
    body, user = await body_user(req)
    ids = [i for i in (body.get("ids") or []) if isinstance(i, int)][:20]
    with db_lock:
        for i in ids:
            db.execute("INSERT OR IGNORE INTO popup_seen VALUES(?,?)", (user["id"], i))
        db.commit()
    return {"ok": True}


@app.post("/api/admin/popup")
async def admin_popup(req: Request):
    body, _ = await body_admin(req)
    text = str(body.get("text", "")).strip()
    title = str(body.get("title", "")).strip()
    icon = str(body.get("icon", "")).strip() or "📢"
    target = str(body.get("target", "")).strip()
    if not text or len(text) > 600 or len(title) > 80 or len(icon) > 8:
        return JSONResponse({"error": "Текст обязателен (до 600 символов), заголовок до 80"}, status_code=400)
    with db_lock:
        uid = None
        name = "всем"
        if target:
            row = resolve_target(target)
            if not row:
                return JSONResponse({"error": "Пользователь не найден"}, status_code=404)
            uid, name = row[0], row[1] or str(row[0])
        db.execute("INSERT INTO popups(user_id, icon, title, text, ts) VALUES(?,?,?,?,?)",
                   (uid, icon, title, text, time.time()))
        db.commit()
    return {"ok": True, "name": name}


# ---------- Промокоды ----------
_promo_fails: dict = {}


def _promo_blocked(uid: int) -> bool:
    now = time.time()
    lst = [t for t in _promo_fails.get(uid, []) if now - t < 600]
    _promo_fails[uid] = lst
    return len(lst) >= 6          # защита от подбора кодов: 6 неудач за 10 минут


def _promo_fail(uid: int, msg: str, status: int = 404):
    _promo_fails.setdefault(uid, []).append(time.time())
    return JSONResponse({"error": msg}, status_code=status)


@app.post("/api/promo/redeem")
async def promo_redeem(req: Request):
    body, user = await body_user(req)
    uid = user["id"]
    code = str(body.get("code", "")).strip().upper()
    if _promo_blocked(uid):
        return JSONResponse({"error": "Слишком много неверных попыток. Попробуйте через 10 минут"}, status_code=429)
    if not PROMO_RE.match(code):
        return _promo_fail(uid, "Неверный промокод", 400)
    with db_lock:
        if is_banned(uid):
            return BANNED_RESP()
        row = db.execute("SELECT coins, spins, bet, max_uses, used, active FROM promos WHERE code=?", (code,)).fetchone()
        if not row or not row[5]:
            return _promo_fail(uid, "Промокод не найден или отключён")
        coins, spins, bet, max_uses, used, _ = row
        if used >= max_uses:
            return JSONResponse({"error": "Промокод уже активировали максимальное число раз"}, status_code=410)
        if db.execute("SELECT 1 FROM promo_uses WHERE code=? AND user_id=?", (code, uid)).fetchone():
            return JSONResponse({"error": "Вы уже активировали этот промокод"}, status_code=409)
        db.execute("INSERT INTO promo_uses VALUES(?,?,?)", (code, uid, time.time()))
        db.execute("UPDATE promos SET used=used+1 WHERE code=?", (code,))
        if spins:
            grant_pack(uid, "freespins", spins, bet, f"promo {code}")
        bal = add_balance(uid, coins, f"promo {code}") if coins else get_balance(uid)
        db.commit()
        packs = packs_nolock(uid)
    return {"ok": True, "coins": coins, "spins": spins, "bet": bet, "balance": bal, "packs": packs}


def _promo_rows():
    rows = db.execute("SELECT code, coins, spins, bet, max_uses, used, active, ts FROM promos ORDER BY ts DESC LIMIT 50").fetchall()
    return [{"code": a, "coins": b, "spins": c, "bet": d, "max_uses": e, "used": f, "active": bool(g), "ts": h}
            for a, b, c, d, e, f, g, h in rows]


@app.post("/api/admin/promo/list")
async def admin_promo_list(req: Request):
    await body_admin(req)
    with db_lock:
        return {"items": _promo_rows()}


@app.post("/api/admin/promo/create")
async def admin_promo_create(req: Request):
    body, user = await body_admin(req)

    def num(k):
        try:
            return int(body.get(k) or 0)
        except (TypeError, ValueError):
            return None
    coins, spins, bet, uses = num("coins"), num("spins"), num("bet"), num("max_uses")
    err = lambda m: JSONResponse({"error": m}, status_code=400)
    if None in (coins, spins, bet, uses):
        return err("Введите числа")
    if not 0 <= coins <= MAX_BET:
        return err("Монеты: от 0 до 1 000 000 000")
    if not 0 <= spins <= 100_000:
        return err("Фриспины: от 0 до 100 000")
    if coins == 0 and spins == 0:
        return err("Укажите монеты и/или фриспины")
    if spins > 0 and not PROMO_MIN_BET <= bet <= 1_000_000:
        return err(f"Ставка за спин: от {PROMO_MIN_BET} до 1 000 000 🪙")
    if spins == 0:
        bet = 0
    if not 1 <= uses <= 1_000_000:
        return err("Количество активаций: от 1 до 1 000 000")
    code = str(body.get("code", "")).strip().upper()
    if code and not PROMO_RE.match(code):
        return err("Код: 3–20 символов, латиница, цифры, - и _")
    with db_lock:
        if code:
            if db.execute("SELECT 1 FROM promos WHERE code=?", (code,)).fetchone():
                return JSONResponse({"error": "Такой код уже существует"}, status_code=409)
        else:
            alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
            while True:
                code = "".join(secrets.choice(alphabet) for _ in range(8))
                if not db.execute("SELECT 1 FROM promos WHERE code=?", (code,)).fetchone():
                    break
        db.execute("INSERT INTO promos(code, coins, spins, bet, max_uses, used, active, created_by, ts) VALUES(?,?,?,?,?,0,1,?,?)",
                   (code, coins, spins, bet, uses, user["id"], time.time()))
        db.commit()
        return {"ok": True, "code": code, "items": _promo_rows()}


@app.post("/api/admin/promo/toggle")
async def admin_promo_toggle(req: Request):
    body, _ = await body_admin(req)
    code = str(body.get("code", "")).strip().upper()
    with db_lock:
        cur = db.execute("UPDATE promos SET active=? WHERE code=?", (1 if body.get("active") else 0, code))
        db.commit()
        if cur.rowcount == 0:
            return JSONResponse({"error": "Промокод не найден"}, status_code=404)
        return {"ok": True, "items": _promo_rows()}


# ---------- Реферальная программа ----------
@app.post("/api/ref/info")
async def ref_info(req: Request):
    _, user = await body_user(req)
    uid = user["id"]
    bot = await ensure_bot_username()
    with db_lock:
        rows = db.execute(
            "SELECT u.first_name, r.ts, r.rewarded FROM referrals r LEFT JOIN users u ON u.user_id=r.user_id "
            "WHERE r.referrer_id=? ORDER BY r.ts DESC LIMIT 50", (uid,)).fetchall()
        total = db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=?", (uid,)).fetchone()[0]
        paid = db.execute("SELECT COUNT(*) FROM referrals WHERE referrer_id=? AND rewarded=1", (uid,)).fetchone()[0]
    return {
        "link": f"https://t.me/{bot}?start=ref_{uid}" if bot else "",
        "invited": total, "deposited": paid,
        "earned_usdt": paid * REF_BONUS_USDT, "earned_coins": paid * REF_BONUS_COINS,
        "bonus_usdt": REF_BONUS_USDT, "min_usdt": REF_MIN_USDT,
        "friends": [{"name": n or "Игрок", "ts": t, "paid": bool(r)} for n, t, r in rows],
    }


async def register_referral(uid: int, ref_id: int, u: dict):
    """Привязываем нового игрока к пригласившему (только для тех, кто впервые открыл бота)."""
    if ref_id == uid:
        return
    with db_lock:
        if not db.execute("SELECT 1 FROM users WHERE user_id=?", (ref_id,)).fetchone():
            return
        if db.execute("SELECT 1 FROM referrals WHERE user_id=?", (uid,)).fetchone():
            return
        db.execute("INSERT INTO referrals(user_id, referrer_id, ts) VALUES(?,?,?)", (uid, ref_id, time.time()))
        db.commit()
    await tg("sendMessage", chat_id=ref_id,
             text=f"👥 По вашей ссылке пришёл новый игрок: {u.get('first_name') or 'друг'}.\n"
                  f"Когда он пополнит счёт от {REF_MIN_USDT} USDT, вы получите {REF_BONUS_USDT} USDT.")


# ---------- приветствие ----------
async def send_welcome(chat_id: int, u: dict, first_time: bool = False):
    name = html.escape((u.get("first_name") or "друг").strip() or "друг")
    play = {"inline_keyboard": [[{"text": "🎰 Играть", "web_app": {"url": BASE_URL}}]]}
    if first_time:
        text = (
            f"🎉 <b>Добро пожаловать, {name}!</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "Ты попал в игровой клуб, где всегда есть во что поиграть:\n\n"
            "🎰 <b>Слоты</b> — фрукты, пельмени и легендарная «Собачья банда» с бонуской\n"
            "🃏 <b>Блэкджек</b> — обыграй дилера\n"
            "🥟 <b>Пельмень или Тапок</b> — орёл и решка по-нашему\n"
            "🎯 <b>Наперстки</b> и <b>крестики-нолики</b> на монеты\n"
            "🎡 <b>Колесо фортуны</b> — бесплатная прокрутка каждые 12 часов\n\n"
            "🎁 Есть промокоды, а за приглашённых друзей — бонусы.\n\n"
            "Жми <b>«Играть»</b> и забирай свою удачу 🍀"
        )
    else:
        with db_lock:
            bal = f"{get_balance(u['id']):,}".replace(",", " ")
        text = (
            f"👋 <b>С возвращением, {name}!</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"💰 Твой баланс: <b>{bal} {CURRENCY}</b>\n"
            "\nСлоты, блэкджек и колесо фортуны уже ждут — жми <b>«Играть»</b> 🎰"
        )
    await tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=play)


# ---------- вебхук бота ----------
@app.post("/webhook")
async def webhook(req: Request, x_telegram_bot_api_secret_token: str = Header(None)):
    if x_telegram_bot_api_secret_token != WEBHOOK_SECRET:
        raise HTTPException(403)
    upd = await req.json()
    if upd.get("callback_query"):
        await handle_callback(upd["callback_query"])
    elif upd.get("message"):
        await handle_message(upd["message"])
    return {"ok": True}


async def handle_message(msg: dict):
    chat_id = msg["chat"]["id"]
    uid = msg["from"]["id"]
    text = msg.get("text", "")
    was_known = True
    if msg["chat"].get("type") == "private":
        with db_lock:
            was_known = db.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone() is not None
        touch_user(msg["from"])

    # владелец вводит причину отклонения вывода (команда отменяет ожидание)
    if uid == OWNER_ID and text:
        with db_lock:
            w = db.execute("SELECT wd_id, card_msg_id FROM admin_wait WHERE owner_id=?", (uid,)).fetchone()
            if w:
                db.execute("DELETE FROM admin_wait WHERE owner_id=?", (uid,))
                db.commit()
        if w and not text.startswith("/"):
            ok, info = await wd_resolve(w[0], "reject", text.strip()[:300], chat_id, w[1])
            await tg("sendMessage", chat_id=chat_id, text=("✅ " if ok else "⚠️ ") + info)
            return

    # владелец отвечает на пересланную обратную связь -> уходит пользователю
    if uid == OWNER_ID and msg.get("reply_to_message"):
        with db_lock:
            row = db.execute(
                "SELECT user_id FROM fb_map WHERE owner_msg_id=?",
                (msg["reply_to_message"]["message_id"],),
            ).fetchone()
        if row:
            await tg("sendMessage", chat_id=row[0], text=f"💬 Ответ:\n{text}")
            return

    if text == "/backup" and uid == OWNER_ID:
        await send_backup(chat_id)
        return

    if text.startswith("/start") or text in (MENU_BTN, OLD_FEEDBACK_BTN):
        if text.startswith("/start"):
            parts = text.split(maxsplit=1)
            payload = parts[1].strip() if len(parts) > 1 else ""
            if payload.startswith("ref_") and payload[4:].isdigit() and not was_known:
                await register_referral(uid, int(payload[4:]), msg["from"])
        await send_welcome(chat_id, msg["from"], first_time=not was_known and text.startswith("/start"))
        if text.startswith("/start") or text == OLD_FEEDBACK_BTN:   # постоянная кнопка «Главное меню» внизу (заменяет старую «Обратная связь»)
            await tg("sendMessage", chat_id=chat_id, text="Кнопка меню всегда под рукой 👇",
                     reply_markup={"keyboard": [[{"text": MENU_BTN}]], "resize_keyboard": True,
                                   "is_persistent": True})
        return

    # любое другое сообщение от игрока — это обращение в поддержку: пересылаем владельцу, он отвечает reply-ем
    if msg["chat"].get("type") == "private" and uid != OWNER_ID and text:
        u = msg["from"]
        who = f"{u.get('first_name', '')} @{u.get('username', '—')} (id {uid})"
        sent = await tg("sendMessage", chat_id=OWNER_ID, text=f"✉️ Сообщение от {who}:\n\n{text}")
        if sent.get("ok"):
            with db_lock:
                db.execute("INSERT OR REPLACE INTO fb_map VALUES(?,?)", (sent["result"]["message_id"], uid))
                db.commit()
        await tg("sendMessage", chat_id=chat_id, text="Спасибо! Сообщение передано, скоро ответим ✅",
                 reply_markup={"inline_keyboard": [[{"text": "🎰 Играть", "web_app": {"url": BASE_URL}}]]})
