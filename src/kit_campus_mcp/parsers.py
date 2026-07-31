"""HTML parsing for CAS Campus pages.

Every data page in CAS Campus renders its content as `<table class="listview">`.
Rows carry the record GUID in their `id` attribute, and detail lines (exam dates,
lecture slots) follow as `<tr class="collapsible">` rows belonging to the row
above them. Parsing that one pattern covers the whole portal, so the domain
specific helpers below are thin wrappers around `parse_tables`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

# Header label -> canonical key. CAS Campus uses German labels; the English
# portal uses the same layout with translated headers, so both are listed.
HEADER_ALIASES: dict[str, str] = {
    "kennung": "code",
    "nr.": "code",
    "lv-nr.": "code",
    "pr.-nr.": "code",
    "no.": "code",
    "identifier": "code",
    "titel": "title",
    "title": "title",
    "bezeichnung": "title",
    "titel (mit kennung)": "title",
    "prüfung": "title",
    "veranstaltung": "title",
    "note": "grade",
    "grade": "grade",
    "status": "status",
    "lp": "credits",
    "ects": "credits",
    "credits": "credits",
    "leistungspunkte": "credits",
    "semester": "semester",
    "datum": "date",
    "date": "date",
    "prüfungsdatum": "date",
    "termin": "date",
    "versuch": "attempt",
    "attempt": "attempt",
    "prüfer/innen": "examiners",
    "prüfer": "examiners",
    "examiner": "examiners",
    "verantwortliche": "lecturers",
    "dozent/innen": "lecturers",
    "lecturer": "lecturers",
    "modulcode": "module_code",
    "leistungsart": "kind",
    "art": "kind",
    "type": "kind",
    "form": "form",
    "sprache": "language",
    "language": "language",
    "einrichtung": "unit",
    "raum": "room",
    "anmeldung": "registration",
    "abmeldung": "deregistration",
    # Carries both the state and the deadline, e.g.
    # "Angemeldet Abmelden bis 10.08.2026 07:59".
    "anmeldestatus": "status",
    "anmeldecode": "registration_code",
    "ist-lp": "credits_earned",
    "soll-lp": "credits_required",
}

# "Mi, 30.09.2026 , 08:00 - 10:00 , 20.40 Fritz-Haller-Hörsaal (HS37)"
DETAIL_RE = re.compile(
    r"(?P<weekday>\w{2,10})[,.]?\s*"
    r"(?P<recurrence>wöchentlich|weekly|einmalig)?\s*,?\s*"
    r"(?P<date>\d{2}\.\d{2}\.\d{4})?\s*,?\s*"
    r"(?P<start>\d{1,2}:\d{2})\s*-\s*(?P<end>\d{1,2}:\d{2})\s*,?\s*"
    r"(?P<room>.*)$"
)

GUID_RE = re.compile(r"gguid=(0x[0-9A-Fa-f]+)")


def clean_text(value: str) -> str:
    """Normalise whitespace and drop the icon glyphs CAS Campus embeds.

    Header cells carry sort-arrow glyphs, and a page served in the legacy
    encoding turns its UTF-8 non-breaking spaces into a stray "Â"; both would
    otherwise end up in the header labels and break the alias lookup.
    """
    text = value.replace("Â ", " ")
    text = text.replace("\xa0", " ").replace("�", " ")
    text = re.sub(r"Â(?=\s|$)", " ", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Co")
    return re.sub(r"\s+", " ", text).strip(" \t\n,;")


def canonical_header(label: str) -> str:
    key = clean_text(label).lower()
    return HEADER_ALIASES.get(key, key)


@dataclass
class ListRow:
    """One record from a listview table."""

    guid: str | None
    cells: list[str]
    values: dict[str, str]
    details: list[str] = field(default_factory=list)
    links: dict[str, str] = field(default_factory=dict)

    def get(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    def to_dict(self) -> dict[str, object]:
        data: dict[str, object] = {k: v for k, v in self.values.items() if v}
        if self.guid:
            data["guid"] = self.guid
        if self.details:
            data["details"] = self.details
        if self.links:
            data["links"] = self.links
        return data


@dataclass
class ListTable:
    """A `<table class="listview">` with its header row resolved."""

    table_id: str
    headers: list[str]
    rows: list[ListRow]

    def dicts(self) -> list[dict[str, object]]:
        return [row.to_dict() for row in self.rows]


def _cell_text(cell: Tag) -> str:
    return clean_text(cell.get_text(" ", strip=True))


def _is_continuation(row: Tag) -> bool:
    classes = row.get("class") or []
    if "collapsible" in classes:
        return True
    cells = row.find_all(["td", "th"], recursive=False)
    return any(int(c.get("colspan", 1) or 1) > 4 for c in cells)


def _row_links(row: Tag, base_url: str) -> dict[str, str]:
    links: dict[str, str] = {}
    for anchor in row.find_all("a", href=True):
        label = clean_text(anchor.get_text(" ", strip=True))
        if not label:
            continue
        links.setdefault(label, urljoin(base_url, anchor["href"]))
    return links


def parse_tables(html: str, base_url: str = "") -> list[ListTable]:
    """Parse every listview table on a CAS Campus page."""
    soup = BeautifulSoup(html, "html.parser")
    tables: list[ListTable] = []
    for node in soup.find_all("table"):
        parsed = _parse_table(node, base_url)
        if parsed is not None and parsed.rows:
            tables.append(parsed)
    return tables


def _own_rows(node: Tag) -> list[Tag]:
    """Rows belonging to this table, not to a table nested inside it.

    CAS Campus splits each table into a `<tbody class="tablehead">` holding the
    header row and a `<tbody class="tablecontent">` holding the data, so the
    rows have to be collected across every tbody rather than from the first one.
    """
    return [
        row
        for row in node.find_all("tr")
        if row.find_parent("table") is node and row.find(["td", "th"])
    ]


def _parse_table(node: Tag, base_url: str) -> ListTable | None:
    rows = _own_rows(node)
    if not rows:
        return None

    headers: list[str] = []
    start = 0
    first_body = rows[0].find_parent("tbody")
    is_header_row = (
        rows[0].find("th") is not None
        or "folding-all-close" in (rows[0].get("class") or [])
        or (first_body is not None and "tablehead" in (first_body.get("class") or []))
    )
    if is_header_row:
        headers = [canonical_header(_cell_text(c)) for c in rows[0].find_all(["td", "th"])]
        start = 1

    parsed_rows: list[ListRow] = []
    for row in rows[start:]:
        cells = [_cell_text(c) for c in row.find_all(["td", "th"])]
        text = clean_text(" ".join(cells))
        if _is_continuation(row):
            if parsed_rows and text:
                parsed_rows[-1].details.append(text)
            continue
        if not text:
            continue
        values: dict[str, str] = {}
        for index, value in enumerate(cells):
            if not value:
                continue
            key = headers[index] if index < len(headers) else f"col{index}"
            if not key:
                key = f"col{index}"
            if key in values and values[key] != value:
                values[key] = f"{values[key]} | {value}"
            else:
                values[key] = value
        row_id = row.get("id") or ""
        guid = row_id if row_id.startswith("0x") else None
        parsed_rows.append(
            ListRow(
                guid=guid,
                cells=cells,
                values=values,
                links=_row_links(row, base_url) if base_url else {},
            )
        )

    return ListTable(
        table_id=node.get("id") or "",
        headers=headers,
        rows=parsed_rows,
    )


def find_table(tables: Iterable[ListTable], *table_ids: str) -> ListTable | None:
    wanted = {t.lower() for t in table_ids}
    for table in tables:
        if table.table_id.lower() in wanted:
            return table
    return None


def find_table_by_headers(
    tables: Iterable[ListTable], *required: str
) -> ListTable | None:
    """Find the first table whose headers contain all `required` canonical keys."""
    for table in tables:
        if all(key in table.headers for key in required):
            return table
    return None


# The study tree marks every row with its depth (`hierarchy1`..`hierarchy4`) and
# its node type. Only `brick` rows are real exam results; the levels above are
# aggregates, and `product` is the degree programme total.
TREE_KINDS = ("product", "field", "module", "brick")

# "T-ETIT-113001 – Lineare Elektrische Netze" -> code, title
TITLE_SPLIT_RE = re.compile(r"^\s*([A-Za-z0-9][\w.-]*)\s*[–—-]\s*(.+)$")

# The grade cell holds the grade and, on aggregate rows, the attempt: "2,5 1".
GRADE_CELL_RE = re.compile(r"^\s*(?P<grade>[\d,.]+|be|nb|be\.|\w{1,3})\s*(?P<attempt>\d+)?\s*$")


@dataclass
class TreeNode:
    """One row of the CAS Campus study tree (contractview.asp)."""

    kind: str
    level: int
    code: str
    title: str
    art: str
    status: str
    grade_raw: str
    attempt: str
    date: str
    credits_earned: float | None
    credits_required: float | None
    guid: str | None = None

    @property
    def grade(self) -> float | None:
        return parse_grade(self.grade_raw)

    @property
    def passed(self) -> bool:
        """`be` is "bestanden"; a numeric grade counts as passed up to 4.0."""
        text = self.grade_raw.lower()
        if text.startswith("be"):
            return True
        if text.startswith("nb"):
            return False
        grade = self.grade
        return grade is not None and grade <= 4.0

    @property
    def has_result(self) -> bool:
        """A result exists once a grade or a date has been recorded."""
        return bool(self.grade_raw or self.date)

    @property
    def outcome(self) -> str:
        if not self.has_result:
            return "open"
        return "passed" if self.passed else "failed"

    def as_result(self) -> dict[str, object]:
        return {
            "code": self.code,
            "title": self.title,
            "grade": self.grade,
            "grade_raw": self.grade_raw,
            "attempt": self.attempt,
            "status": self.status,
            "outcome": self.outcome,
            "credits": self.credits_earned,
            "credits_required": self.credits_required,
            "date": self.date,
            "kind": self.art,
            "guid": self.guid,
        }

    def as_dict(self) -> dict[str, object]:
        data = self.as_result()
        data.update({"node": self.kind, "level": self.level})
        return data


def split_code_title(text: str) -> tuple[str, str]:
    """Split "M-ETIT-106428 – Orientierungsprüfung" into its code and title."""
    match = TITLE_SPLIT_RE.match(clean_text(text))
    if match and any(c.isdigit() for c in match.group(1)):
        return match.group(1), match.group(2).strip()
    return "", clean_text(text)


def _split_grade_cell(text: str) -> tuple[str, str]:
    """Split a grade cell into grade and attempt ("2,5 1" -> "2,5", "1")."""
    cleaned = clean_text(text)
    if not cleaned:
        return "", ""
    parts = cleaned.split()
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0], parts[1]
    return cleaned, ""


def parse_study_tree(html: str) -> list[TreeNode]:
    """Parse the `specific-contract-tree` table on contractview.asp."""
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="specific-contract-tree")
    if table is None:
        table = soup.find("table", class_="listview")
    if table is None:
        return []

    nodes: list[TreeNode] = []
    for row in table.find_all("tr"):
        classes = row.get("class") or []
        kind = next((k for k in TREE_KINDS if k in classes), "")
        if not kind:
            continue
        level = next(
            (int(c[len("hierarchy"):]) for c in classes if c.startswith("hierarchy")), 0
        )
        cells = [_cell_text(c) for c in row.find_all(["td", "th"])]
        # Columns: icon, Titel (mit Kennung), Art, Status, Note, Datum, Ist-LP, Soll-LP
        cells += [""] * (8 - len(cells))
        code, title = split_code_title(cells[1])
        grade_raw, attempt = _split_grade_cell(cells[4])
        row_id = row.get("id") or ""
        nodes.append(
            TreeNode(
                kind=kind,
                level=level,
                code=code,
                title=title,
                art=cells[2],
                status=cells[3],
                grade_raw=grade_raw,
                attempt=attempt,
                date=cells[5],
                credits_earned=parse_credits(cells[6]),
                credits_required=parse_credits(cells[7]),
                guid=row_id if row_id.startswith("0x") else None,
            )
        )
    return nodes


def parse_detail_line(line: str) -> dict[str, str]:
    """Turn "Mi, 30.09.2026 , 08:00 - 10:00 , Room" into structured fields."""
    text = clean_text(line)
    match = DETAIL_RE.search(text)
    if not match:
        return {"raw": text}
    data = {k: clean_text(v or "") for k, v in match.groupdict().items()}
    data["raw"] = text
    return {k: v for k, v in data.items() if v}


def parse_credits(value: str) -> float | None:
    """CAS Campus writes credits German-style, e.g. "5,0"."""
    text = clean_text(value).replace(",", ".")
    match = re.search(r"\d+(?:\.\d+)?", text)
    return float(match.group(0)) if match else None


def parse_grade(value: str) -> float | None:
    """Parse a German grade ("1,7"). Returns None for non-numeric entries."""
    text = clean_text(value).replace(",", ".")
    match = re.fullmatch(r"\d(?:\.\d)?", text)
    return float(match.group(0)) if match else None


def guid_from_url(url: str) -> str | None:
    match = GUID_RE.search(url)
    return match.group(1) if match else None


def extract_hidden_field(html: str, name: str) -> str | None:
    """Read a hidden form value (CAS Campus needs `tguid` on every POST)."""
    soup = BeautifulSoup(html, "html.parser")
    node = soup.find("input", attrs={"name": name})
    if node is None:
        return None
    return node.get("value") or None


def page_title(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find(["h1", "h2"])
    if heading is not None:
        return clean_text(heading.get_text(" ", strip=True))
    return clean_text(soup.title.get_text()) if soup.title else ""
