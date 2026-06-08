import os
import sqlite3
import secrets
from pathlib import Path

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware
from jinja2 import Environment, FileSystemLoader
import hashlib, base64

# ── Config ────────────────────────────────────────────────────────────────────

DB  = Path('data/db.sqlite3')
app = FastAPI()

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    key  = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 260_000)
    return base64.b64encode(salt + key).decode()

def verify_password(password: str, stored: str) -> bool:
    data = base64.b64decode(stored)
    salt, key = data[:16], data[16:]
    check = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 260_000)
    return check == key
app.add_middleware(SessionMiddleware, secret_key=os.environ.get('SECRET_KEY', 'dev-change-me'))
jinja = Environment(loader=FileSystemLoader('templates'), autoescape=True)

ADMIN_KEY = os.environ.get('ADMIN_KEY', 'admin')
BASE_URL  = os.environ.get('BASE_URL', 'https://menoacct-production.up.railway.app')

# ── Database ──────────────────────────────────────────────────────────────────

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    DB.parent.mkdir(exist_ok=True)
    with db() as con:
        con.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id           INTEGER PRIMARY KEY,
                name         TEXT NOT NULL,
                email        TEXT UNIQUE NOT NULL,
                phone        TEXT,
                username     TEXT UNIQUE,
                password_hash TEXT,
                invite_token TEXT UNIQUE NOT NULL,
                created_at   DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS completions (
                id           INTEGER PRIMARY KEY,
                user_id      INTEGER NOT NULL REFERENCES users(id),
                proposition  INTEGER NOT NULL,
                completed_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS events (
                id          INTEGER PRIMARY KEY,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                proposition INTEGER NOT NULL,
                step        INTEGER NOT NULL,
                role        TEXT NOT NULL,
                message     TEXT NOT NULL,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS surveys (
                id          INTEGER PRIMARY KEY,
                user_id     INTEGER NOT NULL REFERENCES users(id),
                proposition INTEGER NOT NULL DEFAULT 1,
                q1          TEXT,
                q2          TEXT,
                q3          TEXT,
                created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        ''')

init_db()

# ── Helpers ───────────────────────────────────────────────────────────────────

def current_user(request: Request):
    uid = request.session.get('user_id')
    if not uid:
        return None
    with db() as con:
        return con.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()

def render(template: str, **ctx) -> HTMLResponse:
    return HTMLResponse(jinja.get_template(template).render(**ctx))

# ── Invite (set up account) ───────────────────────────────────────────────────

@app.get('/invite/{token}', response_class=HTMLResponse)
async def invite_get(token: str):
    with db() as con:
        user = con.execute('SELECT * FROM users WHERE invite_token=?', (token,)).fetchone()
    if not user:
        return HTMLResponse('Link not found.', status_code=404)
    if user['username']:
        return HTMLResponse('This link has already been used. <a href="/login">Log in</a>.')
    return render('invite.html', name=user['name'], token=token, error=None)

@app.post('/invite/{token}', response_class=HTMLResponse)
async def invite_post(
    token: str, request: Request,
    username: str = Form(...), password: str = Form(...), confirm: str = Form(...)
):
    with db() as con:
        user = con.execute('SELECT * FROM users WHERE invite_token=?', (token,)).fetchone()
    if not user or user['username']:
        return HTMLResponse('Invalid or already used link.', status_code=400)

    def err(msg):
        return render('invite.html', name=user['name'], token=token, error=msg)

    if len(username) < 3:
        return err('Username must be at least 3 characters.')
    if len(password) < 8:
        return err('Password must be at least 8 characters.')
    if password != confirm:
        return err('Passwords do not match.')

    with db() as con:
        taken = con.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        if taken:
            return err('That username is taken — try another.')
        con.execute('UPDATE users SET username=?, password_hash=? WHERE id=?',
                    (username, hash_password(password), user['id']))

    request.session['user_id'] = user['id']
    return RedirectResponse('/welcome', status_code=303)

# ── Login / Logout ─────────────────────────────────────────────────────────────

@app.get('/login', response_class=HTMLResponse)
async def login_get(request: Request):
    if current_user(request):
        return RedirectResponse('/welcome', status_code=303)
    return render('login.html', error=None)

@app.post('/login', response_class=HTMLResponse)
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    with db() as con:
        user = con.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not user or not user['password_hash'] or not verify_password(password, user['password_hash']):
        return render('login.html', error='Incorrect username or password.')
    request.session['user_id'] = user['id']
    return RedirectResponse('/welcome', status_code=303)

@app.get('/logout')
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse('/login', status_code=303)

# ── App pages ──────────────────────────────────────────────────────────────────

@app.get('/welcome', response_class=HTMLResponse)
async def welcome(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse('/login', status_code=303)
    return render('welcome.html', name=user['name'], username=user['username'])

@app.get('/i1', response_class=HTMLResponse)
async def i1(request: Request):
    if not current_user(request):
        return RedirectResponse('/login', status_code=303)
    return FileResponse('static/i1.html')

@app.get('/survey', response_class=HTMLResponse)
async def survey(request: Request):
    if not current_user(request):
        return RedirectResponse('/login', status_code=303)
    return FileResponse('static/survey.html')

# ── API ────────────────────────────────────────────────────────────────────────

@app.post('/api/complete')
async def api_complete(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail='Not logged in.')
    body = await request.json()
    with db() as con:
        con.execute('INSERT INTO completions (user_id, proposition) VALUES (?,?)',
                    (user['id'], body.get('proposition', 1)))
    return {'ok': True}

@app.post('/api/survey')
async def api_survey(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail='Not logged in.')
    body = await request.json()
    with db() as con:
        con.execute(
            'INSERT INTO surveys (user_id, proposition, q1, q2, q3) VALUES (?,?,?,?,?)',
            (user['id'], body.get('proposition', 1),
             body.get('q1', ''), body.get('q2', ''), body.get('q3', ''))
        )
    return {'ok': True}

@app.post('/api/event')
async def api_event(request: Request):
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail='Not logged in.')
    body = await request.json()
    with db() as con:
        con.execute(
            'INSERT INTO events (user_id, proposition, step, role, message) VALUES (?,?,?,?,?)',
            (user['id'], body.get('proposition', 1), body.get('step', 0),
             body['role'], body['message'])
        )
    return {'ok': True}

# ── Admin ──────────────────────────────────────────────────────────────────────

@app.get('/admin', response_class=HTMLResponse)
async def admin(key: str = ''):
    if key != ADMIN_KEY:
        return render('login.html', error=None, admin_gate=True)
    with db() as con:
        users = con.execute(
            'SELECT * FROM users ORDER BY created_at'
        ).fetchall()
        completions = con.execute('''
            SELECT c.*, u.name, u.username
            FROM completions c JOIN users u ON c.user_id = u.id
            ORDER BY c.completed_at DESC
        ''').fetchall()
        event_rows = con.execute('''
            SELECT e.*, u.name, u.username
            FROM events e JOIN users u ON e.user_id = u.id
            ORDER BY e.created_at ASC
        ''').fetchall()
        surveys = con.execute('''
            SELECT s.*, u.name, u.username, s.created_at AS completed_at
            FROM surveys s JOIN users u ON s.user_id = u.id
            ORDER BY s.created_at DESC
        ''').fetchall()
    events_by_user = {}
    for e in event_rows:
        events_by_user.setdefault(e['user_id'], []).append(e)
    return render('admin.html', users=users, completions=completions,
                  events_by_user=events_by_user, surveys=surveys, key=key)

@app.get('/admin/login', response_class=HTMLResponse)
async def admin_login_get():
    return render('admin_login.html', error=None)

@app.post('/admin/login', response_class=HTMLResponse)
async def admin_login_post(request: Request, key: str = Form(...)):
    if key != ADMIN_KEY:
        return render('admin_login.html', error='Wrong key.')
    return RedirectResponse(f'/admin?key={key}', status_code=303)

@app.post('/admin/seed')
async def admin_seed(request: Request):
    body = await request.json()
    if body.get('key') != ADMIN_KEY:
        raise HTTPException(status_code=403, detail='Wrong key.')
    results = []
    with db() as con:
        for u in body.get('users', []):
            token = secrets.token_urlsafe(32)
            name  = u['name']
            phone = u.get('phone', '')
            email = u.get('email') or phone.replace('-','').replace(' ','').replace('+','') + '@noemail.invalid'
            try:
                con.execute(
                    'INSERT INTO users (name, email, phone, invite_token) VALUES (?,?,?,?)',
                    (name, email, phone, token)
                )
                con.commit()
                results.append({'name': name, 'status': 'added', 'link': f'{BASE_URL}/invite/{token}'})
            except sqlite3.IntegrityError:
                row = con.execute(
                    'SELECT invite_token, username FROM users WHERE email=?', (email,)
                ).fetchone()
                if row:
                    status = 'already set up' if row['username'] else 'already added'
                    results.append({'name': name, 'status': status, 'link': f"{BASE_URL}/invite/{row['invite_token']}"})
    return {'results': results}

app.mount('/', StaticFiles(directory='static', html=True), name='static')

# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='0.0.0.0', port=int(os.environ.get('PORT', 8000)))
