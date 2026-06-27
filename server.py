import os
import sqlite3
import secrets
import json
from datetime import datetime, timedelta
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
app.add_middleware(SessionMiddleware,
    secret_key=os.environ.get('SECRET_KEY', 'dev-change-me'),
    max_age=90 * 24 * 3600,  # 90 days max; actual expiry set per-login in session
)
app.add_middleware(CORSMiddleware,
    allow_origins=['https://menochat.app', 'https://www.menochat.app', 'https://menoacct-production-d2e7.up.railway.app'],
    allow_methods=['POST'],
    allow_headers=['Content-Type'],
    allow_credentials=True,
)
jinja = Environment(loader=FileSystemLoader('templates'), autoescape=True)

ADMIN_KEY    = os.environ.get('ADMIN_KEY', 'admin')
BASE_URL     = os.environ.get('BASE_URL', 'https://menochat.app')
# Set RESEND_FROM to a verified-domain address in Railway, e.g. "Menochat <noreply@menochat.app>".
# The default onboarding@resend.dev is Resend's sandbox and only delivers to the account owner.
RESEND_FROM  = os.environ.get('RESEND_FROM', 'Menochat <onboarding@resend.dev>')

# ── Database ──────────────────────────────────────────────────────────────────

def db():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con

def init_db():
    DB.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB)
    con.execute('''CREATE TABLE IF NOT EXISTS users (
        id            INTEGER PRIMARY KEY,
        name          TEXT NOT NULL,
        email         TEXT UNIQUE NOT NULL,
        phone         TEXT,
        username      TEXT UNIQUE,
        password_hash TEXT,
        invite_token  TEXT UNIQUE NOT NULL,
        created_at    DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS completions (
        id           INTEGER PRIMARY KEY,
        user_id      INTEGER NOT NULL REFERENCES users(id),
        proposition  INTEGER NOT NULL,
        completed_at DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS events (
        id          INTEGER PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        proposition INTEGER NOT NULL,
        step        INTEGER NOT NULL,
        role        TEXT NOT NULL,
        message     TEXT NOT NULL,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS surveys (
        id          INTEGER PRIMARY KEY,
        user_id     INTEGER NOT NULL REFERENCES users(id),
        proposition INTEGER NOT NULL DEFAULT 1,
        q1          TEXT,
        q2          TEXT,
        q3          TEXT,
        created_at  DATETIME DEFAULT CURRENT_TIMESTAMP
    )''')
    con.execute('''CREATE TABLE IF NOT EXISTS password_resets (
        id         INTEGER PRIMARY KEY,
        user_id    INTEGER NOT NULL REFERENCES users(id),
        token      TEXT UNIQUE NOT NULL,
        expires_at TEXT NOT NULL,
        used       INTEGER NOT NULL DEFAULT 0
    )''')
    con.commit()
    con.close()

init_db()

# ── Helpers ───────────────────────────────────────────────────────────────────

def current_user(request: Request):
    uid = request.session.get('user_id')
    if not uid:
        return None
    expires_at = request.session.get('expires_at')
    if expires_at and datetime.utcnow().isoformat() > expires_at:
        request.session.clear()
        return None
    with db() as con:
        return con.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()

def render(template: str, **ctx) -> HTMLResponse:
    return HTMLResponse(jinja.get_template(template).render(**ctx))

VACATION_DAY_OVERRIDES = {
    'LuluT33': 0,
    'menomama': 0,
    'raidersfootball': 0,
}

def challenge_status(user, props_completed_count: int) -> dict:
    from datetime import date as _date
    created = _date.fromisoformat(user['created_at'][:10])
    today = _date.today()
    days_elapsed = (today - created).days
    vacation_days_used = max(0, days_elapsed - props_completed_count - 2)
    vacation_days_used = VACATION_DAY_OVERRIDES.get(user['username'], vacation_days_used)
    is_complete = props_completed_count >= 48
    is_eliminated = not is_complete and vacation_days_used > 0
    return {
        'day_number':          min(days_elapsed + 1, 49),
        'days_elapsed':        days_elapsed,
        'vacation_days_used':  vacation_days_used,
        'on_last_vacation':    vacation_days_used == 1,
        'is_complete':         is_complete,
        'is_eliminated':       is_eliminated,
    }

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
    import re as _re, urllib.request as _req, json as _json
    to = _re.sub(r'\+[^@]+(@gmail\.com)$', r'\1', to, flags=_re.I)
    api_key = os.environ.get('BREVO_API_KEY', '')
    if not api_key:
        raise RuntimeError('BREVO_API_KEY not set')
    sender = os.environ.get('BREVO_SENDER', os.environ.get('SMTP_USER', 'noreply@menochat.app'))
    payload = _json.dumps({
        'sender':      {'email': sender, 'name': 'Menochat'},
        'to':          [{'email': to}],
        'subject':     subject,
        'textContent': body,
    }).encode()
    request = _req.Request(
        'https://api.brevo.com/v3/smtp/email',
        data=payload,
        headers={'api-key': api_key, 'Content-Type': 'application/json'},
    )
    with _req.urlopen(request, timeout=15) as resp:
        return _json.loads(resp.read())

async def send_email_bg(to: str, subject: str, body: str):
    import asyncio
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, send_email, to, subject, body)
        print(f"[EMAIL] sent to {to}")
    except Exception as e:
        print(f"[EMAIL ERROR] {to}: {e}")

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
async def login_post(request: Request, username: str = Form(...), password: str = Form(...), remember: str = Form(default='')):
    with db() as con:
        user = con.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not user or not user['password_hash'] or not verify_password(password, user['password_hash']):
        return render('login.html', error='Incorrect username or password.')
    request.session['user_id'] = user['id']
    days = 90 if remember else 1
    request.session['expires_at'] = (datetime.utcnow() + timedelta(days=days)).isoformat()
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
            await send_email_bg(
                user['email'],
                'Reset your Menochat password',
                f"Click this link to reset your password (expires in 1 hour):\n\n{link}\n\nIf you didn't request this, ignore it."
            )
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

PROP_URLS = {1: '/i1', 2: '/i2', 3: '/i3', 4: '/i4', 5: '/i5', 6: '/i6', 7: '/i7', 8: '/i8', 9: '/i9', 10: '/i10', 11: '/i11', 12: '/i12', 13: '/i13', 14: '/i14'}
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

    cs = challenge_status(user, n_done)

    return render('welcome.html',
        username=user['username'],
        n_done=n_done,
        total=TOTAL_PROPS,
        progress_bar=progress_bar,
        next_prop=next_prop,
        next_url=next_url,
        completed=completed,
        cs=cs,
    )

@app.get('/eliminated', response_class=HTMLResponse)
async def eliminated_page(request: Request):
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
    n_done = len(rows)
    cs = challenge_status(user, n_done)
    if not cs['is_eliminated']:
        return RedirectResponse('/welcome', status_code=303)
    return render('eliminated.html', username=user['username'], n_done=n_done, cs=cs)

def prop_gate(request: Request):
    """Returns a redirect if the user can't access props, else None."""
    user = current_user(request)
    if not user:
        return RedirectResponse('/login', status_code=303)
    with db() as con:
        n_done = con.execute(
            'SELECT COUNT(DISTINCT proposition) FROM completions WHERE user_id=?', (user['id'],)
        ).fetchone()[0]
    if challenge_status(user, n_done)['is_eliminated']:
        return RedirectResponse('/eliminated', status_code=303)
    return None

@app.get('/classic', response_class=HTMLResponse)
async def classic(request: Request):
    return FileResponse('static/classic.html')

@app.get('/i1', response_class=HTMLResponse)
async def i1(request: Request):
    gate = prop_gate(request)
    return gate or FileResponse('static/i1.html')

@app.get('/i2', response_class=HTMLResponse)
async def i2(request: Request):
    gate = prop_gate(request)
    return gate or FileResponse('static/i2.html')

@app.get('/i3', response_class=HTMLResponse)
async def i3(request: Request):
    gate = prop_gate(request)
    return gate or FileResponse('static/i3.html')

@app.get('/i4', response_class=HTMLResponse)
async def i4(request: Request):
    gate = prop_gate(request)
    return gate or FileResponse('static/i4.html')

@app.get('/i5', response_class=HTMLResponse)
async def i5(request: Request):
    gate = prop_gate(request)
    return gate or FileResponse('static/i5.html')

@app.get('/i6', response_class=HTMLResponse)
async def i6(request: Request):
    gate = prop_gate(request)
    return gate or FileResponse('static/i6.html')

@app.get('/i7', response_class=HTMLResponse)
async def i7(request: Request):
    gate = prop_gate(request)
    return gate or FileResponse('static/i7.html')

@app.get('/i8', response_class=HTMLResponse)
async def i8(request: Request):
    gate = prop_gate(request)
    return gate or FileResponse('static/i8.html')

@app.get('/i9', response_class=HTMLResponse)
async def i9(request: Request):
    gate = prop_gate(request)
    return gate or FileResponse('static/i9.html')

@app.get('/i10', response_class=HTMLResponse)
async def i10(request: Request):
    gate = prop_gate(request)
    return gate or FileResponse('static/i10.html')

@app.get('/i11', response_class=HTMLResponse)
async def i11(request: Request):
    gate = prop_gate(request)
    return gate or FileResponse('static/i11.html')

@app.get('/i12', response_class=HTMLResponse)
async def i12(request: Request):
    gate = prop_gate(request)
    return gate or FileResponse('static/i12.html')

@app.get('/i13', response_class=HTMLResponse)
async def i13(request: Request):
    gate = prop_gate(request)
    return gate or FileResponse('static/i13.html')

@app.get('/i14', response_class=HTMLResponse)
async def i14(request: Request):
    gate = prop_gate(request)
    return gate or FileResponse('static/i14.html')

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
    proposition = int(body.get('proposition', 1))
    with db() as con:
        con.execute(
            '''INSERT INTO completions (user_id, proposition)
               SELECT ?, ?
               WHERE NOT EXISTS (
                   SELECT 1 FROM completions WHERE user_id=? AND proposition=?
               )''',
            (user['id'], proposition, user['id'], proposition)
        )
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
    # per-user completion counts for challenge status
    completion_counts = {}
    for c in completions:
        uid = c['user_id']
        completion_counts[uid] = completion_counts.get(uid, set()) | {c['proposition']}
    user_cs = {u['id']: challenge_status(u, len(completion_counts.get(u['id'], set())))
               for u in users if u['username']}
    return render('admin.html', users=users, completions=completions,
                  events_by_key=events_by_key, surveys=surveys, key=key, user_cs=user_cs)

@app.get('/admin/login', response_class=HTMLResponse)
async def admin_login_get():
    return render('admin_login.html', error=None)

@app.post('/admin/login', response_class=HTMLResponse)
async def admin_login_post(request: Request, key: str = Form(...)):
    if key != ADMIN_KEY:
        return render('admin_login.html', error='Wrong key.')
    return RedirectResponse(f'/admin?key={key}', status_code=303)

@app.get('/admin/test-connect')
async def admin_test_connect(key: str = ''):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail='Wrong key.')
    import socket
    results = {}
    for host, port in [
        ('smtp.gmail.com', 587), ('smtp.gmail.com', 465),
        ('api.sendgrid.com', 443), ('api.mailgun.net', 443),
        ('api.brevo.com', 443), ('api.postmarkapp.com', 443),
    ]:
        try:
            s = socket.create_connection((host, port), timeout=5)
            s.close()
            results[f'{host}:{port}'] = 'OK'
        except Exception as e:
            results[f'{host}:{port}'] = str(e)
    return results

@app.get('/admin/make-reset-link')
async def admin_make_reset_link(key: str = '', username: str = ''):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail='Wrong key.')
    if not username:
        return JSONResponse({'ok': False, 'error': 'Provide ?username=...'}, status_code=400)
    with db() as con:
        user = con.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
        if not user:
            return JSONResponse({'ok': False, 'error': f'User not found: {username}'}, status_code=404)
        token = secrets.token_urlsafe(32)
        expires = (datetime.utcnow() + timedelta(hours=24)).isoformat()
        con.execute('DELETE FROM password_resets WHERE user_id=?', (user['id'],))
        con.execute('INSERT INTO password_resets (user_id, token, expires_at) VALUES (?,?,?)',
                    (user['id'], token, expires))
    link = f"{BASE_URL}/reset-password/{token}"
    return {'ok': True, 'username': username, 'link': link}

@app.get('/admin/user-email')
async def admin_user_email(key: str = '', username: str = ''):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail='Wrong key.')
    with db() as con:
        user = con.execute('SELECT username, email FROM users WHERE username=?', (username,)).fetchone()
        if not user:
            return JSONResponse({'ok': False, 'error': f'User not found: {username}'}, status_code=404)
    return {'ok': True, 'username': user['username'], 'email': user['email']}

@app.get('/admin/set-email')
async def admin_set_email(key: str = '', username: str = '', email: str = ''):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail='Wrong key.')
    with db() as con:
        user = con.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        if not user:
            return JSONResponse({'ok': False, 'error': f'User not found: {username}'}, status_code=404)
        con.execute('UPDATE users SET email=? WHERE id=?', (email, user['id']))
    return {'ok': True, 'username': username, 'email': email}

@app.get('/admin/test-email')
async def admin_test_email(key: str = '', to: str = ''):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail='Wrong key.')
    if not to:
        return JSONResponse({'ok': False, 'error': 'Provide ?to=someone@example.com'}, status_code=400)
    try:
        result = send_email(to, 'Menochat email test', f'Email is working.\n\nBASE_URL={BASE_URL}\nSMTP_USER={os.environ.get("SMTP_USER","(not set)")}')
        return {'ok': True, 'result': result}
    except Exception as e:
        return JSONResponse({'ok': False, 'error': str(e)}, status_code=500)

@app.get('/admin/reset-user-password')
async def admin_reset_password(key: str = '', username: str = '', password: str = ''):
    if key != ADMIN_KEY:
        raise HTTPException(status_code=403, detail='Wrong key.')
    if not username or not password:
        raise HTTPException(status_code=400, detail='username and password required.')
    with db() as con:
        user = con.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        if not user:
            return {'ok': False, 'error': f'User {username} not found.'}
        con.execute('UPDATE users SET password_hash=? WHERE id=?', (hash_password(password), user['id']))
    return {'ok': True, 'message': f'Password reset for {username}'}

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
