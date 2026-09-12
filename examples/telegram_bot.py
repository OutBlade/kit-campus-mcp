#!/usr/bin/env python3
"""Telegram bot that messages you when a KIT exam result appears.

Two ways to run it:

    python examples/telegram_bot.py --once          one poll, then exit
    python examples/telegram_bot.py --interval 1800 stay running, poll every 30 min

`--once` is meant for Windows Task Scheduler; the long-running mode also answers
commands in the chat (/noten, /pruefungen, /status, /modul <id>).

Needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID next to the KIT credentials.
Uses httpx directly, so there is no extra dependency beyond the package itself.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kit_campus_mcp.client import KitCampusClient
from kit_campus_mcp.auth import KitError, KitTemporaryError
from kit_campus_mcp.config import load_settings
from kit_campus_mcp.watch import (
    EXAM_KEY,
    EXAM_WATCHED,
    GRADE_KEY,
    GRADE_WATCHED,
    SnapshotStore,
    check,
    format_grade_change,
)

API = "https://api.telegram.org/bot{token}/{method}"
HELP = (
    "KIT Campus Bot\n"
    "/noten - alle Ergebnisse\n"
    "/pruefungen - angemeldete Pruefungen\n"
    "/status - Login und Zusammenfassung\n"
    "/modul <Kennung> - Modul mit Pruefungsterminen\n"
    "/hilfe - diese Uebersicht"
)


class Telegram:
    """The three Bot API calls this script needs.

    `chat_id` may be a comma-separated list, so a study group all working
    towards the same exams gets the same alerts. Put the bot in a Telegram group
    and use the group's id (negative number) to reach everyone at once.
    """

    def __init__(self, token: str, chat_id: str) -> None:
        self.token = token
        self.recipients = [c.strip() for c in chat_id.split(",") if c.strip()]
        self.chat_id = self.recipients[0] if self.recipients else ""
        self._client = httpx.AsyncClient(timeout=70)

    async def close(self) -> None:
        await self._client.aclose()

    async def send(self, text: str, chat_id: str | None = None) -> None:
        """Send a message, failing loudly.

        A notifier that swallows delivery errors is worse than one that crashes:
        the run would go green while the message never arrived.
        """
        targets = [chat_id] if chat_id else self.recipients
        for target in targets:
            for chunk in _split(text):
                response = await self._client.post(
                    API.format(token=self.token, method="sendMessage"),
                    json={
                        "chat_id": target,
                        "text": chunk,
                        "disable_web_page_preview": True,
                    },
                )
                if response.status_code != 200 or not response.json().get("ok"):
                    raise RuntimeError(
                        f"Telegram refused the message to {target} "
                        f"(HTTP {response.status_code}): {response.text[:200]}"
                    )

    async def updates(self, offset: int, timeout: int = 30) -> list[dict]:
        response = await self._client.get(
            API.format(token=self.token, method="getUpdates"),
            params={"offset": offset, "timeout": timeout},
        )
        response.raise_for_status()
        return response.json().get("result", [])


def _offset_file() -> Path:
    return load_settings().state_dir / "telegram.json"


def read_offset() -> int:
    """Last Telegram update already handled.

    Kept on disk so a scheduled run does not answer the same command again on
    every invocation.
    """
    path = _offset_file()
    if not path.exists():
        return 0
    try:
        return int(json.loads(path.read_text(encoding="utf-8")).get("offset", 0))
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        return 0


def write_offset(offset: int) -> None:
    path = _offset_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"offset": offset}), encoding="utf-8")


def _split(text: str, limit: int = 3900) -> list[str]:
    """Telegram rejects messages over 4096 characters."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > limit:
            chunks.append(current)
            current = ""
        current += line
    if current:
        chunks.append(current)
    return chunks


async def poll_kit() -> list[str]:
    """Poll KIT once and return the notification lines for what changed."""
    settings = load_settings()
    store = SnapshotStore(settings.snapshot_file)
    messages: list[str] = []

    async with KitCampusClient(settings) as client:
        grades = await client.get_grades()
        result = check(store, "grades", grades["results"], GRADE_KEY, GRADE_WATCHED)
        if result["first_run"]:
            print(f"Baseline stored ({result['count']} results); staying quiet this run.")
        else:
            messages += [
                format_grade_change(change, settings.language) for change in result["changes"]
            ]
            if result["changes"]:
                messages.append(
                    f"\nGesamt: {grades['passed']}/{grades['count']} bestanden, "
                    f"{grades['credits_earned']} LP, Schnitt {grades['average']}"
                )
        try:
            exams = await client.list_registered_exams()
        except Exception as exc:  # noqa: BLE001 - exam list is a nice-to-have
            print(f"Could not read exam registrations: {exc}")
        else:
            exam_result = check(
                store, "registered_exams", exams["entries"], EXAM_KEY, EXAM_WATCHED
            )
            if not exam_result["first_run"]:
                for change in exam_result["changes"]:
                    entry = change["entry"]
                    label = "Neue Anmeldung" if change["kind"] == "new" else "Geaendert"
                    messages.append(f"{label}: {entry.get('title', '')}")
    return messages


async def handle_command(text: str) -> str:
    """Answer one chat command."""
    parts = text.strip().split()
    command = parts[0].lower().lstrip("/").split("@")[0]
    argument = " ".join(parts[1:])

    if command in {"start", "hilfe", "help"}:
        return HELP

    async with KitCampusClient() as client:
        if command in {"noten", "grades"}:
            data = await client.get_grades()
            lines = [
                (
                    f"{data['passed']}/{data['count']} bestanden, "
                    f"{data['credits_earned']} LP, Schnitt {data['average']}"
                ),
                "",
            ]
            for result in data["results"]:
                mark = {"passed": "+", "failed": "-", "open": "?"}.get(result["outcome"], " ")
                lines.append(
                    f"{mark} {result['grade_raw'] or '--':>4}  {result['title']}"
                )
            return "\n".join(lines)

        if command in {"pruefungen", "exams"}:
            data = await client.list_registered_exams()
            if not data["entries"]:
                return "Keine Pruefungsanmeldungen gefunden."
            lines = []
            for entry in data["entries"]:
                lines.append(f"- {entry.get('title', '')} {entry.get('status', '')}".rstrip())
                for date in entry.get("dates", []):
                    lines.append(f"    {date.get('raw', '')}")
            return "\n".join(lines)

        if command == "status":
            programs = await client.discover_programs()
            data = await client.get_grades()
            return (
                f"Login ok.\n"
                f"Studiengang: {programs[0]['title']}\n"
                f"{data['passed']}/{data['count']} bestanden, "
                f"{data['credits_earned']} LP, Schnitt {data['average']}"
            )

        if command in {"modul", "module"}:
            if not argument:
                return "Bitte eine Kennung angeben, z.B. /modul M-ETIT-101156"
            module = await client.get_module(argument)
            lines = [module["title"] or argument]
            if module["credits"]:
                lines.append(f"{module['credits']} LP")
            for exam in module["exams"]:
                lines.append(f"\nPruefung: {exam.get('title', '')}")
                for date in exam.get("dates", []):
                    lines.append(f"  {date.get('raw', '')}")
            for lecture in module["lectures"]:
                lines.append(f"\n{lecture.get('title', '')}")
                for date in lecture.get("dates", [])[:3]:
                    lines.append(f"  {date.get('raw', '')}")
            return "\n".join(lines)

    return f"Unbekannter Befehl: {command}\n\n{HELP}"


async def drain_commands(bot: Telegram) -> int:
    """Answer whatever commands arrived since the last run, then return.

    This is what makes chat commands work on a scheduled host: there is no
    long-running process, so each run picks up the backlog instead.
    """
    offset = read_offset()
    answered = 0
    try:
        updates = await bot.updates(offset, timeout=0)
    except httpx.HTTPError as exc:
        print(f"Could not read Telegram updates: {exc}")
        return 0
    for update in updates:
        offset = update["update_id"] + 1
        message = update.get("message") or {}
        text = message.get("text")
        chat_id = str((message.get("chat") or {}).get("id", ""))
        # Only configured chats may query; /noten shows your grades.
        if not text or not text.startswith("/") or chat_id not in bot.recipients:
            continue
        try:
            reply = await handle_command(text)
        except Exception as exc:  # noqa: BLE001 - report back to the chat
            reply = f"Fehler: {type(exc).__name__}: {exc}"
        await bot.send(reply, chat_id)
        answered += 1
    if updates:
        write_offset(offset)
    return answered


async def command_loop(bot: Telegram) -> None:
    """Long-poll Telegram and answer commands."""
    offset = read_offset()
    while True:
        try:
            for update in await bot.updates(offset):
                offset = update["update_id"] + 1
                write_offset(offset)
                message = update.get("message") or {}
                text = message.get("text")
                chat_id = str((message.get("chat") or {}).get("id", ""))
                if not text or not text.startswith("/"):
                    continue
                if chat_id not in bot.recipients:
                    print(f"Ignoring command from unknown chat {chat_id}")
                    continue
                try:
                    reply = await handle_command(text)
                except Exception as exc:  # noqa: BLE001 - report back to the chat
                    reply = f"Fehler: {type(exc).__name__}: {exc}"
                await bot.send(reply, chat_id)
        except httpx.HTTPError as exc:
            print(f"Telegram polling error: {exc}")
            await asyncio.sleep(10)


async def watch_loop(bot: Telegram, interval: int) -> None:
    """Poll KIT on an interval and push whatever changed."""
    while True:
        try:
            messages = await poll_kit()
            if messages:
                await bot.send("\n".join(messages))
                print(f"Sent {len(messages)} notification lines.")
            else:
                print("No changes.")
        except Exception as exc:  # noqa: BLE001 - a bad poll must not kill the bot
            print(f"KIT poll failed: {type(exc).__name__}: {exc}")
        await asyncio.sleep(interval)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument(
        "--check-only", action="store_true",
        help="verify KIT reads without Telegram or snapshot changes",
    )
    parser.add_argument(
        "--ping",
        action="store_true",
        help="send the current standing to the chat, to prove the pipeline works",
    )
    parser.add_argument("--interval", type=int, default=1800, help="seconds between polls")
    parser.add_argument(
        "--no-commands", action="store_true", help="only notify, do not answer chat commands"
    )
    args = parser.parse_args()

    load_settings()  # loads .env so the Telegram values below are available
    if args.check_only:
        async with KitCampusClient() as client:
            await client.get_grades()
            await client.list_registered_exams()
        print("KIT check succeeded: study tree and exam registrations read. No notifications or snapshot changes.")
        return 0
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID (see .env.example).")
        return 2

    bot = Telegram(token, chat_id)
    try:
        if args.ping:
            await bot.send(await handle_command("/status"))
            print("Ping sent.")
            if not args.once:
                return 0

        if args.once:
            messages = await poll_kit()
            if messages:
                await bot.send("\n".join(messages))
                print(f"Sent {len(messages)} notification lines.")
            else:
                print("No changes.")
            if not args.no_commands:
                answered = await drain_commands(bot)
                print(f"Answered {answered} pending command(s).")
            return 0

        print(f"Watching KIT every {args.interval}s. Ctrl+C to stop.")
        tasks = [watch_loop(bot, args.interval)]
        if not args.no_commands:
            tasks.append(command_loop(bot))
        await asyncio.gather(*tasks)
    finally:
        await bot.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except KeyboardInterrupt:
        sys.exit(130)
    except KitTemporaryError as exc:
        print(f"KIT check deferred: {exc}", file=sys.stderr)
        sys.exit(75)  # EX_TEMPFAIL: no successful poll, retry next schedule.
    except KitError as exc:
        print(f"KIT check failed: {exc}", file=sys.stderr)
        sys.exit(1)
