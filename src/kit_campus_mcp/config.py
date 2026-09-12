"""Configuration and on-disk locations for kit-campus-mcp."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# CAS Campus is the application behind campus.studium.kit.edu. The portal at
# campus.studium.kit.edu is only a KIT-branded frame; kit-frame-config.js maps
# every portal page to one of the backend URLs below.
CAMPUS_BASE = "https://cascampus.studium.kit.edu"
CAMPUS_HOST = CAMPUS_BASE
IDP_HOST = "https://idp.scc.kit.edu"

# Login happens on the portal, not on campus.kit.edu: the portal holds the
# Shibboleth session and /token.php mints the short-lived token that authorises
# requests to the CAS Campus backend. campus.kit.edu's own login handshake page
# (campus/login/chooseCampusUser.asp) no longer exists on KIT's server.
PORTAL_BASE = "https://campus.studium.kit.edu"
TOKEN_URL = f"{PORTAL_BASE}/token.php"
PORTAL_LOGIN_TARGET = f"{PORTAL_BASE}/index.php?login=1"

# JSON services on the CAS Campus backend, authorised with `?token=<tokenA>`.
SERVICES = {
    "products": f"{CAMPUS_BASE}/server/services/kit/products.asp",
    "terms": f"{CAMPUS_BASE}/server/services/kit/terms.asp",
}

# CAS Campus serves UTF-8 and declares it as "Charset=utf-8". Older pages in the
# same app occasionally fall back to windows-1252, hence the fallback list.
DEFAULT_ENCODING = "utf-8"
FALLBACK_ENCODING = "cp1252"

USER_AGENT = "kit-campus-mcp/0.1 (+https://github.com/)"

# Backend endpoints, taken from campus.studium.kit.edu/kit-frame-config.js.
# `{pguid}` is the GUID of one of the student's degree programmes.
PATHS = {
    # public, no login required
    "module_list": "/campus/all/abstractModuleList.asp",
    "module_view": "/campus/all/abstractModuleView.asp",
    "program_list": "/campus/all/abstractProductList.asp",
    "brick_list": "/campus/all/abstractBrickList.asp",
    "event_search": "/campus/all/extendedSearch.asp",
    "event_catalog": "/campus/all/fields.asp?group=Vorlesungsverzeichnis",
    # login required.
    # contractview.asp is the study tree: it carries every result, grade and
    # credit total. `view=reports` is only the document archive (PDF exports).
    "study_tree": "/campus/student/contractview.asp?gguid={pguid}",
    "documents": "/campus/student/contractview.asp?gguid={pguid}&view=reports",
    "registered_exams": "/campus/student/registrationlist.asp?type=exam&filter=registered",
    "deregistered_exams": "/campus/student/registrationlist.asp?type=exam&filter=unregistered",
    "event_registrations": "/campus/student/registrationlist.asp?type=event",
    # courseofstudies.asp still exists but renders empty; the study tree above
    # is what the portal shows for Studienverlauf.
    "timetable": "/campus/student/timetable.asp",
    "webcal": "/campus/student/webcal.asp",
    "notifications": "/campus/plus/notifications.asp",
    "thesis_list": "/campus/plus/thesislist.asp",
}


def _state_dir() -> Path:
    override = os.environ.get("KIT_CAMPUS_STATE_DIR")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_STATE_HOME")
    if base:
        return Path(base) / "kit-campus-mcp"
    return Path.home() / ".kit-campus-mcp"


@dataclass(frozen=True)
class Settings:
    """Runtime settings, read from the environment."""

    username: str | None
    password: str | None
    state_dir: Path
    timeout: float
    language: str
    program_guid: str | None

    @property
    def cookie_file(self) -> Path:
        return self.state_dir / "cookies.lwp"

    @property
    def snapshot_file(self) -> Path:
        return self.state_dir / "snapshots.json"

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password)


def load_settings() -> Settings:
    """Build Settings from environment variables, loading a .env file if present."""
    _load_dotenv()
    state = _state_dir()
    state.mkdir(parents=True, exist_ok=True)
    return Settings(
        username=os.environ.get("KIT_USERNAME") or None,
        password=os.environ.get("KIT_PASSWORD") or None,
        state_dir=state,
        timeout=float(os.environ.get("KIT_TIMEOUT", "45")),
        language=os.environ.get("KIT_LANGUAGE", "de"),
        program_guid=os.environ.get("KIT_PROGRAM_GUID") or None,
    )


def _load_dotenv() -> None:
    """Minimal .env loader so the server works without extra dependencies.

    Looks for a .env next to the package root and in the current directory.
    Existing environment variables always win.
    """
    candidates = [
        Path(__file__).resolve().parents[2] / ".env",
        Path.cwd() / ".env",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
