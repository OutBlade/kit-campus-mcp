"""Change detection between polls.

A notification bot must only report what is actually new, and must survive
restarts. This module keeps the last seen state on disk and diffs against it.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Fields that identify a record across polls. Results are keyed by their
# Teilleistung code; a retake raises the attempt, so that is part of the
# identity and a second attempt shows up as a new record rather than a change.
GRADE_KEY = ("code", "title", "attempt")
EXAM_KEY = ("code", "title")

# Fields whose change is worth a notification.
GRADE_WATCHED = ("grade_raw", "status", "outcome", "credits")
EXAM_WATCHED = ("status", "dates", "date", "room")


def _identity(entry: dict[str, Any], keys: Iterable[str]) -> str:
    return "|".join(str(entry.get(k) or "") for k in keys)


@dataclass
class Change:
    """One difference between two polls."""

    kind: str  # "new" | "changed"
    entry: dict[str, Any]
    before: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "entry": self.entry}
        if self.before is not None:
            data["changed_fields"] = {
                key: {"before": self.before.get(key), "after": self.entry.get(key)}
                for key in set(self.before) | set(self.entry)
                if self.before.get(key) != self.entry.get(key)
            }
        return data


class SnapshotStore:
    """Small JSON-backed store of the last seen payload per watch key."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._data: dict[str, Any] = {}
        if path.exists():
            try:
                self._data = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                self._data = {}

    def get(self, key: str) -> list[dict[str, Any]] | None:
        record = self._data.get(key)
        if not isinstance(record, dict):
            return None
        entries = record.get("entries")
        return entries if isinstance(entries, list) else None

    def last_checked(self, key: str) -> str | None:
        record = self._data.get(key)
        return record.get("checked_at") if isinstance(record, dict) else None

    def put(self, key: str, entries: list[dict[str, Any]]) -> None:
        self._data[key] = {
            "checked_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "entries": entries,
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def reset(self, key: str | None = None) -> None:
        if key is None:
            self._data = {}
        else:
            self._data.pop(key, None)


def diff(
    previous: list[dict[str, Any]] | None,
    current: list[dict[str, Any]],
    key_fields: Iterable[str],
    watched_fields: Iterable[str],
) -> list[Change]:
    """Compare two polls. Returns [] on the first ever run (baseline only)."""
    if previous is None:
        return []
    old = {_identity(e, key_fields): e for e in previous}
    changes: list[Change] = []
    for entry in current:
        ident = _identity(entry, key_fields)
        before = old.get(ident)
        if before is None:
            changes.append(Change(kind="new", entry=entry))
            continue
        if any(before.get(f) != entry.get(f) for f in watched_fields):
            changes.append(Change(kind="changed", entry=entry, before=before))
    return changes


def check(
    store: SnapshotStore,
    key: str,
    current: list[dict[str, Any]],
    key_fields: Iterable[str],
    watched_fields: Iterable[str],
    commit: bool = True,
) -> dict[str, Any]:
    """Diff `current` against the stored snapshot and optionally store it."""
    previous = store.get(key)
    changes = diff(previous, current, key_fields, watched_fields)
    result = {
        "watch": key,
        "first_run": previous is None,
        "last_checked": store.last_checked(key),
        "count": len(current),
        "changes": [c.to_dict() for c in changes],
    }
    if commit:
        store.put(key, current)
        store.save()
    return result


def format_grade_change(change: dict[str, Any], language: str = "de") -> str:
    """Render one grade change as a short message for a chat notification."""
    entry = change["entry"]
    title = entry.get("title") or entry.get("code") or "Prüfung"
    grade = entry.get("grade_raw") or entry.get("grade")
    status = entry.get("status") or ""
    credits = entry.get("credits")
    outcome = entry.get("outcome")

    if language == "en":
        head = {"passed": "Passed", "failed": "Not passed"}.get(outcome, "New result")
        parts = [f"{head}: {title}"]
        if grade:
            parts.append(f"Grade: {grade}")
        if credits:
            parts.append(f"{credits} ECTS")
    else:
        head = {"passed": "Bestanden", "failed": "Nicht bestanden"}.get(
            outcome, "Neues Ergebnis"
        )
        parts = [f"{head}: {title}"]
        if grade:
            parts.append(f"Note: {grade}")
        if credits:
            parts.append(f"{credits} LP")
    # The status text only adds information while the outcome is undecided;
    # for passed/failed it just repeats the headline in the portal's wording.
    if status and outcome == "open":
        parts.append(status)
    return " - ".join(str(p) for p in parts)


def summarise(changes: list[dict[str, Any]], formatter: Callable[[dict], str]) -> str:
    return "\n".join(formatter(change) for change in changes)
