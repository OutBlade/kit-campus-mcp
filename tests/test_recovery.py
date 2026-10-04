"""Regression checks for the September 2026 scheduled-run failures."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kit_campus_mcp.auth import KitError, KitRequestError, KitSession, KitTemporaryError, page_url, safe_url
from kit_campus_mcp.client import KitCampusClient, _url
from kit_campus_mcp.config import CAMPUS_BASE, SERVICES, Settings
from kit_campus_mcp.parsers import parse_grade, parse_study_tree


TREE = """<table id="specific-contract-tree">
<tr class="product hierarchy1"><td></td><td>Example degree</td><td></td><td></td>
<td>2,0</td><td></td><td>6</td><td>180</td></tr>
<tr class="brick hierarchy2"><td></td><td>T-TEST-100 - Example exam</td><td>Exam</td>
<td>passed</td><td>2,0 1</td><td>2026-09-01</td><td>6</td><td>6</td></tr>
</table>"""
SENSITIVE_URL = "https://cascampus.studium.kit.edu/campus/student/contractview.asp?login-token=SECRET&login-ts=42"


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    def test_parenthesized_provisional_grades_count_as_passed(self):
        self.assertEqual(parse_grade("(3,0)"), 3.0)
        self.assertEqual(parse_grade("(4,0)"), 4.0)
        self.assertEqual(parse_grade("(5,0)"), 5.0)
        html = """<table id="specific-contract-tree">
        <tr class="brick hierarchy2"><td></td><td>T-TEST-100 - Provisional</td>
        <td>Exam</td><td>incomplete</td><td>(3,0)</td><td></td><td>0</td><td>6</td></tr>
        </table>"""
        [result] = parse_study_tree(html)
        self.assertEqual(result.grade, 3.0)
        self.assertTrue(result.has_result)
        self.assertEqual(result.outcome, "passed")

    async def test_parenthesized_grade_does_not_invent_credits(self):
        html = """<table id="specific-contract-tree">
        <tr class="product hierarchy1"><td></td><td>Example degree</td><td></td><td></td>
        <td></td><td></td><td>35</td><td>180</td></tr>
        <tr class="brick hierarchy2"><td></td><td>T-TEST-100 - Provisional</td>
        <td>Exam</td><td>incomplete</td><td>(3,0)</td><td></td><td>0</td><td>8</td></tr>
        </table>"""
        nodes = parse_study_tree(html)
        client = KitCampusClient(self.settings)
        client._study_tree = AsyncMock(return_value=(nodes, "guid", "url"))
        try:
            data = await client.get_grades()
        finally:
            await client.close()
        self.assertEqual(data["passed"], 1)
        self.assertEqual(data["credits_earned"], 35.0)
        self.assertNotIn("credits_provisional", data)
        self.assertEqual(data["results"][0]["credits"], 0.0)

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.settings = Settings(None, None, Path(self.temp.name), 1, "de", "test-program")
        self.session = KitSession(self.settings)
        await self.session._client.aclose()
        self.sleep = patch("kit_campus_mcp.auth.asyncio.sleep", new_callable=AsyncMock)
        self.mock_sleep = self.sleep.start()

    async def asyncTearDown(self):
        self.sleep.stop()
        await self.session.close()
        self.temp.cleanup()

    def transport(self, handler):
        self.session._client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True,
        )

    async def test_timeout_then_success(self):
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                raise httpx.ReadTimeout("secret must not be logged", request=request)
            return httpx.Response(200, text="recovered")
        self.transport(handler)
        with self.assertLogs("kit_campus_mcp.auth", level="WARNING") as logs:
            html, _ = await self.session.get(SENSITIVE_URL)
        self.assertEqual(html, "recovered")
        self.assertEqual(len(calls), 2)
        self.assertNotIn("SECRET", " ".join(logs.output))
        self.assertNotIn("secret must", " ".join(logs.output))

    async def test_temporary_statuses_recover(self):
        for status in (408, 429, 500, 502, 503, 504):
            with self.subTest(status=status):
                calls = []
                def handler(request):
                    calls.append(request)
                    return httpx.Response(status if len(calls) == 1 else 200, text="ok")
                await self.session._client.aclose()
                self.transport(handler)
                html, _ = await self.session.get(SENSITIVE_URL)
                self.assertEqual(html, "ok")
                self.assertEqual(len(calls), 2)

    async def test_persistent_outage_fails_after_three_attempts(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(503, text="SECRET")
        self.transport(handler)
        with self.assertRaisesRegex(KitTemporaryError, "after 3 attempt") as caught:
            await self.session.get(SENSITIVE_URL)
        self.assertEqual(len(calls), 3)
        self.assertNotIn("SECRET", str(caught.exception))
        self.assertIn("HTTP 503", str(caught.exception))

    async def test_http_errors_do_not_become_empty_html(self):
        for status in (400, 401, 403, 404):
            with self.subTest(status=status):
                calls = []
                def handler(request):
                    calls.append(request)
                    return httpx.Response(status, text="error page")
                await self.session._client.aclose()
                self.transport(handler)
                with self.assertRaisesRegex(KitRequestError, f"HTTP {status}") as caught:
                    await self.session.get(SENSITIVE_URL)
                self.assertNotIsInstance(caught.exception, KitTemporaryError)
                self.assertEqual(len(calls), 1)

    async def test_post_is_not_replayed(self):
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout("timeout", request=request)
        self.transport(handler)
        with self.assertRaises(KitRequestError):
            await self.session.post(SENSITIVE_URL, {"j_password": "SECRET"})
        self.assertEqual(len(calls), 1)
        self.mock_sleep.assert_not_awaited()

    async def test_retry_after_is_bounded(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429 if len(calls) == 1 else 200,
                                  headers={"Retry-After": "999999"})
        self.transport(handler)
        await self.session.get(SENSITIVE_URL)
        self.mock_sleep.assert_awaited_once_with(30.0)

    async def test_missing_tree_refreshes_and_recovers(self):
        async with KitCampusClient(self.settings) as client:
            client.session.fetch_authenticated = AsyncMock(side_effect=[
                ("<html>Temporary error</html>", SENSITIVE_URL), (TREE, SENSITIVE_URL),
            ])
            client.session.token = AsyncMock()
            result = await client.get_grades()
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["results"][0]["grade"], 2.0)
            client.session.token.assert_awaited_once_with(force=True)

    async def test_missing_tree_stays_an_error_and_hides_tokens(self):
        async with KitCampusClient(self.settings) as client:
            client.session.fetch_authenticated = AsyncMock(
                return_value=("<table id='error'><tr class='message'></tr></table>", SENSITIVE_URL),
            )
            client.session.token = AsyncMock()
            with self.assertRaisesRegex(KitError, "No study tree found after") as caught:
                await client.get_grades()
            self.assertNotIn("SECRET", str(caught.exception))
            self.assertIn("Tables: ['error']", str(caught.exception))
            self.assertEqual(client.session.fetch_authenticated.await_count, 2)

    async def test_authenticated_result_url_hides_token(self):
        self.session.token = AsyncMock(return_value=type("Token", (), {"token_a": "SECRET", "timestamp": 1})())
        self.session.get = AsyncMock(return_value=(TREE, SENSITIVE_URL))
        html, url = await self.session.fetch_authenticated(SENSITIVE_URL.split("?")[0])
        self.assertEqual(html, TREE)
        self.assertNotIn("SECRET", url)

    async def test_javascript_challenge_uses_browser_without_repeating_http(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(503, text='Verifying your browser... JavaScript is required to continue.')
        self.transport(handler)
        self.session._browser_get = AsyncMock(return_value=httpx.Response(
            200, text=TREE, request=httpx.Request('GET', SENSITIVE_URL)
        ))
        html, _ = await self.session.get(SENSITIVE_URL)
        self.assertEqual(html, TREE)
        self.assertEqual(len(calls), 1)
        self.session._browser_get.assert_awaited_once()
        self.mock_sleep.assert_not_awaited()

    async def test_challenge_does_not_replay_post_in_browser(self):
        self.transport(lambda request: httpx.Response(
            503, text='Verifying your browser... JavaScript is required to continue.'
        ))
        self.session._browser_get = AsyncMock()
        with self.assertRaises(KitTemporaryError):
            await self.session.post(SENSITIVE_URL, {'password': 'SECRET'})
        self.session._browser_get.assert_not_awaited()

    async def test_transient_saml_error_restarts_with_a_fresh_login_flow(self):
        self.session.settings = Settings("ab1234", "test-password", Path(self.temp.name), 1, "de", None)
        self.session.get = AsyncMock(return_value=("login page", "https://campus.kit.edu/login"))
        self.session.walk_sso = AsyncMock(side_effect=[
            KitTemporaryError(
                "POST https://idp.scc.kit.edu/Shibboleth.sso/SAML2/POST "
                "failed after 1 attempt(s) (HTTP 500). No result data was read."
            ),
            ("student page", "https://campus.kit.edu/student"),
        ])
        with patch.object(self.session, "save_cookies"):
            result = await self.session.login()
        self.assertEqual(result, "https://campus.kit.edu/student")
        self.assertEqual(self.session.get.await_count, 2)
        self.assertEqual(self.session.walk_sso.await_count, 2)
        self.mock_sleep.assert_awaited_once_with(2)

    async def test_browser_failure_hides_sensitive_error_details(self):
        self.session._browser = AsyncMock()
        self.session._browser.get.side_effect = RuntimeError('SECRET')
        with self.assertRaises(KitRequestError) as caught:
            await self.session._browser_get(SENSITIVE_URL)
        self.assertNotIn('SECRET', str(caught.exception))


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_persistent_timeout_is_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(None, None, Path(directory), 1, "de", None)
            async with KitSession(settings) as session:
                await session._client.aclose()
                def handler(request):
                    raise httpx.ReadTimeout("SECRET", request=request)
                session._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
                with patch("kit_campus_mcp.auth.asyncio.sleep", new_callable=AsyncMock), \
                     self.assertRaises(KitTemporaryError) as caught:
                    await session.get(SENSITIVE_URL)
                self.assertIn("ReadTimeout", str(caught.exception))
                self.assertNotIn("SECRET", str(caught.exception))

    async def test_check_only_does_not_touch_telegram_or_snapshots(self):
        from examples import telegram_bot as bot
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "snapshots.json"
            snapshot.write_text('{"sentinel": true}', encoding="utf-8")
            client = AsyncMock()
            with patch.object(sys, "argv", ["telegram_bot.py", "--check-only"]), \
                 patch.object(bot, "load_settings"), \
                 patch.object(bot, "KitCampusClient") as factory, \
                 patch.object(bot, "Telegram") as telegram:
                factory.return_value.__aenter__ = AsyncMock(return_value=client)
                factory.return_value.__aexit__ = AsyncMock(return_value=False)
                self.assertEqual(await bot.main(), 0)
                client.get_grades.assert_awaited_once()
                client.list_registered_exams.assert_awaited_once()
                telegram.assert_not_called()
            self.assertEqual(snapshot.read_text(encoding="utf-8"), '{"sentinel": true}')

    async def test_failed_poll_preserves_previous_results(self):
        from examples import telegram_bot as bot
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(None, None, Path(directory), 1, "de", None)
            original = '{"grades": {"entries": [{"code": "example"}]}}'
            settings.snapshot_file.write_text(original, encoding="utf-8")
            client = AsyncMock()
            client.get_grades.side_effect = KitError("No study tree")
            with patch.object(bot, "load_settings", return_value=settings), \
                 patch.object(bot, "KitCampusClient") as factory:
                factory.return_value.__aenter__ = AsyncMock(return_value=client)
                factory.return_value.__aexit__ = AsyncMock(return_value=False)
                with self.assertRaises(KitError):
                    await bot.poll_kit()
            self.assertEqual(settings.snapshot_file.read_text(encoding="utf-8"), original)


class EndpointTests(unittest.TestCase):
    def test_canonical_backend(self):
        self.assertEqual(CAMPUS_BASE, "https://cascampus.studium.kit.edu")
        self.assertEqual(_url("study_tree", pguid="example"),
                         CAMPUS_BASE + "/campus/student/contractview.asp?gguid=example")
        self.assertTrue(all(url.startswith(CAMPUS_BASE + "/") for url in SERVICES.values()))

    def test_diagnostic_url_drops_credentials_and_fragments(self):
        self.assertEqual(safe_url("https://user:password@host/path?token=SECRET#SECRET"),
                         "https://host/path")

    def test_result_links_keep_page_selectors(self):
        self.assertEqual(page_url("https://host/path?gguid=example&login-token=SECRET&login-ts=1"),
                         "https://host/path?gguid=example")


if __name__ == "__main__":
    unittest.main()
