"""Prevent browser SSO documents from escaping into the HTTP form walker."""
import http.cookiejar
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from kit_campus_mcp.browser import BrowserReader

class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_for_study_tree_and_copies_completed_session_cookies(self):
        jar = http.cookiejar.CookieJar()
        reader = BrowserReader(jar)
        reader._start = AsyncMock()
        frame = MagicMock()
        frame.url = "https://cascampus.studium.kit.edu/campus/student/contractview.asp?gguid=example"
        frame.wait_for_load_state = AsyncMock()
        tree = '<table id="specific-contract-tree"><tr></tr></table>'
        frame.content = AsyncMock(side_effect=[
            '<form><input name="SAMLResponse" value="SECRET"></form>', tree,
        ])
        reader._page = MagicMock()
        reader._page.frames = [frame]
        async def navigate(*args, **kwargs):
            reader._responses[frame] = MagicMock(status=200)
        reader._page.goto = AsyncMock(side_effect=navigate)
        reader._context = MagicMock()
        reader._context.cookies = AsyncMock(return_value=[{
            'name': 'ASPSESSIONID_TEST', 'value': 'test-session',
            'domain': 'cascampus.studium.kit.edu', 'path': '/',
            'secure': True, 'httpOnly': True, 'expires': -1,
        }])
        with patch('kit_campus_mcp.browser.asyncio.sleep', new_callable=AsyncMock):
            status, html, final = await reader.get(frame.url)
        self.assertEqual(status, 200)
        self.assertEqual(html, tree)
        self.assertEqual(frame.content.await_count, 2)
        [cookie] = list(jar)
        self.assertEqual(cookie.value, 'test-session')
        self.assertTrue(cookie.secure)
        self.assertEqual(cookie.domain, 'cascampus.studium.kit.edu')
