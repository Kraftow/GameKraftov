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
FEEDBACK_BTN = "✉️ Обратная связь"

# ---- мини-игра «Пельмень или Тапок» (вместо орла и решки) ----
COIN_SIDES = {"pelmen": ("Пельмень", "🥟"), "tapok": ("Тапок", "🩴")}
MAX_BET = 1_000_000_000
CURRENCY = "🪙"

# ---- пополнение (1 USDT = 25 монет) ----
COINS_PER_USDT = 25
DEPOSIT_WALLET = os.environ.get("DEPOSIT_WALLET") or "TLdiZJdSN82A1HDRB3WBTDeTXmmv2xJLMT"
DEPOSIT_NETWORK = "TRC20"
MIN_DEPOSIT, MAX_DEPOSIT = 25, 1_000_000   # в монетах (минимум = 1 USDT)


def usdt_str(coins: int) -> str:
    cents = coins * 100 // COINS_PER_USDT   # 25 монет = 1 USDT -> всегда ровно до сотых
    return f"{cents // 100}.{cents % 100:02d}"

# ---- вывод ----
MIN_WITHDRAW = 25   # в монетах (1 USDT)
WALLET_RE = re.compile(r"^T[1-9A-HJ-NP-Za-km-z]{33}$")  # адрес TRC20

# ---- крестики-нолики и наперстки ----
TTT_BLOCK_CHANCE = 75        # % — как часто бот замечает вашу выигрышную линию и блокирует её
SHELL_MULT = (27, 10)        # выплата при угадывании: ставка x2.7 (шанс 1 из 3)
POPUP_TTL = 3 * 24 * 3600    # сколько дней непросмотренное окно ждёт игрока

# (название, эмодзи, вес). Сумма весов = 140, поэтому шансы нормируются:
# Фартануло ≈21.4%, Туз ≈42.9%, 100$ ≈10.7%, Не повезло 25%
PRIZES = [
    ("Фартануло", "🍀", 30, "/static/clover.svg"),
    ("Порванный туз червовый", "🃏", 60, "/static/ace.svg"),
    ("Сто долларов", "💵", 15, "/static/money.svg"),
    ("Повезёт в другой раз", "😢", 35, "/static/sad.svg"),
]

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
        """
    )
    db.commit()


async def tg(method: str, **data):
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.post(f"{API}/{method}", json=data)
        return r.json()


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
    yield


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
    db.commit()
    return get_balance(uid)


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


@app.post("/api/status")
async def status(req: Request):
    user = check_init_data((await req.json())["initData"])
    touch_user(user)
    with db_lock:
        bal = get_balance(user["id"])
        adm = is_admin(user["id"])
    return {
        "balance": bal,
        "is_admin": adm,
        "is_owner": user["id"] == OWNER_ID,
        "left": seconds_left(user["id"]),
        "prizes": [{"name": n, "emoji": e, "img": i} for n, e, _, i in PRIZES],
    }


@app.post("/api/history")
async def history(req: Request):
    user = check_init_data((await req.json())["initData"])
    with db_lock:
        rows = db.execute(
            "SELECT prize, ts FROM spins WHERE user_id=? ORDER BY ts DESC LIMIT 50", (user["id"],)
        ).fetchall()
    return {"items": [{"prize": p, "ts": t} for p, t in rows]}


@app.post("/api/spin")
async def spin(req: Request):
    user = check_init_data((await req.json())["initData"])
    uid = user["id"]
    with db_lock:  # защита от двойного нажатия
        left = seconds_left_nolock(uid)
        if left > 0:
            return JSONResponse({"error": "cooldown", "left": left}, status_code=429)
        idx = random.choices(range(len(PRIZES)), weights=[p[2] for p in PRIZES])[0]
        db.execute("INSERT INTO spins VALUES(?,?,?)", (uid, time.time(), PRIZES[idx][0]))
        db.commit()
    name = PRIZES[idx][0]
    await tg("sendMessage", chat_id=uid, text=f"🎉 Вы выиграли: {name}")
    return {"index": idx, "name": name, "left": 0 if uid == OWNER_ID else COOLDOWN}


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
        result = secrets.choice(list(COIN_SIDES))
        won = result == side
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


_IMAGES = {"clover.svg", "ace.svg", "money.svg", "sad.svg"}


@app.get("/static/{name}")
async def static_img(name: str):
    if name not in _IMAGES:
        raise HTTPException(404)
    return FileResponse(os.path.join(os.path.dirname(os.path.abspath(__file__)), name), media_type="image/svg+xml")


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
        ball = secrets.randbelow(3)                 # шарик выбирается честно и независимо от вашего выбора
        won = ball == cup
        payout = bet * SHELL_MULT[0] // SHELL_MULT[1] if won else 0
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
    return grid, wins, total


def slot_public(key: str) -> dict:
    cfg = SLOTS[key]
    syms = sorted(cfg["pay"], key=lambda s: -max(cfg["pay"][s].values()))
    return {
        "name": cfg["name"], "emoji": cfg["emoji"], "reels": cfg["reels"], "rows": cfg["rows"],
        "lines": len(cfg["lines"]), "min_bet": len(cfg["lines"]), "wild": cfg["wild"],
        "symbols": syms, "pay": cfg["pay"], "info": cfg["info"],
    }


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
    try:
        bet = int(body.get("bet"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "bad_bet"}, status_code=400)
    if cfg is None or bet < len(cfg["lines"]) or bet > MAX_BET:   # минимум 1 монета на линию
        return JSONResponse({"error": "bad_bet"}, status_code=400)
    with db_lock:  # атомарно: проверка баланса + списание/начисление
        if is_banned(uid):
            return BANNED_RESP()
        bal = get_balance(uid)
        if bet > bal:
            return JSONResponse({"error": "no_money", "balance": bal}, status_code=400)
        grid, wins, total = slot_spin(cfg)                    # результат выбирает только сервер
        payout = bet * total // len(cfg["lines"])
        bal = add_balance(uid, payout - bet, f"slot {key} {'win' if payout else 'lose'}")
    return {"grid": grid, "wins": wins, "payout": payout, "bet": bet, "balance": bal}


# ---------- Пополнение счёта ----------
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
    with db_lock:
        if is_banned(uid):
            return BANNED_RESP()
        recent = db.execute(
            "SELECT COUNT(*) FROM deposits WHERE user_id=? AND status='new' AND ts>?", (uid, time.time() - 3600)
        ).fetchone()[0]
        if recent >= 10:
            return JSONResponse({"error": "Слишком много заявок, попробуйте позже"}, status_code=429)
        cur = db.execute(
            "INSERT INTO deposits(user_id, ts, amount, status) VALUES(?,?,?,'new')", (uid, time.time(), amount)
        )
        db.commit()
        dep_id = cur.lastrowid
    return {"id": dep_id, "amount": amount, "usdt": usdt_str(amount), "currency": "USDT",
            "network": DEPOSIT_NETWORK, "wallet": DEPOSIT_WALLET}


@app.post("/api/deposit/paid")
async def deposit_paid(req: Request):
    body, user = await body_user(req)
    uid = user["id"]
    try:
        dep_id = int(body.get("id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "Ошибка"}, status_code=400)
    with db_lock:
        row = db.execute("SELECT amount, status FROM deposits WHERE id=? AND user_id=?", (dep_id, uid)).fetchone()
        if not row or row[1] != "new":
            return JSONResponse({"error": "Заявка не найдена или уже отправлена"}, status_code=404)
        db.execute("UPDATE deposits SET status='claimed' WHERE id=?", (dep_id,))
        db.commit()
    who = f"{user.get('first_name', '')} @{user.get('username', '—')} (id {uid})"
    await tg("sendMessage", chat_id=OWNER_ID,
             text=f"💸 Заявка на пополнение #{dep_id}\nОт: {who}\nСумма: {usdt_str(row[0])} USDT ({DEPOSIT_NETWORK}) = {row[0]} {CURRENCY}\n\n"
                  f"Проверьте поступление на кошелёк и зачислите в приложении: Админ → Обзор → Заявки на пополнение.")
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
        db.commit()
    if approve:
        await tg("sendMessage", chat_id=row[0],
                 text=f"✅ Пополнение на {usdt_str(row[1])} USDT зачислено: +{row[1]} {CURRENCY}\nБаланс: {new_bal} {CURRENCY}")
    else:
        await tg("sendMessage", chat_id=row[0],
                 text=f"❌ Заявка на пополнение #{dep_id} отклонена. Если вы оплатили — напишите нам через «Обратная связь».")
    return {"ok": True, "approved": approve}




# ---------- Управление админами (только владелец) ----------
async def body_owner(req: Request):
    body, user = await body_user(req)
    if user["id"] != OWNER_ID:
        raise HTTPException(403, "owner only")
    return body, user


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
        note = f"🧊 Вывод #{wd_id} заморожен. Напишите нам через «{FEEDBACK_BTN}», чтобы уточнить детали."
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
        usdt = usdt_str(coins)
        cur = db.execute("INSERT INTO withdrawals(user_id, ts, coins, usdt, wallet, status) VALUES(?,?,?,?,?,'pending')",
                         (uid, time.time(), coins, usdt, wallet))
        wd_id = cur.lastrowid
        new_bal = add_balance(uid, -coins, f"withdraw #{wd_id}")  # монеты замораживаются до решения админа
        card = wd_card_nolock(wd_id)
    await tg("sendMessage", chat_id=OWNER_ID, text=card[0], parse_mode="HTML", reply_markup=wd_kb("pending", wd_id))
    return {"ok": True, "id": wd_id, "coins": coins, "usdt": usdt, "balance": new_bal}


@app.post("/api/cashier/history")
async def cashier_history(req: Request):
    _, user = await body_user(req)
    uid = user["id"]
    with db_lock:
        deps = db.execute("SELECT amount, status, ts FROM deposits WHERE user_id=? AND status IN ('claimed','done','rejected') "
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
    if msg["chat"].get("type") == "private":
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

    if text.startswith("/start"):
        await tg(
            "sendMessage",
            chat_id=chat_id,
            text="Привет! Жми «Открыть» и крути рулетку 🎰",
            reply_markup={
                "inline_keyboard": [[{"text": "🎰 Открыть", "web_app": {"url": BASE_URL}}]]
            },
        )
        await tg(
            "sendMessage",
            chat_id=chat_id,
            text="Если что — пиши нам через кнопку ниже 👇",
            reply_markup={"keyboard": [[{"text": FEEDBACK_BTN}]], "resize_keyboard": True},
        )
        return

    if text == FEEDBACK_BTN:
        with db_lock:
            db.execute("INSERT OR IGNORE INTO fb_wait VALUES(?)", (uid,))
            db.commit()
        await tg("sendMessage", chat_id=chat_id, text="Напишите ваше сообщение одним текстом, я передам его 👌")
        return

    with db_lock:
        waiting = db.execute("SELECT 1 FROM fb_wait WHERE user_id=?", (uid,)).fetchone()
    if waiting:
        u = msg["from"]
        who = f"{u.get('first_name', '')} @{u.get('username', '—')} (id {uid})"
        sent = await tg("sendMessage", chat_id=OWNER_ID, text=f"✉️ Обратная связь от {who}:\n\n{text}")
        with db_lock:
            db.execute("DELETE FROM fb_wait WHERE user_id=?", (uid,))
            if sent.get("ok"):
                db.execute(
                    "INSERT OR REPLACE INTO fb_map VALUES(?,?)",
                    (sent["result"]["message_id"], uid),
                )
            db.commit()
        await tg("sendMessage", chat_id=chat_id, text="Спасибо! Сообщение отправлено ✅")
