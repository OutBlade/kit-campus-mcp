"""Regression checks for the September 2026 scheduled-run failures."""

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kit_campus_mcp.auth import KitError, KitRequestError, KitSession, safe_url
from kit_campus_mcp.client import KitCampusClient, _url
from kit_campus_mcp.config import CAMPUS_BASE, SERVICES, Settings


TREE = """<table id="specific-contract-tree">
<tr class="product hierarchy1"><td></td><td>Example degree</td><td></td><td></td>
<td>2,0</td><td></td><td>6</td><td>180</td></tr>
<tr class="brick hierarchy2"><td></td><td>T-TEST-100 - Example exam</td><td>Exam</td>
<td>passed</td><td>2,0 1</td><td>2026-09-01</td><td>6</td><td>6</td></tr>
</table>"""
SENSITIVE_URL = "https://cascampus.studium.kit.edu/campus/student/contractview.asp?login-token=SECRET&login-ts=42"


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
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
        with self.assertRaisesRegex(KitRequestError, "after 3 attempt") as caught:
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
                with self.assertRaisesRegex(KitRequestError, f"HTTP {status}"):
                    await self.session.get(SENSITIVE_URL)
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


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
