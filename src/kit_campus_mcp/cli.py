"""Command line interface, mainly for setup and troubleshooting.

`kit-campus dump <path>` is the important one: it saves the raw HTML of a
Campus page next to the tables this package parsed out of it, which is what you
need to adjust the parsers if KIT changes a layout.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .client import KitCampusClient
from .config import load_settings
from .parsers import page_title, parse_tables
from .watch import (
    EXAM_KEY,
    EXAM_WATCHED,
    GRADE_KEY,
    GRADE_WATCHED,
    SnapshotStore,
    check,
    format_grade_change,
)


def _use_utf8_output() -> None:
    """Print umlauts correctly on a Windows console, whose default is cp1252."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def _print(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2, default=str))


async def cmd_login(_: argparse.Namespace) -> int:
    settings = load_settings()
    if not settings.has_credentials:
        print("KIT_USERNAME / KIT_PASSWORD are not set. Copy .env.example to .env.")
        return 2
    async with KitCampusClient(settings) as client:
        who = await client.whoami()
        programs = await client.discover_programs()
        terms = await client.list_terms()
    print(f"Login OK: {who['name']} ({who['username']}), Matrikelnummer {who['matriculation_number']}")
    print("Degree programmes:")
    for program in programs:
        print(f"  {program['guid']}  {program['title']}")
    current = next((t for t in terms if t["default"]), None)
    if current:
        print(f"Current term: {current['name']} ({current['guid']})")
    return 0


async def cmd_grades(args: argparse.Namespace) -> int:
    async with KitCampusClient() as client:
        data = await client.get_grades(args.program)
    if args.json:
        _print(data)
        return 0
    print(
        f"{data['passed']}/{data['count']} passed, {data['credits_earned']} ECTS, "
        f"average {data['average']}"
    )
    for result in data["results"]:
        print(
            f"  [{result['outcome']:6}] {result['grade_raw'] or '-':>4}  "
            f"{result['title']} ({result['code'] or ''})"
        )
    return 0


async def cmd_check(args: argparse.Namespace) -> int:
    settings = load_settings()
    store = SnapshotStore(settings.snapshot_file)
    async with KitCampusClient(settings) as client:
        grades = await client.get_grades(args.program)
        result = check(
            store, "grades", grades["results"], GRADE_KEY, GRADE_WATCHED, commit=not args.dry_run
        )
        exams = await client.list_registered_exams()
        exam_result = check(
            store,
            "registered_exams",
            exams["entries"],
            EXAM_KEY,
            EXAM_WATCHED,
            commit=not args.dry_run,
        )
    if result["first_run"]:
        print(f"Baseline stored ({result['count']} results). Next run reports changes.")
        return 0
    changes = result["changes"]
    if not changes and not exam_result["changes"]:
        print(f"No changes ({result['count']} results known).")
        return 0
    for change in changes:
        print(format_grade_change(change, settings.language))
    for change in exam_result["changes"]:
        print(f"Exam registration {change['kind']}: {change['entry'].get('title', '')}")
    return 0


async def cmd_modules(args: argparse.Namespace) -> int:
    async with KitCampusClient() as client:
        data = await client.search_modules(name=args.query, page_size=args.limit)
    if args.json:
        _print(data)
        return 0
    print(f"{data['count']} results (page {data['page']} of {data['total_pages']})")
    for module in data["modules"]:
        print(f"  {module['module_id']:<18} {module['credits'] or '-':>5} LP  {module['title']}")
    return 0


async def cmd_module(args: argparse.Namespace) -> int:
    async with KitCampusClient() as client:
        data = await client.get_module(args.module)
    if args.json:
        _print(data)
        return 0
    print(data["title"])
    if data["exams"]:
        print("\nExams:")
        for exam in data["exams"]:
            print(f"  {exam.get('code', '')} {exam.get('title', '')}")
            for date in exam.get("dates", []):
                print(f"    {date.get('raw', '')}")
    if data["lectures"]:
        print("\nLectures:")
        for lecture in data["lectures"]:
            print(f"  {lecture.get('code', '')} {lecture.get('title', '')}")
            for date in lecture.get("dates", []):
                print(f"    {date.get('raw', '')}")
    return 0


async def cmd_dump(args: argparse.Namespace) -> int:
    """Save a page's raw HTML plus the parsed tables, for parser work."""
    out_dir = Path(args.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    async with KitCampusClient() as client:
        html, final = await client.fetch_raw(args.path, authenticated=not args.public)

    stem = args.path.replace("/", "_").replace("?", "_").replace("&", "_")[:80] or "page"
    html_path = out_dir / f"{stem}.html"
    json_path = out_dir / f"{stem}.tables.json"
    html_path.write_text(html, encoding="utf-8")

    tables = parse_tables(html, final)
    json_path.write_text(
        json.dumps(
            {
                "url": final,
                "title": page_title(html),
                "tables": [
                    {"table_id": t.table_id, "headers": t.headers, "rows": t.dicts()}
                    for t in tables
                ],
            },
            ensure_ascii=False,
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    print(f"URL:    {final}")
    print(f"HTML:   {html_path}  ({len(html)} chars)")
    print(f"Tables: {json_path}  ({len(tables)} tables)")
    for table in tables:
        print(f"  #{table.table_id or '(no id)':<28} {len(table.rows):>4} rows  {table.headers}")
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    settings = load_settings()
    store = SnapshotStore(settings.snapshot_file)
    store.reset(args.watch)
    store.save()
    print(f"Snapshot reset: {args.watch or 'all watches'} ({settings.snapshot_file})")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kit-campus", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("login", help="verify credentials and list degree programmes")

    grades = sub.add_parser("grades", help="print the Notenspiegel")
    grades.add_argument("--program", help="degree programme GUID (0x...)")
    grades.add_argument("--json", action="store_true")

    checkp = sub.add_parser("check", help="report results that changed since the last run")
    checkp.add_argument("--program", help="degree programme GUID (0x...)")
    checkp.add_argument("--dry-run", action="store_true", help="do not update the baseline")

    modules = sub.add_parser("modules", help="search the public module catalogue")
    modules.add_argument("query")
    modules.add_argument("--limit", type=int, default=20)
    modules.add_argument("--json", action="store_true")

    module = sub.add_parser("module", help="show one module with its exam dates")
    module.add_argument("module", help="module id, GUID or URL")
    module.add_argument("--json", action="store_true")

    dump = sub.add_parser("dump", help="save a page's HTML and parsed tables")
    dump.add_argument("path", help="e.g. student/timetable.asp or a full URL")
    dump.add_argument("--out", default="./dumps")
    dump.add_argument("--public", action="store_true", help="do not log in")

    reset = sub.add_parser("reset", help="clear stored snapshots")
    reset.add_argument("--watch", help="only this watch key (grades, registered_exams)")

    return parser


def main() -> None:
    _use_utf8_output()
    parser = build_parser()
    args = parser.parse_args()
    handlers = {
        "login": cmd_login,
        "grades": cmd_grades,
        "check": cmd_check,
        "modules": cmd_modules,
        "module": cmd_module,
        "dump": cmd_dump,
    }
    try:
        if args.command == "reset":
            sys.exit(cmd_reset(args))
        sys.exit(asyncio.run(handlers[args.command](args)))
    except KeyboardInterrupt:
        sys.exit(130)
    except Exception as exc:  # noqa: BLE001 - CLI surface
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
