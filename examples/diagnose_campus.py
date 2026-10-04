"""Read-only comparison with the portal's current iframe request."""
import asyncio
import re
from urllib.parse import urlencode, urlparse

from kit_campus_mcp.client import KitCampusClient
from kit_campus_mcp.auth import _with_token, _needs_login
from kit_campus_mcp.config import CAMPUS_BASE, PORTAL_BASE
from kit_campus_mcp.parsers import parse_study_tree
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright


async def main():
    async with KitCampusClient() as client:
        guid = await client._program_guid(None)
        term = await client._term_guid()
        token = await client.session.token()
        path = '/campus/student/contractview.asp?' + urlencode({'gguid': guid})
        full = path + '&' + urlencode({'pguid': guid, 'tguid': term, 'lang': 'de'})
        variants = {
            'direct': _with_token(CAMPUS_BASE + path, token),
            'direct-selectors': _with_token(CAMPUS_BASE + full, token),
            'portal': PORTAL_BASE + '/redirect.php?' + urlencode({'system': 'cascampus', 'url': path}),
            'portal-selectors': PORTAL_BASE + '/redirect.php?' + urlencode({'system': 'cascampus', 'url': full}),
            'no-guid': _with_token(CAMPUS_BASE + '/campus/student/contractview.asp', token),
            'study-progress': _with_token(CAMPUS_BASE + '/campus/student/courseofstudies.asp?' + urlencode({'gguid': guid}), token),
            'registered-exams': _with_token(CAMPUS_BASE + '/campus/student/registrationlist.asp?type=exam&filter=registered', token),
            'backend-login': _with_token(CAMPUS_BASE + '/campus/login/login.asp', token),
            'session-only': CAMPUS_BASE + full,
        }
        for name, url in variants.items():
            try:
                response = await client.session._client.get(url, timeout=30)
                print(f'{name}: HTTP {response.status_code}; tree_rows={len(parse_study_tree(response.text))}; final_path={urlparse(str(response.url)).path}', flush=True)
                text = BeautifulSoup(response.text, 'html.parser').get_text(' ', strip=True).lower()
                indicators = [word for word in ('service unavailable', 'maintenance', 'wartung', 'database', 'sql', 'iis', 'runtime error', 'temporarily', 'overloaded') if word in text]
                print(f'{name}: bytes={len(response.content)}; indicators={indicators}', flush=True)
                if response.status_code == 503:
                    for secret in (token.token_a, token.token_b, token.username, token.firstname, token.lastname, token.matriculation_number, guid, term, client.settings.password):
                        if secret:
                            text = text.replace(str(secret).lower(), '<redacted>')
                    text = re.sub(r'https?://\S+', '<url>', text)
                    text = re.sub(r'[a-z0-9+/=_%-]{20,}', '<redacted>', text)
                    print(f'{name}: error_text={text[:500]}', flush=True)
            except Exception as exc:
                print(f'{name}: {type(exc).__name__}', flush=True)
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            context = await browser.new_context(locale='de-DE')
            cookies = []
            for c in client.session._client.cookies.jar:
                entry = {'name': c.name, 'value': c.value, 'domain': c.domain, 'path': c.path or '/', 'secure': c.secure}
                if c.expires and c.expires > 0:
                    entry['expires'] = c.expires
                cookies.append(entry)
            await context.add_cookies(cookies)
            page = await context.new_page()
            try:
                await page.goto(variants['direct-selectors'], wait_until='domcontentloaded', timeout=60000)
                try:
                    await page.wait_for_function("!document.body.innerText.toLowerCase().includes('verifying your browser')", timeout=45000)
                except Exception:
                    pass
                await page.wait_for_timeout(3000)
                html = await page.content()
                if not parse_study_tree(html):
                    await page.goto(variants['portal-selectors'], wait_until='domcontentloaded', timeout=60000)
                    await page.wait_for_timeout(5000)
                    html = await page.content()
                print(f"browser: tree_rows={len(parse_study_tree(html))}; challenge={'verifying your browser' in html.lower()}; final_path={urlparse(page.url).path}", flush=True)
                soup = BeautifulSoup(html, 'html.parser')
                print(f"browser: needs_login={_needs_login(html, page.url)}; tables={[t.get('id') for t in soup.find_all('table')][:10]}; row_classes={sorted({c for r in soup.find_all('tr') for c in r.get('class', [])})[:20]}", flush=True)
                text = soup.get_text(' ', strip=True).lower()
                for secret in (token.token_a, token.token_b, token.username, token.firstname, token.lastname, token.matriculation_number, guid, term, client.settings.password):
                    if secret:
                        text = text.replace(str(secret).lower(), '<redacted>')
                text = re.sub(r'https?://\S+', '<url>', text)
                text = re.sub(r'[a-z0-9+/=_%-]{20,}', '<redacted>', text)
                if not parse_study_tree(html):
                    print(f'browser: diagnostic_text={text[:500]}', flush=True)
            except Exception as exc:
                print(f'browser: {type(exc).__name__}', flush=True)
            await browser.close()


asyncio.run(main())
