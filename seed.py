"""
Fill in USERS and ADMIN_KEY, then run:  python seed.py
It prints each person's invite link. Text it to them.
"""
import sys
import json
import urllib.request

# ── Your Railway ADMIN_KEY env var ────────────────────────────────────────────
ADMIN_KEY = 'admin'          # change this to match your Railway ADMIN_KEY env var

BASE_URL  = 'https://menoacct-production.up.railway.app'

# ── Paste your signups here ───────────────────────────────────────────────────
USERS = [
    # {'name': 'Jane Doe',  'phone': '555-000-0001'},
    # {'name': 'John Smith','phone': '555-000-0002'},
]
# ─────────────────────────────────────────────────────────────────────────────

if not USERS:
    print('No users to seed. Fill in the USERS list first.')
    sys.exit(0)

payload = json.dumps({'key': ADMIN_KEY, 'users': USERS}).encode()
req = urllib.request.Request(
    f'{BASE_URL}/admin/seed',
    data=payload,
    headers={'Content-Type': 'application/json'},
    method='POST'
)

try:
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
except urllib.error.HTTPError as e:
    print(f'Error {e.code}: {e.read().decode()}')
    sys.exit(1)

print()
for r in data['results']:
    print(f"{r['name']:20s}  [{r['status']}]")
    print(f"  {r['link']}")
    print()
