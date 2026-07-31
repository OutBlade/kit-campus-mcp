"""High-level access to the KIT Campus portal.

`KitCampusClient` is the only thing the MCP server and the Telegram bot talk to.
Public catalogue calls work without credentials; everything under
`/campus/student/` transparently logs in through Shibboleth on first use.
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import quote, urljoin

from .auth import KitError, KitSession
from .config import CAMPUS_BASE, PATHS, SERVICES, Settings, load_settings
from .parsers import (
    ListTable,
    TreeNode,
    clean_text,
    extract_hidden_field,
    find_table,
    find_table_by_headers,
    guid_from_url,
    page_title,
    parse_credits,
    parse_detail_line,
    parse_study_tree,
    parse_tables,
)


def _url(key: str, **params: str) -> str:
    path = PATHS[key].format(**params)
    return f"{CAMPUS_BASE}{path}"


class KitCampusClient:
    """Fetches and parses KIT Campus pages."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self.session = KitSession(self.settings)
        self._tguid: str | None = None
        self._term: str | None = None
        self._programs: list[dict[str, str]] | None = None

    async def __aenter__(self) -> KitCampusClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self.session.close()

    # ------------------------------------------------------------ public data

    async def _form_token(self, url: str) -> tuple[str, str]:
        """GET a search page and return (html, tguid) for the follow-up POST."""
        html, _ = await self.session.get(url)
        tguid = extract_hidden_field(html, "tguid") or ""
        if tguid:
            self._tguid = tguid
        return html, tguid

    async def search_modules(
        self,
        name: str = "",
        module_id: str = "",
        module_code: str = "",
        credits_min: str = "",
        credits_max: str = "",
        language: str = "",
        page: int = 1,
        page_size: int = 20,
    ) -> dict[str, Any]:
        """Search the public module catalogue (Modulhandbuch)."""
        url = _url("module_list")
        html, tguid = await self._form_token(url)
        payload = {
            "tguid": tguid,
            "id": module_id,
            "name": name,
            "modulecode": module_code,
            "recurrence": "",
            "language": language,
            "requiredcreditsfrom": credits_min,
            "requiredcreditsto": credits_max,
            "products": "{}",
            "units": "{}",
            "executives": "{}",
            "pagenumber": str(page),
            "pagesize": str(page_size),
            "sortcolumn": "2",
            "sortorder": "up",
            "search": "Suchen",
        }
        html, final = await self.session.post(url, payload)
        tables = parse_tables(html, final)
        table = find_table(tables, "MODULES") or find_table_by_headers(tables, "code", "title")
        modules = [self._module_summary(row, final) for row in table.rows] if table else []
        return {
            "query": {k: v for k, v in payload.items() if v and k not in {"tguid", "search"}},
            "count": len(modules),
            "page": page,
            "total_pages": _total_pages(html),
            "modules": modules,
        }

    def _module_summary(self, row: Any, base_url: str) -> dict[str, Any]:
        link = next(
            (u for u in row.links.values() if "abstractModuleView" in u),
            "",
        )
        return {
            "module_id": row.get("code"),
            "title": row.get("title"),
            "lecturers": row.get("lecturers"),
            "module_code": row.get("module_code"),
            "credits": parse_credits(row.get("credits")),
            "guid": guid_from_url(link) or row.guid,
            "url": link,
        }

    async def get_module(self, module: str) -> dict[str, Any]:
        """Full detail for one module: bricks, lectures, exam dates, programmes.

        `module` may be a module id (M-ETIT-101156), a GUID or a full URL.
        """
        url = await self._resolve_module_url(module)
        html, final = await self.session.get(url)
        tables = parse_tables(html, final)

        bricks = find_table(tables, "abstract-contract-list")
        events = find_table(tables, "EVENTLIST")
        exams = find_table(tables, "EXAMLIST")
        lecturers = find_table(tables, "lecturerlist")
        programs = find_table(tables, "PRODUCTLIST")
        units = find_table(tables, "UNITLIST")

        brick_rows = _rows(bricks, ("code", "title", "language", "credits"))
        return {
            "title": page_title(html),
            "url": final,
            "guid": guid_from_url(final),
            "description": _module_description(html),
            "credits": _module_credits(html, brick_rows),
            "bricks": brick_rows,
            "lectures": _schedule_rows(events, final),
            "exams": _schedule_rows(exams, final),
            "lecturers": _rows(lecturers, ("name", "title", "unit")),
            "programs": _rows(programs, ("title",)),
            "units": _rows(units, ("title",)),
        }

    async def _resolve_module_url(self, module: str) -> str:
        if module.startswith("http"):
            return module
        if module.startswith("0x"):
            return f"{_url('module_view')}?gguid={module}"
        result = await self.search_modules(module_id=module, page_size=5)
        for entry in result["modules"]:
            if entry["url"] and (
                entry["module_id"] or ""
            ).lower() == module.lower():
                return entry["url"]
        if result["modules"] and result["modules"][0]["url"]:
            return result["modules"][0]["url"]
        raise KitError(
            f"No module found for '{module}'. Use kit_search_modules first and "
            "pass the module_id (e.g. M-ETIT-101156) or the url from the result."
        )

    async def list_programs(
        self, name: str = "", program_id: str = "", page_size: int = 200
    ) -> dict[str, Any]:
        """List the public degree programme catalogue.

        The form has no title field, so a `name` filter is applied to the
        fetched rows instead of being pushed to the server.
        """
        url = _url("program_list")
        html, tguid = await self._form_token(url)
        payload = {
            "tguid": tguid,
            "id": program_id,
            "fieldofstudy": "",
            "degreegroup": "",
            "degreetext": "",
            "poversion": "",
            "regularduration": "",
            "requiredcreditsfrom": "",
            "requiredcreditsto": "",
            "units": "{}",
            "executives": "{}",
            "pagenumber": "1",
            "pagesize": str(min(page_size, 200)),
            "sortcolumn": "2",
            "sortorder": "up",
            "search": "Suchen",
        }
        html, final = await self.session.post(url, payload)
        tables = parse_tables(html, final)
        table = tables[0] if tables else None
        programs = _rows(table, ("code", "title"))
        if name:
            needle = name.casefold()
            programs = [p for p in programs if needle in (p.get("title") or "").casefold()]
        return {
            "count": len(programs),
            "total_pages": _total_pages(html),
            "filtered_by_name": bool(name),
            "programs": programs,
        }

    async def search_events(
        self,
        title: str = "",
        course_number: str = "",
        event_type: str = "",
        weekday: str = "",
        date: str = "",
        page_size: int = 25,
    ) -> dict[str, Any]:
        """Search the public course catalogue (Vorlesungsverzeichnis)."""
        url = _url("event_search")
        html, tguid = await self._form_token(url)
        payload = {
            "tguid": tguid,
            "eventcoursenumber": course_number,
            "eventtitle": title,
            "eventtype": event_type,
            "eventformat": "",
            "eventlanguage": "",
            "appointmentperiod": "",
            "appointmentweekday": weekday,
            "appointmentdate": date,
            "appointmenttimestart": "",
            "appointmenttimeend": "",
            "product": "{}",
            "module": "{}",
            "brick": "{}",
            "audience": "{}",
            "field": "{}",
            "unit": "{}",
            "room": "{}",
            "pagenumber": "1",
            "pagesize": str(min(page_size, 200)),
            "sortcolumn": "2",
            "sortorder": "up",
            "search": "Suchen",
        }
        html, final = await self.session.post(url, payload)
        tables = parse_tables(html, final)
        table = find_table(tables, "EVENTLIST") or (tables[0] if tables else None)
        return {
            "count": len(table.rows) if table else 0,
            "total_pages": _total_pages(html),
            "events": _schedule_rows(table, final),
        }

    # ------------------------------------------------------- student, private

    async def _service(self, name: str) -> Any:
        """Call one of the backend JSON services with the portal token."""
        token = await self.session.token()
        url = f"{SERVICES[name]}?token={quote(token.token_a, safe='/@')}"
        html, final = await self.session.get(url)
        try:
            return json.loads(html)
        except json.JSONDecodeError:
            raise KitError(
                f"The {name} service did not return JSON (from {final}). "
                f"First bytes: {html[:120]!r}"
            ) from None

    async def whoami(self) -> dict[str, str]:
        """Identity behind the current session, straight from the portal token."""
        token = await self.session.token()
        return {
            "username": token.username,
            "name": token.full_name,
            "matriculation_number": token.matriculation_number,
        }

    async def discover_programs(self) -> list[dict[str, str]]:
        """Degree programmes this account is enrolled in, from products.asp."""
        if self._programs is not None:
            return self._programs
        if self.settings.program_guid:
            self._programs = [
                {"guid": self.settings.program_guid, "title": "(from KIT_PROGRAM_GUID)"}
            ]
            return self._programs

        data = await self._service("products")
        key = "namede" if self.settings.language != "en" else "nameen"
        self._programs = [
            {
                "guid": guid,
                "title": (entry.get(key) or entry.get("namede") or "").strip(),
            }
            for guid, entry in data.items()
        ]
        if not self._programs:
            raise KitError(
                "The portal reports no degree programme for this account. If you "
                "know the GUID, set KIT_PROGRAM_GUID in the .env file."
            )
        return self._programs

    async def list_terms(self) -> list[dict[str, Any]]:
        """Semesters known to CAS Campus, newest first, with the default flagged."""
        data = await self._service("terms")
        terms = [
            {
                "guid": guid,
                "name": entry.get("name", ""),
                "default": bool(entry.get("default")),
                "starts": entry.get("tstart"),
            }
            for guid, entry in data.items()
        ]
        terms.sort(key=lambda t: t["starts"] or 0, reverse=True)
        return terms

    async def _term_guid(self) -> str:
        if self._term is None:
            terms = await self.list_terms()
            self._term = next(
                (t["guid"] for t in terms if t["default"]),
                terms[0]["guid"] if terms else "",
            )
        return self._term

    async def _program_guid(self, program_guid: str | None) -> str:
        if program_guid:
            return program_guid
        programs = await self.discover_programs()
        return programs[0]["guid"]

    async def _study_tree(self, program_guid: str | None) -> tuple[list[TreeNode], str, str]:
        """Fetch and parse the study tree that carries every result."""
        guid = await self._program_guid(program_guid)
        html, final = await self.session.fetch_authenticated(_url("study_tree", pguid=guid))
        nodes = parse_study_tree(html)
        if not nodes:
            raise KitError(
                f"No study tree found on {final}. Run "
                f"`kit-campus dump \"student/contractview.asp?gguid={guid}\"` "
                "and check which tables the page contains."
            )
        return nodes, guid, final

    async def get_grades(self, program_guid: str | None = None) -> dict[str, Any]:
        """Every recorded exam result, with the programme's credit and grade totals.

        Results live on the Teilleistung (`brick`) level of the study tree; the
        modules and sections above them are aggregates, and the root row carries
        the official overall average and credit count.
        """
        nodes, guid, final = await self._study_tree(program_guid)
        # A Teilleistung can hang under more than one section of the tree (an
        # Orientierungsprüfung entry is also counted in its subject area), so the
        # same result appears twice. Keep the first occurrence of each attempt,
        # otherwise totals are inflated and a watcher notifies twice.
        results: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for node in nodes:
            if node.kind != "brick" or not node.has_result:
                continue
            key = (node.code or node.title, node.attempt)
            if key in seen:
                continue
            seen.add(key)
            results.append(node.as_result())
        passed = [r for r in results if r["outcome"] == "passed"]
        root = next((n for n in nodes if n.kind == "product"), None)
        return {
            "program_guid": guid,
            "program": root.title if root else "",
            "url": final,
            "count": len(results),
            "passed": len(passed),
            "credits_earned": root.credits_earned if root else None,
            "credits_required": root.credits_required if root else None,
            "average": root.grade if root else None,
            "results": results,
        }

    async def list_registered_exams(self) -> dict[str, Any]:
        """Exams the account is currently registered for."""
        return await self._registration_list("registered_exams", "registered")

    async def list_deregistered_exams(self) -> dict[str, Any]:
        """Exams the account has de-registered from."""
        return await self._registration_list("deregistered_exams", "deregistered")

    async def list_event_registrations(self) -> dict[str, Any]:
        """Courses (Lehrveranstaltungen) the account is registered for."""
        return await self._registration_list("event_registrations", "events")

    async def _registration_list(self, path_key: str, kind: str) -> dict[str, Any]:
        html, final = await self.session.fetch_authenticated(_url(path_key))
        tables = parse_tables(html, final)
        entries: list[dict[str, Any]] = []
        for table in tables:
            entries.extend(_schedule_rows(table, final))
        return {"kind": kind, "count": len(entries), "entries": entries, "url": final}

    async def get_study_progress(self, program_guid: str | None = None) -> dict[str, Any]:
        """The full study tree: sections, modules and Teilleistungen with credits.

        Same page as `get_grades`, but keeping the hierarchy, so it answers
        "what is still missing" rather than "what did I score".
        """
        nodes, guid, final = await self._study_tree(program_guid)
        root = next((n for n in nodes if n.kind == "product"), None)
        sections: list[dict[str, Any]] = []
        for node in nodes:
            if node.kind == "product":
                continue
            entry = node.as_dict()
            if node.kind == "field":
                entry["children"] = []
                sections.append(entry)
            elif sections:
                sections[-1]["children"].append(entry)
        outstanding = [
            n.as_dict()
            for n in nodes
            if n.kind == "brick" and not n.has_result and (n.credits_required or 0) > 0
        ]
        return {
            "program_guid": guid,
            "program": root.title if root else "",
            "url": final,
            "credits_earned": root.credits_earned if root else None,
            "credits_required": root.credits_required if root else None,
            "average": root.grade if root else None,
            "outstanding_count": len(outstanding),
            "outstanding": outstanding,
            "sections": sections,
        }

    async def get_timetable(self) -> dict[str, Any]:
        """The personal timetable (Stundenplan)."""
        html, final = await self.session.fetch_authenticated(
            f"{_url('timetable')}?tguid={await self._term_guid()}"
        )
        tables = parse_tables(html, final)
        entries: list[dict[str, Any]] = []
        for table in tables:
            entries.extend(_schedule_rows(table, final))
        return {"count": len(entries), "entries": entries, "url": final}

    async def get_webcal_url(self) -> dict[str, Any]:
        """The personal WebCal URL, usable without authentication."""
        html, final = await self.session.fetch_authenticated(_url("webcal"))
        match = re.search(r"(?:webcal|https?)://[^\s\"'<>]+\.ics[^\s\"'<>]*", html)
        if not match:
            match = re.search(r"(?:webcal|https?)://[^\s\"'<>]*webcal[^\s\"'<>]*", html)
        return {
            "webcal_url": match.group(0) if match else None,
            "url": final,
            "note": "Anyone with this URL can read your appointments - keep it private.",
        }

    async def fetch_raw(self, path: str, authenticated: bool = True) -> tuple[str, str]:
        """Fetch any Campus page verbatim. Used by the `dump` CLI command."""
        url = path if path.startswith("http") else urljoin(f"{CAMPUS_BASE}/campus/", path)
        if authenticated:
            return await self.session.fetch_authenticated(url)
        return await self.session.get(url)


# ----------------------------------------------------------------- formatting


def _rows(table: ListTable | None, keys: tuple[str, ...]) -> list[dict[str, Any]]:
    if table is None:
        return []
    out: list[dict[str, Any]] = []
    for row in table.rows:
        entry = {k: row.get(k) for k in keys if row.get(k)}
        if not entry:
            continue
        if "credits" in entry:
            entry["credits"] = parse_credits(str(entry["credits"]))
        if row.guid:
            entry["guid"] = row.guid
        out.append(entry)
    return out


def _schedule_rows(table: ListTable | None, base_url: str) -> list[dict[str, Any]]:
    """Rows plus their collapsible date/room detail lines, parsed into fields."""
    if table is None:
        return []
    out: list[dict[str, Any]] = []
    for row in table.rows:
        entry: dict[str, Any] = {k: v for k, v in row.values.items() if v}
        if row.guid:
            entry["guid"] = row.guid
        if "credits" in entry:
            entry["credits"] = parse_credits(str(entry["credits"]))
        if row.details:
            entry["dates"] = [parse_detail_line(line) for line in row.details]
        link = next((u for u in row.links.values() if "View.asp" in u or "view.asp" in u), "")
        if link:
            entry["url"] = link
        out.append(entry)
    return out


def _total_pages(html: str) -> int:
    match = re.search(r'name="pagenumber"[^>]*max="(\d+)"', html)
    return int(match.group(1)) if match else 1


def _module_description(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for node in soup.find_all(["div", "p"], class_=re.compile("desc|content|text", re.IGNORECASE)):
        text = clean_text(node.get_text(" ", strip=True))
        if len(text) > 120:
            return text[:2000]
    return ""


def _module_credits(html: str, bricks: list[dict[str, Any]]) -> float | None:
    """Total ECTS: the figure stated on the page, else the sum of its bricks."""
    match = re.search(r"(\d+[,.]\d+)\s*(?:LP|ECTS|credits)\b", html, re.IGNORECASE)
    if match:
        return parse_credits(match.group(1))
    total = sum(b["credits"] for b in bricks if isinstance(b.get("credits"), (int, float)))
    return round(total, 1) or None
