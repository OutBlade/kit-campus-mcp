#!/usr/bin/env python3
"""MCP server for the KIT Campus portal (campus.studium.kit.edu).

Exposes exam results, exam registrations, study progress, the personal
timetable and the public module/course catalogue as MCP tools.

Catalogue tools work without credentials. Everything tied to an account needs
KIT_USERNAME and KIT_PASSWORD in the environment or in a .env file.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

try:  # MCP SDK 2.x
    from mcp.server import MCPServer as _McpServer
except ImportError:  # MCP SDK 1.x, where the same API is called FastMCP
    from mcp.server.fastmcp import FastMCP as _McpServer

from . import __version__
from .auth import KitAuthRequiredError, KitError, KitLoginError
from .client import KitCampusClient
from .config import load_settings
from .watch import (
    EXAM_KEY,
    EXAM_WATCHED,
    GRADE_KEY,
    GRADE_WATCHED,
    SnapshotStore,
    check,
    format_grade_change,
)

mcp = _McpServer("kit_campus_mcp", version=__version__)

READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": True,
}


class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


class BaseInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="'markdown' for a readable summary, 'json' for the full structured payload",
    )


# --------------------------------------------------------------------- shared


async def _run(action: Callable[[KitCampusClient], Awaitable[Any]]) -> Any:
    """Open a client, run one action, always persist cookies afterwards."""
    client = KitCampusClient()
    try:
        return await action(client)
    finally:
        await client.close()


def _error(exc: Exception) -> str:
    """Turn an exception into an actionable message for the calling agent."""
    if isinstance(exc, KitLoginError):
        return f"Login failed.\n{exc}"
    if isinstance(exc, KitAuthRequiredError):
        return f"Not authenticated.\n{exc}"
    if isinstance(exc, KitError):
        return f"Error: {exc}"
    if isinstance(exc, httpx.TimeoutException):
        return (
            "Error: campus.kit.edu did not respond in time. It is regularly "
            "unavailable at night for maintenance - retry later."
        )
    if isinstance(exc, httpx.HTTPStatusError):
        return f"Error: campus.kit.edu returned HTTP {exc.response.status_code}."
    if isinstance(exc, httpx.HTTPError):
        return f"Error: network problem talking to campus.kit.edu ({type(exc).__name__})."
    return f"Error: unexpected {type(exc).__name__}: {exc}"


def _dump(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def _table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> list[str]:
    """Render rows as a markdown table, skipping columns that are entirely empty."""
    used = [(key, label) for key, label in columns if any(r.get(key) for r in rows)]
    if not used:
        return []
    lines = [
        "| " + " | ".join(label for _, label in used) + " |",
        "|" + "|".join("---" for _ in used) + "|",
    ]
    for row in rows:
        cells = [str(row.get(key, "") or "").replace("|", "/") for key, _ in used]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


# ------------------------------------------------------------ public catalogue


class SearchModulesInput(BaseInput):
    """Input for a module catalogue search."""

    name: str = Field(
        default="",
        description="Words from the module title, e.g. 'Regelungstechnik' or 'Höhere Mathematik'",
        max_length=200,
    )
    module_id: str = Field(
        default="",
        description="Exact module identifier, e.g. 'M-ETIT-101156'",
        max_length=50,
    )
    module_code: str = Field(default="", description="Module code, e.g. 'WW3INGETIT2'", max_length=50)
    credits_min: str = Field(default="", description="Minimum ECTS credits, e.g. '5'", max_length=6)
    credits_max: str = Field(default="", description="Maximum ECTS credits, e.g. '10'", max_length=6)
    page: int = Field(default=1, description="Result page (1-based)", ge=1, le=999)
    page_size: int = Field(default=20, description="Results per page", ge=1, le=100)


@mcp.tool(name="kit_search_modules", annotations={"title": "Search KIT modules", **READ_ONLY})
async def kit_search_modules(params: SearchModulesInput) -> str:
    """Search the public KIT module catalogue (Modulhandbuch). No login needed.

    Use this to plan future semesters: find modules by title, identifier or
    credit range across every KIT degree programme.

    Args:
        params (SearchModulesInput): validated input containing:
            - name (str): words from the module title
            - module_id (str): exact identifier like 'M-ETIT-101156'
            - module_code (str): module code
            - credits_min / credits_max (str): ECTS range
            - page (int), page_size (int): pagination
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: Matching modules with schema:
        {
          "count": int, "page": int, "total_pages": int,
          "modules": [{"module_id": str, "title": str, "lecturers": str,
                       "module_code": str, "credits": float|null,
                       "guid": str, "url": str}]
        }
        Pass a returned module_id to kit_get_module for exam dates and lectures.

    Examples:
        - "Which Regelungstechnik modules exist?" -> name="Regelungstechnik"
        - "Show me 6 ECTS modules called Signale" -> name="Signale", credits_min="6", credits_max="6"
    """
    try:
        data = await _run(
            lambda c: c.search_modules(
                name=params.name,
                module_id=params.module_id,
                module_code=params.module_code,
                credits_min=params.credits_min,
                credits_max=params.credits_max,
                page=params.page,
                page_size=params.page_size,
            )
        )
    except Exception as exc:  # noqa: BLE001 - surfaced to the agent as text
        return _error(exc)

    if params.response_format is ResponseFormat.JSON:
        return _dump(data)

    modules = data["modules"]
    if not modules:
        return "No modules found. Try fewer or more general search words."
    lines = [
        f"# Modules ({data['count']} on page {data['page']} of {data['total_pages']})",
        "",
    ]
    lines += _table(
        modules,
        [
            ("module_id", "ID"),
            ("title", "Title"),
            ("credits", "ECTS"),
            ("lecturers", "Lecturers"),
        ],
    )
    return "\n".join(lines)


class GetModuleInput(BaseInput):
    """Input for fetching one module's detail page."""

    module: str = Field(
        ...,
        description="Module identifier ('M-ETIT-101156'), GUID ('0x...') or full abstractModuleView URL",
        min_length=3,
        max_length=300,
    )


@mcp.tool(name="kit_get_module", annotations={"title": "Get KIT module detail", **READ_ONLY})
async def kit_get_module(params: GetModuleInput) -> str:
    """Full detail for one module, including scheduled exam dates. No login needed.

    This is the tool for "when is the exam for module X" and "what lectures
    belong to module X" - the exam list carries concrete dates, times and rooms
    for upcoming terms.

    Args:
        params (GetModuleInput): validated input containing:
            - module (str): identifier, GUID or URL
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: Module detail with schema:
        {
          "title": str, "url": str, "guid": str, "credits": float|null,
          "bricks":   [{"code": str, "title": str, "credits": float}],
          "lectures": [{"code": str, "title": str, "lecturers": str,
                        "dates": [{"weekday": str, "date": str, "start": str,
                                   "end": str, "room": str, "raw": str}]}],
          "exams":    [ same shape as lectures, with "examiners" ],
          "lecturers": [...], "programs": [...], "units": [...]
        }

    Examples:
        - "When is the Regelungstechnik exam?" -> module="M-ETIT-101156"
        - Don't use when: you only have a title -> call kit_search_modules first
    """
    try:
        data = await _run(lambda c: c.get_module(params.module))
    except Exception as exc:  # noqa: BLE001
        return _error(exc)

    if params.response_format is ResponseFormat.JSON:
        return _dump(data)

    lines = [f"# {data['title'] or params.module}", ""]
    if data.get("credits"):
        lines.append(f"**ECTS**: {data['credits']}")
    if data.get("lecturers"):
        names = [e.get("name") or e.get("title", "") for e in data["lecturers"]]
        lines.append("**Lecturers**: " + ", ".join(n for n in names if n))
    lines.append("")

    if data["exams"]:
        lines.append("## Exams")
        for exam in data["exams"]:
            head = f"- **{exam.get('title', '')}** ({exam.get('code', '')})"
            if exam.get("kind"):
                head += f" - {exam['kind']}"
            lines.append(head)
            for date in exam.get("dates", []):
                lines.append(f"  - {date.get('raw', '')}")
        lines.append("")

    if data["lectures"]:
        lines.append("## Lectures")
        for lecture in data["lectures"]:
            lines.append(f"- **{lecture.get('title', '')}** ({lecture.get('code', '')})")
            for date in lecture.get("dates", []):
                lines.append(f"  - {date.get('raw', '')}")
        lines.append("")

    if data["bricks"]:
        lines.append("## Teilleistungen")
        lines += _table(
            data["bricks"], [("code", "ID"), ("title", "Title"), ("credits", "ECTS")]
        )
        lines.append("")

    if data["programs"]:
        lines.append("## Used in programmes")
        for program in data["programs"][:20]:
            lines.append(f"- {program.get('title', '')}")

    lines.append("")
    lines.append(f"Source: {data['url']}")
    return "\n".join(lines)


class SearchEventsInput(BaseInput):
    """Input for a course catalogue search."""

    title: str = Field(default="", description="Words from the course title", max_length=200)
    course_number: str = Field(
        default="", description="Course number (LV-Nr.), e.g. '2243010'", max_length=40
    )
    event_type: str = Field(
        default="",
        description="Course type exactly as the portal spells it: 'Vorlesung (V)', 'Übung (Ü)', 'Klausur', 'Praktikum (P)', 'Seminar (S)', 'Block (B)', 'Tutorium (TU)'",
        max_length=40,
    )
    weekday: str = Field(
        default="",
        description="Weekday as a digit: 1=Monday ... 7=Sunday",
        pattern=r"^[1-7]?$",
    )
    date: str = Field(default="", description="Date as DD.MM.YYYY", max_length=10)
    page_size: int = Field(default=25, description="Results to return", ge=1, le=200)


@mcp.tool(name="kit_search_events", annotations={"title": "Search KIT courses", **READ_ONLY})
async def kit_search_events(params: SearchEventsInput) -> str:
    """Search the public course catalogue (Vorlesungsverzeichnis). No login needed.

    The catalogue has no lecturer filter; to find someone's courses, look up
    their module with kit_search_modules and read its lecture list instead.

    Args:
        params (SearchEventsInput): validated input containing:
            - title (str): words from the course title
            - course_number (str): LV-Nr.
            - event_type (str): course type as spelled by the portal
            - weekday (str): '1'..'7'
            - date (str): 'DD.MM.YYYY'
            - page_size (int): number of results
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: {"count": int, "total_pages": int,
              "events": [{"code": str, "title": str, "lecturers": str, "kind": str,
                          "dates": [{"weekday": str, "date": str, "start": str,
                                     "end": str, "room": str, "raw": str}]}]}
    """
    try:
        data = await _run(
            lambda c: c.search_events(
                title=params.title,
                course_number=params.course_number,
                event_type=params.event_type,
                weekday=params.weekday,
                date=params.date,
                page_size=params.page_size,
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)

    if params.response_format is ResponseFormat.JSON:
        return _dump(data)
    if not data["events"]:
        return "No courses found."
    lines = [f"# Courses ({data['count']})", ""]
    lines += _table(
        data["events"],
        [("code", "Nr."), ("title", "Title"), ("lecturers", "Lecturers"), ("kind", "Type")],
    )
    return "\n".join(lines)


class ListProgramsInput(BaseInput):
    """Input for the degree programme catalogue."""

    name: str = Field(
        default="",
        description="Filter by words in the programme title, e.g. 'Elektrotechnik'",
        max_length=200,
    )
    program_id: str = Field(
        default="", description="Programme identifier, e.g. '82-049-H-2025'", max_length=40
    )
    page_size: int = Field(default=200, description="Rows to fetch before filtering", ge=1, le=200)


@mcp.tool(name="kit_list_programs", annotations={"title": "List KIT degree programmes", **READ_ONLY})
async def kit_list_programs(params: ListProgramsInput) -> str:
    """List KIT degree programmes from the public catalogue. No login needed.

    The portal offers no title search for programmes, so `name` filters the
    fetched page. Raise page_size or narrow with program_id if a programme you
    expect is missing.

    Args:
        params (ListProgramsInput): validated input containing:
            - name (str): words in the programme title (filtered client-side)
            - program_id (str): programme identifier
            - page_size (int): rows fetched before filtering
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: {"count": int, "total_pages": int, "filtered_by_name": bool,
              "programs": [{"code": str, "title": str, "guid": str}]}
    """
    try:
        data = await _run(
            lambda c: c.list_programs(
                name=params.name, program_id=params.program_id, page_size=params.page_size
            )
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    if params.response_format is ResponseFormat.JSON:
        return _dump(data)
    if not data["programs"]:
        return "No programmes found."
    return "\n".join(
        [f"# Degree programmes ({data['count']})", ""]
        + _table(data["programs"], [("code", "ID"), ("title", "Title")])
    )


# ---------------------------------------------------------------- account data


class ProgramInput(BaseInput):
    """Input for tools scoped to one of the student's degree programmes."""

    program_guid: str | None = Field(
        default=None,
        description="GUID of the degree programme ('0x...'). Omit to use the first one found.",
        max_length=50,
    )


@mcp.tool(name="kit_get_grades", annotations={"title": "Get KIT exam results", **READ_ONLY})
async def kit_get_grades(params: ProgramInput) -> str:
    """Read the Notenspiegel: every recorded exam result, grade and credit total.

    Requires KIT_USERNAME and KIT_PASSWORD. Use kit_check_new_results instead
    when you only want results that appeared since the last check.

    Args:
        params (ProgramInput): validated input containing:
            - program_guid (Optional[str]): degree programme GUID, or None for the default
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: {"program_guid": str, "program": str, "count": int, "passed": int,
              "credits_earned": float, "credits_required": float,
              "average": float|null,
              "results": [{"code": str, "title": str,
                           "grade": float|null, "grade_raw": str,
                           "outcome": "passed"|"failed"|"open",
                           "credits": float|null, "credits_required": float|null,
                           "date": str, "attempt": str, "kind": str}]}
        `grade_raw` is "be" for a passed ungraded Teilleistung, otherwise the
        German grade ("2,7"). `average` and the credit totals are the official
        figures the portal computes, not a re-derivation.

    Error Handling:
        Returns "Login failed. ..." when credentials are missing or rejected,
        including what the SSO flow saw when it could not continue.
    """
    try:
        data = await _run(lambda c: c.get_grades(params.program_guid))
    except Exception as exc:  # noqa: BLE001
        return _error(exc)

    if params.response_format is ResponseFormat.JSON:
        return _dump(data)
    results = data["results"]
    if not results:
        return (
            "No exam results recorded yet. If you do have results, run "
            "results, run `kit-campus dump student/contractview.asp` and check "
            "which tables the page actually contains."
        )
    lines = [
        f"# {data['program'] or 'Notenspiegel'}",
        "",
        f"**Passed**: {data['passed']} of {data['count']} recorded  ",
        f"**Credits**: {data['credits_earned']} of {data['credits_required']} ECTS  ",
        f"**Average**: {data['average'] if data['average'] is not None else 'n/a'}",
        "",
    ]
    lines += _table(
        results,
        [
            ("code", "ID"),
            ("title", "Teilleistung"),
            ("grade_raw", "Grade"),
            ("credits", "ECTS"),
            ("date", "Date"),
            ("outcome", "Result"),
        ],
    )
    return "\n".join(lines)


class CheckNewResultsInput(BaseInput):
    """Input for the polling check used by notification bots."""

    program_guid: str | None = Field(
        default=None, description="Degree programme GUID ('0x...'), or None for the default", max_length=50
    )
    include_exams: bool = Field(
        default=True,
        description="Also report changes to exam registrations (new dates, rooms, status)",
    )
    commit: bool = Field(
        default=True,
        description="Store this poll as the new baseline. Set false to preview without consuming changes.",
    )


@mcp.tool(
    name="kit_check_new_results",
    annotations={
        "title": "Check for new KIT exam results",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def kit_check_new_results(params: CheckNewResultsInput) -> str:
    """Report exam results that appeared or changed since the previous check.

    This is the tool a notification bot should poll. It compares the current
    Notenspiegel against a snapshot on disk and returns only the differences,
    then stores the current state as the new baseline. The very first call
    establishes the baseline and reports nothing, so a fresh install does not
    announce every result you ever got.

    Not read-only: it writes the snapshot file (unless commit=false).

    Args:
        params (CheckNewResultsInput): validated input containing:
            - program_guid (Optional[str]): degree programme GUID
            - include_exams (bool): also diff exam registrations
            - commit (bool): store the poll as the new baseline
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: {"first_run": bool,
              "grades": {"watch": str, "count": int,
                         "changes": [{"kind": "new"|"changed",
                                      "entry": {...result...},
                                      "changed_fields": {field: {"before":..,"after":..}}}]},
              "exams": {same shape} | null,
              "messages": [str]}   # ready-to-send notification lines

    Examples:
        - Poll every 30 minutes from a Telegram bot and send "messages" if non-empty
        - Use commit=false to see what would be reported without consuming it
    """
    settings = load_settings()
    store = SnapshotStore(settings.snapshot_file)

    async def action(client: KitCampusClient) -> dict[str, Any]:
        grades = await client.get_grades(params.program_guid)
        payload: dict[str, Any] = {
            "grades": check(
                store,
                "grades",
                grades["results"],
                GRADE_KEY,
                GRADE_WATCHED,
                commit=params.commit,
            ),
            "totals": {
                "passed": grades["passed"],
                "credits_earned": grades["credits_earned"],
                "average": grades["average"],
            },
            "exams": None,
        }
        if params.include_exams:
            exams = await client.list_registered_exams()
            payload["exams"] = check(
                store,
                "registered_exams",
                exams["entries"],
                EXAM_KEY,
                EXAM_WATCHED,
                commit=params.commit,
            )
        return payload

    try:
        data = await _run(action)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)

    messages = [
        format_grade_change(change, settings.language)
        for change in data["grades"]["changes"]
    ]
    data["first_run"] = data["grades"]["first_run"]
    data["messages"] = messages

    if params.response_format is ResponseFormat.JSON:
        return _dump(data)

    if data["first_run"]:
        return (
            f"Baseline stored: {data['grades']['count']} results are now known. "
            "The next check will report anything new."
        )
    if not messages and not (data["exams"] or {}).get("changes"):
        return f"No changes. {data['grades']['count']} results known, last checked {data['grades']['last_checked']}."
    lines = ["# New since last check", ""]
    lines += [f"- {m}" for m in messages]
    for change in (data["exams"] or {}).get("changes", []):
        entry = change["entry"]
        lines.append(f"- Exam registration {change['kind']}: {entry.get('title', '')}")
    return "\n".join(lines)


@mcp.tool(
    name="kit_list_registered_exams",
    annotations={"title": "List registered KIT exams", **READ_ONLY},
)
async def kit_list_registered_exams(params: BaseInput) -> str:
    """List the exams the account is currently registered for, with dates.

    Args:
        params (BaseInput): validated input containing:
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: {"kind": "registered", "count": int,
              "entries": [{"code": str, "title": str, "status": str,
                           "dates": [{"date": str, "start": str, "end": str, "room": str}]}]}
    """
    try:
        data = await _run(lambda c: c.list_registered_exams())
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    if params.response_format is ResponseFormat.JSON:
        return _dump(data)
    if not data["entries"]:
        return "No exam registrations found."
    lines = [f"# Registered exams ({data['count']})", ""]
    for entry in data["entries"]:
        lines.append(f"- **{entry.get('title', '')}** ({entry.get('code', '')}) {entry.get('status', '')}")
        for date in entry.get("dates", []):
            lines.append(f"  - {date.get('raw', '')}")
    return "\n".join(lines)


@mcp.tool(name="kit_get_study_progress", annotations={"title": "Get KIT study progress", **READ_ONLY})
async def kit_get_study_progress(params: ProgramInput) -> str:
    """Read the Studienverlauf: programme structure with modules and credits.

    Use this for "how many credits am I missing" style questions, since it
    shows the required structure alongside what has been completed.

    Args:
        params (ProgramInput): validated input containing:
            - program_guid (Optional[str]): degree programme GUID
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: {"program_guid": str, "program": str, "url": str,
              "credits_earned": float, "credits_required": float, "average": float,
              "outstanding_count": int,
              "outstanding": [{"code": str, "title": str, "credits_required": float}],
              "sections": [{"code": str, "title": str, "credits_earned": float,
                            "credits_required": float, "grade": float,
                            "children": [{...module or Teilleistung...}]}]}
    """
    try:
        data = await _run(lambda c: c.get_study_progress(params.program_guid))
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    if params.response_format is ResponseFormat.JSON:
        return _dump(data)
    lines = [
        f"# {data['program'] or 'Studienverlauf'}",
        "",
        f"**Credits**: {data['credits_earned']} of {data['credits_required']} ECTS  ",
        f"**Average**: {data['average'] if data['average'] is not None else 'n/a'}  ",
        f"**Still open**: {data['outstanding_count']} Teilleistungen",
        "",
    ]
    for section in data["sections"]:
        earned = section.get("credits") or 0
        required = section.get("credits_required") or 0
        lines.append(f"## {section.get('title', '')} ({earned}/{required} ECTS)")
        rows = [c for c in section["children"] if c.get("node") == "brick"]
        lines += _table(
            rows,
            [
                ("code", "ID"),
                ("title", "Teilleistung"),
                ("grade_raw", "Grade"),
                ("credits", "ECTS"),
                ("credits_required", "Req."),
                ("date", "Date"),
            ],
        )
        lines.append("")
    return "\n".join(lines)


@mcp.tool(name="kit_get_timetable", annotations={"title": "Get KIT timetable", **READ_ONLY})
async def kit_get_timetable(params: BaseInput) -> str:
    """Read the personal timetable (Stundenplan) with its appointments.

    Args:
        params (BaseInput): validated input containing:
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: {"count": int, "entries": [{"code": str, "title": str,
              "dates": [{"weekday": str, "start": str, "end": str, "room": str}]}]}
    """
    try:
        data = await _run(lambda c: c.get_timetable())
    except Exception as exc:  # noqa: BLE001
        return _error(exc)
    if params.response_format is ResponseFormat.JSON:
        return _dump(data)
    if not data["entries"]:
        return "Timetable is empty."
    lines = [f"# Timetable ({data['count']})", ""]
    for entry in data["entries"]:
        lines.append(f"- **{entry.get('title', '')}**")
        for date in entry.get("dates", []):
            lines.append(f"  - {date.get('raw', '')}")
    return "\n".join(lines)


@mcp.tool(name="kit_check_session", annotations={"title": "Check KIT login", **READ_ONLY})
async def kit_check_session(params: BaseInput) -> str:
    """Verify that login works and list the degree programmes on the account.

    Call this first when an account tool fails - it separates "credentials are
    wrong" from "the page layout changed". It also returns the programme GUIDs
    that other tools accept as program_guid.

    Args:
        params (BaseInput): validated input containing:
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: {"credentials_configured": bool, "logged_in": bool,
              "programs": [{"guid": str, "title": str}],
              "state_dir": str, "snapshot_watches": [str]}
    """
    settings = load_settings()
    payload: dict[str, Any] = {
        "credentials_configured": settings.has_credentials,
        "username": settings.username,
        "state_dir": str(settings.state_dir),
        "logged_in": False,
        "programs": [],
    }
    if not settings.has_credentials:
        payload["hint"] = (
            "Set KIT_USERNAME (short account, e.g. ab1234) and KIT_PASSWORD in "
            "the environment or in a .env file next to pyproject.toml."
        )
        return _dump(payload) if params.response_format is ResponseFormat.JSON else (
            "Not configured. " + payload["hint"]
        )
    async def check(client: KitCampusClient) -> dict[str, Any]:
        return {"who": await client.whoami(), "programs": await client.discover_programs()}

    try:
        result = await _run(check)
        payload["logged_in"] = True
        payload.update(result["who"])
        payload["programs"] = result["programs"]
    except Exception as exc:  # noqa: BLE001
        payload["error"] = _error(exc)

    if params.response_format is ResponseFormat.JSON:
        return _dump(payload)
    if not payload["logged_in"]:
        return f"Login check failed for {settings.username}.\n\n{payload.get('error', '')}"
    lines = [
        (
            f"Logged in as **{payload.get('name') or settings.username}** "
            f"({payload.get('username')}, Matrikelnummer "
            f"{payload.get('matriculation_number')})."
        ),
        "",
        "Degree programmes:",
    ]
    lines += [f"- {p['title']} (`{p['guid']}`)" for p in payload["programs"]]
    return "\n".join(lines)


class FetchPageInput(BaseInput):
    """Input for the raw page fetch used to debug parsing."""

    path: str = Field(
        ...,
        description="Campus path relative to /sp/campus/ (e.g. 'student/timetable.asp') or a full https URL",
        min_length=3,
        max_length=500,
    )
    authenticated: bool = Field(default=True, description="Log in before fetching")
    max_chars: int = Field(default=4000, description="Truncate the extracted text", ge=200, le=40000)


@mcp.tool(name="kit_fetch_page", annotations={"title": "Fetch a raw Campus page", **READ_ONLY})
async def kit_fetch_page(params: FetchPageInput) -> str:
    """Fetch any Campus page and return its parsed tables. Use for pages without a dedicated tool.

    Every CAS Campus data page renders as `<table class="listview">`, so this
    returns the same structure the dedicated tools build on. Handy when the
    portal layout changes or a page has no tool yet.

    Args:
        params (FetchPageInput): validated input containing:
            - path (str): path under /sp/campus/ or a full URL
            - authenticated (bool): log in first
            - max_chars (int): truncation limit
            - response_format (ResponseFormat): 'markdown' or 'json'

    Returns:
        str: {"url": str, "title": str,
              "tables": [{"table_id": str, "headers": [str], "rows": [{...}]}]}
    """
    from .parsers import page_title, parse_tables

    try:
        html, final = await _run(
            lambda c: c.fetch_raw(params.path, authenticated=params.authenticated)
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)

    tables = parse_tables(html, final)
    data = {
        "url": final,
        "title": page_title(html),
        "tables": [
            {"table_id": t.table_id, "headers": t.headers, "rows": t.dicts()} for t in tables
        ],
    }
    text = _dump(data)
    if len(text) > params.max_chars:
        text = text[: params.max_chars] + f"\n... truncated ({len(text)} chars total)"
    return text


def main() -> None:
    """Entry point for `kit-campus-mcp` (stdio transport)."""
    mcp.run()


if __name__ == "__main__":
    main()
