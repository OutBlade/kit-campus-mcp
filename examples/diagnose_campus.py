"""Read-only comparison with the portal's current iframe request."""
import asyncio
from urllib.parse import urlencode, urlparse

from kit_campus_mcp.client import KitCampusClient
from kit_campus_mcp.auth import _with_token
from kit_campus_mcp.config import CAMPUS_BASE, PORTAL_BASE
from kit_campus_mcp.parsers import parse_study_tree
from bs4 import BeautifulSoup


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
            except Exception as exc:
                print(f'{name}: {type(exc).__name__}', flush=True)


asyncio.run(main())
