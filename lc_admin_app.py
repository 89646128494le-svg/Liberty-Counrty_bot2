"""
Liberty Country Admin Panel
===========================

Это приложение FastAPI предоставляет веб‑панель управления для сервера Liberty Country.
Панель использует Discord OAuth2 для аутентификации, проверяет наличие паспорта
у игрока в базе данных и разделяет доступ для администраторов и обычных игроков.
Музыкальные функции работают через запись команд в файл, который читает бот,
а также считывание состояния плеера из JSON. Дополнительные разделы могут быть
добавлены без изменения основного бота.

Перед запуском настройте переменные окружения:

* DISCORD_CLIENT_ID, DISCORD_CLIENT_SECRET, DISCORD_REDIRECT_URI – параметры OAuth2
* LC_ADMIN_IDS – список Discord ID, которым разрешён административный доступ
* LC_ADMIN_SECRET – секрет для подписи сессий
* LC_STATE_FILE – путь к JSON со состоянием плеера
* LC_CONTROL_FILE – путь к файлу команд для бота
* LC_QUEUE_EXPORT_LIMIT – максимальное количество элементов очереди для панели

Для запуска выполните:
```bash
pip install fastapi uvicorn jinja2 httpx python-multipart
uvicorn lc_admin_app:app --reload --port 8765
```
"""

import os
import json
import sqlite3
import secrets
import urllib.parse
import asyncio
import tempfile

import httpx
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import Response

# Создание приложения
app = FastAPI()

# Секрет для сессий
SESSION_SECRET = os.getenv("LC_ADMIN_SECRET", secrets.token_hex(32))
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET)

# Пути к статике и шаблонам. Мы используем отдельные папки, чтобы не
# конфликтовать с шаблонами оригинального сайта.
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
TEMPLATE_DIR = os.path.join(BASE_DIR, "lc_templates")
STATIC_DIR = os.path.join(BASE_DIR, "lc_static")
templates = Jinja2Templates(directory=TEMPLATE_DIR)

if os.path.isdir(STATIC_DIR):
    app.mount("/lc_static", StaticFiles(directory=STATIC_DIR), name="lc_static")

# Конфигурация через переменные окружения
STATE_FILE = os.getenv("LC_STATE_FILE", os.path.join(BASE_DIR, "lc_nowplaying.json"))
CONTROL_FILE = os.getenv("LC_CONTROL_FILE", os.path.join(BASE_DIR, "control_queue.jsonl"))
QUEUE_EXPORT_LIMIT = int(os.getenv("LC_QUEUE_EXPORT_LIMIT", "100"))

ADMIN_IDS = [i.strip() for i in os.getenv("LC_ADMIN_IDS", "").split(",") if i.strip()]

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:8765/auth/discord/callback")

DATABASE = os.getenv("LC_DB_FILE", os.path.join(BASE_DIR, "liberty_country.db"))


def get_db():
    """Подключение к SQLite с указанием возвращаемого типа строк."""
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn


def user_has_passport(discord_id: str) -> bool:
    """Проверяет, выдан ли паспорт пользователю по discord_id."""
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT passport_issued FROM citizens WHERE discord_id=? AND passport_issued=1", (str(discord_id),))
    row = c.fetchone()
    conn.close()
    return row is not None


def write_control(action: str, payload: dict):
    """Записывает команду для музыкального бота в файл. В случае ошибки
    записывает во временный файл в каталоге %TEMP%/liberty_country."""
    line = json.dumps({"action": action, "payload": payload}, ensure_ascii=False) + "\n"
    # Пробуем записать в основной файл
    try:
        os.makedirs(os.path.dirname(CONTROL_FILE), exist_ok=True)
        with open(CONTROL_FILE, "a", encoding="utf-8") as f:
            f.write(line)
        return
    except Exception:
        pass
    # fallback
    tmp_dir = os.path.join(tempfile.gettempdir(), "liberty_country")
    os.makedirs(tmp_dir, exist_ok=True)
    fallback = os.path.join(tmp_dir, os.path.basename(CONTROL_FILE) or "control_queue.jsonl")
    with open(fallback, "a", encoding="utf-8") as f:
        f.write(line)


async def fetch_lyrics(title: str, artist: str = "") -> dict:
    """Пытается получить текст песни и LRC из внешнего API lrclib.net.
    Возвращает словарь с ключами lines (список) и plain (строка)."""
    query = (artist + " " + title).strip()
    url = f"https://lrclib.net/api/search?limit=1&query={urllib.parse.quote(query)}"
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            resp = await client.get(url)
            data = resp.json()
            if not data:
                return {"lines": [], "plain": ""}
            best = data[0]
            lrc_url = best.get("url")
            if lrc_url:
                lrc_resp = await client.get(lrc_url)
                lrc_text = lrc_resp.text
                # разбираем LRC
                lines = []
                for part in lrc_text.splitlines():
                    if part.startswith("[") and "]" in part:
                        ts, lyric = part.split("]", 1)
                        ts = ts.strip("[]")
                        # mm:ss.xx или hh:mm:ss.xx
                        try:
                            parts = ts.split(":")
                            if len(parts) == 2:
                                m, s = parts
                                t = int(float(m) * 60 + float(s) * 1000)
                            else:
                                h, m, s = parts
                                t = int(float(h) * 3600 + float(m) * 60 + float(s) * 1000)
                        except Exception:
                            continue
                        lines.append({"t": t, "l": lyric.strip()})
                plain = "\n".join([l.get("l") for l in lines])
                return {"lines": lines, "plain": plain}
        except Exception:
            return {"lines": [], "plain": ""}
    return {"lines": [], "plain": ""}


def get_state_for_guild(guild_id: int) -> dict:
    """Читает JSON‑файл состояния и возвращает данные для указанной гильдии."""
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get(str(guild_id)) or {}
    except Exception:
        return {}


def require_logged_in(request: Request) -> dict:
    """Проверяет наличие авторизации. Если нет – выкидывает 302 на /login."""
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=status.HTTP_302_FOUND, headers={"Location": "/login"})
    return user


def require_admin(user: dict):
    """Проверяет, что пользователь администратор. Если нет – ошибка 403."""
    if not user.get("is_admin"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав")


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    user = request.session.get("user")
    return templates.TemplateResponse("home.html", {
        "request": request,
        "user": user,
        "has_passport": user_has_passport(user["id"]) if user else False
    })


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    # Если уже авторизован – на главную
    if request.session.get("user"):
        return RedirectResponse("/")
    return templates.TemplateResponse("login.html", {"request": request})


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/")


@app.get("/auth/discord/login")
async def discord_login():
    """Редирект на авторизацию Discord OAuth2."""
    if not DISCORD_CLIENT_ID or not DISCORD_REDIRECT_URI:
        return HTMLResponse("Discord OAuth не настроен", status_code=500)
    params = {
        "client_id": DISCORD_CLIENT_ID,
        "redirect_uri": DISCORD_REDIRECT_URI,
        "response_type": "code",
        "scope": "identify"
    }
    url = "https://discord.com/api/oauth2/authorize?" + urllib.parse.urlencode(params)
    return RedirectResponse(url)


@app.get("/auth/discord/callback")
async def discord_callback(request: Request, code: str = None):
    """Обработчик возврата после авторизации у Discord."""
    if code is None:
        return RedirectResponse("/login")
    # Обмениваем код на токен
    token_url = "https://discord.com/api/oauth2/token"
    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI
    }
    async with httpx.AsyncClient() as client:
        try:
            token_resp = await client.post(token_url, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"})
            token_json = token_resp.json()
        except Exception:
            return HTMLResponse("Ошибка авторизации", status_code=500)
    access_token = token_json.get("access_token")
    if not access_token:
        return HTMLResponse("Не удалось получить токен", status_code=500)
    # Получаем данные пользователя
    async with httpx.AsyncClient() as client:
        user_resp = await client.get(
            "https://discord.com/api/users/@me",
            headers={"Authorization": f"Bearer {access_token}"}
        )
        user_data = user_resp.json()
    discord_id = user_data.get("id")
    username = user_data.get("username")
    avatar = user_data.get("avatar")
    if not discord_id:
        return HTMLResponse("Не удалось получить данные пользователя", status_code=500)
    # Определяем админа
    is_admin = False
    if discord_id in ADMIN_IDS:
        is_admin = True
    # Сохраняем в сессии
    request.session["user"] = {
        "id": discord_id,
        "username": username,
        "avatar": avatar,
        "is_admin": is_admin
    }
    # Проверяем паспорт
    if not user_has_passport(discord_id):
        return RedirectResponse("/onboarding/passport")
    return RedirectResponse("/")


@app.get("/onboarding/passport", response_class=HTMLResponse)
async def passport_form(request: Request):
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login")
    # Если паспорт уже есть, переходим на главную
    if user_has_passport(user["id"]):
        return RedirectResponse("/")
    return templates.TemplateResponse("passport.html", {"request": request, "user": user})


@app.post("/onboarding/passport", response_class=HTMLResponse)
async def passport_submit(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    birth_date: str = Form(...),
    birth_place: str = Form(...),
    bio: str = Form("")
):
    user = request.session.get("user")
    if not user:
        return RedirectResponse("/login")
    # Записываем/обновляем паспорт
    conn = get_db()
    c = conn.cursor()
    # Проверяем наличие записи
    c.execute("SELECT id FROM citizens WHERE discord_id=?", (str(user["id"]),))
    row = c.fetchone()
    if row:
        c.execute("""
            UPDATE citizens
            SET first_name=?, last_name=?, birth_date=?, birth_place=?, bio=?, passport_issued=1, residence='Отель', citizenship_date=DATE('now')
            WHERE discord_id=?
        """, (first_name, last_name, birth_date, birth_place, bio, str(user["id"])))
    else:
        c.execute("""
            INSERT INTO citizens (discord_id, first_name, last_name, birth_date, birth_place, bio, passport_issued, citizenship_date, residence)
            VALUES (?, ?, ?, ?, ?, ?, 1, DATE('now'), 'Отель')
        """, (str(user["id"]), first_name, last_name, birth_date, birth_place, bio))
    conn.commit()
    conn.close()
    # Перенаправляем на главную
    return RedirectResponse("/", status_code=303)


@app.get("/music", response_class=HTMLResponse)
async def music_page(request: Request, guild_id: int = 0):
    user = require_logged_in(request)
    # Для простоты используем discord_id как id сервера, если guild_id не передан
    if guild_id == 0:
        guild_id = int(user["id"])
    return templates.TemplateResponse("music.html", {"request": request, "user": user, "guild_id": guild_id})


@app.get("/dj", response_class=HTMLResponse)
async def dj_page(request: Request, guild_id: int = 0):
    user = require_logged_in(request)
    require_admin(user)
    if guild_id == 0:
        guild_id = int(user["id"])
    return templates.TemplateResponse("dj.html", {"request": request, "user": user, "guild_id": guild_id})


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    user = require_logged_in(request)
    require_admin(user)
    return templates.TemplateResponse("admin.html", {"request": request, "user": user})


@app.get("/public/music", response_class=HTMLResponse)
async def public_music(request: Request, guild_id: int):
    # Показываем публичный статус проигрывателя без управления
    data = get_state_for_guild(guild_id)
    return templates.TemplateResponse("public_music.html", {"request": request, "data": data, "guild_id": guild_id})


@app.get("/dj/state")
async def dj_state(guild_id: int):
    data = get_state_for_guild(guild_id)
    # Ограничиваем размер очереди
    if data:
        data = data.copy()
        queue = data.get("queue") or []
        data["queue"] = queue[:QUEUE_EXPORT_LIMIT]
    return JSONResponse(data or {})


@app.post("/dj/ctrl")
async def dj_control(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=401)
    body = await request.json()
    action = body.get("action")
    payload = body.get("payload") or {}
    # Разрешаем неадминам только play/playtop
    if not user.get("is_admin") and action not in ("play", "playtop"):
        raise HTTPException(status_code=403)
    # Добавляем в payload информацию о guild_id если отсутствует (используем id пользователя по умолчанию)
    if not payload.get("guild_id"):
        payload["guild_id"] = int(user["id"])
    write_control(action, payload)
    return JSONResponse({"status": "ok"})


@app.get("/dj/lyrics")
async def dj_lyrics(guild_id: int = None, title: str = None, artist: str = None):
    """Возвращает синхронизированные lyrics для текущего трека или по запросу."""
    if title and (not artist):
        # Попытка разделить "Artist - Title"
        if "-" in title:
            parts = title.split("-", 1)
            artist = parts[0].strip()
            title = parts[1].strip()
    if guild_id and not title:
        data = get_state_for_guild(guild_id)
        if not data:
            return JSONResponse({"lines": [], "plain": ""})
        title = data.get("title")
        # разбиваем [artist - title]
        if title and " - " in title:
            parts = title.split(" - ", 1)
            artist = parts[0].strip()
            title = parts[1].strip()
    if not title:
        return JSONResponse({"lines": [], "plain": ""})
    lyrics = await fetch_lyrics(title, artist or "")
    return JSONResponse(lyrics)
