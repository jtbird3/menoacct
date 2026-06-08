"""
Add your signups here, then run:  python seed.py
It prints each person's invite link. Text or email it to them.
"""
import sqlite3, secrets
from pathlib import Path

DB = Path('data/db.sqlite3')

# ── Paste your signups here ───────────────────────────────────────────────────
USERS = [
    # {'name': 'Jane Doe',  'email': 'jane@example.com',  'phone': '555-000-0001'},
    # {'name': 'John Smith','email': 'john@example.com',  'phone': '555-000-0002'},
]

BASE_URL = 'https://your-app.up.railway.app'   # change after Railway deploy

# ─────────────────────────────────────────────────────────────────────────────

DB.parent.mkdir(exist_ok=True)
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row

for u in USERS:
    token = secrets.token_urlsafe(32)
    try:
        con.execute(
            'INSERT INTO users (name, email, phone, invite_token) VALUES (?,?,?,?)',
            (u['name'], u['email'], u.get('phone', ''), token)
        )
        con.commit()
        print(f"{u['name']:20s}  {BASE_URL}/invite/{token}")
    except sqlite3.IntegrityError:
        row = con.execute('SELECT invite_token, username FROM users WHERE email=?', (u['email'],)).fetchone()
        status = 'account set up' if row['username'] else 'not yet registered'
        print(f"{u['name']:20s}  already in DB ({status})")

con.close()
