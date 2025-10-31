import os
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
import httpx


APP_PORT = int(os.getenv("LC_MAIN_PORT", "8787"))
SESSION_SECRET = os.getenv("LC_SESSION_SECRET", "change_me_" + os.urandom(8).hex())
DB_PATH = os.getenv("LC_DB_PATH", "liberty_country.db")
ADMIN_HOST = os.getenv("LC_ADMIN_HOST", "127.0.0.1")
ADMIN_PORT = int(os.getenv("LC_ADMIN_PORT", "8765"))
DISCORD_WEBHOOK_URL = os.getenv("LC_WEBHOOK_URL", "")

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID", "")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET", "")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", f"http://127.0.0.1:{APP_PORT}/auth/discord/callback")
DISCORD_SCOPE = "identify"

ADMIN_IDS = set([int(x) for x in os.getenv("LC_ADMIN_IDS","").split(",") if x.strip().isdigit()])

app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, session_cookie="lc_main_sid", same_site="lax")

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

_I18N = json.loads(Path("i18n.json").read_text(encoding="utf-8")) if Path("i18n.json").exists() else {"ru":{}, "en":{}}

def t(request: Request, key: str) -> str:
    lang = request.session.get('lang', 'ru')
    return _I18N.get(lang, {}).get(key, key)

templates.env.globals['t'] = lambda key: key
templates.env.globals['now'] = lambda: datetime.now()

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def rows(conn, q, params=()):
    cur = conn.execute(q, params)
    return [dict(r) for r in cur.fetchall()]

def row(conn, q, params=()):
    cur = conn.execute(q, params)
    r = cur.fetchone(); return dict(r) if r else None

def ensure_schema():
    conn = db()
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS citizens(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        discord_id TEXT UNIQUE,
        first_name TEXT, last_name TEXT,
        birth_date TEXT, birth_place TEXT,
        bio TEXT, passport_issued INTEGER DEFAULT 0,
        citizenship_date TEXT, residence TEXT
    );
    CREATE TABLE IF NOT EXISTS houses(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        address TEXT, price INTEGER, status TEXT, rooms INTEGER,
        cover_url TEXT, description TEXT
    );
    CREATE TABLE IF NOT EXISTS vehicles(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        brand TEXT, model TEXT, plate TEXT, type TEXT, state TEXT,
        cover_url TEXT, description TEXT
    );
    CREATE TABLE IF NOT EXISTS business(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT, type TEXT, status TEXT, balance INTEGER,
        cover_url TEXT, description TEXT, owner_id INTEGER
    );
    ''')
    conn.commit(); conn.close()

ensure_schema()

@app.middleware('http')
async def inject_globals(request: Request, call_next):
    templates.env.globals['t'] = lambda key: t(request, key)
    request.state.user = request.session.get('user')
    resp = await call_next(request)
    return resp

def current_user(request: Request):
    return request.session.get('user')

def is_admin(user):
    try:
        return bool(user) and int(user.get('id')) in ADMIN_IDS
    except Exception:
        return False

async def send_webhook(msg: str):
    if not os.getenv('LC_WEBHOOK_URL',''):
        return
    try:
        async with httpx.AsyncClient(timeout=8) as c:
            await c.post(os.getenv('LC_WEBHOOK_URL'), json={'content': msg})
    except Exception:
        pass

@app.get('/', response_class=HTMLResponse, name='index')
def index(request: Request):
    return templates.TemplateResponse('index.html', {'request': request, 'current_user': current_user(request), 'session': request.session})

@app.get('/lang/{code}')
def set_lang(request: Request, code: str):
    if code.lower() in ('ru','en'):
        request.session['lang'] = code.lower()
    return RedirectResponse(url=request.headers.get('referer','/'))

@app.get('/login', response_class=HTMLResponse, name='login')
def login(request: Request):
    return templates.TemplateResponse('login.html', {'request': request, 'current_user': current_user(request), 'session': request.session})

@app.get('/logout', name='logout')
def logout(request: Request):
    request.session.clear(); return RedirectResponse(url='/')

@app.get('/auth/discord/login')
def discord_login():
    if not os.getenv('DISCORD_CLIENT_ID') or not os.getenv('DISCORD_REDIRECT_URI'):
        return JSONResponse({'error':'Discord OAuth not configured'}, status_code=500)
    from urllib.parse import urlencode
    params = {'client_id': os.getenv('DISCORD_CLIENT_ID'),'redirect_uri': os.getenv('DISCORD_REDIRECT_URI'),'response_type': 'code','scope': 'identify','prompt': 'consent'}
    return RedirectResponse(f"https://discord.com/api/oauth2/authorize?{urlencode(params)}")

@app.get('/auth/discord/callback')
async def discord_callback(request: Request, code: str = ''):
    if not code:
        return RedirectResponse(url='/login')
    token_url = 'https://discord.com/api/oauth2/token'
    data = {'client_id': os.getenv('DISCORD_CLIENT_ID'),'client_secret': os.getenv('DISCORD_CLIENT_SECRET'),'grant_type':'authorization_code','code':code,'redirect_uri': os.getenv('DISCORD_REDIRECT_URI')}
    headers = {'Content-Type': 'application/x-www-form-urlencoded'}
    async with httpx.AsyncClient(timeout=10) as client:
        tok = await client.post(token_url, data=data, headers=headers)
        if tok.status_code != 200:
            return RedirectResponse(url='/login')
        token = tok.json(); at = token.get('access_token')
        if not at: return RedirectResponse(url='/login')
        u = await client.get('https://discord.com/api/users/@me', headers={'Authorization': f'Bearer {at}'})
        if u.status_code != 200: return RedirectResponse(url='/login')
        info = u.json()
    request.session['user'] = {'id': info.get('id'),'username': info.get('global_name') or info.get('username'),'avatar_url': f"https://cdn.discordapp.com/avatars/{info.get('id')}/{info.get('avatar')}.png" if info.get('avatar') else None,'admin': int(info.get('id') or 0) in set(int(x) for x in os.getenv('LC_ADMIN_IDS','').split(',') if x.strip().isdigit())}
    conn = db(); c = row(conn, 'SELECT id, passport_issued FROM citizens WHERE discord_id=?', (str(info.get('id')),)); conn.close()
    request.session['needs_passport'] = 0 if (c and int(c.get('passport_issued') or 0) == 1) else 1
    return RedirectResponse(url='/')

@app.get('/profile', response_class=HTMLResponse, name='profile')
def profile(request: Request):
    return templates.TemplateResponse('profile.html', {'request': request, 'current_user': current_user(request), 'has_passport': 0 == request.session.get('needs_passport', 1), 'session': request.session})

@app.get('/onboarding/passport', response_class=HTMLResponse)
def passport_form(request: Request):
    tpl = 'passport.html' if Path('templates/passport.html').exists() else 'login.html'
    return templates.TemplateResponse(tpl, {'request': request, 'current_user': current_user(request), 'session': request.session})

@app.post('/onboarding/passport')
async def passport_submit(request: Request,
    first_name: str = Form(...), last_name: str = Form(...), birth_date: str = Form(''), birth_place: str = Form(''), bio: str = Form(''), residence: str = Form('Отель')):
    user = current_user(request)
    if not user: return RedirectResponse(url='/login', status_code=302)
    conn = db()
    conn.execute('''INSERT INTO citizens (discord_id, first_name, last_name, birth_date, birth_place, bio, passport_issued, citizenship_date, residence)
                    VALUES (?, ?, ?, ?, ?, ?, 1, DATE('now'), ?)
                    ON CONFLICT(discord_id) DO UPDATE SET first_name=excluded.first_name, last_name=excluded.last_name, birth_date=excluded.birth_date, birth_place=excluded.birth_place, bio=excluded.bio, passport_issued=1, residence=excluded.residence
                ''', (str(user['id']), first_name, last_name, birth_date, birth_place, bio, residence))
    conn.commit(); conn.close(); request.session['needs_passport'] = 0
    try: await send_webhook(f'🪪 Новый паспорт оформлен: {first_name} {last_name} (Discord ID: {user.get("id")})')
    except Exception: pass
    return RedirectResponse(url='/profile', status_code=302)

def paginate(sql_base: str, params: list, page: int, limit: int, order_by: str, allowed: List[str]):
    order_by = order_by if order_by in allowed else allowed[0]
    off = max(page-1, 0) * limit
    sql = f"{sql_base} ORDER BY {order_by} LIMIT ? OFFSET ?"
    return sql, params + [limit, off]

@app.get('/houses', response_class=HTMLResponse, name='houses')
def houses_page(request: Request, q: str = '', status: str = '', price_max: Optional[int] = None, page: int = 1, limit: int = 12, sort: str = 'id'):
    conn = db(); base = 'SELECT id, address, price, status, rooms, cover_url FROM houses WHERE 1=1'; params = []
    if q: base += ' AND address LIKE ?'; params += [f'%{q}%']
    if status: base += ' AND status = ?'; params += [status]
    if price_max is not None: base += ' AND price <= ?'; params += [price_max]
    sql, p2 = paginate(base, params, page, limit, sort, ['id','price','rooms'])
    items = rows(conn, sql, tuple(p2)); conn.close()
    return templates.TemplateResponse('houses.html', {'request': request, 'houses': items, 'current_user': current_user(request), 'session': request.session})

@app.get('/houses/{hid}', response_class=HTMLResponse)
def house_detail(request: Request, hid: int):
    conn = db(); it = row(conn, 'SELECT * FROM houses WHERE id=?', (hid,)); conn.close()
    if not it: return RedirectResponse(url='/houses')
    return templates.TemplateResponse('house_detail.html', {'request': request, 'house': it, 'current_user': current_user(request), 'session': request.session})

@app.get('/vehicles', response_class=HTMLResponse, name='vehicles')
def vehicles_page(request: Request, q: str = '', plate: str = '', type: str = '', page: int = 1, limit: int = 12, sort: str = 'id'):
    conn = db(); base = 'SELECT id, brand, model, plate, type, state, cover_url FROM vehicles WHERE 1=1'; params = []
    if q: base += ' AND (brand LIKE ? OR model LIKE ?)'; params += [f'%{q}%', f'%{q}%']
    if plate: base += ' AND plate LIKE ?'; params += [f'%{plate}%']
    if type: base += ' AND type = ?'; params += [type]
    sql, p2 = paginate(base, params, page, limit, sort, ['id'])
    items = rows(conn, sql, tuple(p2)); conn.close()
    return templates.TemplateResponse('vehicles.html', {'request': request, 'vehicles': items, 'current_user': current_user(request), 'session': request.session})

@app.get('/vehicles/{vid}', response_class=HTMLResponse)
def vehicle_detail(request: Request, vid: int):
    conn = db(); it = row(conn, 'SELECT * FROM vehicles WHERE id=?', (vid,)); conn.close()
    if not it: return RedirectResponse(url='/vehicles')
    return templates.TemplateResponse('vehicle_detail.html', {'request': request, 'vehicle': it, 'current_user': current_user(request), 'session': request.session})

@app.get('/business', response_class=HTMLResponse, name='business')
def business_page(request: Request, q: str = '', type: str = '', status: str = '', page: int = 1, limit: int = 12, sort: str = 'id'):
    conn = db(); base = 'SELECT id, name, type, status, balance, cover_url FROM business WHERE 1=1'; params = []
    if q: base += ' AND name LIKE ?'; params += [f'%{q}%']
    if type: base += ' AND type = ?'; params += [type]
    if status: base += ' AND status = ?'; params += [status]
    sql, p2 = paginate(base, params, page, limit, sort, ['id','balance','name'])
    items = rows(conn, sql, tuple(p2)); conn.close()
    return templates.TemplateResponse('business.html', {'request': request, 'business': items, 'current_user': current_user(request), 'session': request.session})

@app.get('/business/{bid}', response_class=HTMLResponse)
def business_detail(request: Request, bid: int):
    conn = db(); it = row(conn, 'SELECT * FROM business WHERE id=?', (bid,)); conn.close()
    if not it: return RedirectResponse(url='/business')
    return templates.TemplateResponse('business_detail.html', {'request': request, 'biz': it, 'current_user': current_user(request), 'session': request.session})

@app.get('/api/houses')
def api_houses(q: str = '', status: str = '', price_max: Optional[int] = None, page: int = 1, limit: int = 12, sort: str = 'id'):
    conn = db(); base = 'SELECT id, address, price, status, rooms, cover_url FROM houses WHERE 1=1'; params = []
    if q: base += ' AND address LIKE ?'; params += [f'%{q}%']
    if status: base += ' AND status=?'; params += [status]
    if price_max is not None: base += ' AND price <= ?'; params += [price_max]
    sql, p2 = paginate(base, params, page, limit, sort, ['id','price','rooms'])
    items = rows(conn, sql, tuple(p2))
    for h in items:
        if h.get('price') is not None:
            h['price_fmt'] = f"{h['price']:,}".replace(',', ' ') + '$'
    conn.close(); return {'items': items}

@app.get('/api/vehicles')
def api_vehicles(q: str = '', plate: str = '', type: str = '', page: int = 1, limit: int = 12, sort: str = 'id'):
    conn = db(); base = 'SELECT id, brand, model, plate, type, state, cover_url FROM vehicles WHERE 1=1'; params = []
    if q: base += ' AND (brand LIKE ? OR model LIKE ?)'; params += [f'%{q}%', f'%{q}%']
    if plate: base += ' AND plate LIKE ?'; params += [f'%{plate}%']
    if type: base += ' AND type = ?'; params += [type]
    sql, p2 = paginate(base, params, page, limit, sort, ['id'])
    items = rows(conn, sql, tuple(p2)); conn.close(); return {'items': items}

@app.get('/api/business')
def api_business(q: str = '', type: str = '', status: str = '', page: int = 1, limit: int = 12, sort: str = 'id'):
    conn = db(); base = 'SELECT id, name, type, status, balance, cover_url FROM business WHERE 1=1'; params = []
    if q: base += ' AND name LIKE ?'; params += [f'%{q}%']
    if type: base += ' AND type = ?'; params += [type]
    if status: base += ' AND status = ?'; params += [status]
    sql, p2 = paginate(base, params, page, limit, sort, ['id','balance','name'])
    items = rows(conn, sql, tuple(p2))
    for b in items:
        if b.get('balance') is not None:
            b['balance_fmt'] = f"{b['balance']:,}".replace(',', ' ') + '$'
    conn.close(); return {'items': items}

@app.get('/music', response_class=HTMLResponse, name='music_page')
def music_page(request: Request):
    gid = int(os.getenv('LC_GUILD_ID', '0'))
    return templates.TemplateResponse('music.html', {'request': request, 'guild_id': gid, 'current_user': current_user(request), 'session': request.session})

@app.get('/dj/state')
async def dj_state(guild_id: Optional[int] = None):
    url = f"http://{os.getenv('LC_ADMIN_HOST','127.0.0.1')}:{int(os.getenv('LC_ADMIN_PORT','8765'))}/dj/state"
    params = {}
    if guild_id is not None: params['guild_id'] = guild_id
    async with httpx.AsyncClient(timeout=6) as c:
        r = await c.get(url, params=params)
        return JSONResponse(r.json() if r.content else {}, status_code=r.status_code)

@app.post('/dj/ctrl')
async def dj_ctrl(payload: Dict[str, Any]):
    url = f"http://{os.getenv('LC_ADMIN_HOST','127.0.0.1')}:{int(os.getenv('LC_ADMIN_PORT','8765'))}/dj/ctrl"
    async with httpx.AsyncClient(timeout=6) as c:
        r = await c.post(url, json=payload)
        return JSONResponse(r.json() if r.content else {}, status_code=r.status_code)

@app.get('/admin', response_class=HTMLResponse)
def admin_page(request: Request):
    user = current_user(request)
    if not (user and is_admin(user)): return RedirectResponse(url='/login')
    conn = db(); counts = {'houses': row(conn, 'SELECT COUNT(*) c FROM houses')['c'], 'vehicles': row(conn, 'SELECT COUNT(*) c FROM vehicles')['c'], 'business': row(conn, 'SELECT COUNT(*) c FROM business')['c'], 'citizens': row(conn, 'SELECT COUNT(*) c FROM citizens')['c']}; conn.close()
    return templates.TemplateResponse('admin.html', {'request': request, 'current_user': user, 'counts': counts, 'session': request.session})

@app.post('/admin/upload')
async def admin_upload(request: Request, file: UploadFile = File(...)):
    user = current_user(request)
    if not (user and is_admin(user)): return JSONResponse({'error':'forbidden'}, status_code=403)
    up_dir = Path('static/uploads'); up_dir.mkdir(parents=True, exist_ok=True)
    name = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{file.filename}"
    path = up_dir / name
    with path.open('wb') as out:
        out.write(await file.read())
    return {'url': f'/static/uploads/{name}'}

@app.post('/admin/houses/create')
async def admin_create_house(request: Request, address: str = Form(...), price: int = Form(0), status: str = Form('free'), rooms: int = Form(1), cover_url: str = Form(''), description: str = Form('')):
    user = current_user(request)
    if not (user and is_admin(user)): return RedirectResponse(url='/login')
    conn = db(); conn.execute('INSERT INTO houses(address,price,status,rooms,cover_url,description) VALUES(?,?,?,?,?,?)', (address,price,status,rooms,cover_url,description)); conn.commit(); conn.close()
    try: await send_webhook(f'🏠 Добавлен объект: {address} за {price}$')
    except Exception: pass
    return RedirectResponse(url='/admin', status_code=302)

@app.post('/admin/vehicles/create')
async def admin_create_vehicle(request: Request, brand: str = Form(...), model: str = Form(...), plate: str = Form(''), type: str = Form('car'), state: str = Form('active'), cover_url: str = Form(''), description: str = Form('')):
    user = current_user(request)
    if not (user and is_admin(user)): return RedirectResponse(url='/login')
    conn = db(); conn.execute('INSERT INTO vehicles(brand,model,plate,type,state,cover_url,description) VALUES(?,?,?,?,?,?,?)', (brand,model,plate,type,state,cover_url,description)); conn.commit(); conn.close()
    return RedirectResponse(url='/admin', status_code=302)

@app.post('/admin/business/create')
async def admin_create_business(request: Request, name: str = Form(...), type: str = Form('shop'), status: str = Form('active'), balance: int = Form(0), cover_url: str = Form(''), description: str = Form('')):
    user = current_user(request)
    if not (user and is_admin(user)): return RedirectResponse(url='/login')
    conn = db(); conn.execute('INSERT INTO business(name,type,status,balance,cover_url,description) VALUES(?,?,?,?,?,?)', (name,type,status,balance,cover_url,description)); conn.commit(); conn.close()
    return RedirectResponse(url='/admin', status_code=302)

@app.get('/me')
def me(request: Request): return JSONResponse(request.session.get('user') or {})

if __name__ == '__main__':
    print(f'Liberty Country main site on http://127.0.0.1:{int(os.getenv("LC_MAIN_PORT","8787"))}')
    uvicorn.run('lc_main_site:app', host='127.0.0.1', port=int(os.getenv('LC_MAIN_PORT','8787')), reload=False)
