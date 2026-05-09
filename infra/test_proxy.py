"""Smoke test: launch Playwright through Oxylabs DK and confirm the exit
IP geolocates to Denmark.

Run with:  /opt/ugoingviral/bin/python3 /root/test_proxy.py
"""
import json
import os
import re
import sys

try:
    from dotenv import load_dotenv
    load_dotenv('/root/ugoingviral-backend/.env')
except Exception:
    pass

OXY_HOST = 'dk-pr.oxylabs.io'
OXY_PORT = 19000
OXY_USER = 'customer-ugoingviral_1reRN'
OXY_PASS = os.getenv('OXYLABS_PASSWORD', 'Ugoingviral2026:')

from playwright.sync_api import sync_playwright

print(f'[test] using {OXY_HOST}:{OXY_PORT} as {OXY_USER}')

with sync_playwright() as p:
    browser = p.chromium.launch(
        headless=True,
        proxy={
            'server':   f'{OXY_HOST}:{OXY_PORT}',
            'username': OXY_USER,
            'password': OXY_PASS,
        },
        args=['--no-sandbox', '--disable-dev-shm-usage'],
    )
    ctx = browser.new_context(ignore_https_errors=True)
    page = ctx.new_page()
    page.goto('https://ip.oxylabs.io/location', timeout=30_000)
    body = page.locator('body').inner_text(timeout=10_000)
    browser.close()

m = re.search(r'\{.*\}', body, flags=re.S)
if not m:
    print('[FAIL] no JSON in response')
    print(body[:500])
    sys.exit(1)

data = json.loads(m.group(0))
ip = data.get('ip', '?')
providers = data.get('providers') or {}
countries = [(v or {}).get('country', '').upper() for v in providers.values()]
unique = sorted({c for c in countries if c})

print(f'[test] exit IP : {ip}')
print(f'[test] verdict : {countries}')

if unique == ['DK']:
    print('[PASS] proxy exits in Denmark across all geo providers')
    sys.exit(0)

print(f'[FAIL] expected only DK, got {unique}')
sys.exit(2)
