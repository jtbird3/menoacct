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
    {'name': 'Elle Morris-Benedict', 'phone': '9197174610',  'email': 'ellemb@berkeley.edu'},
    {'name': 'Jeremy Koren',         'phone': '6505612919'},
    {'name': 'Anne Traum',           'phone': '7025219959'},
    {'name': 'Cindy Traum',          'phone': '6507043165'},
    {'name': 'Lauren',               'phone': '6507043655'},
    {'name': 'Aaron Slighitng',      'phone': '7022182167'},
    {'name': 'Sacha Birdsong',       'phone': '7028900737'},
    {'name': 'Nora',                 'phone': '2403932021'},
    {'name': 'Katelyn Kreager',      'phone': '7253041214'},
    {'name': 'Rex',                  'phone': '7024191079'},
    {'name': 'John Wherry',          'phone': '7024608841'},
    {'name': 'Karl',                 'phone': ''},           # no phone on form
    {'name': 'Kyla Fisher',          'phone': '2407235522'},
    {'name': 'Quintin Leger',        'phone': '5057186859',  'email': 'qleger@gmail.com'},
    {'name': 'Samuel Housley',       'phone': '4439262835'},
    {'name': 'Umut Can Atabay',      'phone': '4434812727',  'email': 'ucatabay@sjc.edu'},
    {'name': 'Josse Hosmer',         'phone': '9178627114'},
    {'name': 'jacob',                'phone': '2028766301',  'email': 'jtbird3@gmail.com'},
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
