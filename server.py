import os
import sqlite3
import secrets
import json
import smtplib
import ssl
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
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
app.add_middleware(CORSMiddleware,
    allow_origins=['https://www.menochat.app', 'https://menoacct-production-d2e7.up.railway.app'],
    allow_methods=['POST'],
    allow_headers=['Content-Type'],
    allow_credentials=True,
)
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
            CREATE TABLE IF NOT EXISTS password_resets (
                id         INTEGER PRIMARY KEY,
                user_id    INTEGER NOT NULL REFERENCES users(id),
                token      TEXT UNIQUE NOT NULL,
                expires_at TEXT NOT NULL,
                used       INTEGER NOT NULL DEFAULT 0
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

# ── Open signup ───────────────────────────────────────────────────────────────

@app.get('/signup', response_class=HTMLResponse)
async def signup_get(request: Request):
    if current_user(request):
        return RedirectResponse('/welcome', status_code=303)
    return render('signup.html', error=None)

@app.post('/signup', response_class=HTMLResponse)
async def signup_post(
    request: Request,
    name: str = Form(...), username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...), confirm: str = Form(...)
):
    def err(msg):
        return render('signup.html', error=msg)

    if len(name.strip()) < 1:
        return err('Please enter your name.')
    if len(username) < 3:
        return err('Username must be at least 3 characters.')
    if '@' not in email:
        return err('Please enter a valid email.')
    if len(password) < 8:
        return err('Password must be at least 8 characters.')
    if password != confirm:
        return err('Passwords do not match.')

    token = secrets.token_urlsafe(32)
    email = email.strip().lower()
    if email == 'jtbird3@gmail.com':
        email = f'jtbird3+{username}@gmail.com'
    with db() as con:
        taken = con.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        if taken:
            return err('That username is taken — try another.')
        try:
            con.execute(
                'INSERT INTO users (name, email, username, password_hash, invite_token) VALUES (?,?,?,?,?)',
                (name.strip(), email, username, hash_password(password), token)
            )
            user = con.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        except Exception:
            return err('Something went wrong. Try a different username or email.')

    request.session['user_id'] = user['id']
    return RedirectResponse('/welcome', status_code=303)

# ── Email ─────────────────────────────────────────────────────────────────────

def send_email(to: str, subject: str, body: str):
    host = os.environ.get('SMTP_HOST', '')
    port = int(os.environ.get('SMTP_PORT', 587))
    user = os.environ.get('SMTP_USER', '')
    pwd  = os.environ.get('SMTP_PASSWORD', '')
    if not host or not user:
        raise RuntimeError('SMTP not configured')
    msg = MIMEText(body, 'plain')
    msg['Subject'] = subject
    msg['From']    = user
    msg['To']      = to
    ctx = ssl.create_default_context()
    with smtplib.SMTP(host, port) as s:
        s.ehlo()
        s.starttls(context=ctx)
        s.login(user, pwd)
        s.sendmail(user, to, msg.as_string())

# ── Login / Logout ─────────────────────────────────────────────────────────────

@app.get('/login', response_class=HTMLResponse)
async def login_get(request: Request):
    if current_user(request):
        return RedirectResponse('/welcome', status_code=303)
    return render('login.html', error=None)

def needs_email(user) -> bool:
    e = user['email'] or ''
    return not e or e.endswith('@signup.menochat.app') or e.endswith('@noemail.invalid')

@app.post('/login', response_class=HTMLResponse)
async def login_post(request: Request, username: str = Form(...), password: str = Form(...)):
    with db() as con:
        user = con.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not user or not user['password_hash'] or not verify_password(password, user['password_hash']):
        return render('login.html', error='Incorrect username or password.')
    request.session['user_id'] = user['id']
    if needs_email(user):
        return RedirectResponse('/add-email', status_code=303)
    return RedirectResponse('/welcome', status_code=303)

@app.get('/add-email', response_class=HTMLResponse)
async def add_email_get(request: Request):
    if not current_user(request):
        return RedirectResponse('/login', status_code=303)
    return render('add_email.html', error=None)

@app.post('/add-email', response_class=HTMLResponse)
async def add_email_post(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse('/login', status_code=303)
    form = await request.form()
    email = (form.get('email') or '').strip().lower()
    if '@' not in email or '.' not in email.split('@')[-1]:
        return render('add_email.html', error='Please enter a valid email address.')
    stored_email = email
    if email == 'jtbird3@gmail.com':
        stored_email = f'jtbird3+{user["username"]}@gmail.com'
    with db() as con:
        taken = con.execute('SELECT id FROM users WHERE email=? AND id!=?', (stored_email, user['id'])).fetchone()
        if taken:
            return render('add_email.html', error='That email is already linked to another account.')
        con.execute('UPDATE users SET email=? WHERE id=?', (stored_email, user['id']))
    return RedirectResponse('/welcome', status_code=303)

@app.get('/logout')
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse('/login', status_code=303)

@app.get('/forgot-password', response_class=HTMLResponse)
async def forgot_get():
    return render('forgot_password.html', sent=False, error=None)

@app.post('/forgot-password', response_class=HTMLResponse)
async def forgot_post(request: Request):
    try:
        form = await request.form()
        username = (form.get('username') or '').strip()
        if username:
            with db() as con:
                user = con.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
            if user and user['email'] and '@signup.menochat.app' not in user['email'] and '@noemail.invalid' not in user['email']:
                token = secrets.token_urlsafe(32)
                expires = (datetime.utcnow() + timedelta(hours=1)).isoformat()
                with db() as con:
                    con.execute('DELETE FROM password_resets WHERE user_id=?', (user['id'],))
                    con.execute('INSERT INTO password_resets (user_id, token, expires_at) VALUES (?,?,?)',
                                (user['id'], token, expires))
                link = f"{BASE_URL}/reset-password/{token}"
                try:
                    send_email(
                        user['email'],
                        'Reset your Menochat password',
                        f"Click this link to reset your password (expires in 1 hour):\n\n{link}\n\nIf you didn't request this, ignore it."
                    )
                    print(f"[EMAIL] reset sent to {user['email']}")
                except Exception as e:
                    print(f"[EMAIL ERROR] failed to send reset to {user['email']}: {e}")
    except Exception as e:
        print(f"[FORGOT ERROR] {e}")
    return render('forgot_password.html', sent=True, error=None)

@app.get('/reset-password/{token}', response_class=HTMLResponse)
async def reset_get(token: str):
    with db() as con:
        row = con.execute(
            'SELECT * FROM password_resets WHERE token=? AND used=0', (token,)
        ).fetchone()
    if not row or row['expires_at'] < datetime.utcnow().isoformat():
        return render('reset_password.html', token=token, expired=True, error=None)
    return render('reset_password.html', token=token, expired=False, error=None)

@app.post('/reset-password/{token}', response_class=HTMLResponse)
async def reset_post(token: str, password: str = Form(...), confirm: str = Form(...)):
    with db() as con:
        row = con.execute(
            'SELECT * FROM password_resets WHERE token=? AND used=0', (token,)
        ).fetchone()
    if not row or row['expires_at'] < datetime.utcnow().isoformat():
        return render('reset_password.html', token=token, expired=True, error=None)
    if len(password) < 8:
        return render('reset_password.html', token=token, expired=False, error='Password must be at least 8 characters.')
    if password != confirm:
        return render('reset_password.html', token=token, expired=False, error='Passwords do not match.')
    with db() as con:
        con.execute('UPDATE users SET password_hash=? WHERE id=?', (hash_password(password), row['user_id']))
        con.execute('UPDATE password_resets SET used=1 WHERE id=?', (row['id'],))
    return RedirectResponse('/login?reset=1', status_code=303)

# ── App pages ──────────────────────────────────────────────────────────────────

PROP_URLS = {1: '/i1', 2: '/i2'}
TOTAL_PROPS = 48

def fmt_date(dt_str):
    try:
        dt = datetime.strptime(dt_str[:10], '%Y-%m-%d')
        return f"{dt.strftime('%b')} {dt.day}"
    except Exception:
        return dt_str[:10]

@app.get('/welcome', response_class=HTMLResponse)
async def welcome(request: Request):
    user = current_user(request)
    if not user:
        return RedirectResponse('/login', status_code=303)

    with db() as con:
        rows = con.execute(
            '''SELECT proposition, MIN(completed_at) AS completed_at
               FROM completions WHERE user_id=?
               GROUP BY proposition ORDER BY proposition''',
            (user['id'],)
        ).fetchall()

    completed_set = {r['proposition'] for r in rows}
    n_done = len(completed_set)
    completed = [{'prop': r['proposition'], 'date': fmt_date(r['completed_at']), 'url': PROP_URLS.get(r['proposition'])} for r in rows]

    next_prop = next((p for p in range(1, TOTAL_PROPS + 1) if p not in completed_set), None)
    next_url  = PROP_URLS.get(next_prop) if next_prop else None

    bar_filled = round(n_done / TOTAL_PROPS * 24)
    progress_bar = '█' * bar_filled + '░' * (24 - bar_filled)

    return render('welcome.html',
        username=user['username'],
        n_done=n_done,
        total=TOTAL_PROPS,
        progress_bar=progress_bar,
        next_prop=next_prop,
        next_url=next_url,
        completed=completed,
    )

@app.get('/i1', response_class=HTMLResponse)
async def i1(request: Request):
    if not current_user(request):
        return RedirectResponse('/login', status_code=303)
    return FileResponse('static/i1.html')

@app.get('/i2', response_class=HTMLResponse)
async def i2(request: Request):
    if not current_user(request):
        return RedirectResponse('/login', status_code=303)
    return FileResponse('static/i2.html')

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
    events_by_key = {}
    for e in event_rows:
        k = f"{e['user_id']}_{e['proposition']}"
        events_by_key.setdefault(k, []).append(e)
    return render('admin.html', users=users, completions=completions,
                  events_by_key=events_by_key, surveys=surveys, key=key)

@app.get('/admin/login', response_class=HTMLResponse)
async def admin_login_get():
    return render('admin_login.html', error=None)

@app.post('/admin/login', response_class=HTMLResponse)
async def admin_login_post(request: Request, key: str = Form(...)):
    if key != ADMIN_KEY:
        return render('admin_login.html', error='Wrong key.')
    return RedirectResponse(f'/admin?key={key}', status_code=303)

@app.get('/admin/test-email')
async def admin_test_email(key: str = ''):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail='Wrong key.')
    try:
        send_email(
            os.environ.get('SMTP_USER', ''),
            'Menochat SMTP test',
            f'SMTP is working. BASE_URL={BASE_URL}'
        )
        return {'ok': True, 'message': f"Email sent to {os.environ.get('SMTP_USER')}"}
    except Exception as e:
        return {'ok': False, 'error': str(e)}

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
